"""Shared inspection cursor. Playback never mutates capture records or acquisition."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    timestamp: float | None
    text: str
    address: int | None = None
    direction: str | None = None     # 'W' controller transmits, 'R' target transmits
    ack: bool | None = None
    kind: str = ""                   # cond | addr | rw | data | ack
    note: str = ""
    byte: int | None = None          # byte on the wire this stage belongs to (address byte incl. R/W)
    frame_rw: str | None = None      # 'R' / 'W' of the enclosing frame


def _nack_note(txn, fi, byte_kind, data_index):
    """Classify a NACK exactly like analysis.communication_health (no hiding, no guessing)."""
    f = txn.frames[fi]
    if byte_kind != 'data':
        return 'SDA HIGH at 9th clock — address not acknowledged (unexpected)'
    if f.rw == 'R':
        last = data_index == len(f.data) - 1 and all(a is True for a in f.acks[:data_index])
        boundary = fi + 1 < len(txn.frames) or any(k == 'P' for _, k in txn.events)
        if last and boundary:
            return 'controller left SDA HIGH: normal end-of-read NACK (expected, still counted)'
        if last:
            return 'final read NACK, but STOP/Sr not captured (unverified)'
        return 'controller NACK before the final byte (unexpected)'
    return 'SDA HIGH at 9th clock — written byte not acknowledged (unexpected)'


def stages(txn):
    """Chronological protocol stages built only from decoded record fields."""
    out = []
    for fi, f in enumerate(txn.frames):
        out.append(Stage(f.t_start, 'START' if f.start_kind == 'S' else 'Repeated START', f.addr, kind='cond',
                         frame_rw=f.rw))
        data_index = 0
        for b in (b for b in txn.bytes if b.frame == fi):
            exact = bool(b.bit_times)
            if b.kind == 'addr':
                out.append(Stage(b.t_first if exact else None, f'ADDR 0x{f.addr:02X}', f.addr, 'W', kind='addr',
                                 byte=b.value, frame_rw=f.rw))
                out.append(Stage(b.bit_times[7] if exact else None,
                                 f'{"READ R=1" if f.rw == "R" else "WRITE W=0"}', f.addr, 'W', kind='rw',
                                 byte=b.value, frame_rw=f.rw))
            else:
                out.append(Stage(b.t_first if exact else None, f'{"DATA" if b.kind == "data" else "ADDR10"} 0x{b.value:02X}',
                                 f.addr, f.rw if b.kind == 'data' else 'W', kind='data' if b.kind == 'data' else 'addr',
                                 byte=b.value, frame_rw=f.rw))
            # ACK slot: the receiver of the byte is the expected acknowledger.
            role = 'W' if b.kind == 'data' and f.rw == 'R' else 'R'
            note = '' if b.ack else _nack_note(txn, fi, b.kind, data_index)
            out.append(Stage(b.t_ack if exact else None, 'ACK' if b.ack else 'NACK', f.addr, role, b.ack,
                             kind='ack', note=note, byte=b.value, frame_rw=f.rw))
            if b.kind == 'data':
                data_index += 1
    for t, kind in txn.events:
        if kind == 'P': out.append(Stage(t, 'STOP', kind='cond'))
    if txn.status != 'ok': out.append(Stage(txn.t_end, txn.status.upper() + ' — last verified event', kind='cond'))
    return out


class Playback:
    def __init__(self):
        self.txn = None
        self.events = []
        self.position = 0.0
        self.speed = 1.0
        self.pinned = False

    def select(self, txn, pinned=True):
        self.txn, self.events = txn, stages(txn)
        self.position = 0.0
        self.pinned = pinned

    def advance(self, seconds):
        # Readable event stepping, not an alternate hardware clock/timebase.
        if self.events:
            self.position = min(len(self.events)-1, self.position + max(0, seconds)*self.speed/0.35)

    @property
    def current(self):
        return self.events[int(self.position)] if self.events else None

    @property
    def finished(self):
        return not self.events or self.position >= len(self.events)-1


def format_capture_ns(ns):
    """HH:MM:SS.ffffff on the capture clock (never wall-clock time)."""
    if ns is None:
        return 'UNKNOWN'
    micros = int(round(ns / 1000))
    seconds, us = divmod(micros, 1000000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f'{hours:02d}:{minute:02d}:{sec:02d}.{us:06d}'


def capture_clock_text(engine):
    """Format capture time, never wall-clock time or an invented event timestamp."""
    stamp = format_capture_ns
    name = 'Simulation time' if engine.source == 'demo' else 'Recorded time' if engine.source == 'replay' else 'Capture time'
    lines = [f'{name}: {stamp(engine.hw_now)} (capture clock)']
    for direction in ('READ', 'WRITE'):
        entry = engine.last_transfer_timestamps.get(direction)
        if entry is None:
            lines.append(f'Last {direction}: —')
        else:
            ns, exact, packet_id = entry
            precision = 'data event' if exact else 'frame start; byte time unavailable'
            lines.append(f'Last {direction}: {stamp(ns)} · #{packet_id} · {precision}')
    return '\n'.join(lines)
