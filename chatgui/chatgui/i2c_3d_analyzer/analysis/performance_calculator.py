"""Timing and performance statistics computed from hardware timestamps only.

Definitions
  SCL period      time between consecutive SCL rising edges inside one frame
                  (analysis.scl_timing.cycles: never across START / Sr / STOP or a byte whose clocks were not captured).
                  Periods > 3x the packet's median (clock stretching, inter-byte
                  pauses) are excluded from the frequency estimate (scl_timing.robust_periods).
  f_SCL           1 / mean(valid periods)
  t_HIGH / t_LOW  rise->fall and fall->rise intervals inside frames, same > 3x median exclusion
  busy time       sum of (STOP time - START time) of decoded transactions
  observed time   hardware time covered by records with no sequence gap.
                  Intervals across a capture gap are NOT counted as idle.
  utilization     busy / observed * 100
  rate            completed transactions / observed seconds
Three scopes are reported: instantaneous (last transaction), rolling (last
ROLLING_WINDOW_S of hardware time) and cumulative (whole capture), plus a per-master
breakdown keyed strictly by the attributed txn.master_id (1, 2, None = unattributed).

A native firmware segment only carries the firmware's per-segment average.  Each packet keeps a record of what it
contributed to the clock statistics, so when its raw edges are matched later (CaptureEngine._match_waveform_timing)
retime_transaction() REPLACES that firmware average by the packet's captured cycles everywhere (cumulative, rolling,
per-second buckets / history, min / max, per master): the Performance page then shows the same numbers as the Packet
Analyzer, Waveform Viewer, 3D HUD and Session Report.
"""
from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import numpy as np

from config import HISTORY_POINTS, ROLLING_WINDOW_S
from analysis import scl_timing
from analysis.communication_health import acknowledgment_counts

LONG_FACTOR = scl_timing.LONG_FACTOR
LIVE_MINMAX = 4096          # recent packets whose min/max contribution can still be replaced by a late raw match
MASTER_KEYS = (1, 2, None)  # Master 1, Master 2, unattributed (never guessed)


@dataclass
class TimingSummary:
    periods: np.ndarray
    highs: np.ndarray
    lows: np.ndarray


def _robust(values):
    """Values <= LONG_FACTOR x their median (the same exclusion rule as the SCL cycles)."""
    v = np.asarray(values, dtype=float)
    if not len(v):
        return v
    return v[v <= LONG_FACTOR * float(np.median(v))]


def frame_timing(txn) -> TimingSummary:
    """Accepted cycle periods (scl_timing.robust_periods) and robust t_HIGH / t_LOW of one packet."""
    kept, _long = scl_timing.robust_periods(txn)
    p = np.asarray(kept, dtype=float)
    rises = np.asarray(txn.rises or [], dtype=float)
    falls = np.asarray(txn.falls or [], dtype=float)
    if len(rises) < 1 or len(falls) < 1:
        e = np.empty(0)
        return TimingSummary(p, e, e)
    bounds = [t for t, k in txn.events if k == "Sr"]
    # Pair only within a frame; never measure a repeated-START setup as LOW.
    high_values, low_values = [], []
    start = -math.inf
    for stop in bounds + [math.inf]:
        rr = rises[(rises >= start) & (rises < stop)]
        ff = falls[(falls >= start) & (falls < stop)]
        # Vectorised pairing (identical results to per-edge searchsorted).
        idx = np.searchsorted(ff, rr, side='right')
        ok = idx < len(ff)
        high_values.append(ff[idx[ok]] - rr[ok])
        idx = np.searchsorted(rr, ff, side='right')
        ok = idx < len(rr)
        low_values.append(rr[idx[ok]] - ff[ok])
        start = stop
    highs = np.concatenate(high_values) if high_values else np.empty(0)
    lows = np.concatenate(low_values) if low_values else np.empty(0)
    return TimingSummary(p, _robust(highs), _robust(lows))


def transaction_contribution(txn) -> dict:
    """What ONE transaction adds to the statistics.  The cumulative snapshot and every per-master group are built from
    this same helper, so their totals can never diverge (sum over masters == cumulative)."""
    counts = acknowledgment_counts(txn)
    return dict(transactions=1, complete=int(txn.status == "ok"),
                busy_ns=txn.duration_ns if txn.status in ("ok", "malformed") else 0.0,
                data_bytes=txn.byte_count, nacks=txn.nack_count, unexpected_nacks=counts["unexpected_nacks"])


