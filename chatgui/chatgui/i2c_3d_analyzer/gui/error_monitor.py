"""Error & Diagnostics page — I²C bus events vs analyzer/transport problems."""
from __future__ import annotations

import csv
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QPushButton,
                             QTableView, QVBoxLayout, QWidget)

from config import COLORS as C
from gui.theme import mono_font, on_change
from gui.widgets import Card, MetricCard, ResponsiveGrid, fit_table, fmt_time_ns, label, styled_plot

SEV_COL = {"info": C["cyan"], "warning": C["amber"], "error": C["red"]}
on_change(lambda: SEV_COL.update(info=C["cyan"], warning=C["amber"], error=C["red"]))
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
    # multi-master (controller telemetry on each master's own COM port)
    "Arbitration lost": "A master reported losing arbitration (another master drove SDA LOW first). Normal multi-master "
                        "behaviour, retried by the loser — NOT a NACK; the bus only shows the winner's bits.",
    "Controller clock aligned": "A master's transfer log is now mapped onto the analyzer's capture clock: its packets can "
                                "be attributed from here on.",
    "Bus recovery": "A master clocked 9 SCL pulses + STOP to free a stuck bus (target held SDA LOW).",
    "Controller reset": "A master's I²C controller was reset after an abort / timeout.",
    "Master ID mismatch": "The firmware on this COM port reports a different master ID: check which image is flashed.",
    "Controller record rejected": "A line on a master's COM port was not a valid controller record (transport).",
    "Probe": "A master sent ONE real write to the address you chose (Send invalid address). With no slave there, SDA stays "
             "HIGH at the 9th clock: a genuine Address NACK, listed separately as 'Address NACK'.",
    "Arbitration test": "Trigger arbitration: both masters ran back-to-back transfers with no idle window so their STARTs "
                        "coincided; the loser reported 'Arbitration lost' (retried, never a NACK).",
    "SEQ mode": "Spaced sequence: one transfer per target with the chosen idle gap, so each packet stands alone on the "
                "waveform. SEQ OFF returns to the normal 4 s turns.",
    "Turn incomplete": "Turn-taking firmware: a master finished its turn but one target did not answer correctly "
                       "(RTC / BMP280 / OLED result shown).",
    "Peer master silent": "Turn-taking firmware: the other master took no turn for 13.5 s, so this master took the turn "
                          "anyway (it never waits forever).",
    # analyzer / edge stream
    "Edge stream gap": "Edge-stream records were lost (DMA overrun, USB): the waveform is not drawn across this point; "
                       "segments, timing and packets are unaffected.",
    "Capture gap": "The analyzer lost DMA buffers: samples are missing at this point (an analyzer limit, not a bus fault).",
    "Capture health": "The analyzer's cumulative loss counters increased during this session.",
    "Decode disagreement": "The raw waveform and the firmware segment decoded different bytes; both are preserved.",
    "Byte cut by START/STOP": "A segment ended inside a byte: controller abort / timeout, bus recovery or a master starting "
                              "on a busy bus (multi-master collision).",
    "Partial transaction": "The capture began in the middle of a transaction (repeated START without a START).",
    "Firmware message": "Text message from the analyzer firmware.",
    "Analyzer handshake": "Analyzer identified itself: firmware, protocol, sample rate and capabilities.",
    "SCL setting": "A master's SCL configuration request / reply — a setting, never a measurement.",
    "Retention limit": "Oldest packets were evicted from memory (bounded retention); save captures regularly.",
    "Display backlog": "More packets arrived than the animation can show; every packet is still in the table.",
    "Waveform stream not saved further": "The saved capture keeps only the first part of the edge stream; segments are "
                                         "always saved.",
}
JUMP_HINT = "Double-click (or Jump to Packet / Jump to Waveform) to open the matching packet."
NO_JUMP_HINT = "No hardware timestamp: this event is not tied to a captured packet."


