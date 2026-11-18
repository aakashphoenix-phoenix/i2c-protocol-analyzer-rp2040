"""Device Activity page — 3D topology + per-address statistics; clicking a node filters packets."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QHeaderView, QSplitter, QTableView, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from config import COLORS as C
from analysis.device_registry import DeviceStats
from gui.network_3d import Network3D
from gui.widgets import Card, label
from models.packet_model import C_ACK, C_ADDR, C_DATA, C_DUR, C_ID, C_RW, C_TS, COLUMNS, PacketFilter

HEAD = ["Address", "Device", "Identity confidence", "Activity", "Reads", "Writes", "Bytes", "ACK", "NACK", "Last seen (s)", "Rate (txn/s)"]
HEAD += ["Health", "Unexpected NACK", "NACK % (eligible bits)", "Successful txns", "Failed txns",
         "Incomplete/unverified", "Last confirmed success", "Latest failure", "Freshness/confidence"]
STATUS_COL = {"Active": C["green"], "Idle": C["text2"], "Not responding": C["red"], "No device (NACK)": C["amber"]}


class DeviceActivityPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.selected = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        vs = QSplitter(Qt.Orientation.Vertical)
        vs.setHandleWidth(10)
        hs = QSplitter(Qt.Orientation.Horizontal)
        hs.setHandleWidth(10)
        c3 = Card("Device communication", "click a device to filter", shadow=False)
        self.net = Network3D(prefer_3d=ctx.settings.value("use_3d", True, bool), compact=True)
        self.net.node_clicked.connect(self.select)
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
        self.tbl.cellClicked.connect(lambda r, c: self.select(self.tbl.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        ct.add(self.tbl, 1)
        hs.addWidget(ct)
        hs.setSizes([560, 820])
        vs.addWidget(hs)
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
        vs.setSizes([520, 300])
        lay.addWidget(vs, 1)

    def select(self, key):
        self.selected = key if isinstance(key, int) else None
        self.net.set_selected(key)
        self.proxy.set(addr=self.selected)
        name = self.ctx.engine.registry.name(self.selected)[0] if self.selected is not None else ""
        self.sel_lbl.setText(f"0x{self.selected:02X} · {name}" if self.selected is not None else "All devices")

    def refresh(self, engine):
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
                if c == 11:
                    it.setForeground(QBrush(QColor(C['red'] if h['status'] == 'DEGRADED' else C['amber'])))
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, a)
                if c == 3:
                    it.setForeground(QBrush(QColor(STATUS_COL.get(st, C["text"]))))
                if c == 2:
                    it.setForeground(QBrush(QColor({"Confirmed": C["green"], "Configured": C["cyan"],
                                                    "Likely": C["amber"]}.get(conf, C["red"]))))
