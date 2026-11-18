"""Continuous RLE edge stream: firmware (compiled from the real source) -> parser -> engine ring -> WaveformPage.

The multi-master bus is two controllers taking turns on one SDA/SCL pair, so the analyzer sees one interleaved
sequence of transactions separated by idle time.  Nothing here touches hardware."""
import json
import os
import time
from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest

from analysis.capture_engine import CaptureEngine
from communication.protocol_parser import ProtocolError, parse_line
from tests.test_firmware_host import Pins, build_decoder

BASE = 100000                       # sample index of the first sample in the host harness
IDLE_HEARTBEAT = 200_000            # 100 ms at 2 MS/s


@pytest.fixture(scope="module")
def qt_app(tmp_path_factory):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    d = tmp_path_factory.mktemp("settings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    yield QApplication.instance() or QApplication([])


_PAGES = []        # every WaveformPage a test of this module built


@pytest.fixture(autouse=True)
def _destroy_pages_after_each_test():
    """Each page test builds a full WaveformPage (plots, hundreds of text items).  Closing is not destroying: left alone
    they piled up across the 19 page tests and a *later* test module then crashed natively (access violation) in the
    full suite.  Destroy exactly the pages built here (never Qt's / pyqtgraph's own windows) after every test."""
    yield
    pages, _PAGES[:] = list(_PAGES), []
    if not pages:
        return
    from PyQt6.QtCore import QCoreApplication, QEvent
    for p in pages:
        p.close()
        p.deleteLater()
    del pages, p
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    QCoreApplication.processEvents()


def multi_master_pins():
    """M1: OLED write · M2: BMP280 write+read · long idle · M1: RTC read · M2: RTC write, with idle between."""
    p = Pins()
    p.hold(3, 40)
    p.start(); p.byte(0x3C << 1); p.byte(0x40); p.byte(0xAA); p.stop()                       # M1 -> OLED
    p.hold(3, 60_000)
    p.start(); p.byte(0x76 << 1); p.byte(0xF7); p.restart(); p.byte(0x76 << 1 | 1); p.byte(0x51); p.byte(0x60, False); p.stop()
    p.hold(3, 300_000)                                                                       # 150 ms idle > heartbeat
    p.start(); p.byte(0x68 << 1); p.byte(0x00); p.restart(); p.byte(0x68 << 1 | 1); p.byte(0x22); p.byte(0x15, False); p.stop()
    p.hold(3, 60_000)
    p.start(); p.byte(0x68 << 1); p.byte(0x0F); p.byte(0x00); p.stop()
    p.hold(3, 12_000)
    return p


@pytest.fixture(scope="module")
def stream_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("stream")
    exe = build_decoder(d, stream=True)
    pins = multi_master_pins()
    lines = pins.run(exe, d)
    return pins, lines


def expected_changes(pins):
    out = [(BASE, pins[0])]
    out += [(BASE + i, pins[i]) for i in range(1, len(pins)) if pins[i] != pins[i - 1]]
    return out


def is_edges(d):
    return d.get("t") == "edges" and bool(d.get("stream"))


def reconstruct(lines):
    """[(sample, state)] for every entry of every stream batch, plus the raw batches."""
    ents, batches = [], []
    for ln in lines:
        d = json.loads(ln)
        if is_edges(d):
            d = dict(d, r=parse_line(ln).data["r"])             # the parser expands the dense text form
            batches.append(d)
            t = d["t0"]
            for i, v in enumerate(d["r"]):
                if i:
                    t += v >> 2
                ents.append((t, v & 3))
    return ents, batches


# ------------------------------------------------------------------ firmware: lossless RLE, heartbeat, compact
def test_firmware_stream_is_lossless_and_idle_costs_only_heartbeats(stream_run):
    pins, lines = stream_run
    for ln in lines:
        parse_line(ln)                                                   # every line is valid protocol
    ents, batches = reconstruct(lines)
    changes = [e for i, e in enumerate(ents) if i == 0 or e[1] != ents[i - 1][1]]
    assert changes == expected_changes(pins)                              # every transition, exact time and level
    beats = [e for i, e in enumerate(ents) if i and e[1] == ents[i - 1][1]]
    assert beats and all(b[1] == 3 for b in beats)                        # heartbeats repeat the idle (HIGH) level
    worst = max(b[0] - a[0] for a, b in zip(ents, ents[1:]))
    assert worst <= IDLE_HEARTBEAT + 32768                                # time never goes dark for >100 ms + 1 block
    assert not [json.loads(l) for l in lines if b'"snapshot"' in l]       # the stream replaces the 4/s windows
    assert all(len(b["r"]) <= 256 for b in batches) and not any(b.get("gap") for b in batches)
    edge_bytes = sum(len(l) for l in lines if is_edges(json.loads(l)))
    assert edge_bytes / len(changes) < 2.2                                # ~1.1 B/edge dense text (v1 packed ints: ~4 B)
    segs = [json.loads(l) for l in lines if b'"type":"segment"' in l]
    assert [s["address"] for s in segs][:2] == [0x3C, 0x76]               # segment decoding is untouched


def test_firmware_marks_a_gap_only_when_samples_were_really_lost(tmp_path):
    exe = build_decoder(tmp_path, stream=True)
    pins = multi_master_pins()
    lines = pins.run(exe, tmp_path, extra=(1,))                           # DMA-overrun style reset after buffer 2
    ents, batches = reconstruct(lines)
    gaps = [b for b in batches if b.get("gap")]
    assert len(gaps) == 1                                                 # exactly one, and only because of the reset
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(lines)
    assert e.stream_gaps == 1 and len(e.edge_gaps) == 1 and e.transport_errors == 0
    assert any(ev.kind == "Edge stream gap" for ev in e.events)
    assert e.stream_active and e.waveform_transactions                    # decoding resumed after the gap


def test_status_reports_stream_counters(stream_run):
    _pins, lines = stream_run
    st = [json.loads(l) for l in lines if l.startswith(b'{"type":"status"')][-1]
    assert st["stream_entries"] > 0 and st["stream_dropped"] == 0 and st["stream"] is True


# ------------------------------------------------------------------ parser
def good(**kw):
    d = {"t": "edges", "stream": True, "t0": 1000, "r": [3, (10 << 2) | 2, (10 << 2) | 0]}
    d.update(kw)
    return d


@pytest.mark.parametrize("bad", [dict(r=[]), dict(r=[(5 << 2) | 3]), dict(stream=False), dict(gap="yes"),
                                 dict(r=[3, -1]), dict(r=[3, 2 ** 32]), dict(r="x")])
def test_parser_rejects_malformed_stream_records(bad):
    with pytest.raises(ProtocolError):
        parse_line(json.dumps(good(**bad)))


def ref_encode(level, entries):
    """Independent reference encoder of the dense text form: entries = [(dt, new_state)], level = state at t0."""
    sym = lambda k: chr(35 + k + (1 if 35 + k >= 92 else 0))     # noqa: E731

    def num(v):
        out = ""
        while v >= 45:
            out += sym(45 + v % 45)
            v //= 45
        return out + sym(v)
    out, prev = "", level
    for dt, st in entries:
        x = prev ^ st
        out += num(dt * 2 + (x == 2)) if x in (1, 2) else "~" + num(dt * 2 + (x == 0))
        prev = st
    return out


def test_dense_text_form_round_trips_every_kind_and_length():
    import random
    from communication.protocol_parser import decode_stream_text
    rnd = random.Random(7)
    for _ in range(300):
        level, st, ents, want = rnd.randrange(4), None, [], []
        st = level
        for _ in range(rnd.randrange(1, 60)):
            dt = rnd.choice([0, 1, 5, 10, 11, 12, 21, 22, 44, 45, 1011, 1012, 2024, 232_000, 2 ** 32 - 1])
            nst = rnd.randrange(4)                               # any change incl. none (heartbeat) and both lines
            ents.append((dt, nst)); want.append((dt << 2) | nst)
        got = decode_stream_text(level, ref_encode(level, ents))
        assert got == [level] + want
    assert all(ch not in ('"', chr(92)) for ch in ref_encode(0, [(d, s) for d in range(0, 3000, 7) for s in (1, 2, 3, 0)]))


@pytest.mark.parametrize("bad", [chr(92), chr(31), "~", "n", "~~", "z" * 300])
def test_dense_text_form_rejects_garbage(bad):
    from communication.protocol_parser import decode_stream_text
    with pytest.raises(ProtocolError):
        decode_stream_text(3, bad)


def test_parser_accepts_stream_record_and_legacy_snapshot_unchanged():
    assert parse_line(json.dumps(good(gap=True))).data["gap"] is True
    legacy = {"t": "edges", "snapshot": True, "t0": 5, "e": [[0, 3], [10, 2]]}
    assert parse_line(json.dumps(legacy)).data["e"] == [[0, 3], [10, 2]]


# ------------------------------------------------------------------ engine: rolling ring, history kept, no false gaps
def test_engine_keeps_history_across_batches_and_idle_is_not_a_gap(stream_run):
    _pins, lines = stream_run
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(lines)
    ents, _b = reconstruct(lines)
    assert len(e.edges) == len(ents)                                     # nothing wiped: the ring holds every entry
    assert e.edge_gaps == [] and e.stream_gaps == 0                      # 150 ms of idle created NO gap
    t0, t1 = e.edges[0][0], e.edges[-1][0]
    mid = t0 + (t1 - t0) * 0.5
    ts, ss = e.edges_between(mid - 1e6, mid + 1e6)
    assert len(ts) and set(int(v) for v in ss) == {3}                    # inside the 150 ms idle: flat HIGH
    assert [t.address for t in e.waveform_transactions] == [0x3C, 0x76, 0x68, 0x68]
    assert all(t.status == "ok" and t.bytes[0].bit_times for t in e.waveform_transactions)


def test_ring_is_bounded_and_oldest_entries_are_evicted(stream_run):
    import config
    _pins, lines = stream_run
    e = CaptureEngine()
    e.set_source("live")
    e.edges = type(e.edges)(maxlen=200)
    e.ingest_lines(lines)
    assert len(e.edges) == 200 and e.edges_evicted > 0


def test_waveform_timing_attaches_to_firmware_segments(stream_run):
    _pins, lines = stream_run
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(lines)
    assert len(e.packets) == 4
    assert all(p.bytes[0].bit_times for p in e.packets)                  # measured bit times matched to every packet
    assert not e.decode_disagreements


# ------------------------------------------------------------------ WaveformPage: oscilloscope behaviour
def page_for(qt_app, engine, w=2600, h=800, track=False):
    from gui.waveform_viewer import WaveformPage
    page = WaveformPage(SimpleNamespace(engine=engine))
    page.track.setChecked(track)                           # most tests here guard the CLASSIC per-byte text rows
    _PAGES.append(page)
    page.resize(w, h)
    page.show()
    qt_app.processEvents()
    page.refresh(engine)                       # first refresh only adopts the engine's context (reset); later ones draw
    return page


def split_batches(lines):
    return ([l for l in lines if not is_edges(json.loads(l))], [l for l in lines if is_edges(json.loads(l))])


def heartbeat(t, state=3, gap=False):
    d = {"t": "edges", "stream": True, "t0": int(t), "r": [state]}
    if gap:
        d["gap"] = True
    return json.dumps(d).encode()


def sda_data(page):
    return page.c_sda.getData()[1]


def test_clip_to_view_peak_downsampling_and_step_mode_are_enabled(qt_app):
    e = CaptureEngine()
    page = page_for(qt_app, e)
    for c in (page.c_scl, page.c_sda, page.c_scl_h, page.c_sda_h):
        assert c.opts["clipToView"] is True and c.opts["autoDownsample"] is True
        assert c.opts["downsampleMethod"] == "peak" and c.opts["stepMode"] == "right"
    page.close()


def test_idle_stream_renders_a_flat_high_line_never_a_blank_screen(stream_run, qt_app):
    _pins, lines = stream_run
    head, batches = split_batches(lines)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(head + batches)
    page = page_for(qt_app, e)
    page.trigger.setCurrentText("Roll")
    page.win.setCurrentText("20 ms")
    seen_blank = False
    t = e.stream_now / 500                                               # ticks of 500 ns
    for k in range(8):                                                   # the bus stays idle: only heartbeats arrive
        t += 200_000
        e.ingest_lines([heartbeat(t)])
        page._shown_at -= 5
        page.last_draw -= 5
        page.refresh(e)
        seen_blank |= page.msg.toPlainText() != "" or "No raw samples" in page.info.text() or "Capture gap" in page.info.text()
        y = sda_data(page)
        assert y is not None and len(y) >= 1 and set(np.round(y, 3)) == {1.0}          # SDA flat at HIGH
    assert not seen_blank and page.title.text().startswith("Rolling")
    page.close()


def test_start_trigger_holds_the_last_complete_sweep_until_the_next_one(stream_run, qt_app):
    _pins, lines = stream_run
    head, batches = split_batches(lines)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(head + batches)
    page = page_for(qt_app, e)
    page.trigger.setCurrentText("START")
    page.win.setCurrentText("1 ms")
    t = e.stream_now / 500
    e.ingest_lines([heartbeat(t + 4000)])                                # let the stream pass the end of the last sweep
    page.refresh(e)
    first_key, first_title = page._sweep_key, page.title.text()
    assert first_key is not None and first_title.startswith("Triggered sweep")
    held_y, held_items = np.array(sda_data(page)), list(page.ann_items)
    calls = {"n": 0}
    real = page.c_sda.setData
    page.c_sda.setData = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1]
    for k in range(6):                                                   # a second of pure idle: the sweep must not move
        t += 200_000
        e.ingest_lines([heartbeat(t)])
        page._shown_at -= 5
        page.last_draw -= 5
        page.refresh(e)
    assert page._sweep_key == first_key and page.title.text() == first_title and calls["n"] == 0
    assert np.array_equal(sda_data(page), held_y) and page.ann_items == held_items and page.msg.toPlainText() == ""
    page.close()


