"""Analyzer -> PC protocol, version 1 (JSON Lines).

One JSON object per line, UTF-8, terminated by '\\n'. Every record has "t" (type).
Records that carry "seq" share ONE monotonically increasing uint32 counter, so
the PC can detect lost records. See PROTOCOL.md for the full specification.

  hello  {"t":"hello","proto":1,"fw":"...","board":"RP2040","sample_hz":10000000,
          "tick_ns":100,"caps":["edges","start_stop","seq","stats"]}
  edges  {"t":"edges","seq":7,"t0":123456,"e":[[dt,s],[dt,s],...]}
            t0  = hardware tick of the first edge in this record (uint64)
            dt  = ticks since previous edge in this record (first dt is 0)
            s   = line state after the edge: bit0 = SDA, bit1 = SCL
  evt    {"t":"evt","seq":8,"ts":123999,"kind":"start"|"stop"|"overflow"|"dropped","n":3}
  stat   {"t":"stat","seq":9,"ts":124000,"buf_pct":12,"dropped":0,"overflows":0}
  txn    {"t":"txn","seq":10,...}   (only if caps contains "decoded"; reserved)
  log    {"t":"log","msg":"free text"}

PC -> analyzer commands (one JSON object per line):
  {"cmd":"hello"}  {"cmd":"start"}  {"cmd":"stop"}
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

KNOWN_TYPES = {"hello", "edges", "evt", "stat", "txn", "log", "segment", "loss", "native_status"}
KNOWN_CAPS = {"edges", "start_stop", "seq", "stats", "decoded", "lossless", "binary", "segment", "loss", "status", "7bit", "10bit", "ack", "timing"}
MAX_LINE = 64 * 1024


class ProtocolError(ValueError):
    """A line that could not be turned into a valid record."""


@dataclass
class Record:
    type: str
    seq: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Capabilities:
    proto: int = 0
    firmware: str = "unknown"
    board: str = "unknown"
    sample_hz: int = 0
    tick_ns: float = 0.0
    caps: frozenset = frozenset()

    def has(self, cap: str) -> bool:
        return cap in self.caps

    @property
    def known(self) -> bool:
        return self.proto > 0


def _int(v, name, lo=0, hi=2**64):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError(f"{name} must be an integer")
    if not lo <= v < hi:
        raise ProtocolError(f"{name} out of range")
    return v


def parse_line(line: bytes | str) -> Record:
    """Validate one line. Raises ProtocolError on anything malformed."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE:
            raise ProtocolError("line too long")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ProtocolError("not UTF-8") from e
    if len(line) > MAX_LINE:
        raise ProtocolError("line too long")
    line = line.strip()
    if not line:
        raise ProtocolError("empty line")
    if not line.startswith("{"):
        raise ProtocolError("not a JSON object")
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, RecursionError, ValueError) as e:
        raise ProtocolError(f"bad JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError("not a JSON object")
    # Our RP2040 C bundle uses {"type":...} instead of the GUI's earlier
    # {"t":...} wire format. Normalize the handshake, not the measurements.
    if "t" not in obj and isinstance(obj.get("type"), str):
        native = obj["type"]
        if native == "hello":
            obj = dict(obj)
            obj["t"] = "hello"
            obj["proto"] = obj.get("protocol", 0)
            obj["fw"] = "RP2040 segment analyzer"
            obj["sample_hz"] = obj.get("sample_rate", 0)
            resolution = obj.get("sample_resolution_us", 0)
            if isinstance(resolution, bool) or not isinstance(resolution, (int, float)) or not math.isfinite(resolution) or resolution < 0:
                raise ProtocolError("invalid sample_resolution_us")
            obj["tick_ns"] = resolution * 1000
            obj["caps"] = obj.get("capabilities", [])
            obj["wire"] = "rp2040_segment_v1"
        elif native in {"segment", "loss"}:
            obj = dict(obj)
            obj["t"] = native
        elif native == "status":
            obj = dict(obj)
            obj["t"] = "native_status"
        elif native == "error":
            obj = dict(obj)
            obj["t"] = "log"
            obj["msg"] = str(obj.get("message", "analyzer command error"))
        elif native == "sensor_telemetry":
            raise ProtocolError("master telemetry belongs on the separate master COM port")
    t = obj.get("t")
    if not isinstance(t, str) or t not in KNOWN_TYPES:
        raise ProtocolError(f"unknown record type {t!r}")

    seq = obj.get("seq")
    if seq is not None:
        seq = _int(seq, "seq", 0, 2**32)

    if t == "hello":
        _int(obj.get("proto", 0), "proto", 1, 1000)
        _int(obj.get("sample_hz", 0), "sample_hz")
        tick = obj.get("tick_ns", 0)
        if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not math.isfinite(tick) or tick < 0:
            raise ProtocolError("invalid tick_ns")
        if not isinstance(obj.get("caps", []), list):
            raise ProtocolError("caps must be a list")
    elif t == "edges":
        _int(obj.get("t0"), "t0")
        if "snapshot" in obj and type(obj["snapshot"]) is not bool:
            raise ProtocolError("snapshot must be true/false")
        e = obj.get("e")
        if not isinstance(e, list):
            raise ProtocolError("e must be a list")
        for item in e:
            if (not isinstance(item, list) or len(item) != 2):
                raise ProtocolError("edge must be [dt, state]")
            _int(item[0], "dt", 0, 2**32)
            _int(item[1], "state", 0, 4)
    elif t == "evt":
        if not isinstance(obj.get("kind"), str) or obj["kind"] not in {"start", "stop", "overflow", "dropped", "restart"}:
            raise ProtocolError("unknown evt kind")
        _int(obj.get("ts", 0), "ts")
    elif t == "stat":
        for k in ("buf_pct", "dropped", "overflows"):
            if k in obj:
                _int(obj[k], k)
    elif t == "segment":
        for k in ("start_us", "end_us", "duration_us"):
            _int(obj.get(k), k)
        if obj["end_us"] < obj["start_us"]:
            raise ProtocolError("negative segment duration")
        if obj.get("start") not in ("START", "RESTART") or obj.get("end") not in ("STOP", "RESTART", "GAP"):
            raise ProtocolError("invalid segment boundary")
        if obj.get("direction") not in ("read", "write"):
            raise ProtocolError("invalid direction")
        if obj.get("address_bits") not in (7, 10):
            raise ProtocolError("invalid address width")
        addr = _int(obj.get("address"), "address")
        if addr >= (1 << obj["address_bits"]):
            raise ProtocolError("address out of range")
        for k in ("address_known", "address_ack", "truncated", "incomplete"):
            if type(obj.get(k)) is not bool:
                raise ProtocolError(f"{k} must be true/false")
        data, acks = obj.get("data"), obj.get("acks")
        if not isinstance(data, list) or not isinstance(acks, list) or len(data) != len(acks) or len(data)>64:
            raise ProtocolError("data/acks mismatch or >64 bytes")
        for b in data:
            _int(b, "data byte", 0, 256)
        if any(type(a) is not bool for a in acks):
            raise ProtocolError("ACK entries must be booleans")
        for k in ("scl_hz", "scl_high_avg_ns", "scl_low_avg_ns", "scl_low_over_50us"):
            _int(obj.get(k, 0), k)
    elif t == "loss":
        _int(obj.get("t_us"), "t_us")
        _int(obj.get("lost_buffers"), "lost_buffers")
    elif t == "native_status":
        if type(obj.get("capturing")) is not bool:
            raise ProtocolError("capturing must be true/false")
        _int(obj.get("dma_lost_buffers"), "dma_lost_buffers")
        _int(obj.get("dropped_records"), "dropped_records")
        _int(obj.get("waveform_dropped", 0), "waveform_dropped")
    elif t == "log":
        if not isinstance(obj.get("msg", ""), str):
            raise ProtocolError("msg must be text")
    return Record(type=t, seq=seq, data=obj)


def capabilities_from_hello(rec: Record) -> Capabilities:
    d = rec.data
    sample_hz = int(d.get("sample_hz", 0) or 0)
    tick_ns = float(d.get("tick_ns") or (1e9 / sample_hz if sample_hz else 0))
    caps = frozenset(c for c in d.get("caps", []) if isinstance(c, str))
    return Capabilities(proto=int(d.get("proto", 0)), firmware=str(d.get("fw", "unknown")),
                        board=str(d.get("board", "unknown")), sample_hz=sample_hz,
                        tick_ns=tick_ns, caps=caps)


def command(name: str) -> bytes:
    return (json.dumps({"cmd": name}) + "\n").encode()


class SequenceTracker:
    """Detects lost or repeated records from the shared sequence counter."""

    def __init__(self):
        self.expected: int | None = None
        self.lost = 0
        self.gaps = 0

    def reset(self):
        self.expected = None

    def check(self, seq: int | None) -> int:
        """Return number of records lost before this one (0 if none)."""
        if seq is None:
            return 0
        lost = 0
        if self.expected is not None and seq != self.expected:
            lost = (seq - self.expected) % 2**32
            if lost > 2**31:          # went backwards: firmware restart, not a loss
                lost = 0
            else:
                self.lost += lost
                self.gaps += 1
        self.expected = (seq + 1) % 2**32
        return lost


class LineAssembler:
    """Splits a raw byte stream into lines, coping with partial reads."""

    def __init__(self):
        self.buf = bytearray()
        self.overlong = 0
        self.discarding = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf.extend(chunk)
        out = []
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                if len(self.buf) > MAX_LINE:      # garbage with no newline: resync
                    self.buf.clear()
                    self.overlong += 1
                    self.discarding = True
                break
            if self.discarding:
                self.discarding = False
            elif i > MAX_LINE:
                self.overlong += 1
            else:
                out.append(bytes(self.buf[:i]))
            del self.buf[:i + 1]
        return out
