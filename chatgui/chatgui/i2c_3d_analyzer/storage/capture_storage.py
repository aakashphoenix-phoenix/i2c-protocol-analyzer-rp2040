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
import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from analysis import scl_timing

import config
from analysis.performance_calculator import PerfSnapshot
from analysis.communication_health import acknowledgment_counts
from analysis.multi_master import attribution_detail, attribution_text

STATUS_TEXT = {"ok": "Complete", "incomplete": "Incomplete", "malformed": "Malformed", "gap": "Capture gap"}
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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


PACKET_EXPORT_HEADERS = [
    "packet", "hw_time_s", "address", "device", "name_confidence", "addressing", "operation",
    "data_hex", "bytes", "ack_nack", "events", "duration_us", "status", "notes", "simulated", "source",
    "start_ns", "end_ns", "address_ack", "data_acks", "scl_frequency_hz", "raw_nacks",
    "expected_read_end_nacks", "unexpected_nacks", "address_bytes_8bit", "scl_frequency_source",
    "scl_frequency_scope", "scl_cycles_used", "scl_cycles_excluded", "scl_period_ns", "scl_high_ns",
    "scl_low_ns", "start_event_ns", "stop_event_ns", "gap_from_previous_stop_ns", "long_scl_low_count",
    "master_id", "master_attribution",
]


def _packet_export_rows(engine, packets=None):
    packets = engine.packets if packets is None else packets
    t0 = packets[0].t_start if packets else 0
    rows = []
    for t in packets:
        name, conf = engine.registry.name(t.address)
        ack = acknowledgment_counts(t)
        rows.append([
            t.id, f"{(t.t_start - t0) / 1e9:.7f}",
            "" if t.address is None else f"0x{t.address:02X}", name, conf,
            "10-bit" if t.ten_bit else "7-bit", t.operation, t.data_hex(),
            t.byte_count, " ".join("A" if b.ack else "N" for b in t.bytes),
            t.event_seq(), f"{t.duration_ns / 1e3:.2f}", t.status, "; ".join(t.notes),
            "yes" if t.demo else "no", t.source.upper(), t.t_start, t.t_end,
            json.dumps([f.addr_ack for f in t.frames]), json.dumps([f.acks for f in t.frames]),
            t.timing.get('frequency_hz'), ack['raw_nacks'],
            ack['expected_read_nacks'], ack['unexpected_nacks'],
            " ".join(f"0x{v:02X}" for v in (scl_timing.address_byte(f) for f in t.frames) if v is not None),
            t.timing.get('frequency_source', 'UNKNOWN'), t.timing.get('frequency_scope', 'UNKNOWN'),
            t.timing.get('cycles_used'), t.timing.get('cycles_excluded'), t.timing.get('period_ns'),
            t.timing.get('high_ns'), t.timing.get('low_ns'), t.timing.get('start_ns'),
            t.timing.get('stop_ns'), t.timing.get('bus_free_ns'), len(t.timing.get('long_low_ns') or []),
            t.master_id if t.master_id is not None else "", t.master_method or "unattributed",
        ])
    return rows


def export_packets_csv(path, engine, packets=None) -> int:
    rows = _packet_export_rows(engine, packets=packets)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PACKET_EXPORT_HEADERS)
        w.writerows(rows)
    return len(rows)


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


# ====================================================================== session summary report (HTML / Markdown)
SOURCE_LABELS = {"live": "LIVE hardware capture", "demo": "DEMO · SIMULATED", "replay": "REPLAY of a recorded capture",
                 "none": "no data source"}


def _hz(v):
    return "UNAVAILABLE — no measured SCL cycles" if v is None else f"{v / 1e3:.3f} kHz ({v:,.0f} Hz)"


def _ns(v):
    if v is None:
        return "UNAVAILABLE — no complete transaction measured"
    if v >= 1e9:
        return f"{v / 1e9:.3f} s"
    if v >= 1e6:
        return f"{v / 1e6:.3f} ms"
    if v >= 1e3:
        return f"{v / 1e3:.2f} µs"
    return f"{v:.0f} ns"


def _pct(v, digits=2):
    return "UNAVAILABLE — no ACK slots observed" if v is None else f"{v:.{digits}f} %"