def test_auto_trigger_free_runs_flat_after_the_timeout_then_catches_the_next_trigger(stream_run, qt_app):
    _pins, lines = stream_run
    head, batches = split_batches(lines)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(head + batches)
    page = page_for(qt_app, e)
    page.trigger.setCurrentText("Auto")
    t = e.stream_now / 500
    e.ingest_lines([heartbeat(t + 4000)])
    page.refresh(e)
    assert page.title.text().startswith("Triggered sweep")
    page._sweep_wall -= 5                                                # no new trigger for > 1.5 s
    t += 400_000
    e.ingest_lines([heartbeat(t)])
    page.last_draw -= 5
    page.refresh(e)
    assert page.title.text().startswith("Rolling") and set(np.round(sda_data(page), 3)) == {1.0}
    # next complete transaction arrives: it is picked up immediately (the free-run state bypasses the dwell time)
    p = Pins()
    p.hold(3, 40); p.start(); p.byte(0x76 << 1); p.byte(0xD0); p.stop(); p.hold(3, 8000)
    head2 = [json.loads(l) for l in p.run(build_decoder(page_tmp(qt_app), stream=True), page_tmp(qt_app)) if is_edges(json.loads(l))]
    shift = int(t) + 1000 - head2[0]["t0"]
    for b in head2:
        b["t0"] += shift
        e.ingest_lines([json.dumps(b).encode()])
    page.last_draw -= 5
    page.refresh(e)
    assert page.title.text().startswith("Rolling")                       # window not fully swept yet: do not show half
    e.ingest_lines([heartbeat(e.stream_now / 500 + 4000)])               # the stream moves past the end of the sweep
    page.last_draw -= 5
    page.refresh(e)
    assert page.title.text().startswith("Triggered sweep") and page._free_run is False
    assert any(isinstance(i, pg.TextItem) and i.toPlainText().startswith("ADDR 0x76") for i in page.ann_items)
    page.close()


