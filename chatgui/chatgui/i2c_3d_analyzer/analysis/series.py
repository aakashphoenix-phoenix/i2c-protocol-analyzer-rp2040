"""Rolling-graph series: ONE clock, no breaks, no partial-second jitter.

Every rolling graph (SCL frequency, transactions/s, utilisation, duration, throughput, ACK/NACK, bus
health) is plotted against the SAME x clock: whole seconds relative to the latest COMPLETED capture
second (<= 0).  Seconds that were observed for less than MIN_OBSERVED_S (the partial first second, a gap
edge) and values that do not exist (NaN) are dropped instead of being drawn as spikes, zeros or line breaks:
the polyline simply joins the neighbouring valid seconds.
"""
from __future__ import annotations

import math

import numpy as np

MIN_OBSERVED_S = 0.5          # a bucket observed for less than this is a partial second, not a data point
MIN_SPAN_S = 10               # the visible window never gets narrower than this (grows in 10 s steps)
MIN_SECONDS = 2               # fewer completed seconds than this: plot the per-transaction progression instead

# A capture shorter than two whole seconds of hardware time (the first moments of any capture, and the presentation
# sequence whose capture clock advances only microseconds per transfer) has no completed-second points yet.  The graphs
# then show one point per complete transaction (cumulative rate / utilisation / throughput / NACK %, per-packet SCL
# frequency and duration) on the SAME relative clock (seconds before the latest transaction), instead of staying blank.


def reference_second(engine):
    """Latest completed capture second (the clock origin of every rolling graph), or None."""
    hist = engine.perf.history
    return int(hist[-1]["t"]) if hist else None


def short_capture(engine):
    """True while fewer than MIN_SECONDS completed seconds exist: the graphs use the per-transaction points."""
    hist = engine.perf.history
    return sum(1 for h in hist if h.get("observed", 1.0) >= MIN_OBSERVED_S) < MIN_SECONDS and bool(
        getattr(engine.perf, "txn_points", None))


def _txn_series(engine, keys):
    pts = list(engine.perf.txn_points)
    ref = pts[-1]["t_ns"]
    x = np.array([(p["t_ns"] - ref) / 1e9 for p in pts], dtype=float)
    out = {}
    for k in keys:
        y = np.array([p.get(k, np.nan) for p in pts], dtype=float)
        ok = np.isfinite(y)
        out[k] = (x[ok], y[ok])
    return x, out, ("txn", len(pts), ref)


def perf_series(engine, keys):
    """(x, {key: y}, signature).  x = bucket second - reference second; per-second counts are normalised.
    Short capture (< MIN_SECONDS completed seconds): one cumulative point per transaction instead (see above)."""
    hist = list(engine.perf.history)
    rev = getattr(engine.perf, "history_rev", 0)      # a completed second corrected by a late raw match redraws too
    rows = [h for h in hist if h.get("observed", 1.0) >= MIN_OBSERVED_S]
    if short_capture(engine):
        return _txn_series(engine, keys)
    if not rows:
        return np.empty(0), {k: (np.empty(0), np.empty(0)) for k in keys}, (len(hist), None, rev)
    ref = int(hist[-1]["t"])
    x = np.array([h["t"] - ref for h in rows], dtype=float)
    out = {}
    for k in keys:
        if k in ("acks", "nacks"):                       # raw counts of a bucket -> per observed second
            y = np.array([h[k] / h["observed"] for h in rows], dtype=float)
        else:
            y = np.array([h[k] for h in rows], dtype=float)
        ok = np.isfinite(y)
        out[k] = (x[ok], y[ok])
    return x, out, (len(hist), ref, rev)


def health_series(engine, ref=None):
    """Bus-health curves on the SAME clock as perf_series.  Returns (x_unexpected, y, x_raw, y_raw, signature).

    The point of the capture second still in progress is dropped (it is rewritten on every transaction and
    made the leading edge jitter); None percentages (no ACK slots) are dropped, not drawn as breaks.
    Short capture: one cumulative NACK-percentage point per transaction, on the per-transaction clock."""
    if short_capture(engine):
        pts = list(engine.perf.txn_points)
        r = pts[-1]["t_ns"]
        xu = [(p["t_ns"] - r) / 1e9 for p in pts if p["unexpected_pct"] is not None]
        yu = [p["unexpected_pct"] for p in pts if p["unexpected_pct"] is not None]
        xr = [(p["t_ns"] - r) / 1e9 for p in pts if p["raw_pct"] is not None]
        yr = [p["raw_pct"] for p in pts if p["raw_pct"] is not None]
        return (np.array(xu, float), np.array(yu, float), np.array(xr, float), np.array(yr, float),
                ("txn", len(pts), r))
    points = list(engine.communication.history)
    if not points:
        return np.empty(0), np.empty(0), np.empty(0), np.empty(0), (0, None)
    cur = int((engine.hw_now or 0) // 1e9)
    done = [p for p in points if int(p[0]) < cur] or points[-1:]
    base = ref if ref is not None else int(done[-1][0])
    xu = [int(p[0]) - base for p in done if p[1] is not None]
    yu = [p[1] for p in done if p[1] is not None]
    xr = [int(p[0]) - base for p in done if len(p) > 2 and p[2] is not None]
    yr = [p[2] for p in done if len(p) > 2 and p[2] is not None]
    return (np.array(xu, float), np.array(yu, float), np.array(xr, float), np.array(yr, float),
            (len(done), base, done[-1][0]))


def short_window(x):
    """Visible span (s) of the per-transaction view: the whole recorded progression plus a margin (> 0)."""
    if not len(x):
        return 0.001
    return max(1e-4, -float(np.min(x)) * 1.08)


def time_window(x):
    """Visible span in seconds: grows in 10 s steps, so the axis does not rescale on every data change."""
    if not len(x):
        return MIN_SPAN_S
    return max(MIN_SPAN_S, int(math.ceil(-float(np.min(x)) / 10.0) * 10))