@dataclass
class MasterPerf:
    """Per-master summary (strictly the attributed txn.master_id; None = unattributed)."""
    transactions: int = 0
    complete: int = 0
    busy_ns: float = 0.0
    data_bytes: int = 0
    nacks: int = 0
    unexpected_nacks: int = 0
    period_sum: float = 0.0          # accepted SCL cycle periods of this master's packets (ns)
    period_n: int = 0
    busy_share_pct: float | None = None   # of the bus busy time (set by compute)
    txn_share_pct: float | None = None    # of all transactions (set by compute)

    @property
    def freq_mean_hz(self):
        return self.period_n / self.period_sum * 1e9 if self.period_n and self.period_sum > 0 else None

    def apply(self, c: dict, sign: int = 1):
        self.transactions += sign * c["transactions"]
        self.complete += sign * c["complete"]
        self.busy_ns += sign * c["busy_ns"]
        self.data_bytes += sign * c["data_bytes"]
        self.nacks += sign * c["nacks"]
        self.unexpected_nacks += sign * c["unexpected_nacks"]


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
    unexpected_nacks: int = 0
    unverified_read_end_nacks: int = 0
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
    by_master: dict = field(default_factory=dict)     # 1 / 2 / None -> MasterPerf


def _new_clock():
    # entries: (t_ns, period_sum, period_n, bucket_second); f: per-packet frequencies eligible for min/max
    return dict(entries=[], f=[], hs=0.0, hn=0, ls=0.0, ln=0, source=None)