_TMP = {}


def page_tmp(qt_app):
    import tempfile
    from pathlib import Path
    if "d" not in _TMP:
        _TMP["d"] = Path(tempfile.mkdtemp(prefix="edge_stream_"))
    return _TMP["d"]


def test_address_trigger_ignores_other_devices(stream_run, qt_app):
    _pins, lines = stream_run
    head, batches = split_batches(lines)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(head + batches)
    page = page_for(qt_app, e)
    page.set_device_filter(0x76)
    page.trigger.setCurrentText("Address")
    e.ingest_lines([heartbeat(e.stream_now / 500 + 4000)])
    page.refresh(e)
    assert page.title.text().startswith("Triggered sweep") and "0x76" in page.title.text()
    page.close()


def test_a_real_gap_is_drawn_as_a_marker_not_as_a_blank_screen(stream_run, qt_app):
    _pins, lines = stream_run
    head, batches = split_batches(lines)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(head + batches)
    page = page_for(qt_app, e)
    page.trigger.setCurrentText("Roll")
    page.win.setCurrentText("20 ms")
    t = e.stream_now / 500 + 2000
    e.ingest_lines([heartbeat(t, gap=True), heartbeat(t + 3000)])        # firmware reports lost samples
    page.last_draw -= 5
    page.refresh(e)
    assert page.msg.toPlainText() == "" and len(sda_data(page)) >= 1
    assert any(getattr(i, "_kind", "") == "GAP" for i in page.ann_items)
    assert "1 data-loss gap" in page.info.text()
    page.close()


