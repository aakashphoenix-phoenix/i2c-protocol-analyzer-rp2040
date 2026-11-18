"""Hardware-grounded two-controller demo bench (sources/demo_source.py, DemoGenerator(masters=2)) against the LIVE
bench measurement of 2026-11-15 (work/measurements/MM_RESULTS_20261115.md), and the real capture replay itself.

Ground truth for attribution comes from the simulator's own bus log (which controller really drove each simulated
bus transaction): the GUI tracker must reproduce it from the SIMULATED telemetry alone, never by guessing."""
import csv
import json
import statistics
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from analysis.capture_engine import CaptureEngine
from analysis.communication_health import acknowledgment_counts
from analysis.multi_master import attribution_text, packet_signature
from sources.demo_source import BENCH_MASTERS, BENCH_MEASURED, DemoGenerator
from storage.capture_storage import build_session_report, export_packets_csv, load_capture, save_capture
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
REAL_CAPTURE = ROOT / "work" / "measurements" / "mm_baseline_before.i2ccap"


def run_bench(seconds, seed=11, step=0.05, **kw):
    """Feed the generator into a CaptureEngine exactly like the GUI does (controller lines first, then the analyzer
    lines of the same 50 ms tick), with a simulated PC clock."""
    g = DemoGenerator(seed=seed, masters=2, **kw)
    e = CaptureEngine()
    e.set_source("demo")
    now = [0.0]
    e.wall_clock = lambda: now[0]
    e.ingest_lines([g.hello()])
    ctl = {1: [], 2: []}
    for k in range(1, int(round(seconds / step)) + 1):
        now[0] = k * step
        for mid, lines in g.take_master_lines().items():
            ctl[mid] += lines
            e.ingest_master_lines(mid, lines, wall=now[0])
        e.ingest_lines(g.lines_until(now[0]))
    return g, e, ctl


@pytest.fixture(scope="module")
def bench():
    return run_bench(31.0)


def window_packets(e, t0_s=1.0, t1_s=31.0):
    return [t for t in e.packets if t0_s * 1e9 <= t.t_start < t1_s * 1e9]


def sig_key(t):
    a, w, r, _ = packet_signature(t)
    return f"0x{a:02X} w{w}" + (f" r{r}" if r else "")


# ====================================================================== schedules (I2C.c, both controllers)
def txns(ctl, mid):
    return [json.loads(x) for x in ctl[mid] if b'"type":"txn"' in x]


def intervals(rows, addr, rlen):
    ts = [r["t_us"] for r in rows if r["addr"] == addr and r["rlen"] == rlen]
    return [(b - a) / 1e6 for a, b in zip(ts, ts[1:])]


def test_master_schedules_follow_the_firmware(bench):
    g, e, ctl = bench
    m1, m2 = txns(ctl, 1), txns(ctl, 2)
    assert statistics.median(intervals(m1, 0x76, 6)) == pytest.approx(0.200, abs=0.002)      # BMP every 200 ms
    assert statistics.median(intervals(m1, 0x68, 16)) == pytest.approx(1.000, abs=0.002)     # RTC + OLED every 1 s
    assert statistics.median(intervals(m2, 0x76, 6)) == pytest.approx(0.330, abs=0.002)      # BMP every 330 ms
    assert statistics.median(intervals(m2, 0x68, 16)) == pytest.approx(1.100, abs=0.002)     # RTC every 1.1 s
    for rows in (m1, m2):           # identity every 10 s from loop start (the boot read in bmp_init() comes first)
        loop_reads = intervals(rows, 0x76, 24)[1:]
        assert loop_reads and all(i == pytest.approx(10.0, abs=0.005) for i in loop_reads)
    # Master 2 (Pico 3) -> OLED 0x3C: WRITE only (the SSD1306 is write-only on I2C), one [0x00, 0xA6] command per 1.1 s
    m2_oled = [r for r in m2 if r["addr"] == 0x3C]
    assert m2_oled and all(r["wlen"] == 2 and r["rlen"] == 0 and r["b0"] == 0x00 for r in m2_oled)
    assert statistics.median([(b["t_us"] - a["t_us"]) / 1e6 for a, b in zip(m2_oled, m2_oled[1:])]) == pytest.approx(
        1.100, abs=0.002)
    oled = [r for r in m1 if r["addr"] == 0x3C and 10e6 <= r["t_us"] - m1[0]["t_us"] < 30e6]
    assert len(oled) == 20 * 70                                                             # 6 window cmds + 64 data / s
    first = {mid: min(t for (m, _a, t, _e, _r) in g.bench.bus_log if m == mid) for mid in (1, 2)}
    assert first[2] - first[1] >= BENCH_MASTERS[2]["offset_ms"] * 1e6 * 0.99               # staggered 97 ms start


