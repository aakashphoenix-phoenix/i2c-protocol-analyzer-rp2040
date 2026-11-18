"""Shared inspection cursor. Playback never mutates capture records or acquisition."""
import time
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


@dataclass(frozen=True)
class Chip:
    """One clickable chip of the stage progress bar = one or two consecutive protocol stages."""
    label: str                 # 'START', '0x76 W', 'ACK', '0xF7', 'NACK', 'Sr', 'STOP' ...
    indices: tuple             # indices into stages(txn) this chip stands for (first one = where a click jumps to)
    kind: str                  # cond | addr | data | ack
    ack: bool | None = None
    tip: str = ""


def stage_chips(events):
    """Group stages(txn) into the chips of the progress bar: START -> 0x76 W -> ACK -> 0xF7 -> ACK -> ... -> STOP.

    The ADDR stage and its R/W bit are ONE chip ('0x76 W') that covers both stage indices; every other stage is its own
    chip.  Pure function of the decoded stages (no hardware / ACK logic), so it can be tested without a GUI."""
    chips, i = [], 0
    while i < len(events):
        s = events[i]
        nxt = events[i + 1] if i + 1 < len(events) else None
        if s.kind == 'addr' and s.text.startswith('ADDR 0x') and nxt is not None and nxt.kind == 'rw':
            rw = nxt.frame_rw or ('R' if nxt.text.startswith('READ') else 'W')
            tip = f"{s.text} · {nxt.text}"
            chips.append(Chip(f"0x{s.address:02X} {rw}", (i, i + 1), 'addr', None, tip))
            i += 2
            continue
        if s.kind == 'cond':
            label = {'START': 'START', 'Repeated START': 'Sr', 'STOP': 'STOP'}.get(s.text, s.text.split(' — ')[0])
        elif s.kind == 'ack':
            label = 'ACK' if s.ack else 'NACK'
        else:
            label = s.text.split(' ', 1)[-1] if ' ' in s.text else s.text          # 'DATA 0xF7' -> '0xF7'
        tip = s.text + (f" · {s.note}" if s.note else "")
        chips.append(Chip(label, (i,), s.kind if s.kind in ('cond', 'addr', 'data', 'ack') else 'data', s.ack, tip))
        i += 1
    return chips