def test_rolling_window_only_redraws_when_the_stream_advanced(stream_run, qt_app):
    _pins, lines = stream_run
    head, batches = split_batches(lines)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(head + batches)
    page = page_for(qt_app, e)
    page.trigger.setCurrentText("Roll")
    page.refresh(e)
    calls = {"n": 0}
    real = page.c_scl.setData
    page.c_scl.setData = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1]
    for _ in range(10):                                                  # no new samples: identical window, no redraw
        page.last_draw -= 5
        page.refresh(e)
    assert calls["n"] == 0
    e.ingest_lines([heartbeat(e.stream_now / 500 + 200_000)])           # time advanced: now it redraws, once
    page.last_draw -= 5
    page.refresh(e)
    assert calls["n"] == 1
    page.close()


def test_older_snapshot_firmware_still_holds_its_last_window(qt_app):
    from tests.test_graph_stability import IDLE, native_engine, snapshot_line
    from tests.helpers import synth, write
    e = native_engine()
    page = page_for(qt_app, e)
    e.ingest_lines([snapshot_line(synth(write(0x76, [0x65])))])
    page._shown_at -= 5
    page.last_draw -= 5
    page.refresh(e)
    assert e.stream_active is False and page._kind(e) == "snapshot"
    held = np.array(sda_data(page))
    e.ingest_lines([IDLE(900_000)])
    for _ in range(4):
        page._shown_at -= 5
        page.refresh(e)
    assert np.array_equal(sda_data(page), held) and page.msg.toPlainText() == ""
    page.close()