def test_simulated_bus_matches_the_measured_bench(bench):
    _g, e, _ctl = bench
    pk = window_packets(e)                                                                  # 30 s steady state
    # the measured bench had no Master 2 OLED traffic; the demo adds Master 2's OLED command writes (SIMULATED)
    got = Counter(sig_key(t) for t in pk if not (t.master_id == 2 and t.address == 0x3C))
    for k, n in BENCH_MEASURED["per_30s"].items():
        assert abs(got[k] - n) <= max(2, 0.04 * n), (k, got[k], n)
    c = Counter()
    for t in pk:
        c.update(acknowledgment_counts(t))
    rate = 100 * c["raw_nacks"] / c["opportunities"]
    assert rate == pytest.approx(BENCH_MEASURED["nack_rate_pct"], abs=0.03)
    assert abs(c["acks"] - BENCH_MEASURED["acks"]) / BENCH_MEASURED["acks"] < 0.02
    hz = statistics.median(t.timing["frequency_hz"] for t in pk)
    assert hz == pytest.approx(BENCH_MEASURED["scl_hz_median"], rel=0.01)                  # 94.6 kHz, not 100 kHz
    free = [t.timing["bus_free_ns"] for t in pk if t.timing.get("bus_free_ns") is not None]
    assert min(free) >= 40_000 and statistics.median(free) == pytest.approx(440_000, rel=0.1)
    by = Counter(t.master_id for t in pk if not (t.master_id == 2 and t.address == 0x3C))   # (+ M2 OLED writes)
    assert by[1] == pytest.approx(BENCH_MEASURED["by_master"][1], rel=0.03)
    assert by[2] == pytest.approx(BENCH_MEASURED["by_master"][2], rel=0.06)


def test_every_raw_nack_is_kept_and_classified_honestly(bench):
    _g, e, _ctl = bench
    c = Counter()
    for t in e.packets:
        c.update(acknowledgment_counts(t))
    reads = sum(1 for t in e.packets for f in t.frames if f.rw == "R")
    assert c["raw_nacks"] == reads > 0                     # exactly one NACK per read frame, all of them counted
    assert c["expected_read_nacks"] == c["raw_nacks"]      # every one followed by a captured STOP: read[final]
    assert c["unexpected_nacks"] == c["address_nacks"] == c["write_data_nacks"] == 0
    assert c["unverified_read_end_nacks"] == 0 and all(t.status == "ok" for t in e.packets)
    for t in e.packets:                                    # never relabelled as ACK
        for f in t.frames:
            if f.rw == "R":
                assert f.acks[-1] is False and all(f.acks[:-1])


def test_controller_telemetry_is_simulated_and_attribution_is_never_guessed(bench):
    g, e, ctl = bench
    for mid, lines in ctl.items():
        recs = [json.loads(x) for x in lines]
        assert recs and all(r["simulated"] is True and r["master_id"] == mid for r in recs)
        assert {"hello", "txlog", "scl_config", "master_stats", "txn"} <= {r["type"] for r in recs}
    truth = [m for (m, *_rest) in g.bench.bus_log]
    assert len(truth) == len(e.packets)
    assert [t.master_id for t in e.packets] == truth       # the tracker reproduced the simulator's ground truth
    assert {t.master_method for t in e.packets} == {"telemetry"}
    assert all(t.demo for t in e.packets)
    assert Counter(attribution_text(t) for t in e.packets).keys() == {"Master 1 (SIMULATED)", "Master 2 (SIMULATED)"}
    rows = {r["master_id"]: r for r in e.masters.rows(now_wall=31.0)}
    assert all(r["simulated"] and r["port"] == "SIMULATED" and "SIMULATED" in r["detail"] for r in rows.values())
    assert e.masters.summary(now_wall=31.0)["simulated"] is True
    st = e.masters.state[1].session_stats()
    assert st["addr_nack"] == st["data_nack"] == st["gave_up"] == st["timeouts"] == 0
    assert e.scl_setting["source"].startswith("SIMULATED")


def test_legacy_single_master_demo_is_unchanged():
    g = DemoGenerator(seed=7)
    assert g.masters == 1 and g.bench is None
    lines = [g.hello()] + g.lines_until(1.2)
    assert g.take_master_lines() == {}
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines(lines)
    assert e.packets and all(t.master_id == 1 and t.master_method == "demo" for t in e.packets)
    assert statistics.median(t.timing["frequency_hz"] for t in e.packets) == pytest.approx(100_000, rel=0.01)