def find_transaction(packets, event):
    """(txn, note) for a diagnostics event: the packet whose [t_start, t_end] contains the event's hardware time, else
    the nearest one - among the packets that address event.addr when it has one (falling back to every packet, said so
    in the note).  (None, reason) when the event has no hardware time (USB / session events) or nothing was captured."""
    if event.t_hw is None:
        return None, f"“{event.kind}” has no hardware timestamp ({event.category} event): no packet to jump to"
    if not packets:
        return None, "No packets captured yet: nothing to jump to"
    pool, scope = packets, ""
    if event.addr is not None:
        own = [t for t in packets if any(f.addr == event.addr for f in t.frames)]
        if own:
            pool = own
        else:
            scope = f" (no packet addresses 0x{event.addr:02X}; nearest on the bus)"
    best = None
    for t in pool:
        t0 = t.t_start
        t1 = t.t_end if t.t_end is not None else t0
        d = 0.0 if t0 <= event.t_hw <= t1 else min(abs(event.t_hw - t0), abs(event.t_hw - t1))
        if best is None or d < best[0]:
            best = (d, t)
    d, t = best
    how = "contains the event time" if d == 0 else f"nearest packet, {fmt_time_ns(d)} from the event"
    addr = f" 0x{t.address:02X}" if t.address is not None else ""
    return t, f"Packet #{t.id}{addr}: {how}{scope}"


