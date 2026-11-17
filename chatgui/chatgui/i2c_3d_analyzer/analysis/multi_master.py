"""Multi-master bookkeeping: two controllers, three targets, one passive analyzer.

What each source can prove
  * Analyzer segments (bus truth): which transactions happened, ACK/NACK and timing.  A
    passive two-wire tap can NOT see which master drove a transaction, and it never sees an
    arbitration loser: the wired-AND bus only shows the winner's bits.
  * Controller telemetry (each master's own USB port): what that master attempted and how
    it ended, including arbitration losses and retries that no bus observer can see.

Attribution joins the two.  A controller transfer and an analyzer packet with the same
signature (address, bytes written, bytes read, first written byte) at the same instant -
once the controller's microsecond clock is mapped onto the analyzer's capture clock - are
the same bus transaction.  Every packet carries how it was attributed:
    telemetry   matched to that master's own transfer report
    ownership   configured rule: only that master ever addresses this target (OLED -> Master 1)
    demo        simulated single-master demo stream (legacy DemoGenerator, no controller telemetry)
A demo of the two-controller bench (DemoGenerator(masters=2)) sends SIMULATED controller telemetry ("simulated":
true on every record): its packets are attributed by the SAME telemetry matching as hardware, and every label that
shows them says SIMULATED (txn.demo is True).
    ""          unattributed (no controller telemetry matched; never guessed)

Arbitration losses are counted ONLY from controller telemetry and are never mixed into
NACK statistics: a lost arbitration is normal multi-master behaviour, retried by the loser.
"""
from __future__ import annotations

import bisect
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

import config

VISIBLE_RESULTS = ("ok", "addr_nack", "data_nack")   # leave a complete, comparable bus record
PRIOR_WINDOW_NS = 80_000_000        # USB-arrival prior: candidate offsets within ±80 ms
LOCK_WINDOW_US = 12_000_000         # controller history used to acquire the clock lock
# Without the USB-arrival prior (replay, demo) an offset is ambiguous for periodic traffic until the analyzer evidence
# spans more than the controllers' schedule period (I2C.c: 1 s / 1.1 s RTC+OLED cycles).  Measured on the real bench
# replay (mm_baseline_before): locking on 19 ms of packets mapped Master 1's latest OLED frame onto the analyzer's
# first one, a false lock 2.000 s off.
NO_PRIOR_MIN_SPAN_NS = 2_500_000_000
# Without the prior, records still arrive in time order (replay files, demo): the transfer that triggers a lock attempt
# is "now", so a candidate offset must map it to within this window of the newest analyzer packet.  Measured on the real
# turn-taking bench (mm_turns, 2026-11-17): bursts repeat every 8.3 s and both masters send identical bursts 4.15 s apart,
# so a whole-cycle (or half-cycle) shift explained the evidence as well as the true offset.
NO_PRIOR_RECENT_NS = 1_000_000_000
HEALTH_WINDOW_S = 60.0
FRESH_S = 3.0
STAT_KEYS = ("txn_ok", "addr_nack", "data_nack", "arb_lost", "retries", "gave_up", "busy_waits", "busy_timeouts",
             "timeouts", "other_aborts", "recoveries", "recovery_skipped", "controller_resets")
DEGRADING = ("gave_up", "busy_timeouts", "timeouts", "recoveries", "controller_resets", "other_aborts")


def packet_signature(txn):
    """(addr, data bytes written, data bytes read, first written byte or -1) of a bus packet."""
    addr = txn.address
    if addr is None or txn.ten_bit:
        return None
    w = r = 0
    b0 = -1
    for f in txn.frames:
        if f.addr != addr:
            continue
        if f.rw == "W":
            if b0 < 0 and f.data:
                b0 = f.data[0]
            w += len(f.data)
        elif f.rw == "R":
            r += len(f.data)
    return addr, w, r, b0


@dataclass
class MasterTxn:
    master_id: int
    seq: int
    t_us: int
    dur_us: int
    addr: int
    wlen: int
    rlen: int
    b0: int
    result: str
    attempts: int
    arb_lost: int
    packet: object = None

    @property
    def sig(self):
        return self.addr, self.wlen, self.rlen, (self.b0 if self.wlen else -1)


@dataclass
class ArbEvent:
    master_id: int
    t_us: int
    addr: int
    attempt: int
    byte: int
    backoff_us: int
    retry: bool
    wall: float
    t_bus_ns: float | None = None
    winner: object = None        # analyzer Transaction that held the bus, when resolved


DRIFT_MIN_PAIRS = 6                  # matched pairs needed before a crystal drift (slope) is estimated (2.5-turns: a master
#                                      makes only 3 transfers per ~9 s turn cycle, fewer in the guided flow: 20 took ~1 min)
DRIFT_MIN_SPAN_US = 4_000_000        # ... spread over at least this much controller time
DRIFT_MAX_NS_PER_US = 0.2            # |slope| above 200 ppm is not a crystal: ignored


def _fit_line(pairs):
    """Least-squares (intercept, slope) of offset_ns over t_us, or None."""
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(t for t, _ in pairs) / n
    my = sum(o for _, o in pairs) / n
    sxx = sum((t - mx) ** 2 for t, _ in pairs)
    if sxx <= 0:
        return None
    slope = sum((t - mx) * (o - my) for t, o in pairs) / sxx
    return my - slope * mx, slope