class PerformanceCalculator:
    def __init__(self):
        self.reset()

    def reset(self):
        self.snap = PerfSnapshot()
        self.roll_txn = deque()        # (t_end, duration, bytes)
        self.roll_clk = deque()        # (t, period_sum, n, key)
        self.last_t: float | None = None
        self.period_sum = 0.0
        self.period_n = 0
        self.high_sum = self.high_n = 0.0
        self.low_sum = self.low_n = 0.0
        self.dur_sum = 0.0
        self.bucket: Bucket | None = None
        self.history = deque(maxlen=HISTORY_POINTS)   # per-second points (dicts)
        self.history_rev = 0                          # bumped when a completed second is corrected by a late match
        # one point per complete transaction (cumulative, on the same capture clock): the rolling graphs use it while
        # fewer than 2 whole seconds exist (a short capture, or the presentation whose clock moves microseconds per
        # transfer), so they show the progression instead of staying blank
        self.txn_points = deque(maxlen=HISTORY_POINTS * 10)
        self._cum = dict(n=0, busy=0.0, bytes=0, acks=0, nacks=0, opp=0, unexp=0)
        self.groups = {k: MasterPerf() for k in MASTER_KEYS}
        self.snap.by_master = self.groups
        self._live = OrderedDict()     # key -> [f]: per-packet min/max candidates that a late match may still replace
        self._final_min = self._final_max = None
        self._mm_dirty = False
        self._inst_key = None
        self.retimed = 0               # packets whose firmware average was replaced by captured cycles

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
            "_clock_sum": b.clock_sum, "_clock_n": b.clock_n,     # kept so a late raw match can correct this second
        })

    def _bucket_clock(self, sec, ps, pn):
        """Add (or, with negative values, remove) clock periods of the second `sec` - the running bucket or a completed
        second already in the history."""
        if self.bucket is not None and self.bucket.second == sec:
            self.bucket.clock_sum += ps
            self.bucket.clock_n += pn
            return
        for h in reversed(self.history):
            if h["t"] == sec:
                h["_clock_sum"] = h.get("_clock_sum", 0.0) + ps
                h["_clock_n"] = h.get("_clock_n", 0) + pn
                h["freq_khz"] = (h["_clock_n"] / h["_clock_sum"] * 1e6) if h["_clock_n"] > 0 and h["_clock_sum"] > 0 \
                    else float("nan")
                self.history_rev += 1
                return
            if h["t"] < sec:
                return

    # ---------------------------------------------------------- clock contributions (keyed per packet)
    def _clock_record(self, txn):
        if txn is None:
            return None
        rec = getattr(txn, "_perf_clk", None)
        if rec is None:
            rec = txn._perf_clk = _new_clock()
        return rec

    def _add_clock(self, rec, key, t_ns, ps, pn, sec, f=None, hs=0.0, hn=0, ls=0.0, ln=0):
        s = self.snap
        self.period_sum += ps
        self.period_n += pn
        s.freq_avg_hz = self.period_n / self.period_sum * 1e9 if self.period_n and self.period_sum > 0 else None
        self._bucket_clock(sec, ps, pn)
        self.roll_clk.append((t_ns, ps, pn, key))
        self._trim_rolling()
        if hn:
            self.high_sum += hs
            self.high_n += hn
        if ln:
            self.low_sum += ls
            self.low_n += ln
        s.t_high_ns = self.high_sum / self.high_n if self.high_n else None
        s.t_low_ns = self.low_sum / self.low_n if self.low_n else None
        if rec is not None:
            rec["entries"].append((t_ns, ps, pn, sec))
            rec["hs"] += hs
            rec["hn"] += hn
            rec["ls"] += ls
            rec["ln"] += ln
            if f is not None:
                rec["f"].append(f)
        if f is not None:
            if key is not None:
                self._live.setdefault(key, []).append(f)
                self._live.move_to_end(key)
                while len(self._live) > LIVE_MINMAX:
                    _k, old = self._live.popitem(last=False)
                    self._fold_final(old)
            else:
                self._fold_final([f])
            self._mm_dirty = True

    def _fold_final(self, fs):
        for f in fs:
            self._final_min = f if self._final_min is None else min(self._final_min, f)
            self._final_max = f if self._final_max is None else max(self._final_max, f)

    def _minmax(self):
        if not self._mm_dirty:
            return
        lo, hi = self._final_min, self._final_max
        for fs in self._live.values():
            for f in fs:
                lo = f if lo is None else min(lo, f)
                hi = f if hi is None else max(hi, f)
        self.snap.freq_min_hz, self.snap.freq_max_hz = lo, hi
        self._mm_dirty = False

    # ---------------------------------------------------------- transactions
    def add_transaction(self, txn):
        s = self.snap
        s.has_data = True
        c = transaction_contribution(txn)
        s.total += c["transactions"]
        s.complete += c["complete"]
        if txn.status == "malformed":
            s.malformed += 1
        elif txn.status != "ok":
            s.incomplete += 1
        if txn.has_read:
            s.reads += 1
        if txn.has_write:
            s.writes += 1
        s.acks += txn.ack_count
        s.nacks += c["nacks"]
        counts = acknowledgment_counts(txn)
        s.addr_nacks += counts['address_nacks']
        s.read_end_nacks += counts['expected_read_nacks']
        s.write_data_nacks += counts['write_data_nacks']
        s.unexpected_nacks += c["unexpected_nacks"]
        s.unverified_read_end_nacks += counts['unverified_read_end_nacks']
        nb = c["data_bytes"]
        s.total_bytes += nb
        dur = txn.duration_ns
        if txn.status in ("ok", "malformed"):
            self.dur_sum += dur
            s.busy_ns += c["busy_ns"]
            s.dur_min_ns = dur if s.dur_min_ns is None else min(s.dur_min_ns, dur)
            s.dur_max_ns = dur if s.dur_max_ns is None else max(s.dur_max_ns, dur)
            s.dur_avg_ns = self.dur_sum / max(1, s.complete + s.malformed)

        tm = frame_timing(txn)
        if len(tm.highs):
            txn.timing["high_ns"] = float(np.mean(tm.highs))
        if len(tm.lows):
            txn.timing["low_ns"] = float(np.mean(tm.lows))
        b = self._bucket_for(txn.t_start)
        b.txns += int(txn.status == "ok")
        b.busy += dur
        b.acks += txn.ack_count
        b.nacks += txn.nack_count
        b.dur_sum += dur if txn.status == "ok" else 0
        b.bytes += nb
        rec = self._clock_record(txn)
        if len(tm.periods):
            ps, pn = float(tm.periods.sum()), len(tm.periods)
            f_txn = pn / ps * 1e9
            s.inst_freq_hz, self._inst_key = f_txn, id(txn)
            rec["source"] = "raw"
            # only frames with enough clocks set min/max
            self._add_clock(rec, id(txn), txn.t_end or txn.t_start, ps, pn, b.second, f=f_txn if pn >= 8 else None,
                            hs=float(tm.highs.sum()), hn=len(tm.highs), ls=float(tm.lows.sum()), ln=len(tm.lows))
        elif len(tm.highs) or len(tm.lows):
            self._add_clock(rec, id(txn), txn.t_end or txn.t_start, 0.0, 0, b.second,
                            hs=float(tm.highs.sum()), hn=len(tm.highs), ls=float(tm.lows.sum()), ln=len(tm.lows))
        if txn.status == "ok":
            self.roll_txn.append((txn.t_end or txn.t_start, dur, nb))
            self._trim_rolling()
        cm = self._cum
        cm["n"] += int(txn.status == "ok")
        cm["busy"] += dur if txn.status in ("ok", "malformed") else 0.0
        cm["bytes"] += nb
        cm["acks"] += txn.ack_count
        cm["nacks"] += txn.nack_count
        cm["opp"] += counts["opportunities"]
        cm["unexp"] += c["unexpected_nacks"]
        f_pt = (len(tm.periods) / float(tm.periods.sum()) * 1e6) if len(tm.periods) and tm.periods.sum() > 0             else float("nan")
        # per-master group: the same contribution, keyed by the attribution the packet has NOW (reattribute() moves it)
        g = self.groups.setdefault(txn.master_id, MasterPerf())
        g.apply(c)
        ps_all = sum(e[1] for e in rec["entries"])
        pn_all = sum(e[2] for e in rec["entries"])
        g.period_sum += ps_all
        g.period_n += pn_all
        txn._perf_contrib, txn._perf_group, txn._perf_group_clock = c, txn.master_id, (ps_all, pn_all)
        if txn.t_end is not None:
            self.advance(txn.t_end)
        t_pt = txn.t_end or txn.t_start
        obs_s = self.snap.observed_ns / 1e9
        span_s = max(obs_s, (t_pt - self.txn_points[0]["t_ns"]) / 1e9 if self.txn_points else 0.0) or None
        self.txn_points.append(dict(
            t_ns=t_pt, freq_khz=f_pt, dur_us=dur / 1e3 if txn.status == "ok" else float("nan"),
            # cumulative values over the capture observed so far: a smooth progression, never a guess
            tps=cm["n"] / span_s if span_s else float("nan"),
            util=min(100.0, 100.0 * cm["busy"] / (span_s * 1e9)) if span_s else float("nan"),
            throughput=cm["bytes"] / span_s if span_s else float("nan"),
            acks=cm["acks"] / span_s if span_s else float("nan"),
            nacks=cm["nacks"] / span_s if span_s else float("nan"),
            unexpected_pct=100.0 * cm["unexp"] / cm["opp"] if cm["opp"] else None,
            raw_pct=100.0 * cm["nacks"] / cm["opp"] if cm["opp"] else None))

    def add_firmware_frequency(self, hz, t_ns, high_ns=0, low_ns=0, txn=None):
        """Use real firmware-reported measurements without inventing SCL edges.  txn: the packet this segment belongs to
        (its contribution can then be replaced by captured cycles when its raw edges are matched later)."""
        if hz <= 0:
            return
        s = self.snap
        s.inst_freq_hz = float(hz)
        rec = self._clock_record(txn)
        key = id(txn) if txn is not None else None
        self._inst_key = key
        if rec is not None and rec["source"] is None:
            rec["source"] = "firmware"
        sec = self._bucket_for(t_ns).second
        self._add_clock(rec, key, t_ns, 1e9 / hz, 1, sec, f=float(hz),
                        hs=float(high_ns or 0.0), hn=1 if high_ns else 0, ls=float(low_ns or 0.0), ln=1 if low_ns else 0)

    def retime_transaction(self, txn, raw=None):
        """Late raw-edge match of an already counted packet: replace whatever it contributed to the clock statistics (the
        firmware per-segment average) by its captured cycles - cumulative, rolling, per second, min / max and its master
        group.  raw: the waveform-decoded copy of the packet (its rises / falls give t_HIGH / t_LOW).  Returns True if
        the packet's figures changed."""
        kept, _long = scl_timing.robust_periods(txn)
        if not kept:
            return False
        rec = self._clock_record(txn)
        key = id(txn)
        s = self.snap
        old_entries = list(rec["entries"])
        old_ps = sum(e[1] for e in old_entries)
        old_pn = sum(e[2] for e in old_entries)
        # ---- remove the old contribution everywhere
        self.period_sum -= old_ps
        self.period_n -= old_pn
        for _t, ps, pn, sec in old_entries:
            self._bucket_clock(sec, -ps, -pn)
        self.roll_clk = deque(e for e in self.roll_clk if e[3] != key)
        self.high_sum -= rec["hs"]
        self.high_n -= rec["hn"]
        self.low_sum -= rec["ls"]
        self.low_n -= rec["ln"]
        live = key in self._live
        if live:
            del self._live[key]
            self._mm_dirty = True
        sec = old_entries[0][3] if old_entries else self._bucket_for(txn.t_start).second
        t_ns = old_entries[-1][0] if old_entries else (txn.t_end or txn.t_start)
        new = _new_clock()
        new["source"] = "raw (late match)"
        txn._perf_clk = new
        # ---- add the captured cycles
        ps, pn = float(sum(kept)), len(kept)
        f_txn = pn / ps * 1e9
        hs = ls = 0.0
        hn = ln = 0
        if raw is not None:
            tm = frame_timing(raw)
            hs, hn, ls, ln = float(tm.highs.sum()), len(tm.highs), float(tm.lows.sum()), len(tm.lows)
            if hn:
                txn.timing["high_ns"] = hs / hn
            if ln:
                txn.timing["low_ns"] = ls / ln
        eligible = f_txn if pn >= 8 else None
        if eligible is not None and not live and old_entries:
            # the packet left the replaceable window long ago: its old min/max candidates are already folded in
            self._fold_final([eligible])
            self._mm_dirty = True
            eligible = None
        self._add_clock(new, key, t_ns, ps, pn, sec, f=eligible, hs=hs, hn=hn, ls=ls, ln=ln)
        if eligible is None and pn >= 8:
            new["f"].append(f_txn)
        if self._inst_key == key:
            s.inst_freq_hz = f_txn
        # ---- the packet's master group follows
        grp = getattr(txn, "_perf_group", "absent")
        if grp != "absent":
            g = self.groups.setdefault(grp, MasterPerf())
            ops, opn = getattr(txn, "_perf_group_clock", (0.0, 0))
            g.period_sum += ps - ops
            g.period_n += pn - opn
            txn._perf_group_clock = (ps, pn)
        self.retimed += 1
        return True

    def reattribute(self, txn):
        """Controller telemetry / ownership attributed (or re-attributed) an already counted packet: move its whole
        contribution to the group of txn.master_id.  Never guesses: the key is exactly the attribution the tracker set."""
        c = getattr(txn, "_perf_contrib", None)
        if c is None:
            return
        old, new = txn._perf_group, txn.master_id
        if old == new:
            return
        go = self.groups.setdefault(old, MasterPerf())
        gn = self.groups.setdefault(new, MasterPerf())
        go.apply(c, -1)
        gn.apply(c, +1)
        ps, pn = txn._perf_group_clock
        go.period_sum -= ps
        go.period_n -= pn
        gn.period_sum += ps
        gn.period_n += pn
        txn._perf_group = new

    # ---------------------------------------------------------- snapshot
    def _trim_rolling(self):
        """Drop rolling-window entries older than ROLLING_WINDOW_S before the latest capture time.  Done on every
        append (not only when a page calls compute()): otherwise the deques grew for as long as no page refreshed the
        figures, and every late raw-edge match (retime_transaction) re-copied the whole roll_clk."""
        now = self.last_t
        if now is None:
            return
        lo = now - ROLLING_WINDOW_S * 1e9
        while self.roll_txn and self.roll_txn[0][0] < lo:
            self.roll_txn.popleft()
        while self.roll_clk and self.roll_clk[0][0] < lo:
            self.roll_clk.popleft()

    def compute(self) -> PerfSnapshot:
        s = self.snap
        now = self.last_t
        if now is not None:
            self._trim_rolling()
            window_s = min(ROLLING_WINDOW_S, s.observed_ns / 1e9) if s.observed_ns else 0
            if window_s > 0:                  # a capture shorter than 50 ms is still a measured window
                s.roll_tps = len(self.roll_txn) / window_s
                busy = sum(d for _, d, _ in self.roll_txn)
                s.roll_util = min(100.0, 100.0 * busy / (window_s * 1e9))
                s.roll_throughput = sum(n for _, _, n in self.roll_txn) / window_s
            ps = sum(e[1] for e in self.roll_clk)
            pn = sum(e[2] for e in self.roll_clk)
            s.roll_freq_hz = pn / ps * 1e9 if pn and ps > 0 else None
        self._minmax()
        if s.observed_ns > 0:
            s.util = min(100.0, 100.0 * s.busy_ns / s.observed_ns)
            s.tps = s.complete / (s.observed_ns / 1e9)
            s.idle_ns = max(0.0, s.observed_ns - s.busy_ns)
        for g in self.groups.values():
            g.busy_share_pct = 100.0 * g.busy_ns / s.busy_ns if s.busy_ns > 0 else None
            g.txn_share_pct = 100.0 * g.transactions / s.total if s.total else None
        s.by_master = self.groups
        return s