class EventModel(QAbstractTableModel):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.rows = []
        self.rev = -1
        self.mono = mono_font()

    def rowCount(self, p=QModelIndex()):
        return len(self.rows)

    def columnCount(self, p=QModelIndex()):
        return len(HEAD)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEAD[s]
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

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
        if role == Qt.ItemDataRole.FontRole and c in (0, 1, 5):     # wall time, HW time, address: fixed-pitch digits
            return self.mono
        if role == Qt.ItemDataRole.ForegroundRole and c in (2, 4):
            return QBrush(QColor(C["red"] if "NACK" in e.kind else SEV_COL[e.severity]))
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(x for x in (EXPLAIN.get(e.kind), JUMP_HINT if e.t_hw is not None else NO_JUMP_HINT) if x)
        return None

    def event_row(self, row):            # (not `event`: that is QObject.event(QEvent), called by Qt for every event)
        return self.rows[row]


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
        addr = f"0x{e.addr:02x}" if e.addr is not None else ""          # '0x3C' / '0x68' / '0x76' find their events
        if self.text and self.text.lower() not in f"{e.kind} {e.message} {addr} {e.category}".lower():
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
            ("nack", "Read-end NACKs", "NACK after the last byte of a READ — normal protocol behaviour, not an error.", C["red"]),
        ]:
            m = MetricCard(name, "", tip, col)
            self.cards[k] = m
            grid.add(m)
        lay.addWidget(grid)
        tc = Card("Error trend", "events per 10 s, last 10 minutes (wall clock)")
        self.trend = styled_plot("", "events", "minutes ago")
        self.trend.setFixedHeight(190)
        # key in the plot title instead of a floating legend (a legend can sit on top of the curves)
        self.trend.getPlotItem().setTitle(f"<span style='color:{C['amber']}'>■ warning</span>  "
                                          f"<span style='color:{C['red']}'>■ error</span>")
        self.tr_w = self.trend.plot([], [], pen=pg.mkPen(C["amber"], width=2), name="warning")
        self.tr_e = self.trend.plot([], [], pen=pg.mkPen(C["red"], width=2), name="error")
        tc.add(self.trend)
        lay.addWidget(tc)
        lc = Card("Diagnostics log", "hover a row for an explanation")
        f = QHBoxLayout()
        self.sev = QComboBox()
        self.sev.addItems(["All", "Info", "Warning", "Error"])
        self.cat = QComboBox()
        self.cat.addItems(["All", "I2C bus", "Multi-master", "Analyzer", "Transport", "Topology", "Session"])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search events…")
        self.search.setClearButtonEnabled(True)
        for w, n in ((self.sev, "Severity"), (self.cat, "Category")):
            f.addWidget(label(n, "cardHint"))
            f.addWidget(w)
        f.addWidget(self.search, 1)
        self.btn_packet = QPushButton("Jump to Packet")
        self.btn_packet.setToolTip("Open the packet that contains (or is nearest to) the selected event in the Packet "
                                   "Analyzer — also: double-click a row")
        self.btn_wave = QPushButton("Jump to Waveform")
        self.btn_wave.setToolTip("Show the selected event's packet in the Digital Waveform viewer")
        self.btn_csv = QPushButton("Export CSV")
        self.btn_csv.setToolTip("Save the rows shown (current Severity / Category / Search filters) as CSV")
        self.btn_vis_err = QPushButton("⚠ Visualize Error Waveform")
        self.btn_vis_err.setToolTip("Open the selected row's packet (if it is an error packet), else the newest error "
                                    "packet, in the Digital Waveform: paused, fitted, every field labelled above SCL / SDA")
        self.btn_vis_err.clicked.connect(self.visualize_error)
        self.btn_vis_arb = QPushButton("⚡ Visualize Arbitration")
        self.btn_vis_arb.setToolTip("Open the winning bus packet of the newest arbitration loss, with the collision marked")
        self.btn_vis_arb.clicked.connect(lambda: self.ctx.visualize_last_arbitration())
        for b in (self.btn_packet, self.btn_wave, self.btn_vis_err, self.btn_vis_arb, self.btn_csv):
            f.addWidget(b)
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
        fit_table(self.table, {0: 118, 1: 112, 2: 92, 3: 112, 4: 170, 5: 84})
        self.table.horizontalHeader().setMinimumSectionSize(60)
        lc.add(self.table, 1)
        lay.addWidget(lc, 1)
        self.sev.currentTextChanged.connect(self._f)
        self.cat.currentTextChanged.connect(self._f)
        self.search.textChanged.connect(self._f)
        self.table.doubleClicked.connect(lambda idx: self.jump("packets", self.event_at(idx)))
        self.btn_packet.clicked.connect(lambda: self.jump("packets"))
        self.btn_wave.clicked.connect(lambda: self.jump("waveform"))
        self.btn_csv.clicked.connect(self._export_clicked)

    # ------------------------------------------------------------------ event -> packet / waveform
    def event_at(self, proxy_index):
        """The Event behind a (filtered) table index, or None."""
        if proxy_index is None or not proxy_index.isValid():
            return None
        src = self.proxy.mapToSource(proxy_index)
        return self.model.event_row(src.row()) if src.isValid() else None

    def selected_event(self):
        rows = self.table.selectionModel().selectedRows()
        return self.event_at(rows[0]) if rows else None

    def _toast(self, msg):
        toast = getattr(self.ctx, "toast", None)
        if callable(toast):
            toast(msg)
        self.last_message = msg

    def jump(self, view="packets", event=None):
        """Resolve the event to its packet and navigate there (view 'packets' or 'waveform').  Never raises and never
        navigates without a packet: an event without hardware time, or an empty capture, only explains why."""
        e = event if event is not None else self.selected_event()
        if e is None:
            self._toast("Select a diagnostics row first")
            return None
        txn, note = find_transaction(self.ctx.engine.packets, e)
        if txn is None:
            self._toast(note)
            return None
        go = getattr(self.ctx, "jump_to_packet", None)
        if callable(go):
            go(txn, view)
        else:
            self.ctx.select_transaction(txn)
        self._toast(note)
        return txn

    def visualize_error(self):
        """The selected event's packet when it is an error packet, else the newest error packet."""
        from analysis.frame_view import is_error
        e = self.selected_event()
        if e is not None:
            txn, _note = find_transaction(self.ctx.engine.packets, e)
            if txn is not None and is_error(self.ctx.engine, txn):
                self.ctx.visualize_packet(txn)
                return txn
        self.ctx.visualize_last_error()
        return None

    # ------------------------------------------------------------------ CSV export
    def visible_rows(self):
        """The rows the table shows right now (Severity / Category / Search applied), as display text."""
        return [[self.proxy.index(i, c).data() for c in range(len(HEAD))] for i in range(self.proxy.rowCount())]

    def export_csv(self, path):
        """Write the filtered diagnostics log to `path` (header = the table columns).  Returns the row count."""
        self.model.sync()
        self.proxy.invalidateFilter()
        rows = self.visible_rows()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(HEAD)
            w.writerows(rows)
        return len(rows)

    def _export_clicked(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export diagnostics log", "diagnostics.csv", "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            n = self.export_csv(path)
        except OSError as exc:
            self._toast(f"Cannot write {path}: {exc}")
            return
        self._toast(f"Exported {n} diagnostics rows to {path}")

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
        # fixed window: the last 10 minutes, counts from 0 (a zero baseline sits ON the x axis, never floats)
        ymax = max(5, int(bins.max()) + 1)
        if getattr(self, "_trend_range", None) != ymax:
            self._trend_range = ymax
            vb = self.trend.getPlotItem().getViewBox()
            vb.setXRange(-10, 0, padding=0)
            vb.setYRange(0, ymax, padding=0)
            vb.setMouseEnabled(x=False, y=False)
