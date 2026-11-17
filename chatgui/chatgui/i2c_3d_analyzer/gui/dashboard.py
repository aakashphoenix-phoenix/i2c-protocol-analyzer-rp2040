"""Live Dashboard — the control-center screen."""
from __future__ import annotations

import html
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QGridLayout, QHBoxLayout, QHeaderView, QTableView, QVBoxLayout, QWidget

from analysis.series import health_series, perf_series, reference_second, short_capture, short_window, time_window
from config import COLORS as C
from gui.network_3d import Network3D
from gui.theme import MONO_CSS, on_change
from gui.widgets import (Card, CommBanner, MetricCard, ResponsiveGrid, fit_table, fmt_freq, glow_curve, label,
                         set_time_window, styled_plot)
from models.packet_model import C_ACK, C_ADDR, C_DATA, C_DEV, C_DUR, C_ID, C_RW, C_TS, COLUMNS, PacketFilter

SEV_COL = {"info": C["cyan"], "warning": C["amber"], "error": C["red"]}
on_change(lambda: SEV_COL.update(info=C["cyan"], warning=C["amber"], error=C["red"]))
CAT_Y = {"I2C bus": 3, "Multi-master": 3, "Topology": 2, "Analyzer": 1, "Transport": 0, "Session": 2}


def compute_cards(engine):
    s = engine.perf.compute()
    reg = engine.registry
    now = engine.hw_now or 0
    out = {}
    _x, ser, _sig = perf_series(engine, ("freq_khz", "tps", "util", "acks", "nacks"))
    hist = {k: v[1].tolist() for k, v in ser.items()}                 # same continuous series as the graphs
    if s.has_data:
        f = s.roll_freq_hz or s.inst_freq_hz
        v, u = fmt_freq(f)
        sub = "rolling 1 s · avg " + (fmt_freq(s.freq_avg_hz)[0] + " kHz" if s.freq_avg_hz else "—")
        out["freq"] = (v if f else None, sub, u, hist["freq_khz"])
        out["tps"] = (f"{s.roll_tps:.1f}" if s.roll_tps is not None else None,
                      f"rolling 1 s · cumulative {s.tps:.2f}/s" if s.tps else "rolling 1 s", None,
                      hist["tps"])
        out["util"] = (f"{s.roll_util:.2f}" if s.roll_util is not None else None,
                       f"rolling 1 s · cumulative {s.util:.2f} %" if s.util is not None else "", None,
                       hist["util"])
        out["total"] = (f"{s.total:,}", f"R {s.reads:,} · W {s.writes:,} · incomplete {s.incomplete + s.malformed}",
                        None, hist["tps"])
        bits = s.acks + s.nacks
        out["ack"] = (f"{s.acks:,}", f"{100 * s.acks / bits:.2f} % of ACK slots" if bits else "", None,
                      hist["acks"])
        out["nack"] = (f"{s.nacks:,}", f"raw (session) · end-of-read {s.read_end_nacks} · unexpected {s.unexpected_nacks}"
                       f" · addr {s.addr_nacks} · write {s.write_data_nacks}"
                       + (f" · unverified {s.unverified_read_end_nacks}" if s.unverified_read_end_nacks else ""),
                       None, hist["nacks"])
        active = [a for a in reg.devices if reg.status(a, now) == "Active"]
        out["active"] = (str(len(active)), " ".join(f"0x{a:02X}" for a in sorted(active)) or "none in last 3 s", None, [])
    else:
        for k in ("freq", "tps", "util", "total", "ack", "nack", "active"):
            out[k] = (None, "", None, [])
    word, detail = engine.communication_health()
    out["health"] = (word, detail, "", [])
    return s, out