def observed_data_timing(samples, resolution_ns=None):
    """Digital threshold timing only. No analog rise-time/electrical certification.

    Only DATA bits are measured: an SCL rise counts once its HIGH phase ended with an SCL fall and SDA stayed put (a rise
    followed by an SDA change while SCL is HIGH is the START / Sr / STOP condition, whose setup and preceding LOW are
    t_SU;STA / t_SU;STO, not t_SU;DAT).  Every START / Sr / STOP forgets the previous SDA change and SCL fall, so nothing
    measured across a condition (or before the transaction) can enter setup / hold / LOW.  Setup = last SDA change while
    SCL LOW -> the bit's rise; hold = SCL fall -> the first SDA change after it; LOW = fall -> next data rise.  A level
    change that coincides with an SCL edge in the same sample is ambiguous and not measured.  With a sample period
    `resolution_ns` (500 ns at 2 MS/s) every value is only known to +/- one sample."""
    setup, hold, lows = [], [], []
    last_change = last_fall = None
    cand = None                      # (setup, low) of the SCL rise in progress, committed at its fall
    hold_open = False
    previous = None
    for t, state in samples:
        if previous is None:
            previous = state
            continue
        sda, scl = state & 1, state & 2
        psda, pscl = previous & 1, previous & 2
        if scl and pscl and sda != psda:                 # START / Sr / STOP
            cand = None
            last_change = last_fall = None
            hold_open = False
        elif scl and not pscl:                           # SCL rise
            if sda != psda:                              # SDA moved in the same sample: order unknown
                cand = None
            else:
                cand = (t - last_change if last_change is not None else None,
                        t - last_fall if last_fall is not None else None)
            hold_open = False
        elif pscl and not scl:                           # SCL fall: the bit whose rise is pending was a data bit
            if cand is not None:
                if cand[0] is not None:
                    setup.append(cand[0])
                if cand[1] is not None:
                    lows.append(cand[1])
            cand = None
            last_fall = t
            last_change = None
            hold_open = sda == psda                      # SDA moving in the same sample: hold unmeasurable
        elif not scl and sda != psda:                    # SDA change while SCL LOW
            if hold_open and last_fall is not None:
                hold.append(t - last_fall)
            hold_open = False
            last_change = t
        previous = state
    median = float(np.median(lows)) if lows else None
    out = dict(setup_min_ns=min(setup) if setup else None, hold_min_ns=min(hold) if hold else None,
               long_low_ns=[v for v in lows if median and v > LONG_FACTOR * median],
               stretch_cause='UNDETERMINED — long LOW alone cannot prove target clock stretching')
    if resolution_ns:
        out["timing_resolution_ns"] = float(resolution_ns)
    return out