def build_session_report(engine, session_id: str = "") -> dict:
    """Everything the report shows, taken ONLY from the CaptureEngine.  Nothing is estimated or invented: a value that
    was not measured is written as UNAVAILABLE with the reason.

    Returns {"title", "banner" (str | None), "demo", "source", "generated", "sections": [(title, [block, ...])]} where a
    block is ("kv", [(key, value)]) | ("table", headers, rows) | ("note", text)."""
    import time as _time
    e = engine
    demo = e.source == "demo"
    s = e.perf.compute()
    caps = e.caps
    trusted, trust_reason = e.measurement_trust()
    banner = ("DEMO · SIMULATED — NOT VERIFIED HARDWARE: every number below comes from the built-in simulator."
              if demo else "RECORDED CAPTURE — NOT LIVE TRAFFIC: decoded from a saved file." if e.source == "replay" else None)
    start = e.capture_wall_start
    sections = []

    # ---------------------------------------------------------------- 1. session overview
    kv = [("Session ID", session_id or "—"),
          ("Source", SOURCE_LABELS.get(e.source, e.source)),
          ("Capture started (PC wall clock)", start.isoformat(timespec="seconds") if start else "unknown (no capture started)"),
          ("Capture duration (hardware time)", _ns(s.observed_ns) if s.observed_ns else "UNAVAILABLE — nothing captured"),
          ("Measurement trust", ("verified: " if trusted else "NOT verified: ") + trust_reason)]
    if caps.known:
        kv += [("Analyzer firmware", f"{caps.firmware} (protocol v{caps.proto})"), ("Analyzer board", caps.board),
               ("Sample rate", f"{caps.sample_hz / 1e6:g} MS/s" + (f" · tick {caps.tick_ns:g} ns" if caps.tick_ns else "")),
               ("Capabilities", ", ".join(sorted(caps.caps)) or "none reported")]
    else:
        kv += [("Analyzer firmware", "UNAVAILABLE — no analyzer handshake received"), ("Capabilities", "unknown")]
    kv += [("Records received", f"{e.records_received:,}"), ("Transport / framing errors", f"{e.transport_errors:,}"),
           ("Lost records (sequence gaps)", f"{e.lost_records:,}"),
           ("Analyzer dropped samples", f"{e.fw_stats['dropped']:,}"),
           ("Analyzer buffer overflows", f"{e.fw_stats['overflows']:,}"),
           ("Analyzer buffer fill", "UNAVAILABLE — not reported" if e.fw_stats.get("buf_pct") is None else f"{e.fw_stats['buf_pct']} %"),
           ("Packets decoded / kept in memory", f"{s.total:,} / {len(e.packets):,}"
            + (f" ({e.packets_dropped_from_memory:,} oldest trimmed)" if e.packets_dropped_from_memory else ""))]
    sections.append(("Session overview", [("kv", kv)]))

    # ---------------------------------------------------------------- 2. bus health & timing
    h = e.health_summary()
    c = h["counts"]
    blocks = []
    state = h["state"] + (f" (SIMULATED policy result: {h['simulated_state']} — not verified hardware)" if h["simulated"] else "")
    kv = [("Communication health (rolling 60 s)", state),
          ("Why", h.get("reason") or h.get("detail", "")),
          ("ACK slots in window", f"{c['opportunities']:,}"),
          ("ACK / raw NACK in window", f"{c['acks']:,} / {c['raw_nacks']:,} (raw NACK {_pct(c['raw_nack_pct'])})"),
          ("Expected end-of-read NACK in window", f"{c['expected_read_nacks']:,}"),
          ("Unexpected NACK in window", f"{c['unexpected_nacks']:,} (unexpected NACK {_pct(c['unexpected_nack_pct'])})"),
          ("Unverified end-of-read NACK / incomplete", f"{c['unverified_read_end_nacks']:,} / {c['incomplete']:,}")]
    blocks.append(("kv", kv))
    if not s.has_data:
        blocks.append(("note", "No complete transaction was captured: timing and rates below are UNAVAILABLE, not zero."))
    blocks.append(("kv", [
        ("Transactions (total / complete / incomplete / malformed)", f"{s.total:,} / {s.complete:,} / {s.incomplete:,} / {s.malformed:,}"),
        ("Reads / writes", f"{s.reads:,} / {s.writes:,}"),
        ("Bytes transferred", f"{s.total_bytes:,}"),
        ("ACK (session)", f"{s.acks:,}"),
        ("Raw NACK (session)", f"{s.nacks:,}"),
        ("  · expected end-of-read NACK", f"{s.read_end_nacks:,}"),
        ("  · unexpected NACK", f"{s.unexpected_nacks:,} (address {s.addr_nacks:,}, written byte {s.write_data_nacks:,})"),
        ("  · unverified end-of-read NACK (STOP not captured)", f"{s.unverified_read_end_nacks:,}"),
        ("Bus utilization (cumulative)", _pct(s.util, 3)),
        ("Transactions per second", "UNAVAILABLE" if s.tps is None else f"{s.tps:.2f} /s"),
        ("Measured SCL frequency — average", _hz(s.freq_avg_hz)),
        ("Measured SCL frequency — minimum", _hz(s.freq_min_hz)),
        ("Measured SCL frequency — maximum", _hz(s.freq_max_hz)),
        ("SCL HIGH time (average)", _ns(s.t_high_ns)),
        ("SCL LOW time (average)", _ns(s.t_low_ns)),
        ("Transaction duration (avg / min / max)", " / ".join(_ns(v) for v in (s.dur_avg_ns, s.dur_min_ns, s.dur_max_ns))),
        ("Observed / busy / idle time", f"{_ns(s.observed_ns)} / {_ns(s.busy_ns)} / {_ns(s.idle_ns)}" if s.observed_ns else "UNAVAILABLE"),
        ("Capture gaps", f"{s.gaps:,}")]))
    sections.append(("Bus health & timing", blocks))

    # ---------------------------------------------------------------- 3. masters & arbitration
    mm = e.masters
    summ = mm.summary()
    unattributed = max(0, summ["packets"] - summ["attributed"])
    blocks = [("kv", [("Bus packets seen", f"{summ['packets']:,}"),
                      ("Attributed to a master", f"{summ['attributed']:,}"), ("Unattributed", f"{unattributed:,}"),
                      ("Attribution methods", ", ".join(f"{k}: {v:,}" for k, v in sorted(summ["methods"].items()) if v) or "none"),
                      ("Arbitration losses reported (total)", f"{mm.arb_total:,}"),
                      ("  · retried by the loser", f"{sum(1 for ev in mm.arb_events if ev.retry):,}"),
                      ("  · gave up (transfer skipped)", f"{sum(1 for ev in mm.arb_events if not ev.retry):,}"),
                      ("Segments cut inside a byte (abort/recovery/collision)", f"{e.mid_byte_cuts:,}")])]
    if demo:
        blocks.append(("note", "SIMULATED demo: one simulated master; there is no real controller telemetry."))
    rows = []
    for r in mm.rows():
        st = r["stats"]
        telemetry = (f"ok {st['txn_ok']:,} · addr NACK {st['addr_nack']:,} · data NACK {st['data_nack']:,} · "
                     f"arb lost {st['arb_lost']:,} (retries {st['retries']:,}, gave up {st['gave_up']:,}) · "
                     f"timeouts {st['timeouts']:,} · recoveries {st['recoveries']:,}") if r["have_stats"] else \
            "UNAVAILABLE — no controller telemetry received"
        rows.append([r["label"], r["port"] if r["connected"] else "not connected", f"{r['state']} — {r['detail']}",
                     f"{r['bus_packets']:,}", telemetry])
    blocks.append(("table", ["Master", "COM port", "Bus-access health", "Bus packets attributed", "Controller telemetry (session)"], rows))
    sections.append(("Master 1 vs Master 2 & arbitration", blocks))

    # ---------------------------------------------------------------- 4. devices
    reg = e.registry
    rows = []
    for a, d in sorted(reg.devices.items()):
        name, conf = reg.name(a)
        rows.append([f"0x{a:02X}", name, conf + (f" ({d.confirm_detail})" if d.confirm_detail else ""),
                     f"{d.transactions:,}", f"{d.reads:,}", f"{d.writes:,}", f"{d.total_bytes:,}", f"{d.acks:,}",
                     f"{d.nacks:,}"])
    blocks = [("table", ["Address", "Device", "Confidence", "Transactions", "Reads", "Writes", "Bytes", "ACK", "NACK"], rows)] if rows \
        else [("note", "No device address was observed on the bus.")]
    blocks.append(("note", "Confidence: Confirmed = an ID register read matched · Configured = what this bench is wired as · "
                           "Likely = common part at that address · Unknown = not in the database."))
    sections.append(("Devices observed", blocks))

    # ---------------------------------------------------------------- 5. decoded sensor readings
    sen = e.sensors
    sim = " (SIMULATED)" if demo else ""
    kv = []
    if sen.rtc.time is not None:
        hh, mi, ss, wd, dd, mo, yy = sen.rtc.time.value
        kv.append(("DS3231 RTC (latest decoded read)", f"{hh:02d}:{mi:02d}:{ss:02d} on {yy:04d}-{mo:02d}-{dd:02d}{sim} · raw {sen.rtc.time.raw}"))
    else:
        kv.append(("DS3231 RTC", f"UNAVAILABLE — {sen.rtc.reason}"))
    kv.append(("DS3231 valid time reads", f"{sen.rtc.reads:,}"))
    if sen.bmp.temperature is not None:
        press = f"{sen.bmp.pressure.value:.2f} hPa" if sen.bmp.pressure is not None else "pressure UNAVAILABLE"
        kv.append(("BMP280 (latest compensated read)", f"{sen.bmp.temperature.value:.2f} °C · {press}{sim} · raw {sen.bmp.temperature.raw}"))
    else:
        kv.append(("BMP280", f"UNAVAILABLE — {sen.bmp.reason}"))
    kv.append(("BMP280 calibration (0x88–0x9F)", "captured" if sen.bmp.calib else "not captured"))
    o = sen.oled
    kv += [("SSD1306 OLED address", f"0x{sen.oled_addr:02X}"), ("OLED write transactions", f"{o.writes:,}"),
           ("OLED frames completed", f"{o.frame_counter:,}"),
           ("OLED command / data bytes", f"{o.cmd_bytes:,} / {o.data_bytes:,}"),
           ("OLED framebuffer coverage", f"{100 * o.coverage:.1f} %"),
           ("OLED display state", {True: "ON (0xAF seen)", False: "OFF (0xAE seen)", None: "unknown"}[o.display_on]),
           ("OLED address NACKs", f"{o.nacks:,}")]
    sections.append(("Decoded sensor readings", [("kv", kv),
                    ("note", "Values are decoded from bytes observed on the bus only; a missing value is shown with its reason.")]))

    return dict(title="I²C Session Report" + (" — DEMO · SIMULATED" if demo else ""), banner=banner, demo=demo,
                source=e.source, session_id=session_id or "—",
                generated=_dt.datetime.now().isoformat(timespec="seconds"), sections=sections)


