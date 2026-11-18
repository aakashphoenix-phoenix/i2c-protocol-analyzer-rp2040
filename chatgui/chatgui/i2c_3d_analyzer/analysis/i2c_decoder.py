"""I²C protocol decoder working on timestamped SDA/SCL transition records.

This is the ONE place where I²C is decoded. It consumes (time_ns, state) samples,
where state bit0 = SDA and bit1 = SCL, as reported by the analyzer's edge records.

Rules (NXP UM10204):
  START   SDA high->low while SCL high
  STOP    SDA low->high while SCL high
  Sr      a START while a transaction is open (no STOP in between)
  bit     SDA sampled on every SCL rising edge
  ACK     9th bit low,  NACK = 9th bit high

Transaction counting: one transaction = START ... STOP. Repeated STARTs inside it
open a new *frame* (address + data) but do not start a new transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from config import TXN_IDLE_TIMEOUT_NS


@dataclass
class ByteRec:
    value: int
    ack: bool
    kind: str            # "addr" | "addr10" | "data"
    t_first: float       # first SCL rise of the byte (ns)
    t_ack: float         # SCL rise of the ACK/NACK bit (ns)
    frame: int
    t_end: float | None = None
    bit_times: tuple = ()     # captured rises, MSB first; never interpolated


@dataclass
class Frame:
    start_kind: str              # "S" or "Sr"
    t_start: float
    addr: int | None = None
    rw: str | None = None        # "R" | "W"
    ten_bit: bool = False
    addr_ack: bool | None = None
    data: list = field(default_factory=list)
    acks: list = field(default_factory=list)


@dataclass
class Transaction:
    id: int
    t_start: float
    t_end: float | None = None
    frames: list = field(default_factory=list)
    bytes: list = field(default_factory=list)
    events: list = field(default_factory=list)       # (t, "S"|"Sr"|"P")
    status: str = "open"          # ok | incomplete | malformed | gap
    notes: list = field(default_factory=list)
    rises: list | None = field(default_factory=list)  # SCL rising edges (ns) - freed after metrics
    falls: list | None = field(default_factory=list)
    demo: bool = False
    source: str = "unknown"
    timing: dict = field(default_factory=dict)
    # Multi-master attribution (analysis.multi_master); a passive tap alone cannot know it.
    master_id: int | None = None
    master_method: str = ""          # telemetry | ownership | demo | "" (unattributed)
    # Inline semantic decode ("Read RTC: 08:22:13", ...), set by SensorDecoder.observe from values it verified;
    # empty = nothing could be verified for this packet.
    semantic: str = ""

    # ---------- convenience views used by tables, filters and exporters
    @property
    def address(self) -> int | None:
        for f in self.frames:
            if f.addr is not None:
                return f.addr
        return None

    @property
    def ten_bit(self) -> bool:
        return any(f.ten_bit for f in self.frames)

    @property
    def operation(self) -> str:
        ops = [f.rw for f in self.frames if f.rw]
        if not ops:
            return "—"
        out = [ops[0]]
        for o in ops[1:]:
            if o != out[-1]:
                out.append(o)
        return "→".join({"R": "READ", "W": "WRITE"}[o] for o in out)

    @property
    def has_read(self) -> bool:
        return any(f.rw == "R" for f in self.frames)

    @property
    def has_write(self) -> bool:
        return any(f.rw == "W" for f in self.frames)

    @property
    def byte_count(self) -> int:
        return sum(len(f.data) for f in self.frames)

    @property
    def duration_ns(self) -> float:
        return (self.t_end - self.t_start) if self.t_end is not None else 0.0

    @property
    def addr_nack(self) -> bool:
        return any(f.addr_ack is False for f in self.frames)

    @property
    def data_nack_write(self) -> bool:
        return any(f.rw == "W" and False in f.acks for f in self.frames)

    @property
    def ack_count(self) -> int:
        return sum(1 for b in self.bytes if b.ack)

    @property
    def nack_count(self) -> int:
        return sum(1 for b in self.bytes if not b.ack)

    def data_hex(self, limit: int = 0) -> str:
        parts = []
        for f in self.frames:
            if not f.data:
                continue
            bs = f.data if not limit else f.data[:limit]
            s = " ".join(f"{b:02X}" for b in bs)
            if limit and len(f.data) > limit:
                s += f" … (+{len(f.data) - limit})"
            parts.append(f"{f.rw or '?'}: {s}")
        return " | ".join(parts)

    def ack_seq(self) -> str:
        return " ".join("A" if b.ack else "N" for b in self.bytes[:24]) + (" …" if len(self.bytes) > 24 else "")

    def event_seq(self) -> str:
        return " ".join(k for _, k in self.events)


class I2CDecoder:
    def __init__(self):
        self.next_id = 1
        self.sda: int | None = None
        self.scl: int | None = None
        self.last_t: float | None = None
        self.txn: Transaction | None = None
        self.frame: Frame | None = None
        self.bit_idx = 0
        self.cur = 0
        self.t_first = 0.0
        self.pending_10bit_hi: int | None = None
        self.last_10bit_addr: int | None = None
        self.synced = False
        self.pending = None
        self.bit_times = []
        self.notices: list = []          # (t, severity, kind, message, addr)

    # ------------------------------------------------------------- public
    def lose_sync(self, t: float | None, reason: str) -> list:
        """Called when records were lost: the line state is unknown until next START."""
        done = []
        if self.txn is not None:
            self.txn.status = "gap"
            self.txn.notes.append(f"capture gap: {reason}")
            self.txn.t_end = self.last_t if self.last_t is not None else self.txn.t_start
            done.append(self.txn)
        self.notices.append((t or self.last_t or 0, "warning", "Decoder sync loss", reason, None))
        self._reset_txn()
        self.sda = self.scl = None
        self.synced = False
        return done

    def check_timeout(self, now_ns: float) -> list:
        if self.txn is not None and self.last_t is not None and now_ns - self.last_t > TXN_IDLE_TIMEOUT_NS:
            t = self.txn
            t.status = "incomplete"
            t.notes.append("no STOP before bus went idle")
            t.t_end = self.last_t
            self.notices.append((self.last_t, "warning", "Incomplete transfer",
                                 f"transaction #{t.id}: no STOP", t.address))
            self._reset_txn()
            return [t]
        return []

    def feed(self, samples) -> list:
        """samples: iterable of (t_ns, state). Returns completed transactions."""
        done = []
        for t, s in samples:
            if not math.isfinite(t) or (self.sda is not None and self.last_t is not None and t <= self.last_t and (t != self.last_t or s != ((self.scl << 1) | self.sda))):
                done += self.lose_sync(self.last_t, "invalid/non-increasing timestamp")
                self.last_t = None
                continue
            sda = s & 1
            scl = (s >> 1) & 1
            if self.sda is None:
                self.sda, self.scl, self.last_t = sda, scl, t
                continue
            psda, pscl = self.sda, self.scl
            self.sda, self.scl, self.last_t = sda, scl, t

            if scl and pscl and sda != psda:
                if sda == 0:
                    self._on_start(t)
                else:
                    r = self._on_stop(t)
                    if r is not None:
                        done.append(r)
                continue
            if scl and not pscl and sda != psda:
                done += self.lose_sync(t, "SDA and SCL rose/changed in the same capture tick; bit is ambiguous")
                continue
            if self.txn is None:
                continue
            if scl and not pscl:
                # sample now, commit on the falling edge: if SDA changes while SCL
                # is high this rise belonged to a START/STOP, not to a data bit
                self.pending = (t, sda)
            elif pscl and not scl and self.pending is not None:
                t_rise, bit = self.pending
                self.pending = None
                self.txn.rises.append(t_rise)
                self.txn.falls.append(t)
                nbytes = len(self.txn.bytes)
                self._on_bit(t_rise, bit)
                if len(self.txn.bytes) > nbytes:
                    self.txn.bytes[-1].t_end = t
        return done

    # ------------------------------------------------------------- internals
    def _reset_txn(self):
        self.txn = None
        self.frame = None
        self.bit_idx = 0
        self.cur = 0
        self.pending_10bit_hi = None
        self.last_10bit_addr = None
        self.pending = None

    def _on_start(self, t):
        self.synced = True
        if self.txn is None:
            self.txn = Transaction(id=self.next_id, t_start=t)
            self.next_id += 1
            kind = "S"
        else:
            kind = "Sr"
            if self.bit_idx not in (0,):
                self.txn.status = "malformed"
                self.txn.notes.append(f"repeated START after {self.bit_idx} bits of a byte")
                self.notices.append((t, "error", "Malformed frame",
                                     f"#{self.txn.id}: START inside a byte", self.txn.address))
        self.txn.events.append((t, kind))
        self.frame = Frame(start_kind=kind, t_start=t)
        self.txn.frames.append(self.frame)
        self.pending = None
        self.bit_idx = 0
        self.cur = 0
        self.pending_10bit_hi = None

    def _on_stop(self, t):
        if self.txn is None:
            return None
        txn = self.txn
        txn.events.append((t, "P"))
        txn.t_end = t
        if txn.status == "open":
            if self.bit_idx != 0:
                txn.status = "incomplete"
                txn.notes.append(f"STOP after {self.bit_idx} bits of a byte")
                self.notices.append((t, "warning", "Incomplete transfer",
                                     f"#{txn.id}: STOP inside a byte", txn.address))
            elif any(f.addr is None for f in txn.frames):
                txn.status = "incomplete"
                txn.notes.append("frame without address byte")
            else:
                txn.status = "ok"
        self._reset_txn()
        return txn

    def _on_bit(self, t, bit):
        if self.bit_idx == 0:
            self.t_first = t
            self.cur = 0
            self.bit_times = []
        if self.bit_idx < 8:
            self.bit_times.append(t)
            self.cur = (self.cur << 1) | bit
            self.bit_idx += 1
            return
        # 9th clock: ACK/NACK
        ack = (bit == 0)
        value = self.cur
        self.bit_idx = 0
        fr = self.frame
        fi = len(self.txn.frames) - 1
        if fr.addr is None and self.pending_10bit_hi is None:
            if (value & 0xF8) == 0xF0:                       # 11110xx: 10-bit address
                fr.ten_bit = True
                fr.rw = "R" if value & 1 else "W"
                hi = (value >> 1) & 0x3
                if fr.rw == "R" and self.last_10bit_addr is not None and (self.last_10bit_addr >> 8) == hi:
                    fr.addr = self.last_10bit_addr
                    fr.addr_ack = ack
                else:
                    self.pending_10bit_hi = hi
                    fr.addr_ack = ack
                kind = "addr10"
            else:
                fr.addr = value >> 1
                fr.rw = "R" if value & 1 else "W"
                fr.addr_ack = ack
                kind = "addr"
                if not ack:
                    self.notices.append((t, "error", "Address NACK",
                                         f"Error: Invalid / unacknowledged slave address 0x{fr.addr:02X} — no slave "
                                         "pulled SDA LOW at the 9th clock (Address NACK)", fr.addr))
        elif self.pending_10bit_hi is not None:
            fr.addr = (self.pending_10bit_hi << 8) | value
            self.last_10bit_addr = fr.addr
            fr.addr_ack = fr.addr_ack and ack
            self.pending_10bit_hi = None
            kind = "addr10"
        else:
            fr.data.append(value)
            fr.acks.append(ack)
            kind = "data"
            if not ack and fr.rw == "W":
                self.notices.append((t, "warning", "Data NACK",
                                     f"0x{fr.addr:02X} refused written byte 0x{value:02X}", fr.addr))
        self.txn.bytes.append(ByteRec(value, ack, kind, self.t_first, t, fi, bit_times=tuple(self.bit_times)))
