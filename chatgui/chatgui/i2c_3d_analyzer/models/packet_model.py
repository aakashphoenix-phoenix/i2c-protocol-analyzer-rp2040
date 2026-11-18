"""Qt table model over CaptureEngine.packets (no per-cell widgets)."""
from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor, QFont

from analysis import scl_timing
from analysis.communication_health import acknowledgment_counts
from analysis.multi_master import attribution_detail, attribution_text
from config import COLORS
from gui.theme import mono_font

COLUMNS = ["★", "#", "HW time (s)", "Address", "Device", "Mode", "R/W", "Data (hex)",
           "Bytes", "ACK/NACK", "Events", "Duration", "Status", "Source", "Start (ns)", "End (ns)", "Address ACK", "Data ACKs", "SCL (kHz)",
           "Addr byte (8-bit)", "SCL measurement scope", "Master", "Decoded"]
C_BOOK, C_ID, C_TS, C_ADDR, C_DEV, C_MODE, C_RW, C_DATA, C_BYTES, C_ACK, C_EVT, C_DUR, C_STAT = range(13)
C_MASTER = 21          # appended (keeps every older column index); moved next to Device in the views
C_START, C_END, C_SCL, C_ABYTE = 14, 15, 18, 19
C_SEMANTIC = 22        # inline semantic decode ("Read RTC: 08:22:13"); shown right after the raw hex bytes in the views
# Numeric / hex / timestamp columns: MONO_FONT (digits keep one width while rows refresh); right-aligned where it is a
# single number so the units digits line up.
MONO_COLS = {C_ID, C_TS, C_ADDR, C_DATA, C_BYTES, C_ACK, C_EVT, C_DUR, C_START, C_END, C_SCL, C_ABYTE, 16, 17}
# columns whose text can be longer than any sensible column width: the full text is the tooltip (never silently cut)
LONG_TEXT_COLS = {C_DEV, C_DATA, C_ACK, C_EVT, 16, 17, 20}
RIGHT_COLS = {C_ID, C_TS, C_BYTES, C_DUR, C_START, C_END, C_SCL}

STATUS_TEXT = {"ok": "Complete", "incomplete": "Incomplete", "malformed": "Malformed", "gap": "Capture gap"}


def semantic_text(t) -> str:
    """Inline semantic decode of one packet: only what the sensor decoder verified, otherwise '—'."""
    return getattr(t, "semantic", "") or "—"


