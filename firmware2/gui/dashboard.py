"""Live Dashboard — the control-center screen."""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QHBoxLayout, QHeaderView, QSplitter, QTableView, QVBoxLayout, QWidget

from config import COLORS as C
from gui.network_3d import Network3D
from gui.widgets import Card, MetricCard, ResponsiveGrid, fmt_freq, glow_curve, label, styled_plot
from models.packet_model import C_ACK, C_ADDR, C_DATA, C_DEV, C_DUR, C_ID, C_RW, C_TS, COLUMNS, PacketFilter

SEV_COL = {"info": C["cyan"], "warning": C["amber"], "error": C["red"]}
CAT_Y = {"I2C bus": 3, "Topology": 2, "Analyzer": 1, "Transport": 0, "Session": 2}


def compute_cards(engine):
    s = engine.perf.compute()
    reg = engine.registry
    now = engine.hw_now or 0
    out = {}
    hist = list(engine.perf.history)
    if s.has_data:
        f = s.roll_freq_hz or s.inst_freq_hz
        v, u = fmt_freq(f)
        sub = "rolling 1 s · avg " + (fmt_freq(s.freq_avg_hz)[0] + " kHz" if s.freq_avg_hz else "—")
        out["freq"] = (v if f else None, sub, u, [h["freq_khz"] for h in hist])
        out["tps"] = (f"{s.roll_tps:.1f}" if s.roll_tps is not None else None,
                      f"rolling 1 s · cumulative {s.tps:.2f}/s" if s.tps else "rolling 1 s", None,
                      [h["tps"] for h in hist])
        out["util"] = (f"{s.roll_util:.2f}" if s.roll_util is not None else None,
                       f"rolling 1 s · cumulative {s.util:.2f} %" if s.util is not None else "", None,
                       [h["util"] for h in hist])
        out["total"] = (f"{s.total:,}", f"R {s.reads:,} · W {s.writes:,} · incomplete {s.incomplete + s.malformed}",
                        None, [h["tps"] for h in hist])
        bits = s.acks + s.nacks
        out["ack"] = (f"{s.acks:,}", f"{100 * s.acks / bits:.2f} % of ACK slots" if bits else "", None,
                      [h["acks"] for h in hist])
        out["nack"] = (f"{s.nacks:,}", f"addr {s.addr_nacks} · read-end {s.read_end_nacks} · write {s.write_data_nacks}",
                       None, [h["nacks"] for h in hist])
        active = [a for a in reg.devices if reg.status(a, now) == "Active"]
        out["active"] = (str(len(active)), " ".join(f"0x{a:02X}" for a in sorted(active)) or "none in last 3 s", None, [])
    else:
        for k in ("freq", "tps", "util", "total", "ack", "nack", "active"):
            out[k] = (None, "", None, [])
    word, detail = engine.health()
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
            ("nack", "NACK count", "", "9th bit high. A NACK on the last byte of a READ is normal: "
                                       "the master uses it to end the read.", C["amber"]),
            ("active", "Active slaves", "addr", "Addresses that ACKed in the last 3 s of hardware time.", C["cyan"]),
            ("health", "Capture health", "", "Lost records, analyzer overflows/drops and serial framing errors. "
                                             "'*' = firmware does not guarantee lossless capture.", C["green"]),
        ]
        for key, name, unit, tip, col in spec:
            mc = MetricCard(name, unit, tip, col)
            self.cards[key] = mc
            grid.add(mc)
        lay.addWidget(grid)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        # ---- left: 3D
        c3 = Card("I²C bus · 3D", "drag to rotate · scroll to zoom", shadow=False)
        self.net = Network3D(prefer_3d=ctx.settings.value("use_3d", True, bool), compact=True)
        self.net.node_clicked.connect(ctx.focus_device)
        c3.add(self.net, 1)
        split.addWidget(c3)
        # ---- centre: packet stream
        cp = Card("Live packet stream", "newest at bottom")
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
        for c, w in ((C_ID, 52), (C_TS, 104), (C_ADDR, 64), (C_DEV, 118), (C_RW, 112), (C_DATA, 250), (C_ACK, 120), (C_DUR, 90)):
            self.table.setColumnWidth(c, w)
        self.table.doubleClicked.connect(lambda i: ctx.show_waveform(self.proxy.mapToSource(i).row()))
        cp.add(self.table, 1)
        split.addWidget(cp)
        # ---- right: performance
        cr = Card("Real-time performance", "per second of hardware time")
        self.p_freq = styled_plot("SCL frequency", "kHz")
        self.p_tps = styled_plot("Transactions / s")
        self.p_util = styled_plot("Bus utilization", "%")
        self.c_freq = glow_curve(self.p_freq, C["cyan"])
        self.c_tps = glow_curve(self.p_tps, C["blue"])
        self.c_util = glow_curve(self.p_util, C["green"])
        for pw in (self.p_freq, self.p_tps, self.p_util):
            pw.setMinimumHeight(120)
            cr.add(pw, 1)
        split.addWidget(cr)
        split.setSizes([520, 640, 420])
        lay.addWidget(split, 1)

        # ---- bottom: event timeline
        ct = Card("Live event timeline", "last 120 s · hover a marker for details")
        row = QHBoxLayout()
        self.tl = styled_plot()
        self.tl.setFixedHeight(128)
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
        lay.addWidget(ct)

    # ------------------------------------------------------------------
    def refresh(self, engine):
        s, cards = compute_cards(engine)
        for k, (v, sub, unit, spark) in cards.items():
            col = None
            if k == "health":
                col = {"Good": C["green"], "Good*": C["green"], "Degraded": C["red"]}.get(v, C["text2"])
            if k == "nack" and v is not None and (s.write_data_nacks or s.addr_nacks):
                col = C["amber"]
            self.cards[k].set(v, sub, unit, col)
            self.cards[k].spark.set_values(spark)
        self.ctx.packet_model.sync()
        if self.ctx.autoscroll:
            self.table.scrollToBottom()
        hist = list(engine.perf.history)
        if hist:
            x = np.array([h["t"] for h in hist], dtype=float)
            x -= x[-1]
            self.c_freq.setData(x, np.array([h["freq_khz"] for h in hist]))
            self.c_tps.setData(x, np.array([h["tps"] for h in hist]))
            self.c_util.setData(x, np.array([h["util"] for h in hist]))
        else:
            for c in (self.c_freq, self.c_tps, self.c_util):
                c.setData([], [])
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
                          f"<span style='color:{C['text2']}'>{ev.wall:%H:%M:%S}</span> <b>{ev.kind}</b> — {ev.message[:90]}")
        self.tl_text.setText("<br>".join(recent) or "No events yet.")
