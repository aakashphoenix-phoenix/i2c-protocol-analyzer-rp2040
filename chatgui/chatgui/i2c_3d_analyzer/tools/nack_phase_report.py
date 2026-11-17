"""Per-address, per-PHASE ACK/NACK report from real analyzer records.

Same decoder as the GUI (analysis.capture_engine).  Use it identically before and
after any firmware change so the two numbers are comparable.

  Live (GUI must be disconnected from the analyzer port first):
      python -X utf8 tools/nack_phase_report.py --port COM28 --seconds 60 --addr 0x57
  Saved GUI capture (.i2ccap from "Save capture"):
      python -X utf8 tools/nack_phase_report.py --capture path/to/file.i2ccap --addr 0x57
  Raw analyzer JSON lines (one record per line):
      python -X utf8 tools/nack_phase_report.py --jsonl records.jsonl --addr 0x57

Phases (UM10204 §3.1.5–3.1.10; AT24C32 uses a 2-byte memory address):
  addr-W / addr-R   9th clock after the address byte; the TARGET must pull SDA LOW.
  write[0], write[1] memory-address high/low byte for an AT24C32 write or
                     register-pointer set (TARGET acknowledges).
  write[n>=2]        data byte written (TARGET acknowledges).
  read[i]            9th clock after a byte the TARGET sent; the CONTROLLER drives it.
                     A NACK on the final read byte, followed by a captured STOP/Sr, is
                     the controller's normal end-of-read signal, NOT a target failure.
Nothing is filtered or corrected: every raw NACK is counted and attributed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def phase_table(packets, addr=None):
    """{address: Counter(...)} with every ACK slot attributed to a phase and a responder."""
    out = defaultdict(Counter)
    for t in packets:
        for fi, f in enumerate(t.frames):
            if f.addr is None or (addr is not None and f.addr != addr):
                continue
            c = out[f.addr]
            c["frames_" + (f.rw or "?")] += 1
            ph = "addr-" + (f.rw or "?")
            if f.addr_ack is True:
                c[ph + ":ACK"] += 1
            elif f.addr_ack is False:
                c[ph + ":NACK(target absent/busy)"] += 1
            boundary = fi + 1 < len(t.frames) or any(k == "P" for _, k in t.events)
            for i, ack in enumerate(f.acks):
                if f.rw == "W":
                    ph = f"write[{min(i, 2) if i < 2 else '>=2'}]" if i < 2 else "write[>=2]"
                    who = "target"
                else:
                    last = i == len(f.data) - 1
                    ph = "read[final]" if last else "read[not final]"
                    who = "controller"
                if ack is True:
                    c[f"{ph}:ACK({who})"] += 1
                elif ack is False:
                    if f.rw == "R" and i == len(f.data) - 1 and boundary:
                        c[f"{ph}:NACK(controller end-of-read, normal)"] += 1
                    elif f.rw == "R" and i == len(f.data) - 1:
                        c[f"{ph}:NACK(controller, STOP not captured)"] += 1
                    else:
                        c[f"{ph}:NACK({who}, unexpected)"] += 1
            if t.status != "ok":
                c["incomplete_frames"] += 1
        if addr is None or any(f.addr == addr for f in t.frames):
            for a in {f.addr for f in t.frames if f.addr is not None and (addr is None or f.addr == addr)}:
                out[a]["transactions"] += 1
    return out


def summarise(c):
    acks = sum(v for k, v in c.items() if ":ACK" in k)
    nacks = sum(v for k, v in c.items() if ":NACK" in k)
    slots = acks + nacks
    unexpected = sum(v for k, v in c.items() if ":NACK" in k and ("unexpected" in k or "target absent" in k))
    return dict(transactions=c["transactions"], acks=acks, nacks=nacks, slots=slots,
                nack_rate=(100 * nacks / slots) if slots else None,
                unexpected=unexpected, unexpected_rate=(100 * unexpected / slots) if slots else None)


def trace_lines(packets, addr, limit=6):
    rows = []
    for t in packets:
        if not any(f.addr == addr for f in t.frames):
            continue
        parts = []
        for f in t.frames:
            ab = ((f.addr << 1) | (f.rw == "R")) & 0xFF if f.addr is not None else None
            a = {True: "A", False: "N", None: "?"}[f.addr_ack]
            data = " ".join(f"{d:02X}{'A' if k else 'N'}" for d, k in zip(f.data, f.acks))
            parts.append(f"{f.start_kind} {ab:02X}({'R' if f.rw == 'R' else 'W'}) {a} {data}".rstrip())
        tail = " P" if any(k == "P" for _, k in t.events) else " (no STOP captured)"
        rows.append(f"#{t.id} t={t.t_start / 1e9:.6f}s  " + " | ".join(parts) + tail + f"  [{t.status}]")
        if len(rows) >= limit:
            break
    return rows


def load_lines(args):
    if args.capture:
        from storage.capture_storage import load_capture
        meta, lines = load_capture(args.capture)
        return lines, f"saved capture {args.capture} (simulated={meta.get('simulated')})"
    if args.jsonl:
        return Path(args.jsonl).read_bytes().splitlines(), f"JSON lines {args.jsonl}"
    import serial
    s = serial.Serial(args.port, 115200, timeout=0.1)
    s.dtr = True
    time.sleep(0.3)
    s.write(b"HELLO\n"); time.sleep(0.2); s.write(b"START\n")
    buf, t0 = b"", time.time()
    while time.time() - t0 < args.seconds:
        buf += s.read(s.in_waiting or 1)
    s.write(b"STOP\n"); time.sleep(0.2)
    buf += s.read(s.in_waiting or 0)
    s.close()
    lines = buf.splitlines()
    if args.save:
        Path(args.save).write_bytes(buf)
    return lines, f"live analyzer {args.port} for {args.seconds:g} s"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--port"); src.add_argument("--capture"); src.add_argument("--jsonl")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--addr", type=lambda v: int(v, 0), default=None)
    ap.add_argument("--save", help="with --port: also save the raw analyzer lines to this file")
    args = ap.parse_args(argv)

    from analysis.capture_engine import CaptureEngine
    lines, what = load_lines(args)
    e = CaptureEngine(); e.set_source("replay" if not args.port else "live")
    e.ingest_lines([ln for ln in lines if ln.strip()])
    print(f"Source: {what} · records {e.records_received} · rejected {e.transport_errors} · "
          f"lost {e.lost_records} · packets {len(e.packets)}")
    if e.lost_records or e.transport_errors or e.fw_stats.get("overflows"):
        print("WARNING: capture had losses/errors — counts are lower bounds for the affected interval")
    table = phase_table(e.packets, args.addr)
    allc = sum(table.values(), Counter())
    if args.addr is not None and args.addr not in table:
        print(f"\n0x{args.addr:02X}  transactions 0 — nothing addressed this device in the capture "
              "(no ACK or NACK to report; this is not evidence that the device works or fails)")
    for a in sorted(table):
        c = table[a]; s = summarise(c)
        rate = f"{s['nack_rate']:.2f}%" if s["nack_rate"] is not None else "UNKNOWN"
        urate = f"{s['unexpected_rate']:.2f}%" if s["unexpected_rate"] is not None else "UNKNOWN"
        print(f"\n0x{a:02X}  transactions {s['transactions']} · ACK {s['acks']} · NACK {s['nacks']} · "
              f"NACK rate {rate} · unexpected NACK {s['unexpected']} ({urate})")
        for k in sorted(k for k in c if ":" in k):
            print(f"    {k:<55} {c[k]}")
        if args.addr is not None:
            print("  trace (first transactions):")
            for row in trace_lines(e.packets, a):
                print("    " + row)
    if args.addr is None:
        s = summarise(allc)
        print(f"\nALL  ACK {s['acks']} · NACK {s['nacks']} · unexpected {s['unexpected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
