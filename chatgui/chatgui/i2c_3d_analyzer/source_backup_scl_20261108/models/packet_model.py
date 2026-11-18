"""Qt table model over CaptureEngine.packets (no per-cell widgets)."""
from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor, QFont

from analysis.communication_health import acknowledgment_counts
from config import COLORS

COLUMNS = ["★", "#", "HW time (s)", "Address", "Device", "Mode", "R/W", "Data (hex)",
           "Bytes", "ACK/NACK", "Events", "Duration", "Status", "Source", "Start (ns)", "End (ns)", "Address ACK", "Data ACKs", "SCL (kHz)"]
C_BOOK, C_ID, C_TS, C_ADDR, C_DEV, C_MODE, C_RW, C_DATA, C_BYTES, C_ACK, C_EVT, C_DUR, C_STAT = range(13)

STATUS_TEXT = {"ok": "Complete", "incomplete": "Incomplete", "malformed": "Malformed", "gap": "Capture gap"}


def fmt_dur(ns: float) -> str:
    if ns >= 1e6:
        return f"{ns / 1e6:.3f} ms"
    return f"{ns / 1e3:.1f} µs"


class PacketModel(QAbstractTableModel):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.rows: list = []
        self.bookmarks: set = set()
        self.t_origin: float | None = None
        self.mono = QFont("Consolas")
        self.mono.setStyleHint(QFont.StyleHint.Monospace)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def sync(self):
        """Append new packets in one batch; reset if the engine trimmed or cleared."""
        pk = self.engine.packets
        if self.rows and (not pk or pk[0] is not self.rows[0]):
            self.beginResetModel()
            self.rows = list(pk)
            self.endResetModel()
        elif len(pk) > len(self.rows):
            n0 = len(self.rows)
            self.beginInsertRows(QModelIndex(), n0, len(pk) - 1)
            self.rows.extend(pk[n0:])
            self.endInsertRows()
        if self.rows and self.t_origin is None:
            self.t_origin = self.rows[0].t_start
        if not pk:
            self.t_origin = None

    def txn(self, row):
        return self.rows[row]

    def toggle_bookmark(self, row):
        tid = self.rows[row].id
        self.bookmarks.symmetric_difference_update({tid})
        i = self.index(row, C_BOOK)
        self.dataChanged.emit(i, i)

    def ts(self, t):
        return (t - (self.t_origin or 0)) / 1e9

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        t = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 13: return t.source.upper()
            if c == 14: return str(t.t_start)
            if c == 15: return str(t.t_end) if t.t_end is not None else 'UNKNOWN'
            if c == 16: return ' | '.join('ACK' if f.addr_ack is True else 'NACK' if f.addr_ack is False else 'UNKNOWN' for f in t.frames)
            if c == 17: return ' | '.join(' '.join('ACK' if a is True else 'NACK' if a is False else 'UNKNOWN' for a in f.acks) for f in t.frames)
            if c == 18: return f"{t.timing['frequency_hz']/1e3:.1f}" if t.timing.get('frequency_hz') else 'UNKNOWN'
            if c == C_BOOK:
                return "★" if t.id in self.bookmarks else ""
            if c == C_ID:
                return t.id
            if c == C_TS:
                return f"{self.ts(t.t_start):.6f}"
            if c == C_ADDR:
                a = t.address
                return "—" if a is None else (f"0x{a:03X}" if t.ten_bit else f"0x{a:02X}")
            if c == C_DEV:
                return self.engine.registry.name(t.address)[0]
            if c == C_MODE:
                return "10-bit" if t.ten_bit else "7-bit"
            if c == C_RW:
                return t.operation
            if c == C_DATA:
                return t.data_hex(12) or "—"
            if c == C_BYTES:
                return t.byte_count
            if c == C_ACK:
                return t.ack_seq()
            if c == C_EVT:
                return t.event_seq()
            if c == C_DUR:
                return fmt_dur(t.duration_ns)
            if c == C_STAT:
                return STATUS_TEXT.get(t.status, t.status) + (" · DEMO" if t.demo else "")
        elif role == Qt.ItemDataRole.UserRole:      # sort keys
            return {C_ID: t.id, C_TS: t.t_start, C_ADDR: t.address if t.address is not None else -1,
                    C_BYTES: t.byte_count, C_DUR: t.duration_ns}.get(c, self.data(index))
        elif role == Qt.ItemDataRole.ForegroundRole:
            if c == C_RW:
                if t.has_read and t.has_write:
                    return QBrush(QColor(COLORS["cyan"]))
                return QBrush(QColor(COLORS["cyan"] if t.has_read else COLORS["blue"]))
            if c == C_ACK:
                # Red only for NACKs that are not a verified normal end-of-read NACK.
                # The text column still lists every raw N (nothing is hidden).
                k = acknowledgment_counts(t)
                if k['unexpected_nacks']:
                    return QBrush(QColor(COLORS["red"]))
                if k['unverified_read_end_nacks']:
                    return QBrush(QColor(COLORS["amber"]))
                return QBrush(QColor(COLORS["green"]))
        elif role == Qt.ItemDataRole.ToolTipRole and c == C_ACK:
            k = acknowledgment_counts(t)
            return (f"ACK {k['acks']} · raw NACK {k['raw_nacks']} · expected end-of-read NACK {k['expected_read_nacks']} · "
                    f"unexpected NACK {k['unexpected_nacks']} · unverified end-of-read {k['unverified_read_end_nacks']}")
            if c == C_STAT:
                return QBrush(QColor({"ok": COLORS["green"], "malformed": COLORS["red"]}.get(t.status, COLORS["gray"])))
            if c == C_BOOK:
                return QBrush(QColor(COLORS["amber"]))
            if t.status == "malformed":
                return QBrush(QColor(COLORS["red"]))
            if t.status in ("incomplete", "gap"):
                return QBrush(QColor(COLORS["gray"]))
        elif role == Qt.ItemDataRole.FontRole and c in (C_TS, C_ADDR, C_DATA, C_ACK, C_EVT, C_DUR):
            return self.mono
        elif role == Qt.ItemDataRole.TextAlignmentRole and c in (C_ID, C_BYTES, C_DUR):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(t.notes) if t.notes else None
        return None


class PacketFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.addr: int | None = None
        self.rw = "All"
        self.ack = "All"
        self.status = "All"
        self.text = ""
        self.only_bookmarks = False
        self.min_id = 0
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        m = self.sourceModel()
        t = m.rows[row]
        if t.id <= self.min_id:
            return False
        if self.addr is not None and t.address != self.addr:
            return False
        if self.rw == "READ" and not t.has_read:
            return False
        if self.rw == "WRITE" and not t.has_write:
            return False
        if self.ack == "Has NACK" and t.nack_count == 0:
            return False
        if self.ack == "Address NACK" and not t.addr_nack:
            return False
        if self.ack == "All ACK" and t.nack_count:
            return False
        if self.status != "All" and STATUS_TEXT.get(t.status) != self.status:
            return False
        if self.only_bookmarks and t.id not in m.bookmarks:
            return False
        if self.text:
            q = self.text.lower()
            hay = f"{t.id} {t.address and hex(t.address)} {m.engine.registry.name(t.address)[0]} " \
                  f"{t.data_hex()} {t.operation} {t.status}".lower()
            if q not in hay:
                return False
        return True