class FollowQueue:
    """Fair, bounded display queue for the 'follow live traffic' mode.

    Playback shows ONE packet at a time (seconds each), so keeping only the newest packet (the old `act[-1]`) starved every
    device that was not the busiest.  This queue keeps, per (address, master) bucket, the NEWEST packet that has not been
    played yet (older unplayed ones of the same bucket are superseded - they are still in the packet table), and serves the
    buckets round-robin: addresses in ascending order (0x3C, 0x68, 0x76 ...), and the masters that talk to an address in turn.
    Only genuine decoded transactions are queued; an unattributed packet (master_id None) keeps its own bucket and is never
    assigned to a master.  Memory is bounded by (#addresses x #masters).

    Controller telemetry attributes a packet AFTER the analyzer decoded it (it arrives on another COM port and updates the
    queued packet object in place).  Two things follow from that:
      * every packet is filed under the master it has NOW, not the one it had when it was queued (re-keyed in pop()), so a
        late-attributed Master 2 packet takes its own turn instead of being superseded by an unrelated unattributed one;
      * pop(can_attribute=...) holds a packet that telemetry has not attributed yet (unattributed, or only the configured
        ownership label) back for ATTRIBUTION_SETTLE_S while a controller whose clock is aligned could still claim it - otherwise its first stages play on the analyzer-tap stub (a fraction of
        the wire) and only the later ones on the master's full wire.  After the grace period it plays unattributed:
        a master is never guessed."""

    ATTRIBUTION_SETTLE_S = 0.8
    MASTER_GUARD = 5             # bound: a waiting pair is shown within MASTER_GUARD + 1 packets (5 pairs on the bench)

    def __init__(self):
        self.clear()

    def clear(self):
        self.pending = {}            # address -> {master_id -> txn}
        self.newest = None
        self.last_addr = None
        self.last_master = {}
        self.recent_mids = []        # masters of the last packets played (starvation guard)
        self.shown_at = {}           # (address, master) -> play counter when that pair was last shown
        self.plays = 0
        self._meta = {}              # id(txn) -> (offer sequence, monotonic time it was queued)
        self._seq = 0

    def __len__(self):
        return sum(len(b) for b in self.pending.values())

    @staticmethod
    def _key(v):
        return (v is None, v if v is not None else 0)

    def offer(self, txns, now=None):
        now = time.monotonic() if now is None else now
        self._rekey()                # a packet attributed since it was queued must not be superseded under its old key
        for t in txns:
            self.newest = t
            bucket = self.pending.setdefault(t.address, {})
            mid = getattr(t, "master_id", None)
            waiting_since = now
            if mid in bucket:
                # superseded: the newer packet takes the older one's place AND its waiting time, otherwise a device polled
                # faster than ATTRIBUTION_SETTLE_S would restart the wait with every packet and never get its turn
                waiting_since = self._meta.pop(id(bucket[mid]), (0, now))[1]
            bucket[mid] = t
            self._seq += 1
            self._meta[id(t)] = (self._seq, waiting_since)

    def _rekey(self):
        """File every queued packet under the master it is attributed to now (newest wins when two collide)."""
        for addr, bucket in self.pending.items():
            if all(getattr(t, "master_id", None) == m for m, t in bucket.items()):
                continue
            fixed = {}
            for t in sorted(bucket.values(), key=lambda q: self._meta[id(q)][0]):
                mid = getattr(t, "master_id", None)
                if mid in fixed:                                   # newer wins, the bucket keeps the older waiting time
                    seq, since = self._meta[id(t)]
                    self._meta[id(t)] = (seq, self._meta.pop(id(fixed[mid]))[1])
                fixed[mid] = t
            self.pending[addr] = fixed

    @staticmethod
    def awaiting_telemetry(t):
        """True while a controller's own transfer report could still change this packet's master: it has none yet, or only
        the configured-ownership label (Master 2 may address an owned target; its telemetry overrides ownership)."""
        return getattr(t, "master_id", None) is None or getattr(t, "master_method", "") in ("", "ownership")

    def pop(self, now=None, can_attribute=None):
        """The next packet to play (removed from the queue), or None (empty, or every packet is still awaiting telemetry).

        can_attribute: optional callable; True while some controller could still attribute a packet (connected + clock
        aligned).  Only then is a packet that has no telemetry attribution yet held for ATTRIBUTION_SETTLE_S."""
        if not self.pending:
            return None
        self._rekey()
        now = time.monotonic() if now is None else now
        hold = bool(can_attribute()) if can_attribute is not None else False

        def ready(t):
            return not (hold and self.awaiting_telemetry(t) and now - self._meta[id(t)][1] < self.ATTRIBUTION_SETTLE_S)
        view = {a: {m: t for m, t in b.items() if ready(t)} for a, b in self.pending.items()}
        view = {a: b for a, b in view.items() if b}
        if not view:
            return None
        addrs = sorted(view, key=self._key)
        # Every (target, master) PAIR takes its turn: the pair shown least recently goes next (never the same target twice
        # in a row while another one waits).  At the uniform review pace a real OLED frame or RTC burst plays ~1 min, so
        # only a handful of transfers fit in a few minutes: on the bench (Master 1 x RTC / BMP280 / OLED, Master 2 x RTC /
        # BMP280) every pair is shown within 5 transfers - Master 2's rarer polls can no longer be passed over.
        if self.last_addr is None:                              # first pick: the bucket of the newest packet
            addr = self.newest.address if self.newest is not None and self.newest.address in view else addrs[0]
            bucket = view[addr]
            masters = sorted(bucket, key=self._key)
            newest_here = self.newest is not None and self.newest.address == addr
            mid = getattr(self.newest, "master_id", None) if newest_here else masters[0]
            if mid not in bucket:
                mid = masters[0]
        else:
            pairs = [(a, m) for a in addrs for m in view[a]]
            cand = [q for q in pairs if q[0] != self.last_addr] or pairs
            addr, mid = min(cand, key=lambda q: (self.shown_at.get(q, -1), self._key(q[0]), self._key(q[1])))
            bucket = view[addr]
        txn = bucket[mid]
        del self.pending[addr][mid]
        if not self.pending[addr]:
            del self.pending[addr]
        self._meta.pop(id(txn), None)
        self.last_addr, self.last_master[addr] = addr, mid
        self.plays += 1
        self.shown_at[(addr, mid)] = self.plays
        self.recent_mids = (self.recent_mids + [mid])[-8:]
        return txn