def render_report_markdown(rep: dict) -> str:
    def cell(x):
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = [f"# {rep['title']}", ""]
    if rep["banner"]:
        out += [f"> **{rep['banner']}**", ""]
    out += [f"Session `{rep['session_id']}` · generated {rep['generated']} by {config.APP_NAME} v{config.APP_VERSION}", ""]
    for title, blocks in rep["sections"]:
        out += [f"## {title}", ""]
        for b in blocks:
            if b[0] == "kv":
                out += ["| Metric | Value |", "| --- | --- |"] + [f"| {cell(k)} | {cell(v)} |" for k, v in b[1]] + [""]
            elif b[0] == "table":
                out += ["| " + " | ".join(cell(x) for x in b[1]) + " |", "| " + " | ".join("---" for _ in b[1]) + " |"]
                out += ["| " + " | ".join(cell(x) for x in row) + " |" for row in b[2]] + [""]
            else:
                out += [f"_{b[1]}_", ""]
    out += ["---", "All values are taken directly from the capture engine; nothing is estimated. "
                   "Unmeasured values are marked UNAVAILABLE with the reason.", ""]
    return "\n".join(out)


REPORT_CSS = """
:root { color-scheme: light; }
body { font: 14px/1.5 "Segoe UI", Arial, sans-serif; color: #0F172A; background: #FFFFFF; margin: 0; padding: 28px 36px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #CBD5E1; }
.meta { color: #475569; margin-bottom: 14px; }
.banner { border: 2px solid #C2410C; background: #FFEDD5; color: #7C2D12; font-weight: 700; padding: 10px 14px; border-radius: 6px; margin: 12px 0; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px; } th, td { border: 1px solid #CBD5E1; padding: 5px 9px; text-align: left; vertical-align: top; }
th { background: #E2E8F0; } tr:nth-child(even) td { background: #F8FAFC; } td.k { width: 36%; color: #334155; }
td.v { font-family: Consolas, "Cascadia Mono", monospace; } .note { color: #475569; font-style: italic; margin: 4px 0 10px; }
footer { margin-top: 28px; padding-top: 8px; border-top: 1px solid #CBD5E1; color: #475569; font-size: 12px; }
@media print { body { padding: 0; } h2 { break-after: avoid; } table { break-inside: auto; } }
"""