def test_absent_probe_stays_opt_in_and_is_reported_as_unexpected():
    _g, e, ctl = run_bench(8.0, seed=3, inject_absent_probe=True)
    probes = [t for t in e.packets if t.address == 0x2A]
    assert probes and all(f.addr_ack is False for t in probes for f in t.frames)
    c = Counter()
    for t in e.packets:
        c.update(acknowledgment_counts(t))
    assert c["address_nacks"] == c["unexpected_nacks"] == len(probes)          # never hidden among read-end NACKs
    assert all(t.master_id == 1 for t in probes)
    m1 = [json.loads(x) for x in ctl[1] if b'"type":"txn"' in x]
    assert sum(r["result"] == "addr_nack" for r in m1) == len(probes)


def test_400khz_is_simulated_on_request():
    g = DemoGenerator(seed=4, masters=2)
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines([g.hello()] + g.lines_until(1.0))
    g.request_scl(400_000)
    e.ingest_lines(g.lines_until(3.0))
    late = [t for t in e.packets if t.t_start > 1.2e9]
    assert statistics.median(t.timing["frequency_hz"] for t in late) == pytest.approx(400_000, rel=0.02)
    cfg = [json.loads(x) for lines in g.take_master_lines().values() for x in lines if b"scl_config" in x]
    assert {(r["result"], r["applied_hz"], r["simulated"]) for r in cfg} >= {("applied", 400_000, True)}


def test_saved_demo_capture_keeps_simulated_controllers(tmp_path, bench):
    _g, e, _ctl = bench
    p = tmp_path / "bench_DEMO.i2ccap"
    save_capture(str(p), e)
    meta, lines = load_capture(str(p))
    assert meta["simulated"] is True and any(b'"type":"master_record"' in ln for ln in lines)
    r = CaptureEngine()
    r.set_source("demo")
    for i in range(0, len(lines), 2000):
        r.ingest_lines(lines[i:i + 2000])
    assert [t.master_id for t in r.packets] == [t.master_id for t in e.packets]
    assert all(t.demo for t in r.packets)


# ====================================================================== the REAL capture (hardware, not simulated)
@pytest.fixture(scope="module")
def real():
    meta, lines = load_capture(str(REAL_CAPTURE))
    e = CaptureEngine()
    e.set_source("replay")
    for i in range(0, len(lines), 300):          # small chunks: the lock decision happens on a short packet span
        e.ingest_lines(lines[i:i + 300])
    return meta, e


def test_real_capture_counts_are_exactly_the_phase1_measurement(real):
    meta, e = real
    assert meta["simulated"] is False and meta["source"] == "live"
    c = Counter()
    for t in e.packets:
        c.update(acknowledgment_counts(t))
    assert len(e.packets) == BENCH_MEASURED["packets"]
    assert (c["acks"], c["raw_nacks"], c["expected_read_nacks"], c["unexpected_nacks"]) == (38290, 312, 312, 0)
    assert e.lost_records == e.transport_errors == e.fw_stats["overflows"] == 0
    assert not any(t.demo for t in e.packets)


def test_real_capture_attribution_without_a_false_clock_lock(real):
    _meta, e = real
    assert Counter(t.master_id for t in e.packets) == {1: 2295, 2: 124}
    assert {t.master_method for t in e.packets} == {"telemetry"}
    aligned = [ev for ev in e.events if ev.kind == "Controller clock aligned"]
    assert len(aligned) == 2                    # one lock per master: no wrong lock that had to be undone later
    for s in e.masters.state.values():
        assert not s.simulated


# ====================================================================== GUI: every view shows the same numbers
def gui_window(qt_app):
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    w.timer.stop()
    w.resize(1500, 1000)
    w.show()
    qt_app.processEvents()
    return w


def show(w, qt_app, row):
    w.nav.setCurrentRow(row)
    qt_app.processEvents()
    w._refresh()
    qt_app.processEvents()


