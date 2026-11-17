"""Real-time follow (2026-11-17 live review): never more than ~2.5 s behind the BUS, and in SEQ mode (one transfer every
0.5 s) every packet is shown, in capture order, none skipped."""
import copy

from analysis.capture_engine import CaptureEngine
from analysis.playback import Playback, RealtimeQueue
from sources.presentation_source import PresentationBus


def packets(n):
    e = CaptureEngine()
    e.set_source("demo")
    bus = PresentationBus()
    e.ingest_lines([bus.hello()])
    while len(e.packets) < n:
        e.ingest_lines(bus.next_transfer()[0])
    return e.packets[:n]


def run(arrivals, until_s, refresh_s=0.1, bus_now=lambda t: t):
    """Drive RealtimeQueue + Playback like MainWindow._refresh.  arrivals: [(capture/wall time s, txn)]."""
    q, pb = RealtimeQueue(), Playback()
    pb.realtime, pb.speed = True, 1.0
    shown, lags, i, t = [], [], 0, 0.0
    while t < until_s:
        t += refresh_s
        new = []
        while i < len(arrivals) and arrivals[i][0] <= t:
            new.append(arrivals[i][1]); i += 1
        q.offer(new, now=t)
        now_ns = bus_now(t) * 1e9
        if pb.txn is None or pb.finished:
            nxt = q.pop(now=t, bus_now_ns=now_ns)
            if nxt is not None:
                pb.select(nxt, pinned=False, packet_s=q.budget_s(nxt, now_ns, refresh_s))
                shown.append(nxt)
        pb.advance(min(refresh_s, 0.3, pb.max_step_seconds()))
        if pb.txn is not None and not pb.finished:
            lags.append((now_ns - pb.txn.t_start) / 1e9)
    return shown, lags, q


def test_a_burst_then_an_idle_bus_never_plays_more_than_2_5_s_behind():
    base = packets(12)
    arrivals = []
    for turn in range(3):                                      # a 70-transfer turn in 150 ms, then 4 s of idle bus
        for k in range(70):
            p = copy.copy(base[k % 12])
            p.t_start = (turn * 4.15 + k * 0.15 / 70) * 1e9
            arrivals.append((p.t_start / 1e9, p))
    shown, lags, q = run(arrivals, 13.0)
    assert shown and max(lags) <= 2.5, max(lags)
    assert q.skipped > 0                                       # the rest of each burst is skipped, never reordered
    assert all(a.t_start < b.t_start for a, b in zip(shown, shown[1:]))


def test_seq_every_packet_shown_in_order_at_a_100_ms_refresh():
    base = packets(12)
    arrivals = []
    for k in range(40):                                        # SEQ ON 500: one transfer every 0.5 s
        p = copy.copy(base[k % 12])
        p.t_start = (1.0 + 0.5 * k) * 1e9
        arrivals.append((p.t_start / 1e9 + 0.05, p))           # decoded ~50 ms after it happened
    shown, lags, q = run(arrivals, 22.0)
    assert len(shown) == 40 and q.skipped == 0
    late = [(a + 0.45, p) for a, p in arrivals]                # + a telemetry wait / GUI stall: catches up, no skip
    shown, lags, q = run(late, 22.0)
    assert len(shown) == 40 and q.skipped == 0
    # a ~1.2 s GUI stall (a tab opened for the first time) mid-sequence: the queued packets drain, none skipped
    stalled = [(a if a < 8.0 else max(a, 9.2), p) for a, p in arrivals]
    shown, lags, q = run(stalled, 22.0)
    assert len(shown) == 40 and q.skipped == 0
    assert [p.t_start for p in shown] == [p.t_start for _a, p in arrivals]
    assert max(lags) <= 2.5


def test_packet_budget_respects_the_lag_limit():
    q = RealtimeQueue()
    p = packets(1)[0]
    p = copy.copy(p)
    p.t_start = 0.0
    assert q.budget_s(p, bus_now_ns=2.0e9) <= RealtimeQueue.LAG_LIMIT_S - 2.0 + 1e-9 or \
        q.budget_s(p, bus_now_ns=2.0e9) == RealtimeQueue.MIN_PACKET_S
    assert q.pop(bus_now_ns=5e9) is None                        # nothing queued
    q.offer([p], now=0.0)
    assert q.pop(now=10.0, bus_now_ns=5e9) is None and q.skipped == 1   # far behind the bus: skipped, counted