# ------------------------------------------------------------------ live-hardware regressions (2026-11-14)
def test_a_rejected_line_while_streaming_becomes_a_gap_not_a_silent_hole():
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines([heartbeat(100_000)])
    e.ingest_lines([b'8,"start_us":5,"end_us":9}\r'])                  # head of a line lost on the wire
    e.ingest_lines([heartbeat(100_400)])
    assert e.transport_errors == 1 and e.stream_gaps == 1 and len(e.edge_gaps) == 1


def test_a_damaged_segment_record_does_not_invent_an_edge_gap():
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines([heartbeat(100_000)])
    e.ingest_lines([b'{"type":"segment","seq":9,"start_us":1,"end_us":'])      # edges are unaffected by this loss
    e.ingest_lines([heartbeat(100_400)])
    assert e.transport_errors == 1 and e.stream_gaps == 0 and e.edge_gaps == []


def test_one_idle_partial_line_at_connect_is_not_a_gap():
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines([b'7,"duration_us":1728}\r'])                       # the connect landed mid-line, nothing streamed yet
    e.ingest_lines([heartbeat(100_000)])
    assert e.transport_errors == 1 and e.stream_gaps == 0


def test_dense_traces_use_hairlines_and_sparse_ones_keep_the_wide_pens(stream_run, qt_app):
    """Qt strokes wide pens at ~0.7 ms per vertex (5 ms of 100 kHz traffic = 4 s per paint): dense -> 1 px hairline."""
    from gui.waveform_viewer import WIDE_PEN_MAX_VERTICES
    page = page_for(qt_app, CaptureEngine())
    n = WIDE_PEN_MAX_VERTICES * 3
    x = np.arange(n, dtype=float)
    y = (np.arange(n) % 2).astype(float)
    page._set_curves(x, (y, y))
    assert page._dense == 2 and page.c_scl.opts["pen"].width() == 1 and not page.c_scl_h.isVisible()
    page._set_curves(x[:150], (y[:150], y[:150]))                       # mid: wide pen, no halo
    assert page._dense == 1 and page.c_scl.opts["pen"].width() == 2 and not page.c_scl_h.isVisible()
    page._set_curves(x[:40], (y[:40], y[:40]))                          # sparse: wide pen + halo
    assert page._dense == 0 and page.c_scl.opts["pen"].width() == 2 and page.c_scl_h.isVisible()
    page.close()


# ------------------------------------------------------------------ compact segment form (400 kHz link budget)
FULL_SEG = {"type": "segment", "seq": 7, "start_us": 1000, "end_us": 1325, "duration_us": 325, "start_sample": 2001,
            "end_sample": 2651, "start": "START", "end": "STOP", "address": 118, "address_bits": 7,
            "address_known": True, "direction": "write", "address_ack": True, "scl_hz": 399000, "scl_high_avg_ns": 1100,
            "scl_low_avg_ns": 1400, "scl_low_over_50us": 0, "truncated": False, "incomplete": False,
            "data": [247, 1], "acks": [True, True], "mid_byte_end": False}
COMPACT_SEG = {"type": "segment", "seq": 7, "start_sample": 2001, "end_sample": 2651, "address": 118,
               "scl_hz": 399000, "scl_high_avg_ns": 1100, "scl_low_avg_ns": 1400, "data": [247, 1], "acks": "11"}


