"""Performance Monitoring page — time-series graphs and scoped statistics."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from config import COLORS as C
from gui.widgets import Card, fmt_freq, fmt_time_ns, glow_curve, label, styled_plot


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

        scope = label("Graphs show one point per second of <b>hardware</b> time (rolling-window values). Seconds with "
                      "no data are left blank, not drawn as zero. Configured bus speed is never plotted.",
                      "cardHint", wrap=True)
        lay.addWidget(scope)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.plots = {}
        spec = [("freq", "SCL frequency vs time", "kHz", C["cyan"]), ("tps", "Transactions per second", "txn/s", C["blue"]),
                ("util", "Bus utilization vs time", "%", C["green"]), ("dur", "Average transaction duration", "µs", C["cyan"]),
                ("thr", "Data throughput", "bytes/s", C["blue"])]
        for i, (k, title, unit, col) in enumerate(spec):
            c = Card()
            pw = styled_plot(title, unit, "seconds (relative to latest)", 200)
            c.add(pw)
            self.plots[k] = glow_curve(pw, col)
            grid.addWidget(c, i // 2 + (1 if i >= 2 else 0), i % 2)
        c = Card()
        self.p_ack = styled_plot("ACK / NACK activity", "per second", "seconds (relative to latest)", 200)
        self.p_ack.addLegend(offset=(10, 10), labelTextColor=C["text2"])
        self.c_ack = self.p_ack.plot([], [], pen=pg.mkPen(C["green"], width=2), name="ACK")
        self.c_nack = self.p_ack.plot([], [], pen=pg.mkPen(C["red"], width=2), name="NACK")
        c.add(self.p_ack)
        grid.addWidget(c, 3, 1)
        c = Card()
        self.p_addr = styled_plot("I²C activity by address", "transactions", "", 220)
        self.p_addr.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        self.bars = None
        c.add(self.p_addr)
        grid.addWidget(c, 4, 0, 1, 2)
        health = Card("Bus Communication Health", "Application policy · rolling 60 s of capture time · >=1 complete transaction; zero unexpected NACKs healthy, any degraded")
        self.health_state = label("UNKNOWN", "cardTitle", wrap=True)
        health.add(self.health_state)
        self.health_plot = styled_plot("Unexpected NACK rate", "%", "absolute capture time (s)", 200)
        self.health_plot.setYRange(0, 100, padding=0.03)
        self.health_plot.getAxis("left").enableAutoSIPrefix(False)
        self.health_curve = self.health_plot.plot([], [], pen=pg.mkPen(C['red'], width=2), connect='finite')
        health.add(self.health_plot)
        grid.addWidget(health, 1, 0, 1, 2)
        lay.addLayout(grid)

    def refresh(self, engine):
        c = engine.communication.snapshot(engine.hw_now)
        state, detail = engine.communication_health()
        prefix = 'SIMULATED — NOT VERIFIED HARDWARE · ' if engine.source == 'demo' else ''
        rates = f"raw NACK rate {c['raw_nack_pct']:.2f}% · unexpected rate {c['unexpected_nack_pct']:.2f}%" if c["opportunities"] else "rates UNKNOWN (no known ACK slots)"
        self.health_state.setText(prefix + state + f" | unexpected {c['unexpected_nacks']}/{c['opportunities']} | raw NACK {c['raw_nacks']} | expected read-end {c['expected_read_nacks']}\n" + rates + " · " + detail)
        self.health_state.setStyleSheet('color:' + (C['green'] if state == 'HEALTHY' else C['red'] if state == 'DEGRADED' else C['amber']))
        points = list(engine.communication.history)
        self.health_curve.setData([p[0] for p in points], [p[1] if p[1] is not None else float('nan') for p in points])
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
        if not self.isVisible():
            return
        hist = list(engine.perf.history)
        if hist:
            x = np.array([h["t"] for h in hist], dtype=float)
            x -= x[-1]
            for k, key in (("freq", "freq_khz"), ("tps", "tps"), ("util", "util"), ("dur", "dur_us"), ("thr", "throughput")):
                self.plots[k].setData(x, np.array([h[key] for h in hist], dtype=float))
            self.c_ack.setData(x, np.array([h["acks"] for h in hist], dtype=float))
            self.c_nack.setData(x, np.array([h["nacks"] for h in hist], dtype=float))
        reg = engine.registry
        addrs = sorted(reg.devices)
        pi = self.p_addr.getPlotItem()
        if self.bars is not None:
            pi.removeItem(self.bars)
            self.bars = None
        if addrs:
            xs = np.arange(len(addrs))
            reads = np.array([reg.devices[a].reads for a in addrs], dtype=float)
            writes = np.array([reg.devices[a].writes for a in addrs], dtype=float)
            self.bars = pg.BarGraphItem(x=xs - 0.18, height=writes, width=0.34, brush=C["blue"], pen=None)
            rb = pg.BarGraphItem(x=xs + 0.18, height=reads, width=0.34, brush=C["cyan"], pen=None)
            pi.addItem(self.bars)
            pi.addItem(rb)
            if hasattr(self, "_rb") and self._rb is not None:
                pi.removeItem(self._rb)
            self._rb = rb
            pi.getAxis("bottom").setTicks([[(i, f"0x{a:02X}\n{reg.name(a)[0][:14]}") for i, a in enumerate(addrs)]])
            pi.setXRange(-0.7, len(addrs) - 0.3, padding=0)
            pi.setTitle(f"<span style='color:{C['text']}'>I²C activity by address</span> "
                        f"<span style='color:{C['blue']}'>■ write frames</span> <span style='color:{C['cyan']}'>■ read frames</span>")
