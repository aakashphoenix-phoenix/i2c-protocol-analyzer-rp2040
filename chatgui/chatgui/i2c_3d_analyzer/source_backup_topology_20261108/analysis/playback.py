"""Shared inspection cursor. Playback never mutates capture records or acquisition."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Stage:
    timestamp: float | None
    text: str
    address: int | None = None
    direction: str | None = None
    ack: bool | None = None


def stages(txn):
    out = []
    for fi, f in enumerate(txn.frames):
        out.append(Stage(f.t_start, 'START' if f.start_kind == 'S' else 'Repeated START', f.addr))
        for b in (b for b in txn.bytes if b.frame == fi):
            exact = bool(b.bit_times)
            if b.kind == 'addr':
                out.append(Stage(b.t_first if exact else None, f'ADDR 0x{f.addr:02X}', f.addr, 'W'))
                out.append(Stage(b.bit_times[7] if exact else None,
                                 f'{"READ R=1" if f.rw == "R" else "WRITE W=0"}', f.addr, 'W'))
            else:
                out.append(Stage(b.t_first if exact else None, f'{"DATA" if b.kind == "data" else "ADDR10"} 0x{b.value:02X}',
                                 f.addr, f.rw if b.kind == 'data' else 'W'))
            role = 'W' if b.kind == 'data' and f.rw == 'R' else 'R'
            out.append(Stage(b.t_ack if exact else None, 'ACK' if b.ack else 'NACK', f.addr, role, b.ack))
    for t, kind in txn.events:
        if kind == 'P': out.append(Stage(t, 'STOP'))
    if txn.status != 'ok': out.append(Stage(txn.t_end, txn.status.upper() + ' — last verified event'))
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


def capture_clock_text(engine):
    """Format capture time, never wall-clock time or an invented event timestamp."""
    def stamp(ns):
        if ns is None:
            return 'UNKNOWN'
        # Six decimal places are microseconds; preserve the capture clock epoch.
        micros = int(round(ns / 1000))
        seconds, us = divmod(micros, 1000000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f'{hours:02d}:{minute:02d}:{sec:02d}.{us:06d}'
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