def test_compact_segment_expands_to_exactly_the_full_record():
    full = parse_line(json.dumps(FULL_SEG)).data
    comp = parse_line(json.dumps(COMPACT_SEG)).data
    assert comp == full
    assert len(json.dumps(COMPACT_SEG, separators=(",", ":"))) < 0.6 * len(json.dumps(FULL_SEG, separators=(",", ":")))


def test_compact_segment_keeps_every_non_default_field():
    d = dict(COMPACT_SEG, start="RESTART", end="GAP", direction="read", address_bits=10, address=700,
             address_known=False, address_ack=False, truncated=True, incomplete=True, mid_byte_end=True,
             scl_low_over_50us=3, acks="10", bus_free_ns=16000)
    got = parse_line(json.dumps(d)).data
    assert (got["start"], got["end"], got["direction"], got["address_bits"], got["address_known"], got["address_ack"],
            got["truncated"], got["incomplete"], got["mid_byte_end"], got["scl_low_over_50us"]) == \
           ("RESTART", "GAP", "read", 10, False, False, True, True, True, 3)
    assert got["acks"] == [True, False] and got["bus_free_ns"] == 16000


@pytest.mark.parametrize("bad", [dict(acks="1x"), dict(acks="111"), dict(acks=[1, 1]), dict(address=300),
                                 dict(start_sample=9999)])
def test_compact_segment_still_validates(bad):
    with pytest.raises(ProtocolError):
        parse_line(json.dumps(dict(COMPACT_SEG, **bad)))


def test_firmware_segments_are_compact_and_parse_to_full_records(stream_run):
    _pins, lines = stream_run
    segs = [l for l in lines if json.loads(l).get("type") == "segment"]
    assert segs and all(b'"start_us"' not in l and b'"truncated"' not in l and b'"address_bits"' not in l for l in segs)
    assert max(len(l) for l in segs) < 300
    for l in segs:
        d = parse_line(l).data
        assert d["start_us"] == d["start_sample"] // 2 and isinstance(d["acks"], list) and len(d["acks"]) == len(d["data"])


# ------------------------------------------------------------------ frequency-adaptive time base + labels (100 / 400 kHz)
class RatePins(Pins):
    """Pins whose SCL phases last LOW/HIGH ticks (0.5 us): (10,10) = 100 kHz, (3,2) = 400 kHz."""
    LOW = HIGH = 10

    def hold(self, state, n=10):
        self.extend([state] * (self.HIGH if state & 2 else self.LOW))


@pytest.fixture(scope="module")
def rate_exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("rate")
    return build_decoder(d, stream=True), d


def rate_engine(rate_exe, khz, build):
    exe, d = rate_exe
    p = RatePins()
    p.LOW, p.HIGH = (10, 10) if khz == 100 else (3, 2)
    p.hold(3, 400)
    build(p)
    p.hold(3, 3000)
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(p.run(exe, d, name=f"rate{khz}.bin"))
    e.ingest_lines([heartbeat(e.stream_now / 500 + 8000)])               # the stream moves past the end of the sweep
    return e


def bmp_read(p):
    p.start(); p.byte(0x76 << 1); p.byte(0xF7); p.restart(); p.byte(0x76 << 1 | 1); p.byte(0x51); p.byte(0x60)
    p.byte(0x2C, False); p.stop()


def long_oled_write(p):
    p.start(); p.byte(0x3C << 1); p.byte(0x40)
    for v in range(20):
        p.byte(0x10 + v)
    p.stop()


def shown_page(qt_app, e, trigger="START", w=1500, h=820, track=False):
    page = page_for(qt_app, e, w=w, h=h, track=track)
    page.trigger.setCurrentText(trigger)
    for _ in range(3):
        page._shown_at -= 5
        page.last_draw -= 5
        page.refresh(e)
        qt_app.processEvents()
    return page


def label_texts(page):
    return [i.toPlainText() for i in page.ann_items if isinstance(i, pg.TextItem)]


def overlaps(page):
    r = [i.sceneBoundingRect() for i in page.ann_items if isinstance(i, pg.TextItem)]
    return sum(1 for a in range(len(r)) for b in range(a + 1, len(r)) if r[a].intersects(r[b]))


