"""SCL timing measured from captured timestamps — one implementation for every view.

Definitions (UM10204 Rev. 7.0 §3.1.3, Fig. 4/38; Table 10 for limits):
  * cycle: two CONSECUTIVE committed SCL rising edges inside one frame (the 8 data
    bit rises + the 9th ACK rise of each byte, then the next byte's rises).
    period = next_rise - rise;  frequency = 1 / period.
    A cycle is never formed across START/repeated START/STOP, across an idle
    gap between transactions, or across a byte whose rises were not captured.
  * robust packet mean: mean of the packet's cycle periods after excluding
    periods > 3 x the packet median (those are reported as long-LOW cycles),
    frequency = 1 / mean period.
  * rolling mean: PerformanceCalculator.roll_freq_hz, all accepted cycles in the
    last ROLLING_WINDOW_S (1 s) of capture time.
  * native analyzer segments without raw windows carry only the firmware's
    per-segment average (periods <= 1 ms), labelled as such.
A measurement is never replaced by the configured/requested controller rate.
"""
from __future__ import annotations

import statistics

LONG_FACTOR = 3.0


def rise_groups(txn):
    """Lists of consecutive committed SCL rises (ns); a new group starts per frame or
    whenever a byte without captured bit times interrupts the sequence.

    A byte whose ACK clock was not captured (bit times present, t_ack None: the capture or the frame ended inside the
    byte) contributes its captured bit rises and then CLOSES the group: the next captured rise is not the one after
    them, so no cycle may be formed across the missing 9th clock (and no None ever enters a period)."""
    groups, cur, cur_frame = [], [], None
    for b in txn.bytes:
        if b.frame != cur_frame or not b.bit_times:
            if len(cur) > 1:
                groups.append(cur)
            cur, cur_frame = [], b.frame
        if not b.bit_times:
            continue
        cur.extend(t for t in b.bit_times if t is not None)
        if b.t_ack is None:
            if len(cur) > 1:
                groups.append(cur)
            cur = []
            continue
        cur.append(b.t_ack)
    if len(cur) > 1:
        groups.append(cur)
    return groups


def cycles(txn):
    """[(rise, next_rise, period_ns)] for every individual cycle of the packet."""
    out = []
    for g in rise_groups(txn):
        for a, b in zip(g, g[1:]):
            if b > a:
                out.append((a, b, b - a))
    return out


def robust_periods(txn):
    """(kept, long) cycle periods of the packet: `long` = periods > LONG_FACTOR x the packet median (clock stretching,
    inter-byte pauses), excluded from every frequency figure and reported separately.  The ONE rule shared by the
    packet measurement, PerformanceCalculator (cumulative / rolling / per-second / per-master) and the late raw match."""
    periods = [p for _, _, p in cycles(txn)]
    if not periods:
        return [], []
    med = statistics.median(periods)
    return [p for p in periods if p <= LONG_FACTOR * med], [p for p in periods if p > LONG_FACTOR * med]


def packet_measurement(txn, simulated=False):
    """Packet SCL measurement with explicit source and scope (or UNKNOWN)."""
    kept, long_ = robust_periods(txn)
    if kept:
        mean = sum(kept) / len(kept)
        return dict(frequency_hz=1e9 / mean, period_ns=mean, cycles=len(kept), excluded=len(long_),
                    long_cycle_ns=long_, cycle_min_hz=1e9 / max(kept), cycle_max_hz=1e9 / min(kept),
                    source=("SIMULATED raw edges" if simulated else "captured raw edges"),
                    scope=f"robust packet mean of {len(kept)} cycles"
                          + (f" ({len(long_)} long cycle(s) > 3× median excluded)" if long_ else ""))
    hz = txn.timing.get("frequency_hz")
    if hz:
        return dict(frequency_hz=hz, period_ns=1e9 / hz, cycles=None, excluded=None, long_cycle_ns=[],
                    cycle_min_hz=None, cycle_max_hz=None, source="analyzer firmware segment",
                    scope="firmware per-segment average (periods ≤ 1 ms); individual cycles not captured")
    return dict(frequency_hz=None, period_ns=None, cycles=0, excluded=0, long_cycle_ns=[], cycle_min_hz=None,
                cycle_max_hz=None, source="UNKNOWN", scope="no captured SCL cycles")


def annotate(txn, simulated=False, resolution_ns=None):
    """Store the shared measurement on the record (used by table, details, export, 3D).

    resolution_ns: the analyzer sample period (500 ns at 2 MS/s).  Every captured edge is only known to one sample, so a
    SINGLE cycle is quantised to whole samples (a true 2.52 µs period reads 2.5 or 3.0 µs = 400 / 333 kHz; any 400 kHz
    cycle lands in 333-500 kHz); the robust packet mean
    averages that spread out.  Recorded so every view can disclose the digital limit."""
    m = packet_measurement(txn, simulated)
    tm = txn.timing
    if resolution_ns:
        tm["sample_resolution_ns"] = float(resolution_ns)
    if m["frequency_hz"] is not None:
        tm["frequency_hz"] = m["frequency_hz"]
        tm["period_ns"] = m["period_ns"]
    tm["frequency_source"] = m["source"]
    tm["frequency_scope"] = m["scope"]
    tm["cycles_used"] = m["cycles"]
    tm["cycles_excluded"] = m["excluded"]
    tm["cycle_min_hz"] = m["cycle_min_hz"]
    tm["cycle_max_hz"] = m["cycle_max_hz"]
    starts = [t for t, k in txn.events if k in ("S", "Sr")]
    stops = [t for t, k in txn.events if k == "P"]
    tm["start_ns"] = starts[0] if starts else None
    tm["stop_ns"] = stops[-1] if stops else None
    tm["byte_durations_ns"] = [(b.t_end - b.t_first) if (b.t_end is not None and b.bit_times) else None
                               for b in txn.bytes]
    return m


def cycle_at(txns, t_ns, edges_between=None):
    """The individual cycle containing capture time t_ns, with HIGH/LOW split when the
    raw edge records are available.  Returns None when no captured cycle covers t."""
    for txn in txns:
        for a, b, p in cycles(txn):
            if a <= t_ns < b:
                out = dict(rise=a, next_rise=b, period_ns=p, frequency_hz=1e9 / p, high_ns=None, low_ns=None,
                           txn_id=txn.id)
                if edges_between is not None:
                    ts, ss = edges_between(a, b)
                    for tt, st in zip(ts, ss):
                        if a < tt < b and not (int(st) >> 1) & 1:      # first SCL fall after the rise
                            out["high_ns"], out["low_ns"] = tt - a, b - tt
                            break
                return out
    return None


def address_byte(frame):
    """8-bit address byte on the wire (7-bit address << 1 | R/W) or None."""
    if frame.addr is None or frame.ten_bit or frame.rw not in ("R", "W"):
        return None
    return ((frame.addr << 1) | (1 if frame.rw == "R" else 0)) & 0xFF