def render_report_html(rep: dict) -> str:
    import html as _h
    esc = lambda x: _h.escape(str(x))
    parts = ["<!DOCTYPE html>", '<html lang="en"><head><meta charset="utf-8">',
             f"<title>{esc(rep['title'])}</title>", f"<style>{REPORT_CSS}</style></head><body>",
             f"<h1>{esc(rep['title'])}</h1>",
             f'<div class="meta">Session <b>{esc(rep["session_id"])}</b> · generated {esc(rep["generated"])} by '
             f"{esc(config.APP_NAME)} v{esc(config.APP_VERSION)}</div>"]
    if rep["banner"]:
        parts.append(f'<div class="banner">{esc(rep["banner"])}</div>')
    for title, blocks in rep["sections"]:
        parts.append(f"<h2>{esc(title)}</h2>")
        for b in blocks:
            if b[0] == "kv":
                parts.append("<table>" + "".join(f'<tr><td class="k">{esc(k)}</td><td class="v">{esc(v)}</td></tr>'
                                                 for k, v in b[1]) + "</table>")
            elif b[0] == "table":
                parts.append("<table><tr>" + "".join(f"<th>{esc(x)}</th>" for x in b[1]) + "</tr>"
                             + "".join("<tr>" + "".join(f"<td>{esc(x)}</td>" for x in row) + "</tr>" for row in b[2]) + "</table>")
            else:
                parts.append(f'<p class="note">{esc(b[1])}</p>')
    parts.append("<footer>All values are taken directly from the capture engine; nothing is estimated. "
                 "Unmeasured values are marked UNAVAILABLE with the reason.</footer></body></html>")
    return "\n".join(parts)