def condensed_stages(txn):
    """Real-time view of one transfer in at most 5 phases (one screen refresh each): START + ADDR + R/W · address ACK/NACK
    · DATA xn (incl. Sr) · the deciding ACK/NACK (an unexpected NACK, else the end-of-read NACK, else ACK) · STOP.
    Built only from stages(txn); a NACK is never hidden."""
    full = stages(txn)
    addr = next((s for s in full if s.kind == 'addr'), None)
    if addr is None:
        return full
    rw = next((s for s in full if s.kind == 'rw'), None)
    out = [Stage(full[0].timestamp, f"START · ADDR 0x{addr.address:02X} {'R' if rw and rw.frame_rw == 'R' else 'W'}",
                 addr.address, 'W', kind='addr', byte=addr.byte, frame_rw=addr.frame_rw)]
    first_ack = next((s for s in full if s.kind == 'ack'), None)
    if first_ack is not None:
        out.append(first_ack)
    data = [s for s in full if s.kind == 'data']
    if data and (first_ack is None or first_ack.ack is not False):
        sr = any(s.text == 'Repeated START' for s in full)
        vals = " ".join(s.text.split(" ", 1)[-1].replace("0x", "") for s in data[:4]) + (" …" if len(data) > 4 else "")
        out.append(Stage(data[0].timestamp, f"DATA ×{len(data)}" + (" · Sr · READ" if sr else "") + f" ({vals})"
                         + " · each byte ACK/NACKed - condensed real-time view",
                         data[-1].address, data[-1].direction, kind='data', byte=data[-1].byte, frame_rw=data[-1].frame_rw))
        later = full[full.index(first_ack) + 1:] if first_ack is not None else full
        acks = [s for s in later if s.kind == 'ack']
        bad = next((a for a in acks if a.ack is False and 'normal end-of-read' not in a.note), None)
        if acks:
            out.append(bad or acks[-1])
    out += [s for s in full if s.kind == 'cond' and s.text not in ('START', 'Repeated START')]
    return out


GUIDED_FULL_BYTES = 8      # guided flow: the first 8 data bytes of a run are played byte by byte, each with its ACK


def capped_stages(txn, keep_bytes=GUIDED_FULL_BYTES):
    """Guided-flow view of a long transfer (the 2.6 OLED text write is 251 bytes): every stage of the first `keep_bytes`
    data bytes (each with its own ACK / NACK), then the rest of that run of data bytes as ONE stage plus its deciding
    ACK / NACK - a NACK is never hidden.  START / Sr / address / R/W / STOP are always kept.  Short transfers (RTC,
    BMP280 reads, probes) are returned unchanged."""
    full = stages(txn)
    if sum(1 for s in full if s.kind == "data") <= keep_bytes + 2:
        return full
    out, run, seen = [], [], 0

    def flush():
        data = [s for s in run if s.kind == "data"]
        if len(data) <= 2:
            out.extend(run)
        else:
            acks = [s for s in run if s.kind == "ack"]
            bad = next((a for a in acks if a.ack is False and "normal end-of-read" not in a.note), None)
            vals = " ".join(s.text.split(" ", 1)[-1].replace("0x", "") for s in data[:4]) + " …"
            out.append(Stage(data[0].timestamp, f"DATA bytes {seen - len(data) + 1}–{seen} ×{len(data)} ({vals}) · each "
                             "byte ACK/NACKed - condensed", data[-1].address, data[-1].direction, kind="data",
                             byte=data[-1].byte, frame_rw=data[-1].frame_rw))
            if acks:
                out.append(bad or acks[-1])
        run.clear()

    for s in full:
        if s.kind == "data":
            seen += 1
        if s.kind in ("data", "ack") and (seen > keep_bytes and (s.kind == "data" or run)):
            run.append(s)
            continue
        if run:
            flush()
        out.append(s)
    if run:
        flush()
    return out