class _Clock:
    """analyzer_ns = controller_us * 1000 + offset_ns + slope * (controller_us - ref_us), once locked.

    slope models the crystal drift between the controller and the analyzer (measured 2026-11-17: ~30 ppm, i.e. 0.25 ms
    per 8.3 s turn cycle, already more than half the 400 µs match window when a master is silent between turns)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.locked = False
        self.offset_ns: float | None = None
        self.outcomes = deque(maxlen=120)       # True = matched, False = expired unmatched
        self.last_try_wall = -1e9
        self.last_try_n = 0
        self.last_t_us: int | None = None
        self.arrival = deque(maxlen=600)        # wall_ns - controller_ns (USB arrival latency)
        self.locked_at_wall: float | None = None
        self.source = ""
        self.slope = 0.0                         # ns of offset per µs of controller time (ppm / 1000)
        self.ref_us: int | None = None           # controller time at which offset_ns applies
        self.lock_ref_us: int | None = None      # newest transfer when the lock was taken (older misses are not evidence)
        self.pairs = deque(maxlen=400)           # (t_us, observed offset_ns) of matched transfers

    def to_bus(self, t_us):
        if not self.locked or self.offset_ns is None or t_us is None:
            return None
        return t_us * 1000.0 + self.offset_ns + (self.slope * (t_us - self.ref_us) if self.ref_us is not None else 0.0)

    def fit_drift(self):
        """Re-estimate the slope from the matched pairs (keeps offset_ns continuous at ref_us)."""
        if len(self.pairs) < DRIFT_MIN_PAIRS or self.pairs[-1][0] - self.pairs[0][0] < DRIFT_MIN_SPAN_US:
            return
        fit = _fit_line(self.pairs)
        if fit is None or abs(fit[1]) > DRIFT_MAX_NS_PER_US:
            return
        self.slope = fit[1]


class MasterState:
    def __init__(self, mid, cfg):
        self.master_id = mid
        self.cfg = cfg
        self.port: str | None = None
        self.connected = False
        self.hello: dict | None = None
        self.last_rx_wall: float | None = None
        self.txlog: bool | None = None
        self.clock = _Clock()
        self.simulated = False                  # this controller's records are SIMULATED (demo bench)
        self.reset_session()

    @property
    def label(self):
        return f"{self.cfg['label']} ({self.cfg['board']})"

    @property
    def short(self):
        return self.cfg["label"]

    def reset_session(self):
        self.stats_latest: dict | None = None
        self.stats_base: dict | None = None
        self.stats_carry = Counter()
        self.stats_history = deque(maxlen=600)    # (wall, session stats dict)
        self.txns = deque(maxlen=20000)
        self.pending: dict = defaultdict(lambda: deque(maxlen=600))
        self.per_slave: dict = defaultdict(Counter)
        self.results = Counter()
        self.arb_lost = 0
        self.arb_gave_up = 0
        self.recoveries = 0
        self.controller_resets = 0
        self.matched = 0
        # turn-taking firmware (I2C.c 2.2-turns): controller-reported turns, never inferred from the bus
        self.turns = 0
        self.turn_last: dict | None = None
        self.seq = (False, None)                 # live review: SEQ mode reported by the controller
        self.last_probe: dict | None = None
        self.last_arb_test: dict | None = None
        self.turn_problems = 0
        self.peer_absent = 0

    # ---------------------------------------------------------------- counters
    def add_stats(self, d, wall):
        cur = {k: int(d.get(k) or 0) for k in STAT_KEYS}
        if self.stats_base is None:
            self.stats_base = cur
        elif any(cur[k] < self.stats_latest.get(k, 0) for k in STAT_KEYS):
            # controller rebooted: keep what this session already saw, restart the delta
            for k in STAT_KEYS:
                self.stats_carry[k] += self.stats_latest[k] - self.stats_base[k]
            self.stats_base = {k: 0 for k in STAT_KEYS}
            self.clock.reset()
        self.stats_latest = cur
        self.stats_history.append((wall, self.session_stats()))

    def session_stats(self):
        if self.stats_latest is None:
            return {k: 0 for k in STAT_KEYS}
        return {k: self.stats_carry[k] + self.stats_latest[k] - self.stats_base[k] for k in STAT_KEYS}

    def window_stats(self, now_wall, window_s=HEALTH_WINDOW_S):
        if not self.stats_history:
            return {k: 0 for k in STAT_KEYS}
        newest = self.stats_history[-1][1]
        old = next((s for w, s in self.stats_history if w >= now_wall - window_s), self.stats_history[0][1])
        if old is newest and len(self.stats_history) == 1:
            return dict(newest)        # first snapshot of the session: everything is "recent"
        return {k: newest[k] - old[k] for k in STAT_KEYS}

    def health(self, now_wall):
        """(state, detail).  Bus-access health of this controller, not of the targets."""
        if not self.connected and self.last_rx_wall is None:
            return "NOT CONNECTED", "no COM port opened for this master"
        if self.last_rx_wall is None or now_wall - self.last_rx_wall > FRESH_S:
            return "UNKNOWN", "no fresh controller telemetry (port closed, firmware silent or old firmware)"
        if self.stats_latest is None:
            return "UNKNOWN", "no master_stats yet: flash the multi-master firmware (I2C.c 2.0)"
        if self.txlog is False:
            return "UNKNOWN", ("transfer log is OFF (TXLOG 1 not confirmed): this master's packets cannot be "
                               "attributed; the GUI keeps re-sending TXLOG 1")
        w = self.window_stats(now_wall)
        bad = [f"{k.replace('_', ' ')} {w[k]}" for k in DEGRADING if w[k]]
        if bad:
            return "DEGRADED", "last 60 s: " + ", ".join(bad)
        if w["arb_lost"]:
            return "CONTENDED", (f"last 60 s: {w['arb_lost']} arbitration loss(es), all retried successfully - "
                                 "normal multi-master behaviour, not a target fault")
        return "HEALTHY", "last 60 s: no lost transfers, timeouts or recoveries"


class MultiMasterTracker:
    def __init__(self, masters=None, tolerance_ns=None):
        cfg = config.MASTERS if masters is None else masters
        self.cfg = {mid: dict(c) for mid, c in cfg.items()}
        self.tol = float(tolerance_ns or config.ATTRIBUTION_TOLERANCE_NS)
        self.state = {mid: MasterState(mid, self.cfg[mid]) for mid in sorted(self.cfg)}
        self.owner_of = {}
        for mid, c in self.cfg.items():
            for a in c.get("owns", ()):
                self.owner_of[a] = mid
        self.revision = 0
        self.on_attribution = None            # callable(txn) after every (re)attribution (CaptureEngine -> perf)
        self.reset_session()

    # ---------------------------------------------------------------- lifecycle
    def reset_session(self):
        self.by_sig: dict = defaultdict(lambda: ([], []))   # sig -> (sorted start times, packets)
        self.recent = deque(maxlen=6000)                    # (t_start, t_end, packet) in capture order
        self.packet_count = 0
        self.first_packet_t = None
        self.last_packet_t = None
        self.analyzer_arrival = deque(maxlen=600)           # wall_ns - analyzer_ns
        self.arb_events = deque(maxlen=5000)
        self.arb_total = 0
        self.method_counts = Counter()
        self.pair_counts: dict = defaultdict(Counter)       # master_id|None -> Counter(addr) of bus packets
        self.mid_byte_cuts = 0
        self.simulated_controllers = False                  # SIMULATED controller telemetry seen this session
        for s in self.state.values():
            s.simulated = False
            s.reset_session()
            # keep the clock lock only while the same capture clock is in use; a fresh
            # session re-acquires (cheap) instead of trusting an old mapping
            s.clock.reset()
        self.revision += 1

    def set_connection(self, mid, port, connected):
        s = self.state.get(mid)
        if s is None:
            return
        s.port = port if connected else s.port
        s.connected = connected
        if not connected:
            s.txlog = None
        self.revision += 1

    def configured_ids(self):
        return list(self.state)

    # ---------------------------------------------------------------- controller records
    def ingest(self, slot, d, wall=None):
        """One validated controller record (see communication.master_protocol).

        Returns notices [(severity, kind, message, addr, t_hw_ns)] for the event log."""
        wall = time.monotonic() if wall is None else wall
        mid = d.get("master_id") if d.get("master_id") in self.state else slot
        s = self.state.get(mid)
        if s is None:
            return []
        s.last_rx_wall = wall
        if d.get("simulated") is True:
            s.simulated = True
            self.simulated_controllers = True
        t = d.get("type")
        out = []
        if t == "hello":
            s.hello = d
            s.txlog = bool(d.get("txlog"))
        elif t == "turn_end":
            s.turns += 1
            s.turn_last = d
            bad = {k: d.get(k) for k in ("rtc", "bmp", "oled") if d.get(k) != "ok"}
            if bad:
                s.turn_problems += 1
                out.append(("warning", "Turn incomplete",
                            f"{s.label} turn {d.get('turn')}: " + ", ".join(f"{k} {v}" for k, v in bad.items()),
                            None, s.clock.to_bus(d.get("t_us"))))
        elif t == "seq":
            s.seq = (bool(d.get("enabled")), d.get("gap_ms"))
            out.append(("info", "SEQ mode", f"{s.label}: spaced sequence " + (
                f"ON, {d.get('gap_ms')} ms idle bus between transfers" if d.get("enabled") else "OFF (normal turns)"),
                None, None))
        elif t == "probe" and d.get("state") == "done":
            s.last_probe = d
            res = d.get("result")
            out.append(("warning" if res == "addr_nack" else "info", "Probe",
                        f"{s.label} sent one write to 0x{d.get('addr', 0):02X}: " + (
                            "no slave answered (Address NACK) - invalid / absent address" if res == "addr_nack" else
                            f"result {res}"), d.get("addr"), s.clock.to_bus(d.get("t_us"))))
        elif t == "arb_test":
            s.last_arb_test = d
            out.append(("info", "Arbitration test",
                        f"{s.label} ARB test on 0x{d.get('target', 0):02X}: {d.get('transfers')} back-to-back transfer(s), "
                        f"{d.get('arb_lost')} arbitration loss(es), gave up {d.get('gave_up')} - " + (
                            "lost arbitration to the other master" if d.get("result") == "lost_arbitration" else
                            "never lost (it won every collision, or no collision happened)"),
                        d.get("target"), s.clock.to_bus(d.get("t_us"))))
        elif t == "peer_absent":
            s.peer_absent += 1
            out.append(("warning", "Peer master silent",
                        f"{s.label} saw no turn from the other master for {d.get('silent_ms')} ms and took the turn "
                        "anyway (turn-taking firmware fallback)", None, s.clock.to_bus(d.get("t_us"))))
        elif t == "txlog":
            s.txlog = bool(d.get("enabled"))
        elif t == "master_stats":
            s.add_stats(d, wall)
            if "txlog" in d:                        # an absent field is "unknown", never "off"
                s.txlog = bool(d["txlog"])
        elif t == "txn":
            out += self._on_master_txn(s, d, wall)
        elif t == "arb_lost":
            out += self._on_arb(s, d, wall)
        elif t == "bus_recovery":
            s.recoveries += 1
            out.append(("warning", "Bus recovery",
                        f"{s.label} clocked 9 SCL pulses + STOP: {d.get('reason', '')}", None,
                        s.clock.to_bus(d.get("t_us"))))
        elif t == "controller_reset":
            s.controller_resets += 1
            out.append(("warning", "Controller reset", f"{s.label}: {d.get('reason', '')}", None,
                        s.clock.to_bus(d.get("t_us"))))
        elif t == "master_clock":
            if isinstance(d.get("offset_ns"), (int, float)):
                s.clock.offset_ns = float(d["offset_ns"])
                s.clock.slope, s.clock.ref_us, s.clock.lock_ref_us = 0.0, None, None
                s.clock.locked = True
                s.clock.source = ("simulated controller clock (known at boot)" if d.get("simulated") is True
                                  else "saved with capture")
                self._match_pending(s)
        self.revision += 1
        return out

    def _on_master_txn(self, s, d, wall):
        mt = MasterTxn(s.master_id, d["seq"], d["t_us"], d.get("dur_us") or 0, d["addr"], d["wlen"], d["rlen"],
                       d.get("b0", -1), d["result"], d["attempts"], d["arb_lost"])
        c = s.clock
        if c.last_t_us is not None and mt.t_us + 1_000_000 < c.last_t_us:      # controller rebooted
            c.reset()
            s.pending.clear()
        c.last_t_us = mt.t_us
        c.arrival.append(wall * 1e9 - mt.t_us * 1000.0)
        s.txns.append(mt)
        s.results[mt.result] += 1
        s.per_slave[mt.addr][mt.result] += 1
        if mt.result not in VISIBLE_RESULTS:
            return []
        if c.locked:
            self._match_txn(s, mt)
        if mt.packet is None:
            s.pending[mt.sig].append(mt)
        if not c.locked:
            return self._try_lock(s, wall)
        return []

    def _on_arb(self, s, d, wall):
        ev = ArbEvent(s.master_id, d["t_us"], d["addr"], d["attempt"], int(d.get("byte") or 0),
                      int(d.get("backoff_us") or 0), bool(d.get("retry", True)), wall)
        ev.t_bus_ns = s.clock.to_bus(ev.t_us)
        self.arb_events.append(ev)
        self.arb_total += 1
        s.arb_lost += 1
        s.per_slave[ev.addr]["arb_lost"] += 1
        if not ev.retry:
            s.arb_gave_up += 1
        self._resolve_winner(ev)
        winner = self.describe_winner(ev)
        how = (f"retried after {ev.backoff_us} µs back-off" if ev.retry else
               f"GAVE UP after {ev.attempt} attempts (transfer skipped, target NOT marked offline)")
        sev = "info" if ev.retry else "warning"
        return [(sev, "Arbitration lost",
                 f"{s.label} lost arbitration addressing 0x{ev.addr:02X} (attempt {ev.attempt}, byte {ev.byte}) "
                 f"to {winner}; {how}. Not a NACK: the bus only shows the winner's bits.", ev.addr, ev.t_bus_ns)]

    # ---------------------------------------------------------------- analyzer packets
    def on_packet(self, txn, source="live", wall=None):
        """Called once per stored analyzer transaction (in capture order)."""
        wall = time.monotonic() if wall is None else wall
        self.packet_count += 1
        if any("inside a byte" in n for n in txn.notes):
            self.mid_byte_cuts += 1
        if self.first_packet_t is None:
            self.first_packet_t = txn.t_start
        self.last_packet_t = txn.t_start
        if source == "demo" and not self.simulated_controllers:
            self._attribute(txn, 1, "demo")          # legacy single-master demo: no controller telemetry to match
            return []
        if source == "live":
            self.analyzer_arrival.append(wall * 1e9 - txn.t_start)
        # configured ownership (OLED -> Master 1 on the real bench) only without SIMULATED controller telemetry: the
        # simulated controllers report EVERY transfer they make, Master 2 (Pico 3) <-> OLED 0x3C included
        owner = None if self.simulated_controllers else self.owner_of.get(txn.address)
        if owner is not None and owner in self.state:
            self._attribute(txn, owner, "ownership")
        elif not getattr(txn, "master_method", ""):
            self.pair_counts[None][txn.address] += 1
            txn._mm_counted_none = True
        self.recent.append((txn.t_start, txn.t_end if txn.t_end is not None else txn.t_start, txn))
        sig = packet_signature(txn)
        if sig is None:
            return []
        times, pkts = self.by_sig[sig]
        times.append(txn.t_start)
        pkts.append(txn)
        if len(times) > 4000:
            del times[:1000]
            del pkts[:1000]
        for s in self.state.values():
            if s.clock.locked and getattr(txn, "master_method", "") != "telemetry":
                self._match_packet(s, txn, sig)
        if self.packet_count % 64 == 0:
            for s in self.state.values():
                self._expire_pending(s)
        # arbitration events waiting for the winning packet
        for ev in list(self.arb_events)[-20:]:
            if ev.winner is None:
                self._resolve_winner(ev)
        return []

    def _attribute(self, txn, mid, method, force=False):
        """force: the caller KNOWS the master (presentation scheduler) and may override even a telemetry match."""
        prev_mid = getattr(txn, "master_id", None)
        prev_method = getattr(txn, "master_method", "")
        if prev_method == "telemetry" and method != "telemetry" and not force:
            return
        if prev_method:
            self.pair_counts[prev_mid][txn.address] -= 1
            self.method_counts[prev_method] -= 1
        elif getattr(txn, "_mm_counted_none", False):
            self.pair_counts[None][txn.address] -= 1      # counted as unattributed on arrival
            txn._mm_counted_none = False
        txn.master_id = mid
        txn.master_method = method
        self.pair_counts[mid][txn.address] += 1
        self.method_counts[method] += 1
        self.revision += 1
        if self.on_attribution is not None:          # statistics keyed by master follow this decision exactly
            self.on_attribution(txn)

    def _match_txn(self, s, mt):
        """Locked clock: find the analyzer packet for a controller transfer."""
        exp = s.clock.to_bus(mt.t_us)
        times, pkts = self.by_sig.get(mt.sig, ([], []))
        if not times:
            return False
        i = bisect.bisect_left(times, exp - self.tol)
        best, best_dt = None, None
        while i < len(times) and times[i] <= exp + self.tol:
            p = pkts[i]
            if getattr(p, "master_method", "") != "telemetry":
                dt = abs(times[i] - exp)
                if best is None or dt < best_dt:
                    best, best_dt = p, dt
            i += 1
        if best is None:
            return False
        self._link(s, mt, best, exp)
        return True

    def _match_packet(self, s, txn, sig):
        dq = s.pending.get(sig)
        if not dq:
            return
        best, best_dt = None, None
        for mt in dq:
            dt = abs(s.clock.to_bus(mt.t_us) - txn.t_start)
            if dt <= self.tol and (best is None or dt < best_dt):
                best, best_dt = mt, dt
        if best is not None:
            dq.remove(best)
            self._link(s, best, txn, s.clock.to_bus(best.t_us))

    def _link(self, s, mt, packet, expected):
        mt.packet = packet
        self._attribute(packet, s.master_id, "telemetry")
        packet.master_attempts = mt.attempts
        packet.master_arb_lost = mt.arb_lost
        s.matched += 1
        s.per_slave[mt.addr]["bus_packets"] += 1
        c = s.clock
        c.outcomes.append(True)
        # track crystal drift: move the reference to this transfer (extrapolated with the slope), correct a quarter of
        # the residual, and refit the slope from the matched pairs
        if c.ref_us is None or mt.t_us >= c.ref_us:
            # after a pause (> 0.5 s since the last match: between turns, a guided step) the residual is drift, not jitter:
            # correct all of it.  A quarter only let it build up to ~4x the drift per gap (> the 400 µs match window).
            gain = 1.0 if c.ref_us is None or mt.t_us - c.ref_us > 500_000 else 0.25
            c.offset_ns = (expected - mt.t_us * 1000.0) + gain * (packet.t_start - expected)
            c.ref_us = mt.t_us
        c.pairs.append((mt.t_us, packet.t_start - mt.t_us * 1000.0))
        c.fit_drift()

    def claim_reported(self, packet, mid):
        """Guided flow: the GUI commanded master `mid` to make exactly this transfer and nothing else was on the bus.  Link
        the packet to that master's own, still unmatched report with the same signature (address, bytes written, bytes
        read, first byte) - controller telemetry, independent of the clock alignment.  Returns True when linked."""
        s = self.state.get(mid)
        sig = packet_signature(packet)
        if s is None or sig is None or getattr(packet, "master_method", "") == "telemetry":
            return False
        dq = s.pending.get(sig)
        if not dq:
            return False
        mt = dq.pop()                                   # the newest unmatched report of that signature
        if s.clock.locked and s.clock.to_bus(mt.t_us) is not None:
            self._link(s, mt, packet, s.clock.to_bus(mt.t_us))
        else:
            mt.packet = packet
            self._attribute(packet, mid, "telemetry")
            packet.master_attempts, packet.master_arb_lost = mt.attempts, mt.arb_lost
            s.matched += 1
            s.per_slave[mt.addr]["bus_packets"] += 1
        return True

    def _match_pending(self, s):
        for sig, dq in list(s.pending.items()):
            for mt in list(dq):
                if self._match_txn(s, mt):
                    dq.remove(mt)

    def _expire_pending(self, s):
        """Unmatched controller transfers well behind the analyzer's newest packet are misses."""
        c = s.clock
        if not c.locked or self.last_packet_t is None:
            return
        horizon = self.last_packet_t - 2e9
        for sig, dq in s.pending.items():
            while dq and c.to_bus(dq[0].t_us) < horizon:
                old = dq.popleft()
                # a transfer from before the lock that no longer matches (drift not known yet) is unattributed, but it
                # is not evidence against the lock
                if (self.first_packet_t is not None and c.to_bus(old.t_us) >= self.first_packet_t
                        and (c.lock_ref_us is None or old.t_us >= c.lock_ref_us)):
                    c.outcomes.append(False)
        if len(c.outcomes) >= 60 and sum(c.outcomes) < 0.3 * len(c.outcomes):
            c.reset()                    # mapping no longer explains the traffic: re-acquire

    # ---------------------------------------------------------------- clock lock
    def _has_packet(self, sig, t, mid=None):
        """A packet of this signature near t that master `mid` could have driven (one its telemetry has not given to
        ANOTHER master)."""
        times, pkts = self.by_sig.get(sig, ([], []))
        i = bisect.bisect_left(times, t - self.tol)
        while i < len(times) and times[i] <= t + self.tol:
            p = pkts[i]
            if mid is None or getattr(p, "master_method", "") != "telemetry" or p.master_id == mid:
                return True
            i += 1
        return False

    def prior_offset(self, s):
        """Coarse offset from USB arrival times (min latency of each stream), or None."""
        if len(s.clock.arrival) < 5 or len(self.analyzer_arrival) < 5:
            return None
        return min(s.clock.arrival) - min(self.analyzer_arrival)

    def _try_lock(self, s, wall):
        c = s.clock
        # Throttled (each attempt scans recent history): at most every 0.25 s of PC time OR every 25 new transfers.
        # The transfer count keeps a fast replay (a whole recording decoded in a second) from missing the lock.
        if self.first_packet_t is None or (wall - c.last_try_wall < 0.25 and len(s.txns) - c.last_try_n < 25):
            return []
        c.last_try_wall = wall
        c.last_try_n = len(s.txns)
        newest = c.last_t_us or 0
        window = [m for m in s.txns if m.result in VISIBLE_RESULTS and m.t_us >= newest - LOCK_WINDOW_US][-300:]
        prior = self.prior_offset(s)
        # with the USB-arrival prior (±80 ms) one signature has a single candidate offset: 3 transfers suffice (2.5-turns
        # makes 3 per master per ~9 s turn cycle); without it, 6 as before
        need = 3 if prior is not None else 6
        if len(window) < need:
            return []
        freq = Counter(m.sig for m in window)
        anchors = sorted(window, key=lambda m: (freq[m.sig], -m.t_us))[:8]
        if prior is None and self.last_packet_t - self.first_packet_t < NO_PRIOR_MIN_SPAN_NS:
            return []
        cands = []
        for a in anchors:
            for t in self.by_sig.get(a.sig, ([], []))[0][-500:]:
                o = t - a.t_us * 1000.0
                if prior is None or abs(o - prior) <= PRIOR_WINDOW_NS:
                    cands.append(o)
        if not cands:
            return []
        cands.sort()
        uniq = []
        for o in cands:
            if not uniq or o - uniq[-1] > self.tol / 2:
                uniq.append(o)
        lo, hi = self.first_packet_t - self.tol, self.last_packet_t + self.tol
        scored = []
        for o in uniq[:500]:
            if prior is None and abs(newest * 1000.0 + o - self.last_packet_t) > NO_PRIOR_RECENT_NS:
                continue
            covered = [m for m in window if lo <= m.t_us * 1000.0 + o <= hi]
            if len(covered) < need:
                continue
            hit = [m for m in covered if self._has_packet(m.sig, m.t_us * 1000.0 + o, s.master_id)]
            # every signature weighs the same however often it repeats: a shift by one transfer inside a long run of
            # identical OLED writes (turn-taking bench: 70 per turn, 2.17 ms apart) still matches most of them, but it
            # misses the RTC / BMP280 transfers that tell the offsets apart
            weight = sum(1.0 / freq[m.sig] for m in hit)
            scored.append((len(hit) / len(covered), len(hit), o, weight))
        if not scored:
            return []
        scored.sort(reverse=True)
        ratio, hits, best, best_w = scored[0]
        rival = next((r for r in scored[1:] if abs(r[2] - best) > 4 * self.tol), None)
        if ratio < 0.7 or (rival is not None and rival[1] >= 0.85 * hits and rival[3] >= 0.85 * best_w):
            return []
        # refine with the matched pairs: offset at the newest transfer, plus the crystal drift when they span enough time
        res, pairs = [], []
        for m in window:
            exp = m.t_us * 1000.0 + best
            times = self.by_sig.get(m.sig, ([], []))[0]
            i = bisect.bisect_left(times, exp - self.tol)
            if i < len(times) and times[i] <= exp + self.tol:
                res.append(times[i] - exp)
                pairs.append((m.t_us, times[i] - m.t_us * 1000.0))
        c.offset_ns = best + (statistics.median(res) if res else 0.0)
        c.slope, c.ref_us, c.lock_ref_us = 0.0, newest, newest
        c.pairs.clear()
        c.pairs.extend(pairs)
        if (len(pairs) >= DRIFT_MIN_PAIRS and pairs[-1][0] - pairs[0][0] >= DRIFT_MIN_SPAN_US
                and (fit := _fit_line(pairs)) is not None and abs(fit[1]) <= DRIFT_MAX_NS_PER_US):
            c.slope = fit[1]
            c.offset_ns = fit[0] + fit[1] * newest
        c.locked = True
        c.locked_at_wall = wall
        c.source = "USB-arrival prior + transfer signatures" if prior is not None else "transfer signatures"
        self._match_pending(s)
        for ev in self.arb_events:
            if ev.master_id == s.master_id and ev.t_bus_ns is None:
                ev.t_bus_ns = c.to_bus(ev.t_us)
                self._resolve_winner(ev)
        return [("info", "Controller clock aligned",
                 f"{s.label}: telemetry aligned to analyzer capture time ({hits}/{len(window)} recent transfers "
                 f"matched bus packets, offset {c.offset_ns / 1e9:+.6f} s). Packets are now attributed to this master.",
                 None, None)]

    # ---------------------------------------------------------------- arbitration
    def _resolve_winner(self, ev):
        if ev.t_bus_ns is None or ev.winner is not None:
            return
        for t0, t1, p in reversed(self.recent):
            if t1 < ev.t_bus_ns - 3e6:
                break
            if t0 - self.tol <= ev.t_bus_ns <= t1 + self.tol:
                ev.winner = p
                return

    def describe_winner(self, ev):
        p = ev.winner
        if p is None:
            return ("the other master (bus packet not yet matched)" if ev.t_bus_ns is not None
                    else "the other master (controller clock not yet aligned to capture time)")
        who = self.master_name(getattr(p, "master_id", None))
        addr = f"0x{p.address:02X}" if p.address is not None else "?"
        return f"{who} (packet #{p.id} {addr})"

    def master_name(self, mid):
        s = self.state.get(mid)
        return s.label if s else "unattributed controller"

    def arb_rows(self, limit=200):
        rows = []
        for ev in list(self.arb_events)[-limit:][::-1]:
            if ev.winner is None:
                self._resolve_winner(ev)
            rows.append(dict(master=self.master_name(ev.master_id), master_id=ev.master_id, addr=ev.addr,
                             attempt=ev.attempt, byte=ev.byte, backoff_us=ev.backoff_us, retry=ev.retry,
                             t_bus_ns=ev.t_bus_ns, winner=self.describe_winner(ev),
                             winner_id=getattr(ev.winner, "id", None)))
        return rows

    def arb_window(self, now_wall=None, window_s=HEALTH_WINDOW_S):
        now_wall = time.monotonic() if now_wall is None else now_wall
        evs = [e for e in self.arb_events if e.wall >= now_wall - window_s]
        return dict(lost=len(evs), gave_up=sum(1 for e in evs if not e.retry),
                    by_master=Counter(e.master_id for e in evs))

    def new_arb_events_since(self, n):
        """(events appended after running count n, new count) for animations."""
        k = min(self.arb_total - n, len(self.arb_events))
        return (list(self.arb_events)[-k:] if k > 0 else []), self.arb_total

    def can_attribute(self):
        """True while some controller could still claim a just-decoded packet: its port is open and its clock is
        aligned to the analyzer.  (Display hint only - it never assigns anything.)"""
        return any((s.connected or s.simulated) and s.clock.locked for s in self.state.values())

    # ---------------------------------------------------------------- why not attributed
    def explain_unattributed(self, txn):
        """Specific reason a packet has no master, in the order a user can act on it."""
        connected = [s for s in self.state.values() if s.connected or s.simulated]
        if not connected:
            return ("no master COM port is connected. Connect Master 1 and Master 2 (or press Connect all) "
                    "before starting the capture: a passive tap cannot tell which master drove a transaction")
        silent = [s.short for s in connected if not s.txns]
        if silent:
            return (f"{', '.join(silent)} is connected but has sent no transfer log yet "
                    "(firmware older than I2C.c 2.0, or the controller is silent)")
        unlocked = [s for s in connected if not s.clock.locked]
        if unlocked:
            return (f"aligning {', '.join(s.short for s in unlocked)} to the analyzer clock "
                    f"({sum(len(s.txns) for s in unlocked)} transfers seen so far); this takes a few seconds")
        firsts = [s.clock.to_bus(s.txns[0].t_us) for s in connected]
        if txn.t_start < min(f for f in firsts if f is not None) - self.tol:
            return ("captured before the masters' telemetry started (the capture began before the master ports "
                    "were connected). Connect the masters first, then start the capture")
        missing = [s.short for s in self.state.values() if not (s.connected or s.simulated)]
        if missing:
            return f"{', '.join(missing)} is not connected, so its packets cannot be matched"
        return ("no master reported this transfer: a controller log line was dropped, or another device "
                "drove the bus")

    # ---------------------------------------------------------------- GUI queries
    def clock_text(self, s):
        c = s.clock
        if c.locked:
            ok = sum(c.outcomes)
            n = len(c.outcomes)
            rate = f", {100 * ok / n:.0f}% of recent transfers matched" if n else ""
            return f"aligned ({c.source}{rate})"
        if not s.txns:
            return "no transfer log (TXLOG off or old firmware)" if s.last_rx_wall else "no telemetry"
        if self.first_packet_t is None:
            return "waiting for analyzer packets"
        return "acquiring alignment…"

    def rows(self, now_wall=None):
        now_wall = time.monotonic() if now_wall is None else now_wall
        out = []
        for mid, s in self.state.items():
            state, detail = s.health(now_wall)
            st = s.session_stats()
            schedule = (s.hello or {}).get("schedule") or ("turns" if s.turns else "")
            if schedule == "turns":
                tl = s.turn_last or {}
                detail += (f" · turn-taking ({(s.hello or {}).get('turn_gap_ms', 4000) / 1000:g} s idle between turns): "
                           f"{s.turns} turn(s) this session" + (f", last {tl.get('dur_us', 0) / 1000:.0f} ms with "
                                                                  f"{tl.get('transfers')} transfers" if tl else "")
                           + (f", {s.turn_problems} incomplete" if s.turn_problems else "")
                           + (f", peer silent {s.peer_absent}x" if s.peer_absent else ""))
            out.append(dict(
                master_id=mid, label=s.label, pico=s.cfg.get("pico", ""), board=s.cfg["board"],
                port=("SIMULATED" if s.simulated else s.port or s.cfg.get("port_hint", "")),
                connected=s.connected or s.simulated, simulated=s.simulated, state=state,
                detail=("SIMULATED controller (demo, not verified hardware): " + detail) if s.simulated else detail,
                stats=st, have_stats=s.stats_latest is not None, txlog=s.txlog, clock=self.clock_text(s),
                bus_packets=sum(v for v in self.pair_counts[mid].values() if v > 0),
                arb_lost_events=s.arb_lost, arb_gave_up=s.arb_gave_up,
                schedule=schedule, turns=s.turns, turn_last=s.turn_last, peer_absent=s.peer_absent,
                fw=(s.hello or {}).get("fw_version"), hello=s.hello,
                age_s=None if s.last_rx_wall is None else max(0.0, now_wall - s.last_rx_wall)))
        return out

    def matrix(self, addrs):
        """Per (master, target): controller-reported results + bus packets attributed to it."""
        rows = []
        for mid, s in self.state.items():
            for a in addrs:
                k = s.per_slave.get(a, Counter())
                gave_up = sum(1 for e in self.arb_events if e.master_id == mid and e.addr == a and not e.retry)
                rows.append(dict(master_id=mid, master=s.short, addr=a, ok=k["ok"], addr_nack=k["addr_nack"],
                                 data_nack=k["data_nack"], arb_lost=k["arb_lost"], gave_up=gave_up,
                                 timeouts=k["timeout"], bus_busy=k["bus_busy"],
                                 bus_packets=max(0, self.pair_counts[mid].get(a, 0))))
        for a in addrs:
            n = max(0, self.pair_counts[None].get(a, 0))
            if n:
                rows.append(dict(master_id=None, master="unattributed", addr=a, ok=0, addr_nack=0, data_nack=0,
                                 arb_lost=0, gave_up=0, timeouts=0, bus_busy=0, bus_packets=n))
        return rows

    def summary(self, now_wall=None):
        now_wall = time.monotonic() if now_wall is None else now_wall
        arb = self.arb_window(now_wall)
        parts = []
        for r in self.rows(now_wall):
            if r["state"] == "NOT CONNECTED":
                parts.append(f"{r['label']}: not connected")
                continue
            st = r["stats"]
            parts.append(("SIMULATED " if r["simulated"] else "") + f"{r['label']} {r['state']}" + (f" · ok {st['txn_ok']:,} · arb lost {st['arb_lost']}"
                                                          f" (retried {st['retries']}, gave up {st['gave_up']})"
                                                          if r["have_stats"] else ""))
        attributed = sum(self.method_counts[m] for m in ("telemetry", "ownership", "demo", "presentation"))
        return dict(text=" | ".join(parts), arb_lost_60s=arb["lost"], arb_gave_up_60s=arb["gave_up"],
                    simulated=self.simulated_controllers,
                    attributed=attributed, packets=self.packet_count, mid_byte_cuts=self.mid_byte_cuts,
                    methods=dict(self.method_counts))