def export_session_report(path, engine, session_id: str = "", fmt: str | None = None) -> str:
    """One-click summary of a session as a self-contained HTML (.html) or Markdown (.md) file.

    fmt: "html" | "md" (also "markdown"); None picks it from the file extension (.md/.markdown -> Markdown, anything
    else -> HTML).  Only metrics measured by `engine` are included, DEMO captures are labelled DEMO · SIMULATED.
    Returns the format that was written ("html" or "md")."""
    import os
    if fmt is None:
        fmt = "md" if os.path.splitext(str(path))[1].lower() in (".md", ".markdown") else "html"
    fmt = {"markdown": "md", "htm": "html"}.get(fmt.lower().lstrip("."), fmt.lower().lstrip("."))
    if fmt not in ("html", "md"):
        raise ValueError(f"unsupported report format {fmt!r} (use 'html' or 'md')")
    rep = build_session_report(engine, session_id)
    text = render_report_markdown(rep) if fmt == "md" else render_report_html(rep)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return fmt


# ====================================================================== in-tab preview helpers
def preview_packets_from_engine(engine, max_packets: int = 2000) -> list[dict]:
    """Build bounded preview rows from `engine.packets` without mutating `engine`."""
    packets = engine.packets
    if not packets:
        return []
    t0 = packets[0].t_start
    out = []
    for t in packets[:max_packets]:
        name, conf = engine.registry.name(t.address)
        addr = "—" if t.address is None else (f"0x{t.address:03X}" if t.ten_bit else f"0x{t.address:02X}")
        ack_counts = acknowledgment_counts(t)
        out.append({
            "id": t.id,
            "time_s": (t.t_start - t0) / 1e9,
            "time_str": f"{(t.t_start - t0) / 1e9:.6f}",
            "addr_int": t.address,
            "addr": addr,
            "device": name,
            "confidence": conf,
            "op": t.operation,
            "data_hex": t.data_hex(12) or "—",
            "full_data_hex": t.data_hex() or "—",
            "ack_nack": t.ack_seq() or "—",
            "ack_counts": ack_counts,
            "master": attribution_text(t),
            "master_id": t.master_id,
            "master_detail": attribution_detail(t, engine.masters),
            "status": STATUS_TEXT.get(t.status, t.status) + (" · DEMO" if t.demo else ""),
            "raw_status": t.status,
            "demo": bool(t.demo),
            "has_read": bool(t.has_read),
            "has_write": bool(t.has_write),
            "nack_count": int(t.nack_count),
            "addr_nack": bool(t.addr_nack),
            "duration_ns": t.duration_ns,
            "notes": "; ".join(t.notes),
            "semantic": getattr(t, "semantic", "") or "",
        })
    return out