class RealtimeQueue:
    """Real-time follow: every decoded packet in CAPTURE ORDER (FIFO), each played in about the real time to the next one.

    When traffic is faster than a packet can be shown (a 70-transfer OLED frame in 150 ms), the oldest queued packets are
    skipped - counted in `skipped`, never reordered - so the animation is never more than MAX_LAG_NS of capture time behind
    the newest captured packet.  A packet that controller telemetry may still attribute waits at most SETTLE_S."""

    MAX_LAG_NS = 2.25e9          # a queued packet this far behind the BUS's current time is skipped (counted)
    LAG_LIMIT_S = 2.35           # ... and the one on display never plays past this much behind the bus
    TYPICAL_AGE_S = 0.6          # normal decode + controller-report latency: a packet older than this catches up
    SETTLE_S = 0.35
    MIN_PACKET_S = 0.3
    CATCHUP_MIN_S = 0.08         # a late packet (after a GUI stall: a tab opened for the first time ~1 s) may play this
    #                              short, so 3-5 queued SEQ packets drain before they reach MAX_LAG_NS (measured live:
    #                              with 0.3 s each they were skipped ~2 s after every first tab switch)
    MAX_PACKET_S = 1.5

    def __init__(self):
        self.clear()

    def clear(self):
        from collections import deque
        self.q = deque()
        self.skipped = 0
        self.newest = None
        self.interval_s = None       # moving estimate of the capture-time spacing between packets

    def __len__(self):
        return len(self.q)

    def offer(self, txns, now=None):
        now = time.monotonic() if now is None else now
        for t in txns:
            if self.newest is not None:
                gap = max(0.0, (t.t_start - self.newest.t_start) / 1e9)
                self.interval_s = gap if self.interval_s is None else 0.7 * self.interval_s + 0.3 * gap
            self.q.append((t, now))
            self.newest = t

    def pop(self, now=None, can_attribute=None, bus_now_ns=None):
        """bus_now_ns: the analyzer's current capture time.  Measured live (2026-11-17): bounding the lag only against
        the newest PACKET let a burst (a 70-transfer turn) keep playing through the 4 s idle gap after it - 4.3 s behind
        the bus - because no newer packet arrived to push it out."""
        now = time.monotonic() if now is None else now
        while len(self.q) > 1 and self.newest.t_start - self.q[0][0].t_start > self.MAX_LAG_NS:
            self.q.popleft()
            self.skipped += 1
        if bus_now_ns is not None:
            while self.q and bus_now_ns - self.q[0][0].t_start > self.MAX_LAG_NS:
                self.q.popleft()
                self.skipped += 1
        if not self.q:
            return None
        t, since = self.q[0]
        if (can_attribute is not None and can_attribute() and FollowQueue.awaiting_telemetry(t)
                and now - since < self.SETTLE_S):
            return None
        self.q.popleft()
        return t

    def budget_s(self, txn, bus_now_ns=None, refresh_s=0.0):
        """Wall seconds to play `txn`: the capture-time gap to the next queued packet (else the recent spacing), minus
        one screen refresh (the next packet starts on the refresh after this one ends), and never past LAG_LIMIT_S behind
        the bus."""
        if self.q:
            gap = (self.q[0][0].t_start - txn.t_start) / 1e9
        else:
            gap = self.interval_s if self.interval_s is not None else self.MIN_PACKET_S
        late = 0.0
        if bus_now_ns is not None:            # started late (a telemetry wait, a GUI stall): play shorter and catch up,
            late = max(0.0, (bus_now_ns - txn.t_start) / 1e9 - self.TYPICAL_AGE_S)   # instead of drifting into skips
        floor = self.CATCHUP_MIN_S if late > 0 else self.MIN_PACKET_S
        b = min(self.MAX_PACKET_S, max(floor, gap - refresh_s - late))
        if bus_now_ns is not None:
            age = (bus_now_ns - txn.t_start) / 1e9
            b = min(b, max(floor, self.LAG_LIMIT_S - age))
        return b


