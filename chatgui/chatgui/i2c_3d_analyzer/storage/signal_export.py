"""Export CAPTURED SDA/SCL edges exactly as recorded (no resampling, no interpolation).

  * VCD (IEEE 1364 value change dump): opens in PulseView / sigrok (add the I2C protocol decoder on SCL / SDA to get an
    independent reference decode), GTKWave and most logic-analyzer software.  Timestamps are integer nanoseconds since
    the first exported sample (the absolute capture time of that sample is written in the header comment).
  * CSV: one row per captured sample: capture time (ns), time from the first sample (ns), SDA, SCL.
Samples are (t_ns, state) with state bit0 = SDA, bit1 = SCL, as produced by CaptureEngine.edges_between().
"""
from __future__ import annotations

import csv
import datetime as _dt


def _clean(samples):
    """Time-ordered samples, consecutive duplicates of the same level removed (a VCD stores changes only)."""
    out = []
    for t, s in samples:
        s = int(s) & 3
        if out and s == out[-1][1]:
            continue
        out.append((float(t), s))
    return out


def write_vcd(path, samples, tick_ns=None, title="I2C capture", source=""):
    """Write a VCD with two 1-bit wires (SCL, SDA).  Returns the number of value changes written."""
    sm = _clean(samples)
    if not sm:
        raise ValueError("no captured samples in the selected range")
    t0 = sm[0][0]
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(f"$date {_dt.datetime.now():%Y-%m-%d %H:%M:%S} $end\n")
        f.write(f"$version I2C Packet Analyzer - captured edges, not resampled $end\n")
        f.write(f"$comment {title}; source {source or 'capture'}; first sample at capture time {t0:.0f} ns"
                + (f"; sample resolution {tick_ns:g} ns" if tick_ns else "") + " $end\n")
        f.write("$timescale 1 ns $end\n$scope module i2c $end\n")
        f.write("$var wire 1 ! SCL $end\n$var wire 1 \" SDA $end\n$upscope $end\n$enddefinitions $end\n")
        s = sm[0][1]
        f.write(f"#0\n$dumpvars\n{(s >> 1) & 1}!\n{s & 1}\"\n$end\n")
        n = 0
        prev = s
        for t, s in sm[1:]:
            f.write(f"#{int(round(t - t0))}\n")
            if (s ^ prev) & 2:
                f.write(f"{(s >> 1) & 1}!\n")
            if (s ^ prev) & 1:
                f.write(f"{s & 1}\"\n")
            prev = s
            n += 1
    return n


def write_edges_csv(path, samples):
    sm = _clean(samples)
    if not sm:
        raise ValueError("no captured samples in the selected range")
    t0 = sm[0][0]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["capture_time_ns", "t_from_first_ns", "SDA", "SCL"])
        for t, s in sm:
            w.writerow([f"{t:.0f}", f"{t - t0:.0f}", s & 1, (s >> 1) & 1])
    return len(sm)


def read_vcd(path):
    """Minimal reader for the files write_vcd() produces (tests / round trip): [(t_ns, state)]."""
    ids, out, t, sda, scl = {}, [], 0, None, None
    with open(path, encoding="ascii") as f:
        lines = f.read().split("\n")
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("$var"):
            parts = ln.split()
            ids[parts[3]] = parts[4]
        elif ln.startswith("#"):
            if sda is not None and scl is not None:
                out.append((t, (scl << 1) | sda))
            t = int(ln[1:])
        elif ln[:1] in "01" and ln[1:] in ids:
            if ids[ln[1:]] == "SDA":
                sda = int(ln[0])
            else:
                scl = int(ln[0])
    if sda is not None and scl is not None:
        out.append((t, (scl << 1) | sda))
    dedup = []
    for t, s in out:
        if dedup and dedup[-1][0] == t:
            dedup[-1] = (t, s)
        else:
            dedup.append((t, s))
    return dedup