def preview_capture_file(path, max_packets: int = 2000, max_lines: int = 25000) -> dict:
    """Decode an .i2ccap file inside an isolated local CaptureEngine for in-tab preview.

    Never touches or mutates any live CaptureEngine instance."""
    from analysis.capture_engine import CaptureEngine

    meta, lines = load_capture(path)
    preview_engine = CaptureEngine()
    preview_engine.set_source("demo" if meta.get("simulated") else "replay")
    if meta.get("oled_address"):
        try:
            preview_engine.set_oled_address(int(meta["oled_address"]))
        except (TypeError, ValueError):
            pass

    limit = min(len(lines), max_lines)
    decoded_lines = 0
    for i in range(0, limit, 2000):
        batch = lines[i:min(i + 2000, limit)]
        preview_engine.ingest_lines(batch)
        decoded_lines += len(batch)
        if len(preview_engine.packets) >= max_packets:
            break

    perf = preview_engine.perf.compute()
    packet_rows = preview_packets_from_engine(preview_engine, max_packets=max_packets)
    devices = [
        {
            "address": f"0x{a:02X}",
            "addr_int": a,
            "name": preview_engine.registry.name(a)[0],
            "confidence": preview_engine.registry.name(a)[1],
            "transactions": d.transactions,
            "reads": d.reads,
            "writes": d.writes,
            "bytes": d.total_bytes,
            "acks": d.acks,
            "nacks": d.nacks,
        }
        for a, d in sorted(preview_engine.registry.devices.items())
    ]
    return {
        "meta": meta,
        "total_raw_lines": len(lines),
        "decoded_lines": decoded_lines,
        "packets": packet_rows,
        "total_packets_decoded": len(preview_engine.packets),
        "truncated": decoded_lines < len(lines) or len(preview_engine.packets) > max_packets,
        "devices": devices,
        "perf": {
            "total": perf.total,
            "complete": perf.complete,
            "incomplete": perf.incomplete,
            "malformed": perf.malformed,
            "observed_s": perf.observed_ns / 1e9,
            "freq_avg_hz": perf.freq_avg_hz,
            "acks": perf.acks,
            "nacks": perf.nacks,
            "unexpected_nacks": perf.unexpected_nacks,
        },
    }


# ====================================================================== Excel (.xlsx) export & preview
def _excel_cell(val):
    if val is None or isinstance(val, (bool, int)):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else "UNAVAILABLE"
    return _ILLEGAL_XML_RE.sub("", str(val))