class Playback:
    def __init__(self):
        self.txn = None
        self.events = []
        self.position = 0.0
        self.speed = 1.0
        self.pinned = False
        self.full_pace = False       # every stage at STAGE_S (presentation / stepping), even while following
        self.realtime = False        # real-time follow: condensed stages, packet budget from RealtimeQueue
        self.rt_packet_s = None

    def select(self, txn, pinned=True, full_pace=False, packet_s=None, stage_s=None):
        """packet_s (real-time follow only): play the condensed packet in this many wall seconds.
        stage_s (guided flow): play EVERY stage - each byte and its own ACK / NACK - for this many wall seconds each."""
        rt = self.realtime and not pinned and not full_pace and packet_s is not None and stage_s is None
        self.txn, self.events = txn, (condensed_stages(txn) if rt else capped_stages(txn) if stage_s else stages(txn))
        self.rt_packet_s = packet_s if rt else None
        self.fixed_stage_s = stage_s
        self.position = 0.0
        self.pinned = pinned
        self.full_pace = full_pace

    # Pace: ONE uniform timing rule for every master, every target and every mode (live follow, pinned, stepping,
    # presentation).  One protocol stage = one animation step of STAGE_S seconds at 1x (divide by the toolbar speed: 3.2 s
    # at the 0.25x default); its dot crosses the wire in 0.8 x the step and the rest is a pause on that phase.
    #   START / Repeated START / STOP, ADDRESS, every DATA byte, every ACK / NACK   1 step each
    #   the R/W bit                                                                 RW_W step (the 8th bit of the address
    #                                                                               byte: same phase 2, no dot of its own)
    # Nothing is compressed or rushed any more: the old follow-mode pacing squeezed a packet into FOLLOW_PACKET_S and
    # played data bytes 4+ of a frame at 0.55 x, so BMP280 bursts (6 bytes) and OLED frames (17 bytes) visibly
    # fast-forwarded while short RTC transfers did not.  (Originally 0.20 s per stage: far too fast to follow.)
    STAGE_S = 0.80
    COND_W = 1.0
    RW_W = 0.5
    # kept for older imports; no longer used for pacing
    FOLLOW_PACKET_S = 14.0
    FOLLOW_MIN_STAGE_S = STAGE_S
    REPEAT_W = 1.0
    FOLLOW_FULL_BYTES = 3

    def _weights(self):
        """Relative duration of every stage (1.0 = one step) - identical rule for every device, master and mode."""
        key = (id(self.events), len(self.events))
        if getattr(self, "_wkey", None) != key:
            self._wkey, self._w = key, [self.RW_W if s.kind == "rw" else 1.0 for s in self.events]
        return self._w

    def stage_seconds(self):
        """Wall seconds one step lasts at 1x (divide by speed for the real duration): the same for every packet - except a
        real-time follow packet, whose condensed stages share its real-time budget."""
        if self.rt_packet_s is not None and self.events:
            return self.rt_packet_s * max(self.speed, 0.05) / max(sum(self._weights()), 1e-9)
        if getattr(self, "fixed_stage_s", None):
            return self.fixed_stage_s * max(self.speed, 0.05)
        return self.STAGE_S

    def stage_duration(self, i):
        """Seconds stage `i` lasts at 1x."""
        w = self._weights()
        return self.stage_seconds() * (w[i] if 0 <= i < len(w) else 1.0)

    def stage_wall_seconds(self, i=None):
        """Real seconds stage `i` (default: the current one) lasts at the current speed."""
        return self.stage_duration(self.stage_index if i is None else i) / max(self.speed, 0.05)

    def total_seconds(self):
        """Real seconds the whole packet takes at the current speed (follow / pinned pace as selected)."""
        return sum(self._weights()) * self.stage_seconds() / max(self.speed, 0.05)

    def max_step_seconds(self):
        """Longest wall time one advance() may cover: just under the shorter of the current and the next stage, so a GUI
        stall can never jump over a stage (every stage is shown, its dot spawned and its label displayed at least once).
        Real-time follow: the rest of the packet - its condensed stages must fit the packet's real-time budget even when a
        stage is shorter than one screen refresh (SEQ: a packet every 0.5 s), or the display falls behind the bus."""
        if self.rt_packet_s is not None and self.events:
            return max(0.0, (len(self.events) - self.position)) * self.stage_seconds() / max(self.speed, 0.05)
        i = self.stage_index
        d = self.stage_duration(i)
        if i + 1 < len(self.events):
            d = min(d, self.stage_duration(i + 1))
        return 0.95 * d / max(self.speed, 0.05)

    def advance(self, seconds):
        # Readable event stepping, not an alternate hardware clock/timebase.  position runs from 0 to len(events): the
        # LAST stage (STOP) is played for its full duration too, and only then is the packet `finished`.
        if not self.events:
            return
        n = len(self.events)
        left = max(0.0, seconds) * max(self.speed, 0.0)            # seconds of 1x time to play
        while left > 1e-12 and self.position < n:
            i = int(self.position)
            dur = self.stage_duration(i)
            remaining = (1.0 - (self.position - i)) * dur
            if left >= remaining:
                self.position = float(i + 1)
                left -= remaining
            else:
                self.position += left / dur
                left = 0.0
        if self.position > n - 1e-9:
            self.position = float(n)        # a rounding hair short of the end would never finish (max step -> 0)
        self.position = min(float(n), self.position)

    @property
    def stage_index(self):
        return min(int(self.position), len(self.events) - 1) if self.events else 0

    @property
    def stage_fraction(self):
        """How far through the current stage playback is (0..1; 1 once the packet has finished)."""
        if not self.events:
            return 0.0
        return 1.0 if self.position >= len(self.events) else self.position - int(self.position)

    @property
    def current(self):
        return self.events[self.stage_index] if self.events else None

    @property
    def finished(self):
        return not self.events or self.position >= len(self.events)


