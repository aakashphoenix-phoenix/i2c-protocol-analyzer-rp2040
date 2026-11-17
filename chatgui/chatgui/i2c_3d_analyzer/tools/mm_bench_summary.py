"""Multi-master bench summary from SAVED captures (never opens a COM port).

    py -3.12 -X utf8 tools/mm_bench_summary.py --analyzer work/measurements/mm_baseline_before.jsonl \
        --master work/measurements/mm_before_com32.log --master work/measurements/mm_before_com34.log \
        --out work/measurements/mm_before_summary.json

Inputs
  --analyzer  raw analyzer JSON lines (tools/nack_phase_report.py --save ...)
  --master    a controller COM log with "txn" / "master_stats" lines (repeatable)

Every raw NACK is counted.  Categories (UM10204 §3.1.6):
  addr_nack_unexpected      9th clock after an address byte was NACK
  write_data_nack           a byte the controller wrote was NACKed by the target
  read_nack_premature       controller NACKed a read byte that was NOT the last one
  read_end_nack_unverified  NACK on the last read byte with no STOP/Sr captured after it
  read_end_nack_normal      NACK on the last read byte followed by STOP/Sr (mandatory, still a raw NACK)
Analyzer integrity: lost records, DMA overflows, dropped records, edge-stream gaps, mid_byte_end
cuts and packets whose status is not "ok" are reported next to the NACK counts.

Master telemetry is matched to analyzer packets by (address, bytes written, bytes read) after fitting
the master's microsecond clock to the analyzer clock (offset + drift).  Nothing is guessed: a packet
that no master reported stays "unattributed".
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


def _jsonl(path):
    out, bad = [], 0
    for ln in Path(path).read_bytes().splitlines():
        ln = ln.strip()
        if not ln.startswith(b"{"):
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            bad += 1
    return out, bad


def packet_signature(t):
    """(addr, wlen, rlen) as the bus carried it, or None for packets without an address."""
    addrs = {f.addr for f in t.frames if f.addr is not None}
    if len(addrs) != 1:
        return None
    a = addrs.pop()
    w = sum(len(f.data) for f in t.frames if f.rw == "W")
    r = sum(len(f.data) for f in t.frames if f.rw == "R")
    return a, w, r


def nack_categories(packets):
    cat = defaultdict(Counter)
    for t in packets:
        has_p = any(k == "P" for _, k in t.events)
        for fi, f in enumerate(t.frames):
            if f.addr is None:
                cat["(no address)"]["frames_without_address"] += 1
                continue
            c = cat[f"0x{f.addr:02X}"]
            if f.addr_ack is False:
                c[f"addr_nack_unexpected[{f.rw}]"] += 1
            elif f.addr_ack is None:
                c["addr_ack_unknown"] += 1
            boundary = fi + 1 < len(t.frames) or has_p
            for i, ack in enumerate(f.acks):
                if ack is not False:
                    if ack is None:
                        c["ack_unknown"] += 1
                    continue
                if f.rw == "W":
                    c["write_data_nack"] += 1
                elif i < len(f.data) - 1:
                    c["read_nack_premature"] += 1
                elif boundary:
                    c["read_end_nack_normal"] += 1
                else:
                    c["read_end_nack_unverified"] += 1
            if f.rw == "R" and f.acks and f.acks[-1] is True and not boundary:
                c["read_ended_with_ack_no_stop"] += 1
    return {k: dict(v) for k, v in sorted(cat.items())}


def analyzer_summary(path):
    from analysis.capture_engine import CaptureEngine
    from nack_phase_report import phase_table, summarise
    recs, bad = _jsonl(path)
    lines = [ln for ln in Path(path).read_bytes().splitlines() if ln.strip()]
    e = CaptureEngine(); e.set_source("replay")
    e.ingest_lines(lines)
    types = Counter((r.get("type") or r.get("t")) for r in recs)
    status = [r for r in recs if r.get("type") == "status"]
    seg = [r for r in recs if r.get("type") == "segment"]
    loss = [r for r in recs if r.get("type") == "loss"]
    gaps = sum(1 for r in recs if r.get("t") == "edges" and r.get("gap"))
    seg_end = Counter(r.get("end") for r in seg)
    seg_start = Counter(r.get("start") for r in seg)
    mid = sum(1 for r in seg if r.get("mid_byte_end"))
    bus_free = [r["bus_free_ns"] for r in seg if "bus_free_ns" in r]
    scl = [r["scl_hz"] for r in seg if r.get("scl_hz")]
    pst = Counter(t.status for t in e.packets)
    no_stop = sum(1 for t in e.packets if not any(k == "P" for _, k in t.events))
    sigs = Counter(packet_signature(t) for t in e.packets)
    span_ns = (e.packets[-1].t_end - e.packets[0].t_start) if e.packets else 0
    table = phase_table(e.packets)
    per_addr = {}
    for a, c in sorted(table.items()):
        s = summarise(c)
        per_addr[f"0x{a:02X}"] = {"summary": s, "phases": {k: v for k, v in sorted(c.items())}}
    allc = sum(table.values(), Counter())
    first, last = (status[0], status[-1]) if status else ({}, {})
    keys = ("dma_lost_buffers", "dropped_records", "waveform_dropped", "mid_byte_ends", "stream_dropped")
    return {
        "file": str(path),
        "records": dict(types), "unparsable_lines": bad,
        "engine": {"records_received": e.records_received, "transport_errors": e.transport_errors,
                   "lost_records": e.lost_records, "stream_gaps": e.stream_gaps,
                   "mid_byte_cuts": e.mid_byte_cuts, "fw_stats": dict(e.fw_stats)},
        "firmware_status_counters": {"first": {k: first.get(k) for k in keys},
                                     "last": {k: last.get(k) for k in keys},
                                     "decode_max_us_max": max((s.get("decode_max_us", 0) for s in status), default=None)},
        "loss_records": len(loss), "edge_batches_with_gap": gaps,
        "segments": {"count": len(seg), "start": dict(seg_start), "end": dict(seg_end), "mid_byte_end": mid},
        "packets": {"count": len(e.packets), "status": dict(pst), "without_stop": no_stop,
                    "span_s": span_ns / 1e9,
                    "per_s": (len(e.packets) / (span_ns / 1e9)) if span_ns else None,
                    "signatures": {f"0x{s[0]:02X} w{s[1]} r{s[2]}" if s else "none": n
                                   for s, n in sorted(sigs.items(), key=lambda kv: (kv[0] is None, kv[0] or (0, 0, 0)))}},
        "timing": {"scl_hz_median": statistics.median(scl) if scl else None,
                   "scl_hz_min": min(scl) if scl else None, "scl_hz_max": max(scl) if scl else None,
                   "bus_free_ns_min": min(bus_free) if bus_free else None,
                   "bus_free_ns_median": statistics.median(bus_free) if bus_free else None,
                   "bus_free_below_tBUF_4700ns": sum(1 for b in bus_free if b < 4700)},
        "phase_table": per_addr,
        "nack_categories": nack_categories(e.packets),
        "totals": summarise(allc),
    }, e


def master_summary(path):
    recs, bad = _jsonl(path)
    hello = next((r for r in recs if r.get("type") == "hello" and r.get("role") == "controller"), None)
    scl = [r for r in recs if r.get("type") == "scl_config"]
    st = [r for r in recs if r.get("type") == "master_stats"]
    txn = [r for r in recs if r.get("type") == "txn"]
    keys = ("txn_ok", "addr_nack", "data_nack", "arb_lost", "retries", "gave_up", "busy_waits",
            "busy_timeouts", "timeouts", "other_aborts", "recoveries", "recovery_skipped", "controller_resets")
    delta = {k: st[-1][k] - st[0][k] for k in keys} if len(st) >= 2 else {}
    span = (st[-1]["t_us"] - st[0]["t_us"]) / 1e6 if len(st) >= 2 else None
    by = Counter((f"0x{r['addr']:02X} w{r['wlen']} r{r['rlen']}", r["result"]) for r in txn)
    res = Counter(r["result"] for r in txn)
    dur = defaultdict(list)
    for r in txn:
        dur[f"0x{r['addr']:02X}"].append(r["dur_us"])
    return {
        "file": str(path), "unparsable_lines": bad,
        "hello": hello, "scl_config": scl[-1] if scl else None,
        "events": dict(Counter(r.get("type") for r in recs)),
        "master_stats_first": st[0] if st else None, "master_stats_last": st[-1] if st else None,
        "master_stats_delta": delta, "master_stats_span_s": span,
        "txn": {"count": len(txn), "results": dict(res),
                "multi_attempt": sum(1 for r in txn if r.get("attempts", 1) > 1),
                "by_signature": {f"{s} {r}": n for (s, r), n in sorted(by.items())},
                "dur_us_median": {a: statistics.median(v) for a, v in sorted(dur.items())},
                "dur_us_max": {a: max(v) for a, v in sorted(dur.items())}},
        "arb_lost_events": [r for r in recs if r.get("type") == "arb_lost"],
        "resets_recoveries": [r for r in recs if r.get("type") in ("controller_reset", "bus_recovery")],
    }, txn


def align_and_match(packets, txns, tol_us=1500.0):
    """Fit analyzer_ns ≈ a*master_us*1000 + b from unambiguous matches; return match statistics."""
    pk = [(t.t_start / 1e3, packet_signature(t), t) for t in packets]
    pk = [p for p in pk if p[1]]
    tx = [(r["t_us"], (r["addr"], r["wlen"], r["rlen"]), r) for r in txns if r["result"] == "ok"]
    if not pk or not tx:
        return None
    # coarse offset: vote over all pairs with an equal, rare signature
    rare = Counter(s for _, s, _ in tx)
    votes = Counter()
    for tu, s, _ in tx:
        if rare[s] > 400:
            continue
        for pu, ps, _ in pk:
            if ps == s:
                votes[round((pu - tu) / 500.0)] += 1
    if not votes:
        return None
    off = votes.most_common(1)[0][0] * 500.0
    pairs = []
    for tu, s, _ in tx:
        best = min((abs(pu - (tu + off)), pu) for pu, ps, _ in pk if ps == s) if any(ps == s for _, ps, _ in pk) else None
        if best and best[0] < 3000:
            pairs.append((tu, best[1]))
    if len(pairs) >= 2:
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 1.0
        b = my - a * mx
    else:
        a, b = 1.0, off
    return a, b


def attribute(packets, masters, tol_us=1500.0):
    fits, owner = {}, {}
    for mid, txns in masters.items():
        fit = align_and_match(packets, txns)
        fits[mid] = fit
    used = set()
    claimed = Counter()
    unmatched_txn = Counter()
    t0, t1 = packets[0].t_start / 1e3, packets[-1].t_end / 1e3
    by_sig = defaultdict(list)
    for i, t in enumerate(packets):
        s = packet_signature(t)
        if s:
            by_sig[s].append((t.t_start / 1e3, i))
    for mid, txns in masters.items():
        fit = fits[mid]
        if not fit:
            continue
        a, b = fit
        for r in txns:
            if r["result"] != "ok":
                continue
            pu = a * r["t_us"] + b
            if pu < t0 - 5000 or pu > t1 + 5000:
                continue                       # outside the analyzer window
            s = (r["addr"], r["wlen"], r["rlen"])
            cand = [(abs(u - pu), i) for u, i in by_sig.get(s, []) if i not in used and abs(u - pu) < tol_us]
            if cand:
                _, i = min(cand)
                used.add(i)
                owner[i] = mid
                claimed[(mid, f"0x{r['addr']:02X}")] += 1
            elif t0 <= pu <= t1:
                unmatched_txn[(mid, f"0x{r['addr']:02X} w{r['wlen']} r{r['rlen']}")] += 1
    unattributed = Counter(f"0x{packet_signature(t)[0]:02X}" if packet_signature(t) else "none"
                           for i, t in enumerate(packets) if i not in owner)
    # per master: raw NACKs on the bus, by phase category
    per_master = defaultdict(Counter)
    for i, t in enumerate(packets):
        m = owner.get(i, "unattributed")
        has_p = any(k == "P" for _, k in t.events)
        for fi, f in enumerate(t.frames):
            if f.addr is None:
                continue
            key = f"0x{f.addr:02X}"
            per_master[(m, key)]["frames_" + (f.rw or "?")] += 1
            boundary = fi + 1 < len(t.frames) or has_p
            for j, ack in enumerate(f.acks):
                if ack is False:
                    if f.rw == "R" and j == len(f.data) - 1:
                        per_master[(m, key)]["read_end_nack_normal" if boundary else "read_end_nack_unverified"] += 1
                    else:
                        per_master[(m, key)]["other_nack"] += 1
                elif ack is True:
                    per_master[(m, key)]["ack"] += 1
            if f.addr_ack is True:
                per_master[(m, key)]["ack"] += 1
            elif f.addr_ack is False:
                per_master[(m, key)]["addr_nack"] += 1
    return {
        "clock_fit": {str(k): ({"drift_ppm": (v[0] - 1) * 1e6, "offset_us": v[1]} if v else None) for k, v in fits.items()},
        "packets_attributed": {str(k): sum(1 for m in owner.values() if m == k) for k in masters},
        "packets_unattributed": dict(unattributed),
        "master_txn_in_window_not_seen_on_bus": {f"M{m} {s}": n for (m, s), n in sorted(unmatched_txn.items())},
        "per_master_address": {f"{'M' + str(m) if m != 'unattributed' else m} {a}": dict(c)
                               for (m, a), c in sorted(per_master.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyzer", required=True)
    ap.add_argument("--master", action="append", default=[])
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    an, eng = analyzer_summary(args.analyzer)
    out = {"analyzer": an, "masters": {}}
    tx_by = {}
    for p in args.master:
        ms, txn = master_summary(p)
        mid = (ms["hello"] or {}).get("master_id") or (ms["master_stats_last"] or {}).get("master_id")
        out["masters"][f"master_{mid}"] = ms
        tx_by[mid] = txn
    if tx_by and eng.packets:
        out["attribution"] = attribute(eng.packets, tx_by)
    txt = json.dumps(out, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