UNKNOWN_MASTER = "Unknown"


def attribution_text(txn, tracker=None):
    """Short label for tables: 'Master 2', 'Master 1 (owner)', 'Master 1 (demo)' or 'Unknown' (no master report validated
    this packet: a passive tap alone cannot tell which master drove it, so it is never guessed)."""
    mid = getattr(txn, "master_id", None)
    method = getattr(txn, "master_method", "")
    if mid is None:
        return UNKNOWN_MASTER
    name = config.MASTERS.get(mid, {}).get("label", f"Master {mid}")
    if getattr(txn, "demo", False) and method != "demo":
        return name + {"telemetry": " (SIMULATED)", "ownership": " (owner, SIMULATED)",
                       "presentation": " (scripted, SIMULATED)"}.get(method, f" ({method}, SIMULATED)")
    return name + {"telemetry": "", "ownership": " (owner)", "demo": " (demo)"}.get(method, f" ({method})")


def attribution_detail(txn, tracker=None):
    mid = getattr(txn, "master_id", None)
    method = getattr(txn, "master_method", "")
    if mid is None:
        if tracker is not None:
            return "Master UNKNOWN (not validated): " + tracker.explain_unattributed(txn)
        return ("Master UNKNOWN (not validated): no controller telemetry matched this packet (connect both master COM ports; a "
                "passive tap cannot tell which master drove a transaction)")
    cfg = config.MASTERS.get(mid, {})
    who = f"{cfg.get('label', f'Master {mid}')} · {cfg.get('pico', '')} {cfg.get('board', '')}".strip()
    how = {"telemetry": "matched to that controller's own transfer report (signature + aligned time)",
           "ownership": "configured ownership: only this master addresses this target",
           "demo": "simulated demo stream (single simulated master)",
           "presentation": "scripted review sequence: the presentation bus scheduler released this transfer for "
                           "this master"}.get(method, method)
    if getattr(txn, "demo", False) and method != "demo":
        how = f"SIMULATED — NOT VERIFIED HARDWARE: {how} (simulated controller telemetry, demo bench)"
    extra = ""
    if getattr(txn, "master_arb_lost", 0):
        extra = (f" · won the bus after {txn.master_arb_lost} lost arbitration attempt(s) "
                 f"({txn.master_attempts} attempts)")
    return f"{who} — {how}{extra}"