# ------------------------------------------------------------------ 6-phase view of a transfer (review presentation)
PHASES = ("START", "ADDRESS + R/W", "ADDRESS ACK/NACK", "DATA", "DATA ACK/NACK", "STOP")
PHASE_SHORT = ("START", "ADDRESS", "ACK/NACK", "DATA", "ACK/NACK", "STOP")
DOT_TRAVEL = 0.8             # share of a stage the dot needs to cross its wire (Network3D.TRAVEL_FRACTION)


def phase_of(events, i):
    """Phase number 1..6 of stage i (START, ADDRESS + R/W, ADDRESS ACK/NACK, DATA, DATA ACK/NACK, STOP)."""
    s = events[i]
    if s.kind == "cond":
        return 6 if s.text == "STOP" or "last verified" in s.text else 1
    if s.kind in ("addr", "rw"):
        return 2
    if s.kind == "data":
        return 4
    prev = events[i - 1].kind if i > 0 else None      # an ACK slot answers the byte right before it
    return 3 if prev in ("addr", "rw") else 5


def phase_info(events, i, fraction=0.0, master="controller", slave="target"):
    """Everything the phase banner / live Packet Analyzer panel shows for stage i, `fraction` (0..1) into it.

    Pure function of the decoded stages: who sends, who receives, the SDA / SCL levels of the phase, the byte and the
    bits already on the wire (the dot crosses its wire in DOT_TRAVEL of the stage: bit k of 8 is the one being clocked)."""
    if not events:
        return None
    i = max(0, min(i, len(events) - 1))
    s = events[i]
    n = phase_of(events, i)
    rw = s.frame_rw or ("R" if any(e.kind == "rw" and e.text.startswith("READ") for e in events) else "W")
    op = "READ" if rw == "R" else "WRITE"
    m2s, s2m = f"{master} → {slave}", f"{slave} → {master}"
    bit = min(7, int(max(0.0, fraction) / DOT_TRAVEL * 8))
    info = dict(num=n, phase=PHASES[n - 1], short=PHASE_SHORT[n - 1], op=op, kind=s.kind, text=s.text, value=None,
                bits=None, bit=None, ack=s.ack, sender=master, receiver=slave, direction="Master → Slave", sda="", scl="")
    if s.kind == "cond":
        if n == 1:
            rep = s.text == "Repeated START"
            info.update(name="Repeated START" if rep else "START", sda="1 → 0", scl="1",
                        detail=f"{master} pulls SDA LOW while SCL is HIGH" + (" (repeated START, bus kept)" if rep else ""))
        else:
            stop = s.text == "STOP"
            info.update(name="STOP" if stop else "END (capture incomplete)", sda="0 → 1" if stop else "?", scl="1",
                        detail=(f"{master} releases SDA while SCL is HIGH: bus back to IDLE" if stop else s.text))
        return info
    if s.kind in ("addr", "rw"):
        addr_byte = s.byte if s.byte is not None else ((s.address or 0) << 1) | (rw == "R")
        bits = f"{addr_byte:08b}"
        k = 7 if s.kind == "rw" else min(bit, 6)
        info.update(name=f"Address 0x{(s.address or 0):02X} + {'R=1' if rw == 'R' else 'W=0'}", value=addr_byte,
                    bits=bits, bit=k, sda=bits[k], scl=f"clock {k + 1}/8",
                    detail=f"7-bit address 0x{(s.address or 0):02X} then the R/W bit = byte 0x{addr_byte:02X}")
        return info
    if s.kind == "data":
        v = s.byte if s.byte is not None else 0
        bits = f"{v:08b}"
        # numbered within ITS frame (START / repeated START up to the next one): in a register read the first byte read
        # back is "Data byte 1/n" of the read frame, not counted after the pointer byte written before the Sr
        f0 = max((j for j in range(i + 1) if events[j].kind == "cond"), default=0)
        f1 = next((j for j in range(i + 1, len(events)) if events[j].kind == "cond"), len(events))
        data_idx = sum(1 for e in events[f0:i] if e.kind == "data")
        data_n = sum(1 for e in events[f0:f1] if e.kind == "data")
        w = s.direction == "W"
        info.update(name=f"Data byte {data_idx + 1}/{max(data_n, data_idx + 1)} = 0x{v:02X}", value=v, bits=bits, bit=bit,
                    sda=bits[bit], scl=f"clock {bit + 1}/8", direction="Master → Slave" if w else "Slave → Master",
                    sender=master if w else slave, receiver=slave if w else master,
                    detail=(f"{m2s}: WRITE data" if w else f"{s2m}: READ data"))
        return info
    # ACK / NACK slot (9th clock): the receiver of the byte answers
    by_slave = s.direction == "R"
    who, to = (slave, master) if by_slave else (master, slave)
    word = "ACK" if s.ack else "NACK" if s.ack is False else "ACK?"
    label = ("Address " if n == 3 else "Data ") + word
    note = s.note or ""
    if s.ack is False and note.startswith("controller left SDA HIGH"):
        note = "normal end of read: the master NACKs the last byte"
    info.update(name=label, sda="0" if s.ack else "1", scl="9th clock", sender=who, receiver=to,
                direction="Slave → Master" if by_slave else "Master → Slave",
                detail=f"{who} answers {word} (SDA {'LOW' if s.ack else 'HIGH'})" + (f" · {note}" if note else ""))
    return info


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
