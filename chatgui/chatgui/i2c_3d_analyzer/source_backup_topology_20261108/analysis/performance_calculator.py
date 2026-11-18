"""Timing and performance statistics computed from hardware timestamps only.

Definitions
  SCL period      time between consecutive SCL rising edges inside one frame.
                  Periods > 3x the frame's median (clock stretching, inter-byte
                  pauses) are excluded from the frequency estimate.
  f_SCL           1 / mean(valid periods)
  t_HIGH / t_LOW  rise->fall and fall->rise intervals inside frames
  busy time       sum of (STOP time - START time) of decoded transactions
  observed time   hardware time covered by records with no sequence gap.
                  Intervals across a capture gap are NOT counted as idle.
  utilization     busy / observed * 100
  rate            completed transactions / observed seconds
Three scopes are reported: instantaneous (last transaction), rolling (last
ROLLING_WINDOW_S of hardware time) and cumulative (whole capture).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from config import HISTORY_POINTS, ROLLING_WINDOW_S
from analysis.communication_health import acknowledgment_counts


@dataclass
class TimingSummary:
    periods: np.ndarray
    highs: np.ndarray
    lows: np.ndarray


def frame_timing(txn) -> TimingSummary:
    rises = np.asarray(txn.rises or [], dtype=float)
    falls = np.asarray(txn.falls or [], dtype=float)
    if len(rises) < 2:
        e = np.empty(0)
        return TimingSummary(e, e, e)
    # split at repeated-START boundaries so Sr gaps are not counted as clock periods
    bounds = [t for t, k in txn.events if k == "Sr"]
    periods = []
    seg_start = 0
    for b in bounds + [math.inf]:
        seg_end = int(np.searchsorted(rises, b))
        seg = rises[seg_start:seg_end]
        if len(seg) >= 2:
            periods.append(np.diff(seg))
        seg_start = seg_end
    p = np.concatenate(periods) if periods else np.empty(0)
    if len(p):
        med = np.median(p)
        p = p[p <= 3 * med]
    # Pair only within a frame; never measure a repeated-START setup as LOW.
    high_values, low_values = [], []
    start = -math.inf
    for stop in bounds + [math.inf]:
        rr = rises[(rises >= start) & (rises < stop)]
        ff = falls[(falls >= start) & (falls < stop)]
        for rise in rr:
            idx = np.searchsorted(ff, rise, side='right')
            if idx < len(ff): high_values.append(ff[idx]-rise)
        for fall in ff:
            idx = np.searchsorted(rr, fall, side='right')
            if idx < len(rr): low_values.append(rr[idx]-fall)
        start = stop
    highs, lows = np.asarray(high_values), np.asarray(low_values)
    return TimingSummary(p, highs, lows)


@dataclass
class Bucket:
    second: int
    clock_sum: float = 0.0
    clock_n: int = 0
    txns: int = 0
    busy: float = 0.0
    observed: float = 0.0
    acks: int = 0
    nacks: int = 0
    dur_sum: float = 0.0
    bytes: int = 0


@dataclass
class PerfSnapshot:
    has_data: bool = False
    # instantaneous
    inst_freq_hz: float | None = None
    # rolling
    roll_freq_hz: float | None = None
    roll_tps: float | None = None
    roll_util: float | None = None
    roll_throughput: float | None = None
    # cumulative
    freq_avg_hz: float | None = None
    freq_min_hz: float | None = None
    freq_max_hz: float | None = None
    t_high_ns: float | None = None
    t_low_ns: float | None = None
    total: int = 0
    complete: int = 0
    incomplete: int = 0
    malformed: int = 0
    reads: int = 0
    writes: int = 0
    acks: int = 0
    nacks: int = 0
    addr_nacks: int = 0
    read_end_nacks: int = 0
    write_data_nacks: int = 0
    total_bytes: int = 0
    dur_avg_ns: float | None = None
    dur_min_ns: float | None = None
    dur_max_ns: float | None = None
    observed_ns: float = 0.0
    busy_ns: float = 0.0
    util: float | None = None
    tps: float | None = None
    idle_ns: float = 0.0
    gaps: int = 0


class PerformanceCalculator:
    def __init__(self):
        self.reset()

    def reset(self):
        self.snap = PerfSnapshot()
        self.roll_txn = deque()        # (t_end, duration, bytes)
        self.roll_clk = deque()        # (t, period_sum, n)
        self.last_t: float | None = None
        self.period_sum = 0.0
        self.period_n = 0
        self.high_sum = self.high_n = 0.0
        self.low_sum = self.low_n = 0.0
        self.dur_sum = 0.0
        self.bucket: Bucket | None = None
        self.history = deque(maxlen=HISTORY_POINTS)   # per-second points (dicts)

    # ---------------------------------------------------------- time base
    def advance(self, t_ns: float, gap: bool = False):
        """Hardware time moved to t_ns. If gap, the interval was not observed."""
        if self.last_t is None:
            self.last_t = t_ns
            self._bucket_for(t_ns)
            return
        if t_ns <= self.last_t:
            return
        if gap:
            self.snap.gaps += 1
        else:
            # split the observed interval across one-second buckets
            t0 = self.last_t
            while True:
                b = self._bucket_for(t0)
                end_of_b = (b.second + 1) * 1e9
                seg_end = min(t_ns, end_of_b)
                b.observed += seg_end - t0
                self.snap.observed_ns += seg_end - t0
                if seg_end >= t_ns:
                    break
                t0 = seg_end
        self.last_t = t_ns
        self._bucket_for(t_ns)

    def _bucket_for(self, t_ns) -> Bucket:
        sec = int(t_ns // 1e9)
        if self.bucket is None:
            self.bucket = Bucket(sec)
        elif sec != self.bucket.second:
            if sec > self.bucket.second:
                self._flush_bucket()
                # empty seconds (no data at all) are skipped, not invented
                self.bucket = Bucket(sec)
        return self.bucket

    def _flush_bucket(self):
        b = self.bucket
        if b is None or b.observed <= 0:
            return
        obs_s = b.observed / 1e9
        self.history.append({
            "t": b.second,
            "freq_khz": (b.clock_n / b.clock_sum * 1e6) if b.clock_n else float("nan"),
            "tps": b.txns / obs_s,
            "util": min(100.0, 100.0 * b.busy / b.observed),
            "acks": b.acks, "nacks": b.nacks,
            "dur_us": (b.dur_sum / b.txns / 1e3) if b.txns else float("nan"),
            "throughput": b.bytes / obs_s,
            "observed": obs_s,
        })

    # ---------------------------------------------------------- transactions
    def add_transaction(self, txn):
        s = self.snap
        s.has_data = True
        s.total += 1
        if txn.status == "ok":
            s.complete += 1
        elif txn.status == "malformed":
            s.malformed += 1
        else:
            s.incomplete += 1
        if txn.has_read:
            s.reads += 1
        if txn.has_write:
            s.writes += 1
        s.acks += txn.ack_count
        s.nacks += txn.nack_count
        counts = acknowledgment_counts(txn)
        s.addr_nacks += counts['address_nacks']
        s.read_end_nacks += counts['expected_read_nacks']
        s.write_data_nacks += counts['write_data_nacks']
        nb = txn.byte_count
        s.total_bytes += nb
        dur = txn.duration_ns
        if txn.status in ("ok", "malformed"):
            self.dur_sum += dur
            s.busy_ns += dur
            s.dur_min_ns = dur if s.dur_min_ns is None else min(s.dur_min_ns, dur)
            s.dur_max_ns = dur if s.dur_max_ns is None else max(s.dur_max_ns, dur)
            s.dur_avg_ns = self.dur_sum / max(1, s.complete + s.malformed)

        tm = frame_timing(txn)
        if len(tm.periods):
            txn.timing.update(period_ns=float(np.mean(tm.periods)), frequency_hz=1e9/float(np.mean(tm.periods)),
                              high_ns=float(np.mean(tm.highs)) if len(tm.highs) else None,
                              low_ns=float(np.mean(tm.lows)) if len(tm.lows) else None)
        b = self._bucket_for(txn.t_start)
        b.txns += int(txn.status == "ok")
        b.busy += dur
        b.acks += txn.ack_count
        b.nacks += txn.nack_count
        b.dur_sum += dur if txn.status == "ok" else 0
        b.bytes += nb
        if len(tm.periods):
            ps, pn = float(tm.periods.sum()), len(tm.periods)
            self.period_sum += ps
            self.period_n += pn
            b.clock_sum += ps
            b.clock_n += pn
            f_txn = pn / ps * 1e9
            s.inst_freq_hz = f_txn
            if pn >= 8:   # only frames with enough clocks set min/max
                s.freq_min_hz = f_txn if s.freq_min_hz is None else min(s.freq_min_hz, f_txn)
                s.freq_max_hz = f_txn if s.freq_max_hz is None else max(s.freq_max_hz, f_txn)
            s.freq_avg_hz = self.period_n / self.period_sum * 1e9
            self.roll_clk.append((txn.t_end or txn.t_start, ps, pn))
        if len(tm.highs):
            self.high_sum += float(tm.highs.sum())
            self.high_n += len(tm.highs)
            s.t_high_ns = self.high_sum / self.high_n
        if len(tm.lows):
            self.low_sum += float(tm.lows.sum())
            self.low_n += len(tm.lows)
            s.t_low_ns = self.low_sum / self.low_n
        if txn.status == "ok":
            self.roll_txn.append((txn.t_end or txn.t_start, dur, nb))
        if txn.t_end is not None:
            self.advance(txn.t_end)

    def add_firmware_frequency(self, hz, t_ns, high_ns=0, low_ns=0):
        """Use real firmware-reported measurements without inventing SCL edges."""
        if hz <= 0:
            return
        s = self.snap
        s.inst_freq_hz = float(hz)
        self.period_n += 1
        self.period_sum += 1e9 / hz
        s.freq_avg_hz = self.period_n / self.period_sum * 1e9
        s.freq_min_hz = hz if s.freq_min_hz is None else min(s.freq_min_hz, hz)
        s.freq_max_hz = hz if s.freq_max_hz is None else max(s.freq_max_hz, hz)
        self.roll_clk.append((t_ns, 1e9 / hz, 1))
        bucket = self._bucket_for(t_ns)
        bucket.clock_sum += 1e9 / hz
        bucket.clock_n += 1
        if high_ns:
            self.high_sum += high_ns
            self.high_n += 1
            s.t_high_ns = self.high_sum / self.high_n
        if low_ns:
            self.low_sum += low_ns
            self.low_n += 1
            s.t_low_ns = self.low_sum / self.low_n

    # ---------------------------------------------------------- snapshot
    def compute(self) -> PerfSnapshot:
        s = self.snap
        now = self.last_t
        if now is not None:
            lo = now - ROLLING_WINDOW_S * 1e9
            while self.roll_txn and self.roll_txn[0][0] < lo:
                self.roll_txn.popleft()
            while self.roll_clk and self.roll_clk[0][0] < lo:
                self.roll_clk.popleft()
            window_s = min(ROLLING_WINDOW_S, s.observed_ns / 1e9) if s.observed_ns else 0
            if window_s > 0.05:
                s.roll_tps = len(self.roll_txn) / window_s
                busy = sum(d for _, d, _ in self.roll_txn)
                s.roll_util = min(100.0, 100.0 * busy / (window_s * 1e9))
                s.roll_throughput = sum(n for _, _, n in self.roll_txn) / window_s
            ps = sum(p for _, p, _ in self.roll_clk)
            pn = sum(n for _, _, n in self.roll_clk)
            s.roll_freq_hz = pn / ps * 1e9 if pn else None
        if s.observed_ns > 0:
            s.util = min(100.0, 100.0 * s.busy_ns / s.observed_ns)
            s.tps = s.complete / (s.observed_ns / 1e9)
            s.idle_ns = max(0.0, s.observed_ns - s.busy_ns)
        return s


def observed_data_timing(samples):
    """Digital threshold timing only. No analog rise-time/electrical certification."""
    setup, hold, lows = [], [], []
    last_change = last_fall = None
    previous = None
    for t, state in samples:
        if previous is None:
            previous = state
            continue
        sda, scl = state & 1, state & 2
        psda, pscl = previous & 1, previous & 2
        if pscl and not scl: last_fall = t
        if sda != psda and not scl:
            last_change = t
            if last_fall is not None: hold.append(t-last_fall)
        if scl and not pscl:
            if last_change is not None: setup.append(t-last_change)
            if last_fall is not None: lows.append(t-last_fall)
        previous = state
    median = float(np.median(lows)) if lows else None
    return dict(setup_min_ns=min(setup) if setup else None, hold_min_ns=min(hold) if hold else None,
                long_low_ns=[v for v in lows if median and v > 3*median],
                stretch_cause='UNDETERMINED — long LOW alone cannot prove target clock stretching')
