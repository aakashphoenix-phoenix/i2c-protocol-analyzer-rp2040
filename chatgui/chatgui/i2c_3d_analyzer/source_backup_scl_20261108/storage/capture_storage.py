"""Saving, loading and exporting captures.

A capture file (.i2ccap) is JSON Lines:
  line 1   {"capture": {...metadata...}}
  line 2+  the analyzer's protocol records exactly as received
Because the raw records are kept, reloading re-runs the same decoder and gives
identical packets and timestamps; nothing measured is altered on replay.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json

import config
from analysis.performance_calculator import PerfSnapshot
from analysis.communication_health import acknowledgment_counts


def save_capture(path, engine, notes: str = "") -> int:
    s = engine.perf.compute()
    meta = {
        "format": "i2ccap", "format_version": 1,
        "app_version": config.APP_VERSION,
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "capture_started": engine.capture_wall_start.isoformat(timespec="seconds") if engine.capture_wall_start else None,
        "source": engine.source,
        "simulated": engine.source == "demo",
        "analyzer": {"firmware": engine.caps.firmware, "board": engine.caps.board, "protocol": engine.caps.proto,
                     "sample_hz": engine.caps.sample_hz, "tick_ns": engine.caps.tick_ns,
                     "caps": sorted(engine.caps.caps)},
        "loss": {"lost_records": engine.lost_records, "fw_overflows": engine.fw_stats["overflows"],
                 "fw_dropped": engine.fw_stats["dropped"], "transport_errors": engine.transport_errors,
                 "raw_lines_truncated": len(engine.raw_lines) == engine.raw_lines.maxlen},
        "summary": {"transactions": s.total, "complete": s.complete, "observed_s": s.observed_ns / 1e9},
        "oled_address": engine.sensors.oled_addr,
        "notes": notes,
    }
    n = 0
    with open(path, "wb") as f:
        f.write(json.dumps({"capture": meta}).encode() + b"\n")
        for ln in engine.raw_lines:
            f.write(ln.rstrip(b"\r\n") + b"\n")
            n += 1
    return n


def load_capture(path):
    """Returns (metadata dict, list of raw line bytes)."""
    with open(path, "rb") as f:
        first = f.readline()
        try:
            meta = json.loads(first).get("capture")
        except (json.JSONDecodeError, AttributeError):
            meta = None
        if not isinstance(meta, dict):
            raise ValueError("not an I²C capture file (missing metadata header)")
        lines = [ln.rstrip(b"\r\n") for ln in f if ln.strip()]
    return meta, lines


def export_packets_csv(path, engine, packets=None) -> int:
    packets = engine.packets if packets is None else packets
    t0 = packets[0].t_start if packets else 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["packet", "hw_time_s", "address", "device", "name_confidence", "addressing", "operation",
                    "data_hex", "bytes", "ack_nack", "events", "duration_us", "status", "notes", "simulated", "source", "start_ns", "end_ns", "address_ack", "data_acks", "scl_frequency_hz", "raw_nacks", "expected_read_end_nacks", "unexpected_nacks"])
        for t in packets:
            name, conf = engine.registry.name(t.address)
            w.writerow([t.id, f"{(t.t_start - t0) / 1e9:.7f}",
                        "" if t.address is None else f"0x{t.address:02X}", name, conf,
                        "10-bit" if t.ten_bit else "7-bit", t.operation, t.data_hex(),
                        t.byte_count, " ".join("A" if b.ack else "N" for b in t.bytes),
                        t.event_seq(), f"{t.duration_ns / 1e3:.2f}", t.status, "; ".join(t.notes),
                        "yes" if t.demo else "no", t.source.upper(), t.t_start, t.t_end,
                        json.dumps([f.addr_ack for f in t.frames]), json.dumps([f.acks for f in t.frames]),
                        t.timing.get('frequency_hz'), acknowledgment_counts(t)['raw_nacks'],
                        acknowledgment_counts(t)['expected_read_nacks'], acknowledgment_counts(t)['unexpected_nacks']])
    return len(packets)


def export_packets_json(path, engine) -> int:
    t0 = engine.packets[0].t_start if engine.packets else 0
    out = []
    for t in engine.packets:
        out.append({
            "packet": t.id, "hw_time_s": (t.t_start - t0) / 1e9, "duration_us": t.duration_ns / 1e3,
            "status": t.status, "simulated": t.demo, "source": t.source, "start_ns": t.t_start, "end_ns": t.t_end,
            "timing": t.timing, "acknowledgments": acknowledgment_counts(t),
            "frames": [{"start": f.start_kind, "address": f.addr, "rw": f.rw, "ten_bit": f.ten_bit,
                        "address_ack": f.addr_ack, "data": f.data, "acks": f.acks} for f in t.frames],
            "events": [{"t_s": (tt - t0) / 1e9, "kind": k} for tt, k in t.events],
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"source": engine.source, "packets": out}, f, indent=1)
    return len(out)


def export_statistics_csv(path, engine) -> None:
    s: PerfSnapshot = engine.perf.compute()
    rows = [
        ("source", engine.source), ("simulated", engine.source == "demo"),
        ("observed_time_s", s.observed_ns / 1e9), ("busy_time_s", s.busy_ns / 1e9),
        ("bus_utilization_pct", s.util), ("transactions_total", s.total), ("complete", s.complete),
        ("incomplete", s.incomplete), ("malformed", s.malformed), ("transaction_rate_per_s", s.tps),
        ("scl_freq_avg_hz", s.freq_avg_hz), ("scl_freq_min_hz", s.freq_min_hz), ("scl_freq_max_hz", s.freq_max_hz),
        ("t_high_avg_ns", s.t_high_ns), ("t_low_avg_ns", s.t_low_ns),
        ("duration_avg_us", s.dur_avg_ns and s.dur_avg_ns / 1e3), ("duration_min_us", s.dur_min_ns and s.dur_min_ns / 1e3),
        ("duration_max_us", s.dur_max_ns and s.dur_max_ns / 1e3), ("bytes_total", s.total_bytes),
        ("acks", s.acks), ("nacks", s.nacks), ("address_nacks", s.addr_nacks),
        ("read_end_nacks", s.read_end_nacks), ("write_data_nacks", s.write_data_nacks),
        ("capture_gaps", s.gaps), ("lost_records", engine.lost_records),
        ("fw_overflows", engine.fw_stats["overflows"]), ("fw_dropped_samples", engine.fw_stats["dropped"]),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(rows)
        w.writerow([])
        w.writerow(["address", "device", "confidence", "transactions", "reads", "writes", "bytes", "acks", "nacks"])
        for a, d in sorted(engine.registry.devices.items()):
            n, c = engine.registry.name(a)
            w.writerow([f"0x{a:02X}", n, c, d.transactions, d.reads, d.writes, d.total_bytes, d.acks, d.nacks])