@pytest.mark.parametrize("khz,expect_ms", [(100, 1.0), (400, 0.25)])
def test_auto_time_base_follows_the_measured_scl_rate(rate_exe, qt_app, khz, expect_ms):
    e = rate_engine(rate_exe, khz, lambda p: (bmp_read(p), p.hold(3, 3000), bmp_read(p), p.hold(3, 3000), bmp_read(p)))
    page = shown_page(qt_app, e)
    hz, src = page.scl_hz()
    assert src == "measured" and abs(hz - khz * 1e3) < 0.15 * khz * 1e3        # the packets say what the bus does
    assert page._window_ns() == expect_ms * 1e6
    assert page.title.text().startswith("Triggered sweep")
    vx = page.plot.getPlotItem().viewRange()[0]
    assert 0.9 * expect_ms * 1e3 < vx[1] - vx[0] < 1.4 * expect_ms * 1e3          # the X axis really is that wide
    page.close()


def test_auto_time_base_uses_the_applied_setting_until_packets_are_measured(qt_app):
    e = CaptureEngine()
    page = page_for(qt_app, e)
    assert page.scl_hz() == (100_000.0, "default") and page._window_ns() == 1e6
    e.record_scl_setting(400_000, 399_361, "applied", "controller")
    assert page.scl_hz() == (399_361.0, "applied setting") and page._window_ns() == 2.5e5
    page.win.setCurrentText("5 ms")                                              # a manual window always wins
    assert page._window_ns() == 5e6
    page.win.setCurrentText("100 µs")
    assert page._window_ns() == 1e5
    page.close()


def test_auto_window_changes_when_the_bus_clock_changes(rate_exe, qt_app):
    e = rate_engine(rate_exe, 100, lambda p: (bmp_read(p), p.hold(3, 3000), bmp_read(p), p.hold(3, 3000), bmp_read(p)))
    page = shown_page(qt_app, e)
    assert page._window_ns() == 1e6
    e.packets.clear()                                                            # a new capture at the other rate
    e.record_scl_setting(400_000, 399_361, "applied", "controller")
    page.refresh(e)
    assert page._window_ns() == 2.5e5 and page._auto_w == 2.5e5
    page.close()


@pytest.mark.parametrize("khz", [100, 400])
def test_address_data_ack_and_bit_labels_are_readable_at_both_rates(rate_exe, qt_app, khz):
    e = rate_engine(rate_exe, khz, bmp_read)
    page = shown_page(qt_app, e)
    texts = label_texts(page)
    assert "ADDR 0x76" in texts and "W=0" in texts and "R=1" in texts          # 7-bit address + R/W bit
    assert "0xEC" in texts and "0xED" in texts                                  # raw address byte on the wire
    assert all(f"DATA 0x{v:02X}" in texts for v in (0xF7, 0x51, 0x60, 0x2C))   # data bytes with their hex
    assert texts.count("ACK") >= 5 and any(t.startswith("NACK") and "end" in t for t in texts)   # end-of-read NACK
    bits = [t for t in texts if t in ("0", "1")]
    assert len(bits) >= 8 * 5                                                   # every address/data bit digit
    assert overlaps(page) == 0
    rects = list(page.label_px)
    assert not [(a, b) for i, a in enumerate(rects) for b in rects[i + 1:]
                if a[0] < b[1] and a[1] > b[0] and a[2] < b[3] and a[3] > b[2]]
    page.close()


def test_bit_digits_sit_on_their_own_clock_pulse_in_order(rate_exe, qt_app):
    e = rate_engine(rate_exe, 400, lambda p: (p.start(), p.byte(0x3C << 1), p.byte(0xA5), p.stop()))
    page = shown_page(qt_app, e)
    digits = sorted((i.pos().x(), i.toPlainText()) for i in page.ann_items
                    if isinstance(i, pg.TextItem) and i.toPlainText() in ("0", "1") and abs(i.pos().y() - 1.25) < 1e-6)
    seq = "".join(d for _x, d in digits)
    assert seq == "01111000" + "10100101"                                        # 0x78 (=0x3C<<1) then 0xA5, MSB first
    xs = [x for x, _d in digits]
    assert all(b > a for a, b in zip(xs, xs[1:]))
    page.close()