def active_communication(engine, txn=None):
    """Who is talking to whom, for the 'Active communication' banner.

    txn: the packet on display; None = the latest captured packet.  Returns dict(html, plain, txn, color).
    An unattributed packet is shown as 'Unattributed ↔ target' (a passive tap cannot name the master)."""
    if txn is None:
        txn = engine.packets[-1] if engine.packets else None
    if txn is None:
        msg = "no communication captured yet"
        return dict(html=msg, plain=msg, txn=None, color=config.COLORS["text2"])
    mid = getattr(txn, "master_id", None)
    cfg = config.MASTERS.get(mid)
    addr = txn.address
    target = (f"{engine.registry.name(addr)[0]} (0x{addr:02X})" if addr is not None else "target unknown")
    if cfg:
        who, color, arrow = f"{cfg['label']} ({cfg['board']})", cfg["color"], "→"
    else:
        who, color, arrow = "Unattributed", config.COLORS["amber"], "↔"
    extra = ""
    if any(f.addr_ack is False for f in txn.frames):
        extra += " · ADDRESS NACK"
    if txn.status != "ok":
        extra += f" · {txn.status.upper()}"
    if txn.demo:
        src, scol = "SIMULATED", config.COLORS["demo"]
    elif txn.source == "replay":
        src, scol = "REPLAY", "#8C7BFF"
    elif engine.measurement_trust()[0]:
        src, scol = "LIVE", config.COLORS["green"]
    else:
        src, scol = "RECORDED", config.COLORS["text2"]
    plain = f"{src} · {who} {arrow} {target} · {txn.operation}{extra} · #{txn.id}"
    html = (f"<b style='color:{scol}'>{src}</b> · <span style='color:{color}'><b>{who}</b></span> {arrow} "
            f"<b>{target}</b> · {txn.operation}{extra} · #{txn.id}")
    return dict(html=html, plain=plain, txn=txn, color=color)
