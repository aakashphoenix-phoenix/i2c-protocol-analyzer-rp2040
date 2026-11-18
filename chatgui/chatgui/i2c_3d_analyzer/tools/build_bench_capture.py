"""Merge a REAL analyzer capture and the controllers' own COM logs into one replayable .i2ccap.

    py -3.12 -X utf8 tools/build_bench_capture.py --analyzer work/measurements/mm_baseline_before.jsonl \
        --master work/measurements/mm_before_com32.log --master work/measurements/mm_before_com34.log \
        --out work/measurements/mm_baseline_before.i2ccap

The GUI saves live captures in this format itself (analyzer records + {"type":"master_record"} lines).  Here the
analyzer lines are copied byte for byte and every JSON controller record is wrapped unchanged.  The only derived
input is the ORDER: each controller record is placed after the analyzer records up to its own time (controller
clock mapped with an offline signature fit), like USB arrival in a live session.  No clock offset is stored: the
GUI's tracker aligns and attributes on its own, exactly as it does live.  Plain-text and '#' comment lines of the
controller logs are not copied.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

ARRIVAL_DELAY_US = 20_000          # controller record placed 20 ms after its transfer (USB latency, order only)


def _analyzer_keys(lines, tick_ns):
    """Capture time (µs) of each analyzer line; lines without a time inherit the previous one."""
    keys, last = [], 0.0
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            keys.append(last)
            continue
        s = d.get("start_sample", d.get("t0", d.get("t_sample")))
        if isinstance(s, (int, float)):
            last = s * tick_ns / 1000.0
        keys.append(last)
    return keys


def build(analyzer, masters, out, notes=""):
    from analysis.capture_engine import CaptureEngine
    from communication.master_protocol import parse_master_line, MasterProtocolError
    import mm_bench_summary as M

    a_lines = [ln for ln in Path(analyzer).read_bytes().splitlines() if ln.strip()]
    eng = CaptureEngine(); eng.set_source("replay")
    eng.ingest_lines(a_lines)
    tick = eng.tick_ns
    keys = _analyzer_keys(a_lines, tick)
    merged = [(k, 0, i, ln) for i, (k, ln) in enumerate(zip(keys, a_lines))]
    fits = {}
    for path in masters:
        recs = []
        for ln in Path(path).read_bytes().splitlines():
            try:
                d = parse_master_line(ln)
            except MasterProtocolError:
                continue
            if d is not None:
                recs.append(d)
        mid = next((d["master_id"] for d in recs if d.get("master_id") in (1, 2)), None)
        txns = [d for d in recs if d.get("type") == "txn"]
        fit = M.align_and_match(eng.packets, txns)
        if fit is None:
            raise SystemExit(f"{path}: no transfer of this controller matches the analyzer capture")
        fits[mid] = fit
        a, b = fit
        t = None
        for j, d in enumerate(recs):
            if isinstance(d.get("t_us"), int):
                t = a * d["t_us"] + b + ARRIVAL_DELAY_US
            k = t if t is not None else -1.0
            rec = json.dumps({"type": "master_record", "slot": mid, "record": d}, separators=(",", ":")).encode()
            merged.append((k, 1, (mid, j), rec))
    merged.sort(key=lambda r: (r[0], r[1], r[2]))
    meta = {
        "format": "i2ccap", "format_version": 1, "app_version": "bench-merge",
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "capture_started": None, "source": "live", "simulated": False,
        "analyzer": {"firmware": eng.caps.firmware, "board": eng.caps.board, "protocol": eng.caps.proto,
                     "sample_hz": eng.caps.sample_hz, "tick_ns": eng.caps.tick_ns, "caps": sorted(eng.caps.caps)},
        "loss": {"lost_records": eng.lost_records, "fw_overflows": eng.fw_stats["overflows"],
                 "fw_dropped": eng.fw_stats["dropped"], "transport_errors": eng.transport_errors,
                 "raw_lines_truncated": False},
        "summary": {"transactions": len(eng.packets)},
        "oled_address": eng.sensors.oled_addr,
        "notes": notes or ("REAL HARDWARE: analyzer COM28 + Master 1 COM32 + Master 2 COM34 live logs merged by "
                           "tools/build_bench_capture.py (records unchanged, order only)"),
        "merged_from": [str(analyzer)] + [str(m) for m in masters],
    }
    with open(out, "wb") as f:
        f.write(json.dumps({"capture": meta}).encode() + b"\n")
        for *_, ln in merged:
            f.write(ln.rstrip(b"\r\n") + b"\n")
    return len(merged), fits


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyzer", required=True)
    ap.add_argument("--master", action="append", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    n, fits = build(args.analyzer, args.master, args.out)
    print(f"wrote {n} lines to {args.out}; ordering fits: " +
          ", ".join(f"M{m}: drift {(a - 1) * 1e6:+.1f} ppm offset {b / 1e3:+.3f} ms" for m, (a, b) in fits.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