def test_long_packet_in_auto_keeps_the_bit_level_scale_and_can_be_panned(rate_exe, qt_app):
    e = rate_engine(rate_exe, 400, long_oled_write)
    page = shown_page(qt_app, e)
    pi = page.plot.getPlotItem()
    dur_us = (e.waveform_transactions[-1].t_end - e.waveform_transactions[-1].t_start) / 1e3
    vx = pi.viewRange()[0]
    assert dur_us > 400 and vx[1] - vx[0] < 330                                 # whole packet >> the 250 us view
    assert "drag to pan" in page.title.text()
    assert "ADDR 0x3C" in label_texts(page) and "DATA 0x40" in label_texts(page)
    assert "DATA 0x23" not in label_texts(page)                                 # the last byte is off to the right
    pi.setXRange(dur_us - 200, dur_us + 20, padding=0)                          # pan to the end of the packet
    page._reannotate()
    assert "DATA 0x23" in label_texts(page) and "ADDR 0x3C" not in label_texts(page)
    assert overlaps(page) == 0
    page.fit.click()                                                            # Fit packet shows everything again
    qt_app.processEvents()
    vx = pi.viewRange()[0]
    assert vx[1] - vx[0] > dur_us
    page.close()


def test_labels_collapse_to_hex_only_then_markers_as_the_view_zooms_out(rate_exe, qt_app):
    e = rate_engine(rate_exe, 400, bmp_read)
    page = shown_page(qt_app, e)
    pi = page.plot.getPlotItem()
    pi.setXRange(0, 800, padding=0); page._reannotate()                          # ~40 px per byte: hex only
    mid = label_texts(page)
    assert any(t in ("F7", "51", "60", "2C") for t in mid)                       # hex values survive
    assert not any(t.startswith(("DATA", "ADDR")) for t in mid) and "0" not in mid and "1" not in mid   # no verbose / bits
    pi.setXRange(0, 2500, padding=0); page._reannotate()                         # ~13 px per byte: markers only
    assert [t for t in label_texts(page)] == []
    page.close()


def test_a_held_sweep_is_relaid_out_when_the_plot_is_resized(rate_exe, qt_app):
    """Seen on live hardware: labels laid out while the page was hidden/narrow stayed in their small form."""
    import time as _t
    e = rate_engine(rate_exe, 400, bmp_read)
    page = shown_page(qt_app, e, w=420)                                     # narrow: ~35 px per byte, hex only
    assert "ADDR 0x76" not in label_texts(page)
    page.resize(1600, 820)
    deadline = _t.time() + 3
    while _t.time() < deadline and "ADDR 0x76" not in label_texts(page):    # no redraw, no new data: only the resize
        qt_app.processEvents()
        _t.sleep(0.02)
    assert "ADDR 0x76" in label_texts(page) and any(t in ("0", "1") for t in label_texts(page))
    page.close()


def test_a_packet_that_fits_the_auto_window_is_not_called_long(rate_exe, qt_app):
    e = rate_engine(rate_exe, 100, bmp_read)                               # ~890 us in a 1 ms window
    page = shown_page(qt_app, e)
    assert "drag to pan" not in page.title.text()
    vx = page.plot.getPlotItem().viewRange()[0]
    assert vx[1] - vx[0] < 1200
    page.close()


def test_all_bytes_of_one_packet_share_one_label_style(rate_exe, qt_app):
    """Live hardware: bytes of 22.0 / 23.0 us straddled the tier boundary, mixing 'F7' and 'DATA 0x67' in one packet."""
    e = rate_engine(rate_exe, 400, bmp_read)
    page = shown_page(qt_app, e, w=1330)                                  # ~100 px per byte: right at the boundary
    for width in (1250, 1290, 1330, 1370):
        page.resize(width, 820)
        qt_app.processEvents()
        page._ann_key = None
        page._reannotate()
        texts = label_texts(page)
        data = [t for t in texts if t.startswith("DATA") or t in ("F7", "51", "60", "2C")]
        # a label may be dropped by a cursor letter, but the ones shown all use the same style
        assert len(data) >= 3 and (all(t.startswith("DATA") for t in data) or not any(t.startswith("DATA") for t in data)), (width, data)
    page.close()


def test_segment_carries_up_to_255_data_bytes():
    """Analyzer MAX_DATA 255 (2026-11-17): the controllers' 2.6-live OLED text write (251 bytes) arrives whole;
    more than 255 is still a protocol error."""
    from communication.protocol_parser import ProtocolError
    data = [0x80, 0x20] * 8 + [0x40] + [0x7F] * 234
    got = parse_line(json.dumps(dict(COMPACT_SEG, address=0x3C, end_sample=20000, data=data, acks="1" * 251))).data
    assert got["data"] == data and len(got["acks"]) == 251 and all(got["acks"])
    with pytest.raises(ProtocolError):
        parse_line(json.dumps(dict(COMPACT_SEG, data=[1] * 256, acks="1" * 256)))