def _col_letter(idx: int) -> str:
    out = []
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def _write_xlsx_fallback(path, sheets: list[tuple[str, list[list]]]) -> None:
    """Minimal OOXML .xlsx writer using only stdlib zipfile + xml.etree.ElementTree."""
    from xml.sax.saxutils import escape as _xml_escape

    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    wb_sheets = []
    wb_rels = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        for idx, (title, rows) in enumerate(sheets, start=1):
            safe_title = _xml_escape(title[:31])
            wb_sheets.append(f'<sheet name="{safe_title}" sheetId="{idx}" r:id="rId{idx}"/>')
            wb_rels.append(
                f'<Relationship Id="rId{idx}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{idx}.xml"/>'
            )
            content_types.append(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
            xml_rows = []
            for r_idx, row in enumerate(rows, start=1):
                cells = []
                for c_idx, raw in enumerate(row, start=1):
                    v = _excel_cell(raw)
                    if v is None:
                        continue
                    ref = f"{_col_letter(c_idx)}{r_idx}"
                    if isinstance(v, bool):
                        cells.append(f'<c r="{ref}" t="b"><v>{1 if v else 0}</v></c>')
                    elif isinstance(v, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                    else:
                        cells.append(
                            f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_escape(v)}</t></is></c>'
                        )
                xml_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
            zf.writestr(
                f"xl/worksheets/sheet{idx}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
            )
        content_types.append("</Types>")
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(wb_sheets)}</sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{"".join(wb_rels)}</Relationships>',
        )


def _read_xlsx_fallback(path, max_rows: int = 500) -> dict:
    """Minimal OOXML .xlsx reader using only stdlib zipfile + xml.etree.ElementTree."""
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheets_out = []
    with zipfile.ZipFile(path, "r") as zf:
        shared_strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{{{ns_main}}}si"):
                shared_strings.append("".join(t.text or "" for t in si.iter(f"{{{ns_main}}}t")))

        rels = {}
        if "xl/_rels/workbook.xml.rels" in zf.namelist():
            rroot = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rroot:
                rid = rel.attrib.get("Id")
                target = rel.attrib.get("Target", "").lstrip("/")
                if target and not target.startswith("xl/"):
                    target = "xl/" + target
                if rid and target:
                    rels[rid] = target

        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        for sh in wb_root.findall(f".//{{{ns_main}}}sheet"):
            name = sh.attrib.get("name", "Sheet")
            rid = sh.attrib.get(f"{{{ns_rel}}}id")
            target = rels.get(rid, "")
            if not target or target not in zf.namelist():
                continue
            sroot = ET.fromstring(zf.read(target))
            headers = []
            rows = []
            row_count = 0
            col_count = 0
            for r_i, r_el in enumerate(sroot.findall(f".//{{{ns_main}}}row")):
                vals = []
                for c_el in r_el.findall(f"{{{ns_main}}}c"):
                    ref = c_el.attrib.get("r", "")
                    col_idx = 0
                    for ch in ref:
                        if ch.isalpha():
                            col_idx = col_idx * 26 + (ord(ch.upper()) - 64)
                        else:
                            break
                    while col_idx > len(vals) + 1:
                        vals.append("")
                    ctype = c_el.attrib.get("t", "")
                    v_el = c_el.find(f"{{{ns_main}}}v")
                    is_el = c_el.find(f"{{{ns_main}}}is")
                    if ctype == "s" and v_el is not None and v_el.text is not None:
                        text = shared_strings[int(v_el.text)]
                    elif ctype == "inlineStr" and is_el is not None:
                        text = "".join(t.text or "" for t in is_el.iter(f"{{{ns_main}}}t"))
                    elif v_el is not None and v_el.text is not None:
                        text = v_el.text
                    else:
                        text = ""
                    if col_idx > 0 and col_idx <= len(vals):
                        vals[col_idx - 1] = text
                    else:
                        vals.append(text)
                col_count = max(col_count, len(vals))
                if r_i == 0:
                    headers = vals
                else:
                    row_count += 1
                    if len(rows) < max_rows:
                        rows.append(vals)
            sheets_out.append({
                "name": name,
                "row_count": row_count,
                "col_count": col_count,
                "headers": headers,
                "rows": rows,
            })
    return {"path": str(path), "sheets": sheets_out}


