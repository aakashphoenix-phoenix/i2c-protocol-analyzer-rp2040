"""Device Activity page — 3D topology + per-address statistics; clicking a node filters packets."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QHeaderView, QSplitter, QTableView, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

import pyqtgraph as pg
from analysis.device_activity import ranking_text
from analysis.playback import format_capture_ns
from config import COLORS as C
from analysis.device_registry import DeviceStats
from gui.network_3d import Network3D
from gui.widgets import Card, label, styled_plot
from models.packet_model import C_ACK, C_ADDR, C_DATA, C_DUR, C_ID, C_RW, C_TS, COLUMNS, PacketFilter

HEAD = ["Address", "Device", "Identity confidence", "Activity", "Reads", "Writes", "Bytes", "ACK", "NACK", "Last seen (s)", "Rate (txn/s)"]
HEAD += ["Health", "Unexpected NACK", "NACK % (eligible bits)", "Successful txns", "Failed txns",
         "Incomplete/unverified", "Last confirmed success", "Latest failure", "Freshness/confidence"]
ACT_HEAD = ["Address", "Device", "Status", "READ frames", "WRITE frames", "READ data bytes", "WRITE data bytes",
            "READ frames/s", "WRITE frames/s", "Bus transactions", "Address-NACK attempts", "Latest READ (capture t)",
            "Latest WRITE (capture t)", "Why"]
ACT_COL = {"ACTIVE": C["green"], "NO OBSERVED ACTIVITY": C["text2"], "PARTIAL": C["amber"], "UNKNOWN": C["amber"]}
STATUS_COL = {"Active": C["green"], "Idle": C["text2"], "Not responding": C["red"], "No device (NACK)": C["red"]}


class DeviceActivityPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.selected = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        vs = QSplitter(Qt.Orientation.Vertical)
        vs.setHandleWidth(10)
        vs.setChildrenCollapsible(False)        # never squeeze the 3D box below its fitted minimum
        hs = QSplitter(Qt.Orientation.Horizontal)
        hs.setHandleWidth(10)
        c3 = Card("Device communication", "click a device to filter", shadow=False)
        self.net = Network3D(prefer_3d=ctx.settings.value("use_3d", True, bool), compact=True)
        self.net.view.setMinimumHeight(260)     # whole topology must fit (see tools/verify_topology_views.py)
        self.net.node_clicked.connect(lambda k: ctx.focus_device(k) if isinstance(k, int) else self.select(k))
        c3.add(self.net, 1)
        hs.addWidget(c3)
        ct = Card("Detected addresses", "unknown addresses are listed, never ignored")
        self.tbl = QTableWidget(0, len(HEAD))
        self.tbl.setHorizontalHeaderLabels(HEAD)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setShowGrid(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.cellClicked.connect(lambda r, c: ctx.focus_device(self.tbl.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        ct.add(self.tbl, 1)
        hs.addWidget(ct)
        hs.setSizes([560, 820])
        # Word-wrapped HUD labels under-report their height to the splitter; reserve
        # room for HUD + fitted diagram + legend so nothing is painted over the scene.
        hs.setMinimumHeight(600)
        vs.addWidget(hs)
        # ---- Device Communication Activity (frames from decoded records only)
        ca = Card("Device Communication Activity",
                  "READ/WRITE frames = acknowledged address phases in complete captured transactions · "
                  "rolling 10 s of capture time · click a row to filter packets, waveform and highlight in 3D")
        self.act_banner = label("", "cardHint", wrap=True)
        ca.add(self.act_banner)
        self.rank_lbl = label("", wrap=True)
        self.rank_lbl.setTextFormat(Qt.TextFormat.RichText)
        ca.add(self.rank_lbl)
        row = QSplitter(Qt.Orientation.Horizontal)
        self.act = QTableWidget(0, len(ACT_HEAD))
        self.act.setHorizontalHeaderLabels(ACT_HEAD)
        self.act.verticalHeader().setVisible(False)
        self.act.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.act.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.act.setAlternatingRowColors(True)
        self.act.setShowGrid(False)
        self.act.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.act.horizontalHeader().setStretchLastSection(True)
        self.act.cellClicked.connect(lambda r, c: ctx.focus_device(self.act.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        row.addWidget(self.act)
        self.act_plot = styled_plot("Frames per device (rolling 10 s)", "frames", "", 180)
        self.act_plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        self.act_bars = []
        row.addWidget(self.act_plot)
        row.setSizes([900, 380])
        ca.add(row, 1)
        vs.addWidget(ca)
        cp = Card("Packets for selected device")
        self.sel_lbl = label("All devices", "cardHint")
        cp.header.addWidget(self.sel_lbl)
        self.proxy = PacketFilter()
        self.proxy.setSourceModel(ctx.packet_model)
        self.pt = QTableView()
        self.pt.setModel(self.proxy)
        self.pt.setAlternatingRowColors(True)
        self.pt.verticalHeader().setVisible(False)
        self.pt.verticalHeader().setDefaultSectionSize(25)
        self.pt.setShowGrid(False)
        self.pt.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        keep = {C_ID, C_TS, C_ADDR, C_RW, C_DATA, C_ACK, C_DUR}
        for c in range(len(COLUMNS)):
            self.pt.setColumnHidden(c, c not in keep)
        self.pt.setColumnWidth(C_DATA, 360)
        self.pt.setColumnWidth(C_ACK, 160)
        self.pt.doubleClicked.connect(lambda i: ctx.show_waveform(self.proxy.mapToSource(i).row()))
        cp.add(self.pt, 1)
        vs.addWidget(cp)
        vs.setSizes([560, 300, 240])
        lay.addWidget(vs, 1)

    def select(self, key):
        self.selected = key if isinstance(key, int) else None
        self.net.set_focus(self.selected)
        self.proxy.set(addr=self.selected)
        name = self.ctx.engine.registry.name(self.selected)[0] if self.selected is not None else ""
        self.sel_lbl.setText(f"0x{self.selected:02X} · {name}" if self.selected is not None else "All devices")

    def refresh_activity(self, engine):
        a = engine.device_activity()
        sim = "SIMULATED — NOT VERIFIED HARDWARE · " if a["simulated"] else ""
        self.act_banner.setText(f"{sim}window: rolling {a['window_s']:g} s of capture time · "
                                f"{'reliable' if a['reliable'] else 'UNRELIABLE: ' + a['reason']} · units: {a['units']} · "
                                "ties: all tied devices listed, ascending address · 'least' includes zero only for "
                                "devices verified connected in a reliable window")
        self.rank_lbl.setText("<br>".join((f"<span style='color:{C['demo']}'>SIMULATED</span> " if a["simulated"] else "") + t
                                          for t in (ranking_text(a, "reads", "<b>READ</b>"),
                                                    ranking_text(a, "writes", "<b>WRITE</b>"))))
        rows = list(a["rows"].values())
        self.act.setRowCount(len(rows))
        for i, r in enumerate(rows):
            lr, lw = r["latest_read"], r["latest_write"]
            vals = [f"0x{r['addr']:02X}", r["name"], r["status"] + (" · SIMULATED" if a["simulated"] else ""),
                    str(r["reads"]), str(r["writes"]), str(r["read_bytes"]), str(r["write_bytes"]),
                    f"{r['read_rate']:.2f}", f"{r['write_rate']:.2f}", str(r["transactions"]), str(r["nack_attempts"]),
                    f"{format_capture_ns(lr[0])} #{lr[1]}" if lr else "none observed",
                    f"{format_capture_ns(lw[0])} #{lw[1]}" if lw else "none observed", r["why"]]
            if r["status"] == "UNKNOWN":
                for k in range(3, 11):
                    vals[k] = "UNKNOWN"
            elif r["status"] == "PARTIAL":
                for k in range(3, 10):
                    vals[k] = "≥ " + vals[k]
            for c, v in enumerate(vals):
                it = self.act.item(i, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.act.setItem(i, c, it)
                it.setText(v)
                it.setToolTip(v)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, r["addr"])
                if c == 2:
                    it.setForeground(QBrush(QColor(ACT_COL.get(r["status"], C["text"]))))
        pi = self.act_plot.getPlotItem()
        for b in self.act_bars:
            pi.removeItem(b)
        self.act_bars = []
        known = [r for r in rows if r["status"] in ("ACTIVE", "NO OBSERVED ACTIVITY")]
        if known:
            import numpy as np
            xs = np.arange(len(known))
            self.act_bars = [pg.BarGraphItem(x=xs - 0.18, height=[r["writes"] for r in known], width=0.34, brush=C["blue"], pen=None, name="WRITE frames"),
                             pg.BarGraphItem(x=xs + 0.18, height=[r["reads"] for r in known], width=0.34, brush=C["cyan"], pen=None, name="READ frames")]
            for b in self.act_bars:
                pi.addItem(b)
            pi.getAxis("bottom").setTicks([[(i, f"0x{r['addr']:02X}") for i, r in enumerate(known)]])
        self.act_plot.setTitle(("SIMULATED · " if a["simulated"] else "") +
                               "Frames per device · blue WRITE, cyan READ · rolling 10 s (UNKNOWN devices not plotted)")
        return a

    def refresh(self, engine):
        self.refresh_activity(engine)
        reg = engine.registry
        now = engine.hw_now or 0
        t0 = engine.packets[0].t_start if engine.packets else 0
        addrs = sorted(set(reg.devices) | set(reg.configured))
        self.net.set_devices(reg.node_list(now))
        self.ctx.packet_model.sync()
        self.tbl.setRowCount(len(addrs))
        for r, a in enumerate(addrs):
            d = reg.devices.get(a, DeviceStats(a))
            name, conf = reg.name(a)
            st = reg.status(a, now)
            vals = [f"0x{a:02X}", name, conf + (f" ({d.confirm_detail})" if d.confirm_detail else ""), st,
                    f"{d.reads:,}", f"{d.writes:,}", f"{d.total_bytes:,}", f"{d.acks:,}", f"{d.nacks:,}",
                    f"{(d.last_seen - t0) / 1e9:.3f}" if d.last_seen is not None else "—", f"{reg.rate(a, now):.2f}"]
            h = engine.device_health(a)
            last = (f"HW {h['last_success_hw']/1e9:.6f} s; {h['last_success_age_s']:.1f} s ago"
                    if h['last_success_hw'] is not None else "Never confirmed")
            rate = f"{h['unexpected_nack_pct']:.3f}%" if h['unexpected_nack_pct'] is not None else "Unmeasurable"
            age = f"{h['observation_age_s']:.1f} s" if h['observation_age_s'] is not None else "No observations"
            vals += [h['status'], str(h['unexpected_nacks']), rate, str(h['successful']), str(h['failed']),
                     str(h['incomplete']), last, h['last_failure'] or "None observed", age+"; "+h['confidence']]
            for c, v in enumerate(vals):
                it = self.tbl.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.tbl.setItem(r, c, it)
                it.setText(v)
                it.setToolTip(h['detail'] if c == 11 else str(v))
                if c in (8, 12):
                    it.setForeground(QBrush(QColor(C["red"])))
                if c == 7:
                    it.setForeground(QBrush(QColor(C["green"])))
                if c == 11:
                    it.setForeground(QBrush(QColor(C['red'] if h['status'] == 'DEGRADED' else C['amber'])))
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, a)
                if c == 3:
                    it.setForeground(QBrush(QColor(STATUS_COL.get(st, C["text"]))))
                if c == 2:
                    it.setForeground(QBrush(QColor({"Confirmed": C["green"], "Configured": C["cyan"],
                                                    "Likely": C["amber"]}.get(conf, C["red"]))))
