"""Performance Monitoring page — time-series graphs and scoped statistics."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget

import config
from analysis.series import health_series, perf_series, reference_second, short_capture, short_window, time_window
from config import COLORS as C
from gui.widgets import Card, CommBanner, fmt_freq, fmt_time_ns, glow_curve, label, set_time_window, styled_plot

# UM10204 bus-speed targets drawn on the SCL-frequency graph (reference only: never a measurement, never plotted as data)
TARGET_LINES_KHZ = ((100.0, "100 kHz · Standard-mode target"), (400.0, "400 kHz · Fast-mode target"))
COMPARE_ROWS = (("txns", "Transactions"), ("busy", "Share of bus busy time"), ("bytes", "Data bytes"),
                ("nacks", "Unexpected / total NACKs"), ("fscl", "Measured mean f_SCL"))
COMPARE_COLS = ((1, "Master 1"), (2, "Master 2"), (None, "Unattributed"))


class PerformancePage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("scrollBody")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(14)
        self.stats = {}
        groups = [
            ("Clock (cumulative)", [("fmin", "Minimum SCL frequency"), ("fmax", "Maximum SCL frequency"),
                                    ("favg", "Average SCL frequency"), ("finst", "Instantaneous (last packet)"),
                                    ("froll", "Rolling 1 s"), ("thigh", "SCL HIGH duration (avg)"),
                                    ("tlow", "SCL LOW duration (avg)")]),
            ("Transactions", [("complete", "Complete transactions"), ("incomplete", "Incomplete / gap"),
                              ("malformed", "Malformed"), ("davg", "Average duration"), ("dmin", "Minimum duration"),
                              ("dmax", "Maximum duration"), ("bytes", "Total bytes transferred")]),
            ("Capture & bus time", [("obs", "Observed capture time"), ("busy", "Bus busy time"), ("idle", "Idle time"),
                                    ("util", "Utilization (cumulative)"), ("gaps", "Capture gaps"),
                                    ("dropped", "Dropped samples (analyzer)"), ("ovf", "Buffer overflows (analyzer)"),
                                    ("lost", "Lost records (sequence)")]),
        ]
        for title, items in groups:
            c = Card(title)
            g = QGridLayout()
            g.setHorizontalSpacing(18)
            g.setVerticalSpacing(6)
            for i, (k, name) in enumerate(items):
                g.addWidget(label(name, "cardHint"), i, 0)
                v = label("—", "mono")
                v.setAlignment(Qt.AlignmentFlag.AlignRight)
                g.addWidget(v, i, 1)
                self.stats[k] = v
            c.add(g)
            c.add(label("", "cardHint"))
            top.addWidget(c, 1)
        lay.addLayout(top)

        # ---- M1 vs M2: strictly the attributed master of each packet (telemetry / ownership); unattributed separately
        cmp_card = Card("M1 vs M2 comparison",
                        "per attributed master (controller telemetry / configured ownership) · a passive tap never "
                        "guesses the master: unattributed packets are their own column · f_SCL = robust mean of the "
                        "captured cycles of that master's packets")
        cg = QGridLayout()
        cg.setHorizontalSpacing(22)
        cg.setVerticalSpacing(5)
        self.cmp = {}
        self._cmp_heads = {}
        cg.addWidget(label("", "cardHint"), 0, 0)
        for j, (mid, name) in enumerate(COMPARE_COLS, start=1):
            h = label(name, "cardTitle")
            h.setAlignment(Qt.AlignmentFlag.AlignRight)
            self._cmp_heads[mid] = h
            cg.addWidget(h, 0, j)
        for i, (key, name) in enumerate(COMPARE_ROWS, start=1):
            cg.addWidget(label(name, "cardHint"), i, 0)
            for j, (mid, _n) in enumerate(COMPARE_COLS, start=1):
                v = label("—", "mono")
                v.setAlignment(Qt.AlignmentFlag.AlignRight)
                cg.addWidget(v, i, j)
                self.cmp[(mid, key)] = v
        cg.setColumnStretch(0, 1)
        cmp_card.add(cg)
        self._color_heads()
        lay.addWidget(cmp_card)
        self.cmp_card = cmp_card

        self.comm_banner = CommBanner()                    # which master <-> which slave is on the bus
        lay.addWidget(self.comm_banner)
        tools = QHBoxLayout()
        self.show_targets = QCheckBox("100 / 400 kHz target lines")
        self.show_targets.setChecked(True)
        self.show_targets.setToolTip("UM10204 Standard-mode (100 kHz) and Fast-mode (400 kHz) targets on the SCL "
                                     "frequency graph — references only, never measurements; they do not change the "
                                     "graph's auto-range")
        self.btn_reset = QPushButton("Reset View")
        self.btn_reset.setToolTip("Restore the default time window and auto-range of every graph after pan / zoom")
        tools.addWidget(self.show_targets)
        tools.addStretch(1)
        tools.addWidget(self.btn_reset)
        lay.addLayout(tools)
        scope = label("Graphs show one point per completed second of <b>hardware</b> time on ONE shared clock "
                      "(seconds relative to the latest completed second). Seconds with no measurement are skipped "
                      "(the line joins the neighbouring seconds), never drawn as zero; partial seconds are not "
                      "plotted. Until two whole seconds are captured (and in the presentation sequence, whose capture "
                      "clock moves microseconds per transfer) the graphs show one cumulative point per complete "
                      "transaction instead, on the same relative clock. Configured bus speed is never plotted.",
                      "cardHint", wrap=True)
        lay.addWidget(scope)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.plots = {}
        self._rel_widgets = []
        self._sel_t = None
        spec = [("freq", "SCL frequency vs time", "kHz", C["cyan"]), ("tps", "Transactions per second", "txn/s", C["blue"]),
                ("util", "Bus utilization vs time", "%", C["green"]), ("dur", "Average transaction duration", "µs", C["cyan"]),
                ("thr", "Data throughput", "bytes/s", C["blue"])]
        for i, (k, title, unit, col) in enumerate(spec):
            c = Card()
            pw = styled_plot(title, unit, "seconds (relative to latest)", 200)
            self._rel_widgets.append(pw)
            c.add(pw)
            self.plots[k] = glow_curve(pw, col)
            if k == "freq":
                self.freq_plot = pw
            grid.addWidget(c, i // 2 + (1 if i >= 2 else 0), i % 2)
        self._add_target_lines()
        c = Card()
        # the key sits in the title (a pyqtgraph legend floats over the plot area and can cover the curves)
        self.p_ack = styled_plot("", "per second", "seconds (relative to latest)", 200)
        self.p_ack.getPlotItem().setTitle(f"<span style='color:{C['text']};font-size:11pt'>ACK / NACK activity</span> "
                                          f"<span style='color:{C['green']}'>■ ACK</span> "
                                          f"<span style='color:{C['red']}'>■ NACK</span>")
        self.c_ack = self.p_ack.plot([], [], pen=pg.mkPen(C["green"], width=2), name="ACK")
        self.c_nack = self.p_ack.plot([], [], pen=pg.mkPen(C["red"], width=2), name="NACK")
        c.add(self.p_ack)
        self._rel_widgets.append(self.p_ack)
        grid.addWidget(c, 3, 1)
        c = Card()
        self.p_addr = styled_plot("I²C activity by address", "transactions", "", 220)
        self.p_addr.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        self.p_addr.getPlotItem().getAxis("bottom").setHeight(36)   # room for the device-name labels
        # persistent bar items: updated with setOpts, never removed/recreated (that flashed on every tick)
        self.bars = pg.BarGraphItem(x=np.zeros(0), height=np.zeros(0), width=0.34, brush=C["blue"], pen=None)
        self._rb = pg.BarGraphItem(x=np.zeros(0), height=np.zeros(0), width=0.34, brush=C["cyan"], pen=None)
        self.p_addr.getPlotItem().addItem(self.bars)
        self.p_addr.getPlotItem().addItem(self._rb)
        self._bars_sig = None
        self._series_sig = None
        self._health_sig = None
        c.add(self.p_addr)
        grid.addWidget(c, 4, 0, 1, 2)
        health = Card("Bus Communication Health", "Application policy · rolling 60 s of capture time · red = unexpected NACK rate, amber dashed = raw NACK rate (includes normal end-of-read NACKs)")
        self.health_state = label("UNKNOWN", "cardTitle", wrap=True)
        health.add(self.health_state)
        self.health_plot = styled_plot("NACK rate (unexpected / raw)", "%", "seconds (relative to latest)", 200)
        self.health_plot.setYRange(0, 100, padding=0.03)
        self.health_plot.getAxis("left").enableAutoSIPrefix(False)
        self.health_curve = self.health_plot.plot([], [], pen=pg.mkPen(C['red'], width=2), connect='finite')
        self.health_raw = self.health_plot.plot([], [], pen=pg.mkPen(C['amber'], width=1.5, style=Qt.PenStyle.DashLine),
                                                connect='finite')
        health.add(self.health_plot)
        grid.addWidget(health, 1, 0, 1, 2)
        self._rel_widgets.append(self.health_plot)             # same clock as every other rolling graph
        lay.addLayout(grid)
        self.show_targets.toggled.connect(self.set_targets_visible)
        self.btn_reset.clicked.connect(self.reset_view)

    # ------------------------------------------------------------------ 100 / 400 kHz targets
    def _add_target_lines(self):
        """Dashed horizontal references at the UM10204 Standard-mode and Fast-mode targets.  Added with ignoreBounds, so
        a 100 kHz-only capture keeps its tight auto-range (the 400 kHz line simply sits outside it)."""
        self.target_lines = []
        pen = pg.mkPen(C["text2"], width=1.2, style=Qt.PenStyle.DashLine)
        for khz, text in TARGET_LINES_KHZ:
            ln = pg.InfiniteLine(pos=khz, angle=0, movable=False, pen=pen, label=text,
                                 labelOpts=dict(position=0.02, color=C["text2"], anchors=[(0, 1), (0, 1)]))
            ln.setZValue(-5)                                    # under the measured curve
            self.freq_plot.getPlotItem().addItem(ln, ignoreBounds=True)
            self.target_lines.append(ln)
        self.freq_plot.getPlotItem().setTitle(
            f"<span style='color:{C['text']};font-size:11pt'>SCL frequency vs time</span> "
            f"<span style='color:{C['text2']}'>┄ 100 / 400 kHz targets</span>")

    def set_targets_visible(self, on):
        for ln in self.target_lines:
            ln.setVisible(bool(on))
        if self.show_targets.isChecked() != bool(on):
            self.show_targets.setChecked(bool(on))
        self._apply_freq_range()

    def _apply_freq_range(self, y=None):
        """Y range of the SCL-frequency graph: the measured points, plus a target line only when it is NEAR them (within
        25 %): a 100 kHz capture shows its 100 kHz target without the 400 kHz line squashing the curve flat, and the
        reverse at 400 kHz.  No measured point yet -> plain auto-range."""
        if y is None:
            y = getattr(self, "_freq_y", None)
        self._freq_y = y
        vb = self.freq_plot.getPlotItem().getViewBox()
        if y is None or not len(y):
            vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            return
        lo, hi = float(np.min(y)), float(np.max(y))
        if self.show_targets.isChecked():
            for khz, _t in TARGET_LINES_KHZ:
                if lo / 1.25 <= khz <= hi * 1.25:
                    lo, hi = min(lo, khz), max(hi, khz)
        pad = max((hi - lo) * 0.15, hi * 0.002, 0.05)
        vb.setYRange(lo - pad, hi + pad, padding=0)

    def targets_in_view(self):
        """{kHz: visible inside the current y range} (diagnostics / tests)."""
        lo, hi = self.freq_plot.getPlotItem().getViewBox().viewRange()[1]
        return {ln.value(): ln.isVisible() and lo <= ln.value() <= hi for ln in self.target_lines}

    # ------------------------------------------------------------------ Reset View
    def _ref_s(self, engine):
        """Clock origin of every rolling graph (s): the latest completed second, or - for a short capture plotted per
        transaction - the latest transaction."""
        if short_capture(engine):
            return engine.perf.txn_points[-1]["t_ns"] / 1e9
        ref = reference_second(engine)
        return float(ref) if ref is not None else None

    def _apply_window(self, engine, x):
        """[-span, 0] on every rolling graph: 10 s steps for whole seconds, the recorded progression (plus a small
        right margin) while the graphs plot per transaction."""
        short = short_capture(engine)
        span = short_window(x) if short else time_window(x)
        for pw in self._rel_widgets:
            if short:
                if getattr(pw, "_span", None) != ("txn", span):
                    pw._span = ("txn", span)
                    vb = pw.getPlotItem().getViewBox()
                    vb.setXRange(-span, span * 0.05, padding=0)
                    vb.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
            else:
                if isinstance(getattr(pw, "_span", None), tuple):
                    pw._span = None
                set_time_window(pw, span)
        unit = "seconds before the latest transaction (per-transaction, < 2 s captured)" if short else \
            "seconds (relative to latest)"
        if unit != getattr(self, "_x_unit", None):
            self._x_unit = unit
            for pw in self._rel_widgets:
                pw.getPlotItem().getAxis("bottom").setLabel(unit, color=C["text2"])

    def reset_view(self):
        """Back to the default view after pan / zoom: rolling graphs to the [-span, 0] window of the data with Y
        auto-range (the NACK-rate graph keeps its fixed 0-100 %), the address bars to all devices."""
        engine = self.ctx.engine
        x, _ser, _sig = perf_series(engine, ("freq_khz",))
        hx_u, _hy_u, hx_r, _hy_r, _hsig = health_series(engine, None if short_capture(engine) else
                                                        reference_second(engine))
        for pw in self._rel_widgets:
            pw._span = None                                    # force the window to re-apply
        self._apply_window(engine, np.concatenate([x, hx_u, hx_r]))
        for pw in self._rel_widgets:
            vb = pw.getPlotItem().getViewBox()
            if pw is self.health_plot:
                pw.setYRange(0, 100, padding=0.03)
            elif pw is self.freq_plot:
                self._apply_freq_range(_ser["freq_khz"][1])
            else:
                vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        pi = self.p_addr.getPlotItem()
        n = len(engine.registry.devices)
        pi.setXRange(-0.7, max(n, 1) - 0.3, padding=0)
        pi.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self._place_markers(self._ref_s(engine))

    # ------------------------------------------------------------------ M1 vs M2
    def _color_heads(self):
        for mid, h in self._cmp_heads.items():
            col = config.MASTERS[mid]["color"] if mid in config.MASTERS else C["unattr"]
            h.setStyleSheet(f"color:{col}; font-weight:600;")

    def retheme(self):
        """Theme switch: master colours come from config.MASTERS (updated per theme), not from a hex in a style sheet."""
        self._color_heads()

    @staticmethod
    def comparison_texts(s):
        """{(master_id, row): text} of the M1 vs M2 card for a PerfSnapshot (pure: tested without the widgets)."""
        out = {}
        for mid, _n in COMPARE_COLS:
            g = s.by_master.get(mid)
            if g is None or not g.transactions:
                for key, _r in COMPARE_ROWS:
                    out[(mid, key)] = "—"
                continue
            v, u = fmt_freq(g.freq_mean_hz)
            out[(mid, "txns")] = f"{g.transactions:,}" + (f" ({g.txn_share_pct:.1f} %)" if g.txn_share_pct is not None else "")
            out[(mid, "busy")] = (f"{g.busy_share_pct:.1f} % · {fmt_time_ns(g.busy_ns)}" if g.busy_share_pct is not None
                                  else "—")
            out[(mid, "bytes")] = f"{g.data_bytes:,}"
            out[(mid, "nacks")] = f"{g.unexpected_nacks:,} / {g.nacks:,}"
            out[(mid, "fscl")] = f"{v} {u}" if g.freq_mean_hz else "UNKNOWN (no captured cycles)"
        return out

    def _refresh_comparison(self, s):
        texts = self.comparison_texts(s)
        for k, v in texts.items():
            w = self.cmp[k]
            if w.text() != v:
                w.setText(v)
        g = s.by_master.get(None)
        show_un = bool(g and g.transactions)                   # the unattributed column only when it has packets
        for (mid, _key), w in self.cmp.items():
            if mid is None:
                w.setVisible(show_un)
        self._cmp_heads[None].setVisible(show_un)

    def mark_selected(self, t_ns, engine):
        """Selected packet's capture time on every graph (same records, same clock)."""
        self._sel_t = None if t_ns is None else t_ns / 1e9
        self._place_markers(self._ref_s(engine))

    def _place_markers(self, latest_s):
        if not hasattr(self, "_sel_lines"):
            pen = pg.mkPen(C["amber"], width=1, style=Qt.PenStyle.DashLine)
            self._sel_lines = [(pw, pg.InfiniteLine(angle=90, pen=pen)) for pw in self._rel_widgets]
            for pw, ln in self._sel_lines:
                pw.addItem(ln)
        show = self._sel_t is not None
        for _, ln in self._sel_lines:
            ln.setVisible(show and latest_s is not None)
            if show and latest_s is not None:
                ln.setValue(self._sel_t - latest_s)

    def refresh(self, engine):
        from gui.dashboard import Dashboard
        h = engine.health_summary()
        state = h['state']          # HEALTHY / DEGRADED only on trustworthy live hardware, otherwise UNKNOWN
        self.health_state.setText(state + "\n" + Dashboard.health_text(h) + "\n" + h['rule'])
        colour = C['green'] if state == 'HEALTHY' else C['red'] if state == 'DEGRADED' else C['amber']
        if getattr(self, '_health_colour', None) != colour:      # re-polishing the style every tick flashed the card
            self._health_colour = colour
            self.health_state.setStyleSheet('color:' + colour)
        s = engine.perf.compute()
        st = self.stats

        def f(hz):
            v, u = fmt_freq(hz)
            return f"{v} {u}" if hz else "—"
        st["fmin"].setText(f(s.freq_min_hz))
        st["fmax"].setText(f(s.freq_max_hz))
        st["favg"].setText(f(s.freq_avg_hz))
        st["finst"].setText(f(s.inst_freq_hz))
        st["froll"].setText(f(s.roll_freq_hz))
        st["thigh"].setText(fmt_time_ns(s.t_high_ns))
        st["tlow"].setText(fmt_time_ns(s.t_low_ns))
        st["complete"].setText(f"{s.complete:,}")
        st["incomplete"].setText(f"{s.incomplete:,}")
        st["malformed"].setText(f"{s.malformed:,}")
        st["davg"].setText(fmt_time_ns(s.dur_avg_ns))
        st["dmin"].setText(fmt_time_ns(s.dur_min_ns))
        st["dmax"].setText(fmt_time_ns(s.dur_max_ns))
        st["bytes"].setText(f"{s.total_bytes:,}")
        st["obs"].setText(fmt_time_ns(s.observed_ns) if s.observed_ns else "—")
        st["busy"].setText(fmt_time_ns(s.busy_ns) if s.busy_ns else "—")
        st["idle"].setText(fmt_time_ns(s.idle_ns) if s.observed_ns else "—")
        st["util"].setText(f"{s.util:.3f} %" if s.util is not None else "—")
        st["gaps"].setText(str(s.gaps))
        st["dropped"].setText(str(engine.fw_stats["dropped"]))
        st["ovf"].setText(str(engine.fw_stats["overflows"]))
        st["lost"].setText(str(engine.lost_records))
        self._refresh_comparison(s)
        self.comm_banner.update_from(engine)
        if not self.isVisible():
            return
        # ---- rolling graphs: touched only when a new second completed (no per-tick setData / auto-range)
        keys = ("freq_khz", "tps", "util", "dur_us", "throughput", "acks", "nacks")
        x, ser, sig = perf_series(engine, keys)
        hx_u, hy_u, hx_r, hy_r, hsig = health_series(engine, None if short_capture(engine) else reference_second(engine))
        if sig != self._series_sig:
            self._series_sig = sig
            for k, key in (("freq", "freq_khz"), ("tps", "tps"), ("util", "util"), ("dur", "dur_us"), ("thr", "throughput")):
                self.plots[k].setData(*ser[key])           # NaN seconds are already dropped: one continuous line
            self._apply_freq_range(ser["freq_khz"][1])
            self.c_ack.setData(*ser["acks"])
            self.c_nack.setData(*ser["nacks"])
            # one window for every rolling graph, wide enough for the NACK-rate curves too (their oldest second can
            # precede the oldest perf second): no point is ever cut off at the left edge
            self._apply_window(engine, np.concatenate([x, hx_u, hx_r]))
            self._place_markers(self._ref_s(engine))
        if hsig != self._health_sig:
            self._health_sig = hsig
            self.health_curve.setData(hx_u, hy_u)
            self.health_raw.setData(hx_r, hy_r)
            self._apply_window(engine, np.concatenate([x, hx_u, hx_r]))
        # ---- activity by address: persistent bars, updated in place
        reg = engine.registry
        addrs = sorted(reg.devices)
        reads = [reg.devices[a].reads for a in addrs]
        writes = [reg.devices[a].writes for a in addrs]
        bsig = (tuple(addrs), tuple(reads), tuple(writes), tuple(reg.name(a)[0] for a in addrs))
        if bsig != self._bars_sig:
            self._bars_sig = bsig
            pi = self.p_addr.getPlotItem()
            xs = np.arange(len(addrs), dtype=float)
            self.bars.setOpts(x=xs - 0.18, height=np.array(writes, float), width=0.34)
            self._rb.setOpts(x=xs + 0.18, height=np.array(reads, float), width=0.34)
            if addrs:
                # One line per device with its full name, e.g. "OLED SSD1306 (0x3C)"
                pi.getAxis("bottom").setTicks([[(i, reg.name(a)[0].replace("(", "").replace(")", "") + f" (0x{a:02X})")
                                                for i, a in enumerate(addrs)]])
                pi.setXRange(-0.7, len(addrs) - 0.3, padding=0)
                pi.setTitle(f"<span style='color:{C['text']}'>I²C activity by address</span> "
                            f"<span style='color:{C['blue']}'>■ write frames</span> <span style='color:{C['cyan']}'>■ read frames</span>")