def test_gui_cross_view_parity_on_the_simulated_bench(qt_app, tmp_path):
    w = gui_window(qt_app)
    try:
        g = DemoGenerator(seed=21, masters=2)
        e = w.engine
        e.set_source("demo")
        now = [0.0]
        e.wall_clock = lambda: now[0]
        e.ingest_lines([g.hello()])
        for k in range(1, 241):                                    # 12 s
            now[0] = k * 0.05
            for mid, lines in g.take_master_lines().items():
                e.ingest_master_lines(mid, lines, wall=now[0])
            e.ingest_lines(g.lines_until(now[0]))
        c = Counter()
        for t in e.packets:
            c.update(acknowledgment_counts(t))
        by = Counter(t.master_id for t in e.packets)
        assert by[1] and by[2] and not by[None]
        show(w, qt_app, 0)
        hx = w.p_dash.cards["health"].sub.text()
        assert hx.startswith("SIMULATED — NOT VERIFIED HARDWARE")
        assert (f"ACK {c['acks']:,}" in hx and f"raw NACK {c['raw_nacks']:,}" in hx
                and f"end-of-read {c['expected_read_nacks']:,}" in hx and "unexpected 0" in hx)
        assert "SIMULATED controllers" in w.p_dash.masters_text.text()
        net = w.p_net.net
        show(w, qt_app, 1)
        net._show_health(e)
        assert "SIMULATED" in net._health_short and f"NACK raw {c['raw_nacks']}" in net.health_label.text()
        show(w, qt_app, 2)
        m = w.packet_model
        m.sync()
        col = [m.headerData(i, __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.Orientation.Horizontal)
               for i in range(m.columnCount())].index("Master")
        assert Counter(m.data(m.index(r, col)) for r in range(m.rowCount())) == {
            "Master 1 (SIMULATED)": by[1], "Master 2 (SIMULATED)": by[2]}
        show(w, qt_app, 4)
        cmp_ = w.p_perf.comparison_texts(e.perf.compute())
        for mid in (1, 2):
            raw = sum(acknowledgment_counts(t)["raw_nacks"] for t in e.packets if t.master_id == mid)
            assert cmp_[(mid, "nacks")] == f"0 / {raw:,}" and cmp_[(mid, "txns")].startswith(f"{by[mid]:,}")
        show(w, qt_app, 6)
        tbl = w.p_dev.tbl
        head = [tbl.horizontalHeaderItem(i).text() for i in range(tbl.columnCount())]
        for r in range(tbl.rowCount()):
            a = int(tbl.item(r, 0).text(), 16)
            cc = Counter()
            for t in e.packets:
                cc.update(acknowledgment_counts(t, a))
            assert tbl.item(r, head.index("ACK")).text() == f"{cc['acks']:,}"
            assert tbl.item(r, head.index("NACK")).text() == f"{cc['raw_nacks']:,}"
            assert tbl.item(r, head.index("Unexpected NACK")).text() == "0"
        mm = w.p_dev.mm
        assert [mm.item(r, 2).text() for r in range(mm.rowCount())] == ["SIMULATED", "SIMULATED"]
        assert [mm.item(r, 14).text() for r in range(mm.rowCount())] == [f"{by[1]:,}", f"{by[2]:,}"]
        assert "two SIMULATED controllers" in w.p_dev.mm_banner.text()
        show(w, qt_app, 7)
        assert w.p_err.cards["nack"].value.text() == f"{c['expected_read_nacks']:,}"
        assert w.p_err.cards["bus"].value.text() == "0"
        rep = build_session_report(e, "t")
        assert "SIMULATED" in (rep["banner"] or "")
        p = tmp_path / "p.csv"
        export_packets_csv(str(p), e)
        rows = list(csv.DictReader(open(p, newline="", encoding="utf-8")))
        assert {r["simulated"] for r in rows} == {"yes"}
        assert sum(int(r["raw_nacks"]) for r in rows) == c["raw_nacks"]
        assert sum(int(r["unexpected_nacks"]) for r in rows) == 0
        # the recording of this demo is reopened as SIMULATED, with the same attribution
        cap = tmp_path / "bench_DEMO.i2ccap"
        save_capture(str(cap), e)
        w.open_capture(str(cap), speed=0)
        qt_app.processEvents()
        assert w.engine.source == "demo" and "SIMULATED" in w.banner.text() and "recorded demo capture" in w.banner.text()
        assert Counter(t.master_id for t in w.engine.packets) == by
    finally:
        w.close()
        qt_app.processEvents()


def test_gui_demo_mode_runs_the_two_controller_bench(qt_app):
    import time
    w = gui_window(qt_app)
    try:
        w.btn_demo.setChecked(True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4.0 and not all(s.simulated for s in w.engine.masters.state.values()):
            qt_app.processEvents()
            time.sleep(0.01)
        w._refresh_master_pills()
        assert all(s.simulated for s in w.engine.masters.state.values())
        assert all("SIMULATED" in lb.text() for lb in w.master_states.values())
        assert w.banner.text().startswith("DEMO · SIMULATED — NOT VERIFIED HARDWARE")
    finally:
        w.btn_demo.setChecked(False)
        w.close()
        qt_app.processEvents()