class Dashboard(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)

        self.cards = {}
        grid = ResponsiveGrid(205)
        spec = [
            ("freq", "SCL frequency", "kHz", "Measured from SCL rising-edge periods (hardware timestamps). "
                                             "Never the configured speed.", C["cyan"]),
            ("tps", "Transaction rate", "txn/s", "Completed START…STOP transactions per second of observed time.", C["blue"]),
            ("util", "Bus utilization", "%", "Busy time (START→STOP) ÷ observed time. Capture gaps are excluded, "
                                             "not counted as idle.", C["green"]),
            ("total", "Total transactions", "", "All decoded transactions in this capture.", C["blue"]),
            ("ack", "ACK count", "", "Address + data bytes acknowledged (9th bit low).", C["green"]),
            ("nack", "NACK count", "", "Raw NACKs (9th bit high), all kept. A NACK on the last byte of a READ is the "
                                       "controller's normal end-of-read signal (UM10204 3.1.6) and is counted as "
                                       "'end-of-read', not as unexpected. Red only when unexpected NACKs exist.", C["amber"]),
            ("active", "Active slaves", "addr", "Addresses that ACKed in the last 3 s of hardware time.", C["cyan"]),
            ("health", "Bus communication health", "", "Measured device failures and capture reliability. "
                                             "UNKNOWN when policy/evidence is insufficient; a normal final read NACK is not a failure.", C["green"]),
        ]
        for key, name, unit, tip, col in spec:
            mc = MetricCard(name, unit, tip, col)
            mc.sub.setWordWrap(True)
            self.cards[key] = mc
            grid.add(mc)
        lay.addWidget(grid)
        self.device_health_text = label("", "cardHint", wrap=True)
        lay.addWidget(self.device_health_text)
        self.masters_text = label("", "cardHint", wrap=True)
        self.masters_text.setTextFormat(Qt.TextFormat.RichText)
        self.masters_text.setToolTip("Per-master bus access from each controller's own USB telemetry. Arbitration "
                                     "losses are normal on a multi-master bus (the loser retries) and are never "
                                     "counted as NACKs. Details: Device Activity → Controllers.")
        lay.addWidget(self.masters_text)
        self.activity_text = label("", "cardHint", wrap=True)
        self.activity_text.setTextFormat(Qt.TextFormat.RichText)
        self.activity_text.setToolTip("Device Communication Activity: READ/WRITE frames (acknowledged address phases in "
                                      "complete captured transactions) in a rolling 10 s of capture time. Details and "
                                      "chart on the Device Activity page.")
        lay.addWidget(self.activity_text)

        # Retain shared update hooks without displaying duplicate dashboard panels.
        c3 = Card("I²C bus · 3D", "drag to rotate · scroll to zoom", parent=self, shadow=False)
        self.net = Network3D(prefer_3d=ctx.settings.value("use_3d", True, bool), compact=True)
        self.net.node_clicked.connect(ctx.focus_device)
        c3.add(self.net, 1)
        c3.hide()
        cp = Card("Live packet stream", "newest at bottom", parent=self)
        self.proxy = PacketFilter()
        self.proxy.setSourceModel(ctx.packet_model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        keep = {C_ID, C_TS, C_ADDR, C_DEV, C_RW, C_DATA, C_ACK, C_DUR}
        for c in range(len(COLUMNS)):
            self.table.setColumnHidden(c, c not in keep)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(False)
        fit_table(self.table, {C_ID: 62, C_TS: 118, C_ADDR: 80, C_DEV: 150, C_RW: 112, C_DATA: 360, C_ACK: 150,
                               C_DUR: 112})
        self.table.doubleClicked.connect(lambda i: ctx.show_waveform(self.proxy.mapToSource(i).row()))
        cp.add(self.table, 1)
        cp.hide()
        # ---- full-width performance
        cr = Card("Real-time performance", "per completed second of hardware time · one shared clock")
        self.comm_banner = CommBanner()                       # which master <-> which slave is on the bus
        cr.add(self.comm_banner)
        self.p_freq = styled_plot("SCL frequency", "kHz")
        self.p_tps = styled_plot("Transactions / s")
        self.p_util = styled_plot("Bus Communication Health", "NACK rate %", "seconds (relative to latest) · red unexpected · amber dashed raw")
        self.p_util.setYRange(0, 100, padding=0.03)
        self.p_util.getAxis("left").enableAutoSIPrefix(False)
        self.c_freq = glow_curve(self.p_freq, C["cyan"])
        self.c_tps = glow_curve(self.p_tps, C["blue"])
        self.c_util = glow_curve(self.p_util, C["red"])          # unexpected NACK rate
        self.c_raw = self.p_util.plot([], [], pen=pg.mkPen(C["amber"], width=1.5, style=Qt.PenStyle.DashLine),
                                      connect="finite")             # raw NACK rate (includes end-of-read)
        plots = QGridLayout()
        plots.setContentsMargins(0, 0, 0, 0)
        plots.setHorizontalSpacing(18)
        plots.setVerticalSpacing(14)
        for col, pw in enumerate((self.p_freq, self.p_tps)):
            pw.setMinimumHeight(180)
            plots.addWidget(pw, 0, col)
            plots.setColumnStretch(col, 1)
        self.p_util.setMinimumHeight(220)
        plots.addWidget(self.p_util, 1, 0, 1, 2)
        plots.setRowStretch(0, 1)
        plots.setRowStretch(1, 1)
        cr.add(plots, 1)
        cr.setMinimumHeight(cr.minimumSizeHint().height())
        lay.addWidget(cr, 1)

        # ---- bottom: event timeline
        ct = Card("Live event timeline", "last 120 s · hover a marker for details")
        row = QHBoxLayout()
        self.tl = styled_plot()
        self.tl.setMinimumHeight(160)
        pi = self.tl.getPlotItem()
        pi.getAxis("left").setTicks([[(3, "Bus"), (2, "Topology"), (1, "Analyzer"), (0, "Link")]])
        pi.setYRange(-0.6, 3.6)
        pi.getAxis("bottom").setLabel("seconds ago", color=C["text2"])
        self.tl_scatter = pg.ScatterPlotItem(size=11, hoverable=True, tip=lambda x, y, data: data)
        pi.addItem(self.tl_scatter)
        row.addWidget(self.tl, 3)
        self.tl_text = label("", "cardHint", wrap=True)
        self.tl_text.setMinimumWidth(320)
        self.tl_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.tl_text, 1)
        ct.add(row)
        ct.setFixedHeight(ct.minimumSizeHint().height())
        lay.addWidget(ct)

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        # Include wrapped dashboard text when the page is fitted to its scroll area.
        size.setHeight(max(size.height(), self.layout().minimumHeightForWidth(self.width())))
        return size

    # ------------------------------------------------------------------
    @staticmethod
    def health_text(h):
        c = h["counts"]
        n = c["opportunities"]
        rates = (f"raw NACK {c['raw_nack_pct']:.2f}% · unexpected {c['unexpected_nack_pct']:.2f}%" if n
                 else "rates — (no known ACK slots)")
        lines = [f"{h['window']}: ACK slots {n:,} · ACK {c['acks']:,} · raw NACK {c['raw_nacks']:,} · "
                 f"end-of-read {c['expected_read_nacks']:,} · unexpected {c['unexpected_nacks']:,}",
                 rates]
        if c.get("unverified_read_end_nacks"):
            lines[0] += f" · unverified {c['unverified_read_end_nacks']}"
        if h["simulated"]:
            # demo / presentation: never a HEALTHY / DEGRADED word, the counts above are of SIMULATED records
            lines.insert(0, f"SIMULATED — NOT VERIFIED HARDWARE · {h['reason']}: health is evaluated on live "
                            "hardware only")
        elif h["state"] == "UNKNOWN":
            lines.insert(0, h["reason"])
        return "\n".join(lines)

    def refresh_health(self, engine):
        h = engine.health_summary()
        word = h["state"]
        col = C['green'] if word == 'HEALTHY' else C['red'] if word == 'DEGRADED' else C['amber']
        self.cards['health'].set(word, "", '', col)
        self.cards["health"].sub.setText(self.health_text(h))
        self.cards["health"].setToolTip(h["detail"] + "\n\n" + h["rule"])
        self.device_health_text.setText("  |  ".join(
            f"{engine.registry.name(a)[0]} 0x{a:02X}: {engine.device_health(a)['status']}"
            for a in sorted(engine.registry.configured)))
        mm = h.get("multi_master") or {}
        if mm:
            from gui.device_activity import MM_COL
            parts = []
            for r in engine.masters.rows():
                st = r["stats"]
                col = MM_COL.get(r["state"], C["text2"])
                extra = (f" · ok {st['txn_ok']:,} · arb lost {st['arb_lost']} (gave up {st['gave_up']})"
                         if r["have_stats"] else "")
                parts.append(f"<b>{r['label']}</b> <span style='color:{col}'>{r['state']}</span>{extra}")
            unattributed = max(0, mm["packets"] - mm["attributed"])
            sim = (f"<span style='color:{C['demo']}'><b>SIMULATED controllers</b> (demo bench, not verified hardware)"
                   "</span> · " if mm.get("simulated") else "")
            self.masters_text.setText("Masters: " + sim + "  |  ".join(parts) +
                                      f"  |  packets attributed {mm['attributed']:,}/{mm['packets']:,}"
                                      + (f" (unattributed {unattributed:,})" if unattributed else ""))
    def refresh_activity(self, engine):
        from analysis.device_activity import ranking_text
        a = engine.device_activity()
        sim = f"<span style='color:{C['demo']}'>SIMULATED</span> · " if a["simulated"] else ""
        state = "" if a["reliable"] else f" · <span style='color:{C['amber']}'>UNKNOWN: {html.escape(a['reason'])}</span>"
        self.activity_text.setText(f"{sim}<b>Device activity</b> (frames, rolling {a['window_s']:g} s){state}<br>"
                                   + ranking_text(a, "reads", "READ") + "<br>" + ranking_text(a, "writes", "WRITE"))

    @staticmethod
    def _ref_s(engine):
        """Clock origin (s) of the rolling graphs: the latest transaction while a short capture is plotted per
        transaction, otherwise the latest completed second."""
        if short_capture(engine):
            return engine.perf.txn_points[-1]["t_ns"] / 1e9
        ref = reference_second(engine)
        return None if ref is None else float(ref)

    def mark_selected(self, t_ns, engine):
        """Mark the selected packet on the health graph, on the graph's own clock (seconds relative to latest)."""
        if not hasattr(self, "sel_line"):
            self.sel_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(C["amber"], width=1, style=Qt.PenStyle.DashLine))
            self.p_util.addItem(self.sel_line)
        self._sel_t_ns = t_ns
        ref_s = self._ref_s(engine) if t_ns is not None else None
        self.sel_line.setVisible(t_ns is not None and ref_s is not None)
        if t_ns is not None and ref_s is not None:
            self.sel_line.setValue(t_ns / 1e9 - ref_s)

    def _apply_window(self, engine, x):
        """[-span, 0] on the 3 rolling graphs: 10 s steps for whole seconds; the recorded per-transaction progression
        (plus a small right margin) during a short capture / presentation, so the curve is not squashed at x = 0."""
        short = short_capture(engine)
        span = short_window(x) if short else time_window(x)
        for pw in (self.p_freq, self.p_tps, self.p_util):
            if short:
                if getattr(pw, "_span", None) != ("txn", span):
                    pw._span = ("txn", span)
                    vb = pw.getPlotItem().getViewBox()
                    vb.setXRange(-span, span * 0.05, padding=0)
                    vb.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
            else:
                if isinstance(getattr(pw, "_span", None), tuple):
                    pw._span = None                            # leaving per-transaction mode: re-apply the window
                set_time_window(pw, span)
        unit = ("seconds before the latest transaction (per-transaction, < 2 s captured)" if short else
                "seconds (relative to latest)")
        if unit != getattr(self, "_x_unit", None):
            self._x_unit = unit
            for pw in (self.p_freq, self.p_tps):
                pw.getPlotItem().getAxis("bottom").setLabel(unit, color=C["text2"])
            self.p_util.getPlotItem().getAxis("bottom").setLabel(
                unit + " · red unexpected · amber dashed raw", color=C["text2"])

    def refresh(self, engine):
        self.refresh_health(engine)
        self.refresh_activity(engine)
        s, cards = compute_cards(engine)
        for k, (v, sub, unit, spark) in cards.items():
            col = None
            if k == "health":
                col = {"HEALTHY": C["green"], "Good": C["green"], "Good*": C["green"], "Degraded": C["red"], "DEGRADED": C["red"], "UNKNOWN": C["amber"]}.get(v, C["text2"])
            if k == "nack" and v is not None:
                col = C["red"] if s.unexpected_nacks else C["amber"]
            if k == "health":
                continue            # refresh_health() owns this card
            self.cards[k].set(v, sub, unit, col)
            self.cards[k].spark.set_values(spark)
        h = engine.health_summary()
        c = h["counts"]
        sim = "SIMULATED · " if h["simulated"] else ""
        title = (f"{sim}Bus Communication Health · {h['state']}"
                 + f"<br>unexpected {c['unexpected_nacks']} / raw {c['raw_nacks']} of {c['opportunities']} slots · 60 s")
        if title != getattr(self, "_util_title", None):        # re-setting an unchanged title re-lays-out the plot
            self._util_title = title
            self.p_util.setTitle(title)
        self.comm_banner.update_from(engine)
        self.ctx.packet_model.sync()
        if self.ctx.autoscroll:
            self.table.scrollToBottom()
        # rolling graphs: ONE clock (seconds relative to the latest completed second), NaN-free, partial seconds
        # dropped; touched only when a new second completed, so they cannot flicker or rescale between ticks
        x, ser, sig = perf_series(engine, ("freq_khz", "tps"))
        hx_u, hy_u, hx_r, hy_r, hsig = health_series(engine, None if short_capture(engine) else reference_second(engine))
        if (sig, hsig) != getattr(self, "_series_sig", None):
            self._series_sig = (sig, hsig)
            self.c_freq.setData(*ser["freq_khz"])
            self.c_tps.setData(*ser["tps"])
            self.c_util.setData(hx_u, hy_u)
            self.c_raw.setData(hx_r, hy_r)
            # window wide enough for the health curves too (their oldest second can precede the perf series')
            self._apply_window(engine, np.concatenate([x, hx_u, hx_r]))
            if getattr(self, "_sel_t_ns", None) is not None:  # the clock origin moved: keep the marker on its packet
                self.mark_selected(self._sel_t_ns, engine)
        # timeline (wall clock so transport errors without hardware time still show)
        now = time.time()
        pts = []
        recent = []
        for ev in list(engine.events)[-400:]:
            age = now - ev.wall.timestamp()
            if age <= 120:
                pts.append({"pos": (-age, CAT_Y.get(ev.category, 2)), "brush": pg.mkBrush(SEV_COL[ev.severity]),
                            "pen": pg.mkPen(None), "data": f"{ev.kind}: {ev.message}"})
        self.tl_scatter.setData(pts)
        self.tl.getPlotItem().setXRange(-120, 0, padding=0.01)
        for ev in list(engine.events)[-4:][::-1]:
            recent.append(f"<span style='color:{SEV_COL[ev.severity]}'>●</span> "
                          f"<span style='color:{C['text2']};{MONO_CSS}'>{ev.wall:%H:%M:%S}</span> <b>{ev.kind}</b> — {ev.message[:90]}")
        self.tl_text.setText("<br>".join(recent) or "No events yet.")
