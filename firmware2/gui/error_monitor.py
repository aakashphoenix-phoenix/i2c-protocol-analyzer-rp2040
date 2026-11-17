"""Error & Diagnostics page — I²C bus events vs analyzer/transport problems."""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLineEdit, QTableView,
                             QVBoxLayout, QWidget)

from config import COLORS as C
from gui.widgets import Card, MetricCard, ResponsiveGrid, label, styled_plot

SEV_COL = {"info": C["cyan"], "warning": C["amber"], "error": C["red"]}
HEAD = ["Wall time", "HW time (s)", "Severity", "Category", "Event", "Address", "Details"]
EXPLAIN = {
    "Address NACK": "No device acknowledged the address. Expected for bus scans/probes; a problem if the device should exist.",
    "Data NACK": "Slave refused a written byte — usually a real error (wrong register, device busy).",
    "Incomplete transfer": "Transaction ended without a STOP or mid-byte.",
    "Malformed frame": "START/STOP appeared inside a byte.",
    "Decoder sync loss": "Line state unknown after missing data; decoding resumes at the next START.",
    "Buffer overflow": "Analyzer hardware lost samples — an analyzer limit, not a bus fault.",
    "Dropped samples": "Analyzer hardware lost samples — an analyzer limit, not a bus fault.",
    "Dropped records": "Records missing on USB (sequence gap) — transport loss, not a bus fault.",
    "Serial framing error": "A line from the analyzer was not valid protocol — transport problem.",
    "Analyzer disconnected": "USB link to the analyzer was lost.",
    "Capture interruption": "Analyzer restarted capture.",
}


class EventModel(QAbstractTableModel):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.rows = []
        self.rev = -1

    def rowCount(self, p=QModelIndex()):
        return len(self.rows)

    def columnCount(self, p=QModelIndex()):
        return len(HEAD)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEAD[s]

    def sync(self):
        if self.engine.revision == self.rev:
            return
        self.rev = self.engine.revision
        self.beginResetModel()
        self.rows = list(self.engine.events)
        self.endResetModel()

    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        e = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.ItemDataRole.DisplayRole:
            t0 = self.engine.packets[0].t_start if self.engine.packets else 0
            return [e.wall.strftime("%H:%M:%S.%f")[:-3],
                    f"{(e.t_hw - t0) / 1e9:.6f}" if e.t_hw is not None and self.engine.packets else "—",
                    e.severity.upper(), e.category, e.kind, f"0x{e.addr:02X}" if e.addr is not None else "",
                    e.message][c]
        if role == Qt.ItemDataRole.ForegroundRole and c in (2, 4):
            return QBrush(QColor(SEV_COL[e.severity]))
        if role == Qt.ItemDataRole.ToolTipRole:
            return EXPLAIN.get(e.kind)
        return None


class EventFilter(QSortFilterProxyModel):
    sev = "All"
    cat = "All"
    text = ""

    def filterAcceptsRow(self, r, p):
        e = self.sourceModel().rows[r]
        if self.sev != "All" and e.severity != self.sev.lower():
            return False
        if self.cat != "All" and e.category != self.cat:
            return False
        if self.text and self.text.lower() not in f"{e.kind} {e.message} {e.addr}".lower():
            return False
        return True


class ErrorPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        grid = ResponsiveGrid(200)
        self.cards = {}
        for k, name, tip, col in [
            ("bus", "I²C bus events", "Address/data NACKs, incomplete and malformed frames observed ON the bus.", C["amber"]),
            ("err", "Errors", "Events with severity 'error' (any category).", C["red"]),
            ("ana", "Analyzer problems", "Overflows, dropped samples, sync loss — limits of the capture hardware.", C["red"]),
            ("tr", "Transport problems", "USB/serial framing errors, lost records, disconnections.", C["red"]),
            ("nack", "Read-end NACKs", "NACK after the last byte of a READ — normal protocol behaviour, not an error.", C["green"]),
        ]:
            m = MetricCard(name, "", tip, col)
            self.cards[k] = m
            grid.add(m)
        lay.addWidget(grid)
        tc = Card("Error trend", "events per 10 s, last 10 minutes (wall clock)")
        self.trend = styled_plot("", "events", "minutes ago")
        self.trend.setFixedHeight(170)
        self.trend.addLegend(offset=(10, 5), labelTextColor=C["text2"])
        self.tr_w = self.trend.plot([], [], pen=pg.mkPen(C["amber"], width=2), name="warning")
        self.tr_e = self.trend.plot([], [], pen=pg.mkPen(C["red"], width=2), name="error")
        tc.add(self.trend)
        lay.addWidget(tc)
        lc = Card("Diagnostics log", "hover a row for an explanation")
        f = QHBoxLayout()
        self.sev = QComboBox()
        self.sev.addItems(["All", "Info", "Warning", "Error"])
        self.cat = QComboBox()
        self.cat.addItems(["All", "I2C bus", "Analyzer", "Transport", "Topology", "Session"])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search events…")
        self.search.setClearButtonEnabled(True)
        for w, n in ((self.sev, "Severity"), (self.cat, "Category")):
            f.addWidget(label(n, "cardHint"))
            f.addWidget(w)
        f.addWidget(self.search, 1)
        lc.add(f)
        self.model = EventModel(ctx.engine)
        self.proxy = EventFilter()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(25)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        for c, w in enumerate([100, 100, 80, 90, 160, 70]):
            self.table.setColumnWidth(c, w)
        lc.add(self.table, 1)
        lay.addWidget(lc, 1)
        self.sev.currentTextChanged.connect(self._f)
        self.cat.currentTextChanged.connect(self._f)
        self.search.textChanged.connect(self._f)

    def _f(self):
        self.proxy.sev = self.sev.currentText()
        self.proxy.cat = self.cat.currentText()
        self.proxy.text = self.search.text().strip()
        self.proxy.invalidateFilter()

    def refresh(self, engine):
        self.model.sync()
        self.proxy.invalidateFilter()
        evs = list(engine.events)
        s = engine.perf.compute()
        bus = sum(1 for e in evs if e.category == "I2C bus")
        self.cards["bus"].set(f"{bus:,}", "NACK · incomplete · malformed")
        self.cards["err"].set(f"{sum(1 for e in evs if e.severity == 'error'):,}", "all categories",
                              color=C["red"] if any(e.severity == "error" for e in evs) else None)
        ana = sum(1 for e in evs if e.category == "Analyzer" and e.severity != "info")
        self.cards["ana"].set(f"{ana:,}", f"{engine.fw_stats['overflows']} overflows · {engine.lost_records} lost records")
        self.cards["tr"].set(f"{engine.transport_errors:,}", "framing errors / disconnects")
        self.cards["nack"].set(f"{s.read_end_nacks:,}", "expected protocol behaviour")
        now = time.time()
        bins = np.zeros((2, 60))
        for e in evs:
            age = now - e.wall.timestamp()
            if age < 600 and e.severity in ("warning", "error"):
                bins[0 if e.severity == "warning" else 1, int(age // 10)] += 1
        x = -np.arange(60) * 10 / 60
        self.tr_w.setData(x, bins[0])
        self.tr_e.setData(x, bins[1])