def export_session_excel(path, engine, session_id: str = "") -> int:
    """Export verified CaptureEngine data to an .xlsx workbook with structured sheets."""
    rep = build_session_report(engine, session_id=session_id)

    summary_rows = [
        ["Section", "Metric", "Value"],
        ["Report", "Title", rep["title"]],
        ["Report", "Banner", rep["banner"] or "LIVE hardware capture"],
        ["Report", "Generated", rep["generated"]],
    ]
    masters_section = None
    for sec_title, blocks in rep["sections"]:
        if sec_title == "Master 1 vs Master 2 & arbitration":
            masters_section = blocks
        elif sec_title in ("Session overview", "Bus health & timing", "Decoded sensor readings"):
            for b in blocks:
                if b[0] == "kv":
                    for k, v in b[1]:
                        summary_rows.append([sec_title, k, v])
                elif b[0] == "note":
                    summary_rows.append([sec_title, "Note", b[1]])

    packet_rows = [PACKET_EXPORT_HEADERS] + _packet_export_rows(engine)

    device_rows = [["Address", "Device", "Confidence", "Transactions", "Reads", "Writes", "Bytes", "ACK", "NACK"]]
    for a, d in sorted(engine.registry.devices.items()):
        name, conf = engine.registry.name(a)
        device_rows.append([f"0x{a:02X}", name, conf, d.transactions, d.reads, d.writes, d.total_bytes, d.acks, d.nacks])

    masters_rows = []
    masters_kv = []
    masters_notes = []
    if masters_section:
        for b in masters_section:
            if b[0] == "table":
                masters_rows.append(list(b[1]))
                masters_rows.extend(list(r) for r in b[2])
            elif b[0] == "kv":
                masters_kv.extend(b[1])
            elif b[0] == "note":
                masters_notes.append(b[1])
    if not masters_rows:
        masters_rows.append(["Master", "COM port", "Bus-access health", "Bus packets attributed", "Controller telemetry (session)"])
    if masters_kv or masters_notes:
        masters_rows.append([])
        masters_rows.append(["Metric", "Value"])
        for k, v in masters_kv:
            masters_rows.append([k, v])
        for note in masters_notes:
            masters_rows.append(["Note", note])

    sheets = [
        ("Summary", summary_rows),
        ("Packets", packet_rows),
        ("Devices", device_rows),
        ("Masters & Arbitration", masters_rows),
    ]

    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        _write_xlsx_fallback(path, sheets)
        return len(engine.packets)

    wb = openpyxl.Workbook()
    default_ws = wb.active
    for idx, (title, rows) in enumerate(sheets):
        ws = default_ws if idx == 0 else wb.create_sheet(title=title)
        ws.title = title
        widths: dict[int, int] = {}
        for r_i, row in enumerate(rows, start=1):
            clean = [_excel_cell(v) for v in row]
            ws.append(clean)
            for c_i, v in enumerate(clean, start=1):
                if v is not None:
                    widths[c_i] = max(widths.get(c_i, 0), len(str(v)))
            if r_i == 1 and clean:
                for cell in ws[1]:
                    cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
        for c_i, w in widths.items():
            ws.column_dimensions[_col_letter(c_i)].width = max(10, min(60, w + 3))
    wb.save(path)
    return len(engine.packets)


def load_excel_preview(path, max_rows: int = 500) -> dict:
    """Read sheet names, dimensions, headers, and up to `max_rows` rows per sheet from an .xlsx file."""
    try:
        import openpyxl
    except ImportError:
        return _read_xlsx_fallback(path, max_rows=max_rows)

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = []
    try:
        for name in wb.sheetnames:
            ws = wb[name]
            headers = []
            rows = []
            row_count = 0
            col_count = 0
            for r_i, row in enumerate(ws.iter_rows(values_only=True)):
                vals = ["" if v is None else str(v) for v in row]
                while vals and vals[-1] == "":
                    vals.pop()
                col_count = max(col_count, len(vals))
                if r_i == 0:
                    headers = vals
                else:
                    row_count += 1
                    if len(rows) < max_rows:
                        rows.append(vals)
            sheets.append({
                "name": name,
                "row_count": row_count,
                "col_count": col_count,
                "headers": headers,
                "rows": rows,
            })
    finally:
        wb.close()
    return {"path": str(path), "sheets": sheets}