def is_error_packet(t) -> bool:
    """'Errors / NACKs only': a packet that is not a complete transaction, or has a NACK that is not the normal
    end-of-read NACK (address NACK, written-byte NACK, early read NACK, or an end-of-read NACK whose STOP was not
    captured).  The normal controller end-of-read NACK is protocol behaviour, not an error (see Error page)."""
    if t.status != "ok" or t.addr_nack:
        return True
    k = acknowledgment_counts(t)
    return bool(k["unexpected_nacks"] or k["unverified_read_end_nacks"])


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
        self.mono = mono_font()
        # The packet the 3D view is playing right now: its row fills in stage by stage, in lockstep with the animation
        # (data bytes, ACK/NACK letters and events appear when their stage is shown) and is highlighted, with the live
        # phase in its Status cell.  (txn, stages, stage index) or None.
        self.live = None
        self._live_row = None

    # ------------------------------------------------------------------ live row (3D lockstep)
    def set_live(self, txn, events=None, index=0):
        """Called by the main window on every playback step; emits dataChanged only when the shown stage changes."""
        key = None if txn is None else (id(txn), int(index))
        old = self.live
        if (None if old is None else (id(old[0]), old[2])) == key:
            return
        self.live = None if txn is None else (txn, events or [], int(index))
        rows = set()
        for item in (old, self.live):
            if item is not None:
                r = self._row_of(item[0])
                if r is not None:
                    rows.add(r)
        for r in rows:
            self.dataChanged.emit(self.index(r, 0), self.index(r, len(COLUMNS) - 1))

    def _row_of(self, txn):
        r = self._live_row
        if r is not None and r < len(self.rows) and self.rows[r] is txn:
            return r
        for i in range(len(self.rows) - 1, max(-1, len(self.rows) - 400), -1):
            if self.rows[i] is txn:
                self._live_row = i
                return i
        return None

    def live_cell(self, t, c):
        """Partial text of the live row: only what the animation has shown so far (None = not the live row)."""
        lv = self.live
        if lv is None or lv[0] is not t:
            return None
        from analysis.playback import phase_info
        _txn, ev, idx = lv
        shown = ev[:idx + 1]
        if c == C_DATA:
            parts, cur, rw = [], [], None
            for st in shown:
                if st.kind == "cond" and cur:
                    parts.append(f"{rw}: " + " ".join(cur)); cur = []
                if st.kind == "data":
                    rw = st.frame_rw or rw or "?"
                    cur.append(f"{st.byte:02X}" if st.byte is not None else "??")
            if cur:
                parts.append(f"{rw}: " + " ".join(cur))
            return (" | ".join(parts) or "…") + (" ▸" if idx < len(ev) - 1 else "")
        if c == C_ACK:
            return " ".join("A" if st.ack else "N" for st in shown if st.kind == "ack") or "…"
        if c == C_EVT:
            return " ".join({"START": "S", "Repeated START": "Sr", "STOP": "P"}.get(st.text, "") for st in shown
                            if st.kind == "cond").strip() or "…"
        if c == C_BYTES:
            return sum(1 for st in shown if st.kind == "data")
        if c in (C_DUR, C_SEMANTIC, 16, 17):
            return "…"
        if c == C_STAT:
            info = phase_info(ev, idx)
            if not info:
                return "▶ playing"
            arb = (getattr(t, "presentation", None) or {}).get("arbitration")
            name = "Arbitration & START" if (arb and idx == 0) else info["name"]
            return f"▶ Phase {info['num']}/6 · {name}"
        return None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            right = section in RIGHT_COLS
            return int((Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter)
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
        live = self.live is not None and self.live[0] is t
        if live and role == Qt.ItemDataRole.DisplayRole:
            v = self.live_cell(t, c)
            if v is not None:
                return v
        if live and role == Qt.ItemDataRole.BackgroundRole:
            return QBrush(QColor(COLORS["table_sel"]))
        if live and role == Qt.ItemDataRole.ForegroundRole and c == C_STAT:
            return QBrush(QColor(COLORS["amber"]))
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 13: return t.source.upper()
            if c == 14: return str(t.t_start)
            if c == 15: return str(t.t_end) if t.t_end is not None else 'UNKNOWN'
            if c == 16: return ' | '.join('ACK' if f.addr_ack is True else 'NACK' if f.addr_ack is False else 'UNKNOWN' for f in t.frames)
            if c == 17: return ' | '.join(' '.join('ACK' if a is True else 'NACK' if a is False else 'UNKNOWN' for a in f.acks) for f in t.frames)
            if c == 18: return f"{t.timing['frequency_hz']/1e3:.1f}" if t.timing.get('frequency_hz') else 'UNKNOWN'
            if c == 19:
                return ' | '.join(f"0x{v:02X}" if v is not None else '—'
                                  for v in (scl_timing.address_byte(f) for f in t.frames))
            if c == 20:
                return f"{t.timing.get('frequency_scope', 'UNKNOWN')} · {t.timing.get('frequency_source', '')}"
            if c == C_MASTER:
                return attribution_text(t)
            if c == C_SEMANTIC:
                return semantic_text(t)
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
            return self.foreground(t, c)
        elif role == Qt.ItemDataRole.ToolTipRole:
            return self.tooltip(t, c, index)
        elif role == Qt.ItemDataRole.FontRole and c in MONO_COLS:
            return self.mono
        elif role == Qt.ItemDataRole.TextAlignmentRole and c in RIGHT_COLS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    # ------------------------------------------------------------------ colours / tooltips
    def foreground(self, t, c):
        """ONE place for every text colour (looked up in config.COLORS at call time, so a theme switch applies at once).

        Column colours: R/W (cyan read / blue write), ACK/NACK (red unexpected, amber unverified, green ok), Master
        (the controller's own colour, amber when unattributed), Status (green complete / red malformed / gray
        otherwise) and the bookmark star (amber).  Every other cell follows the row: malformed = red, incomplete or
        capture gap = gray, complete = theme text colour (None)."""
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
        if c == C_MASTER:
            from config import MASTERS
            mid = t.master_id
            return QBrush(QColor(MASTERS[mid]["color"]) if mid in MASTERS else QColor(COLORS["amber"]))
        if c == C_STAT:
            return QBrush(QColor({"ok": COLORS["green"], "malformed": COLORS["red"]}.get(t.status, COLORS["gray"])))
        if c == C_BOOK:
            return QBrush(QColor(COLORS["amber"]))
        if t.status == "malformed":
            return QBrush(QColor(COLORS["red"]))
        if t.status in ("incomplete", "gap"):
            return QBrush(QColor(COLORS["gray"]))
        return None

    def tooltip(self, t, c, index=None):
        if c == C_MASTER:
            return attribution_detail(t, self.engine.masters)
        if c == C_ACK:
            k = acknowledgment_counts(t)
            return (f"ACK {k['acks']} · raw NACK {k['raw_nacks']} · expected end-of-read NACK {k['expected_read_nacks']} · "
                    f"unexpected NACK {k['unexpected_nacks']} · unverified end-of-read {k['unverified_read_end_nacks']}")
        if c == C_SEMANTIC:
            return (("Decoded from bytes the sensor decoder verified" if t.semantic else
                     "— nothing verified for this packet (unknown device, NACK, incomplete, or e.g. no BMP280 calibration "
                     "captured yet)") + (" · SIMULATED (demo)" if t.demo else ""))
        notes = "\n".join(t.notes) if t.notes else None
        if c in LONG_TEXT_COLS and index is not None:
            full = t.data_hex() if c == C_DATA else str(self.data(index, Qt.ItemDataRole.DisplayRole))
            if full and full != "—":
                return full + ("\n" + notes if notes else "")
        return notes


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
        self.errors_only = False     # quick filter: incomplete/malformed packets and NACKs that are not normal end-of-read
        self.master = "All"          # "All" | 1 | 2 | "unattributed"
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
        if self.master != "All":
            if self.master == "unattributed":
                if t.master_id is not None:
                    return False
            elif t.master_id != self.master:
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
        if self.errors_only and not is_error_packet(t):
            return False
        if self.text:
            q = self.text.lower()
            hay = f"{t.id} {t.address and hex(t.address)} {m.engine.registry.name(t.address)[0]} " \
                  f"{t.data_hex()} {t.operation} {t.status} {attribution_text(t)} {semantic_text(t)}".lower()
            if q not in hay:
                return False
        return True
