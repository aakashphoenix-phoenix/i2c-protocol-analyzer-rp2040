"""SCL clock setting, measured timing and frame display (deterministic software vectors).

Nothing here proves the physical bus changed speed; see the controller host test
for the firmware command path and docs for the hardware checks still required.
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from analysis import scl_timing
from analysis.capture_engine import CaptureEngine
from analysis.playback import stages
from sources.demo_source import DemoGenerator, SIM_TIMING
from tests.helpers import lines_for, seq_of, synth, write, write_read
from tests.test_hardware_pipeline import qt_app
from tests.test_native_firmware import HELLO, seg


def run(edges, source="live"):
    e = CaptureEngine(); e.set_source(source)
    e.stream_connected = True; e.capture_active = True; e.last_record_wall = time.monotonic()
    e.ingest_lines(lines_for(edges))
    return e


# ------------------------------------------------------------------ clock rates
@pytest.mark.parametrize("hz", [100_000, 400_000])
def test_measured_frequency_and_high_low_at_each_rate(hz):
    e = run(synth(write(0x3C, [0x00, 0xAF, 0x55, 0xAA]), scl_hz=hz))
    t = e.packets[0]
    tm = SIM_TIMING[hz]
    assert t.timing["frequency_hz"] == pytest.approx(hz, rel=0.02)
    assert t.timing["high_ns"] == pytest.approx(tm["high"] * 100, rel=0.08)
    assert t.timing["low_ns"] == pytest.approx(tm["low"] * 100, rel=0.08)
    assert t.timing["frequency_source"] == "captured raw edges"
    assert t.timing["frequency_scope"].startswith("robust packet mean of 44 cycles")  # 5 bytes x 9 rises - 1
    assert e.perf.compute().roll_freq_hz == pytest.approx(hz, rel=0.02)              # rolling 1 s mean


def test_cycle_is_next_rise_minus_rise_within_frame_only():
    e = run(synth(write_read(0x76, 0xF7, [0x65, 0x4A])))
    t = e.packets[0]
    cyc = scl_timing.cycles(t)
    rises = [r for b in t.bytes for r in list(b.bit_times) + [b.t_ack]]
    sr = next(tt for tt, k in t.events if k == "Sr")
    assert len(cyc) == (2 * 9 - 1) + (3 * 9 - 1)         # two frames, no cycle across the repeated START
    assert all(not (a < sr < b) for a, b, _ in cyc)
    for a, b, p in cyc:
        assert p == b - a and rises.index(b) == rises.index(a) + 1
    m = scl_timing.packet_measurement(t)
    assert m["frequency_hz"] == pytest.approx(1e9 / (sum(p for *_, p in cyc) / len(cyc)))


def test_idle_gap_between_transactions_is_not_a_cycle():
    e = run(synth(seq_of(write(0x3C, [1]), write(0x3C, [2]), gap=20_000)))
    a, b = e.packets
    assert all(p < 20_000 for t in (a, b) for *_, p in scl_timing.cycles(t))
    assert b.timing["bus_free_ns"] == pytest.approx(b.t_start - a.t_end)
    assert b.timing["bus_free_ns"] > 2_000_000                      # 2 ms gap reported separately


def test_long_low_reported_separately_not_as_stretch_and_not_in_mean():
    def build(s):
        s.start(); s.byte(0x76 << 1); s.wait(3000); s.byte(0x65); s.stop()
    t = run(synth(build)).packets[0]
    assert t.timing["cycles_excluded"] == 1 and "long cycle" in t.timing["frequency_scope"]
    assert t.timing["frequency_hz"] == pytest.approx(100_000, rel=0.02)
    assert max(t.timing["long_low_ns"]) > 300_000 and "UNDETERMINED" in t.timing["stretch_cause"]


def test_native_segment_frequency_is_labelled_firmware_average():
    e = CaptureEngine(); e.set_source("live")
    e.ingest_lines([json.dumps(HELLO).encode(),
                    json.dumps(seg(1, 100, 400, "START", "STOP", 0x3C, "write", [0], [True], start_sample=200,
                                   end_sample=800)).encode()])
    t = e.packets[0]
    assert t.timing["frequency_source"] == "analyzer firmware segment"
    assert "individual cycles not captured" in t.timing["frequency_scope"]
    assert t.t_start == 200 * 500 and t.t_end == 800 * 500              # exact sample timestamps


# ------------------------------------------------------------------ frame structure
def test_seven_bit_address_separate_from_rw_and_address_byte():
    t = run(synth(write_read(0x76, 0xF7, [0x65]))).packets[0]
    addr_bytes = [b for b in t.bytes if b.kind == "addr"]
    assert [scl_timing.address_byte(f) for f in t.frames] == [0xEC, 0xED]
    for b, f in zip(addr_bytes, t.frames):
        assert b.value >> 1 == f.addr == 0x76 and (b.value & 1) == (f.rw == "R")
    st = stages(t)
    assert [s.text for s in st[:3]] == ["START", "ADDR 0x76", "WRITE W=0"]
    assert st[1].byte == 0xEC and st[8].text == "READ R=1" and st[8].byte == 0xED


def test_plotted_levels_match_decoded_bits_and_ack_slot():
    e = run(synth(write_read(0x76, 0xF7, [0x65, 0x4A])))
    t = e.packets[0]
    edges = list(e.edges)

    def level_at(tt):
        st = 3
        for et, s in edges:
            if et > tt:
                break
            st = s
        return st
    for b in t.bytes:
        assert len(b.bit_times) == 8 and b.t_ack > b.bit_times[7]
        for i, tt in enumerate(b.bit_times):                    # MSB first, sampled at the SCL rise
            assert level_at(tt) >> 1 == 1 and level_at(tt) & 1 == (b.value >> (7 - i)) & 1
        assert level_at(b.t_ack) & 1 == (0 if b.ack else 1)    # 9th clock: LOW = ACK, HIGH = NACK
    assert [b.ack for b in t.bytes] == [True, True, True, True, False]
    assert t.event_seq() == "S Sr P"                             # no STOP after every ACK


def test_incomplete_capture_is_marked_and_never_completed(qt_app):
    import pyqtgraph as pg
    from gui.waveform_viewer import WaveformPage

    def build(s):
        s.start(); s.byte(0x76 << 1); s.byte(0xF7)
        for bit in (1, 0, 1):                                    # 3 bits of a byte, then the capture ends
            s.bit(bit)
    e = run(synth(build))
    txn = e.decoder.check_timeout(e.hw_now + 1e9)[0]
    e._store(txn)
    assert txn.status == "incomplete" and [b.value for b in txn.bytes] == [0xEC, 0xF7]   # partial byte not invented
    assert txn.timing["stop_ns"] is None
    page = WaveformPage(SimpleNamespace(engine=e)); page.track.setChecked(False);       # classic text-label mode
    page.resize(2800, 800); page.show(); qt_app.processEvents()       # a real window: labels are size-aware
    page.show_packet(txn)
    labels = [x.toPlainText() for x in page.ann_items if isinstance(x, pg.TextItem)]
    assert any(l.startswith("INCOMPLETE") for l in labels)
    assert "STOP UNKNOWN (not captured)" in page.info.text()
    page.close()


def test_waveform_cursor_individual_cycle_and_reciprocal(qt_app):
    from gui.waveform_viewer import WaveformPage
    e = run(synth(write(0x3C, [0x55]), scl_hz=400_000))
    t = e.packets[0]
    page = WaveformPage(SimpleNamespace(engine=e)); page.show_packet(t)
    r0, r1 = t.bytes[1].bit_times[2], t.bytes[1].bit_times[3]
    page.ca.setValue((r0 + 50 - page.origin) / 1e3)
    page.cb.setValue((r1 - page.origin) / 1e3)
    m = page.cursor_measurement()
    assert m["dt_ns"] == pytest.approx(r1 - r0 - 50, abs=1)
    assert m["freq_hz"] == pytest.approx(1e9 / m["dt_ns"])
    c = m["cycle"]
    assert (c["rise"], c["next_rise"], c["period_ns"]) == (r0, r1, r1 - r0)
    assert c["high_ns"] + c["low_ns"] == r1 - r0 and c["high_ns"] == pytest.approx(1000, abs=100)
    page._cursor_text()
    txt = page.cursor_lbl.text()
    assert "SCL cycle at A" in txt and "Cursor B" in txt and "Δt = |t_B − t_A|" in txt and "1/Δt" in txt
    page.close()


def test_final_read_nack_labelled_and_counted(qt_app):
    import pyqtgraph as pg
    from gui.waveform_viewer import WaveformPage
    e = run(synth(write_read(0x68, 0x00, [1, 2, 3])))
    assert e.health_summary()["counts"]["raw_nacks"] == 1 and e.health_summary()["counts"]["expected_read_nacks"] == 1
    page = WaveformPage(SimpleNamespace(engine=e)); page.track.setChecked(False);       # classic text-label mode
    page.resize(2800, 800); page.show(); qt_app.processEvents()       # a real window: labels are size-aware
    page.show_packet(e.packets[0])
    labels = [x.toPlainText() for x in page.ann_items if isinstance(x, pg.TextItem)]
    assert labels.count("NACK (end of read)") == 1 and labels.count("ACK") == 5   # W addr, reg, R addr, data 1, data 2
    assert "byte 0xD1 = 0x68<<1 | 1" in labels
    page.close()


# ------------------------------------------------------------------ setting vs measurement
def test_demo_setting_changes_simulated_timing_only_after_request():
    g = DemoGenerator(seed=2)
    e = CaptureEngine(); e.set_source("demo"); e.ingest_lines([g.hello()])
    e.record_scl_setting(100_000, 100_000, "boot", "SIMULATED demo controller")
    for k in range(1, 41):
        e.ingest_lines(g.lines_until(k * 0.05))
    n0 = len(e.packets)
    g.request_scl(400_000)
    e.record_scl_setting(400_000, 400_000, "applied", "SIMULATED demo controller")
    for k in range(41, 81):
        e.ingest_lines(g.lines_until(k * 0.05))
    before = [t.timing["frequency_hz"] for t in e.packets[:n0] if t.timing.get("frequency_hz")]
    after = [t.timing["frequency_hz"] for t in e.packets[n0 + 1:] if t.timing.get("frequency_hz")]
    assert before and all(abs(f - 100_000) < 3_000 for f in before)
    assert after and all(abs(f - 400_000) < 15_000 for f in after)
    assert all(t.timing["frequency_source"] == "SIMULATED raw edges" for t in e.packets)
    c = e.scl_confirmation()
    assert c["state"] == "CONFIRMED" and c["measured_hz"] == pytest.approx(400_000, rel=0.03)
    with pytest.raises(ValueError):
        g.request_scl(1_000_000)


def test_controller_reply_is_not_treated_as_bus_change(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    from communication.serial_manager import SerialWorker
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow(); e = w.engine; e.set_source("live")
        e.stream_connected = True; e.capture_active = True; e.last_record_wall = time.monotonic()
        first = lines_for(synth(write(0x3C, [0, 1])))
        e.ingest_lines(first)
        reply = {"type": "scl_config", "source": "controller", "result": "applied", "requested_hz": 400000,
                 "applied_hz": 399361, "supported_hz": [100000, 400000], "reason": "divider setting"}
        w._on_master_lines([json.dumps(reply).encode()])
        assert "reported 399.361 kHz (divider, not a measurement)" in w.scl_status.text()
        assert "PENDING" in w.scl_status.text()
        # The analyzer keeps capturing 100 kHz traffic: the GUI must report the mismatch, not 400 kHz.
        more = lines_for(synth(seq_of(*[write(0x3C, [k]) for k in range(4)]), t_start=50_000_000),
                         seq0=len(first) - 1)[1:]
        e.ingest_lines(more)
        w._update_scl_status()
        assert "MISMATCH" in w.scl_status.text() and e.scl_confirmation()["measured_hz"] == pytest.approx(100_000, rel=0.02)
        assert "measured rolling 1 s mean" in w.scl_status.text()
        w.close()
    with pytest.raises(RuntimeError):                         # the analyzer port never takes SCL commands
        SerialWorker("COM_TEST", telemetry=False).send_controller("SCL 400000")


def test_visual_playback_speed_does_not_touch_scl(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow(); e = w.engine; e.set_source("live")
        e.ingest_lines(lines_for(synth(write(0x3C, [7]))))
        before = (e.packets[0].timing["frequency_hz"], e.scl_setting, list(e.edges))
        w.visual_speed.setCurrentIndex(w.visual_speed.findData(0.4))   # 0.4x
        assert w.playback.speed == 0.4
        assert (e.packets[0].timing["frequency_hz"], e.scl_setting, list(e.edges)) == before
        w.close()


# ====================================================================== 2026-11-15 audit: 2 MS/s accuracy & one pipeline
# A sample-accurate two-master bus (tests/bus_synth.py): true controller clocks, every level change seen at the first
# 0.5 µs analyzer sample after it, wire records exactly as the analyzer / controllers send them.
from analysis.capture_engine import CaptureEngine as _Engine               # noqa: E402
from analysis.performance_calculator import observed_data_timing           # noqa: E402
from analysis.i2c_decoder import ByteRec, Transaction                      # noqa: E402
from tests.bus_synth import (BusSynth, SHORT_BUF_NS, STRETCH_NS, TICK_NS, TRUE_HZ_100K, TRUE_HZ_400K,  # noqa: E402
                             feed, standard_traffic)


def raw_capture(true_hz, telemetry=True, **kw):
    """Raw 2 MS/s edge capture (the GUI's own decoder) + both masters' telemetry."""
    b = standard_traffic(BusSynth(), true_hz, **kw)
    e = _Engine(); e.set_source("live"); e.stream_connected = True; e.capture_active = True
    for m in (1, 2):
        e.masters.set_connection(m, f"COM3{m + 1}", True)
    e.ingest_lines([b.raw_hello().encode()])
    feed(e, b.raw_records() + (b.master_records() if telemetry else []), ports=(1, 2) if telemetry else ())
    return b, e


def native_capture(true_hz, drop_batch=None, **kw):
    """Native firmware segments + continuous RLE edge stream (late raw-edge matching), + telemetry."""
    b = standard_traffic(BusSynth(), true_hz, **kw)
    e = _Engine(); e.set_source("live"); e.stream_connected = True; e.capture_active = True
    for m in (1, 2):
        e.masters.set_connection(m, f"COM3{m + 1}", True)
    e.ingest_lines([b.native_hello().encode()])
    feed(e, b.segment_records() + b.stream_records(drop_batch=drop_batch) + b.master_records())
    return b, e


def pooled_hz(packets):
    """f_SCL of the accepted cycles of these packets pooled (what every cumulative / per-master figure must equal)."""
    ps = pn = 0
    for t in packets:
        kept, _ = scl_timing.robust_periods(t)
        ps += sum(kept)
        pn += len(kept)
    return pn / ps * 1e9 if pn else None


@pytest.mark.parametrize("hz_map,tol", [(TRUE_HZ_100K, 0.002), (TRUE_HZ_400K, 0.005)])
def test_2ms_packet_and_rolling_means_are_accurate_and_single_cycles_disclose_quantisation(hz_map, tol):
    b, e = raw_capture(hz_map)
    assert len(e.packets) == len(b.txns)
    errors = []
    for t, tx in zip(e.packets, b.txns):
        kept, _long = scl_timing.robust_periods(t)
        assert kept and t.timing["sample_resolution_ns"] == TICK_NS
        # each rise group's span is quantised by < 1 sample at each end: |mean error| < groups x 0.5 µs / cycles
        bound = len(scl_timing.rise_groups(t)) * TICK_NS / sum(kept)
        err = abs(t.timing["frequency_hz"] - tx["hz"]) / tx["hz"]
        assert err < bound, (t.id, tx["hz"], t.timing["frequency_hz"], bound)
        errors.append(err)
    assert sorted(errors)[len(errors) // 2] < tol                          # typical packet (multi-byte) error
    s = e.perf.compute()
    assert s.roll_freq_hz == pytest.approx(pooled_hz([t for t in e.packets if t.t_end >= e.perf.last_t - 1e9]), rel=1e-9)
    true_mean = sum(hz_map.values()) / 2
    assert s.freq_avg_hz == pytest.approx(true_mean, rel=0.01)
    if hz_map is TRUE_HZ_400K:
        # a true 2.51-2.52 µs cycle is 5 or 6 samples apart: single cycles read 400 or 333 kHz, the packet mean does not
        periods = {p for t in e.packets for *_x, p in scl_timing.cycles(t)}
        assert {p for p in periods if p < 10_000} == {2500.0, 3000.0}
        lo = min(t.timing["cycle_min_hz"] for t in e.packets if t.timing.get("cycle_min_hz"))
        hi = max(t.timing["cycle_max_hz"] for t in e.packets if t.timing.get("cycle_max_hz"))
        assert lo == pytest.approx(1e9 / 3000) and hi == pytest.approx(1e9 / 2500)


def test_sr_frames_are_split_and_clock_stretch_is_excluded_but_reported():
    b, e = raw_capture(TRUE_HZ_100K)
    stretched = next(t for t, tx in zip(e.packets, b.txns)
                     if tx["frames"][-1]["addr"] == 0x76 and any(r - q > 100_000 for f in tx["frames"]
                                                                 for q, r in zip(f["rises"], f["rises"][1:])))
    assert stretched.event_seq() == "S Sr P"
    sr = next(tt for tt, k in stretched.events if k == "Sr")
    assert all(not (a < sr < bb) for a, bb, _ in scl_timing.cycles(stretched))           # no cycle across the Sr
    assert stretched.timing["cycles_excluded"] == 1 and "long cycle" in stretched.timing["frequency_scope"]
    assert stretched.timing["frequency_hz"] == pytest.approx(TRUE_HZ_100K[2], rel=0.002)
    assert max(stretched.timing["long_low_ns"]) == pytest.approx(STRETCH_NS + 0.55 * 1e9 / TRUE_HZ_100K[2], abs=TICK_NS)
    # the mean t_LOW is NOT inflated by the stretch (same > 3x median rule as the cycles) and HIGH + LOW ~ period
    assert stretched.timing["low_ns"] == pytest.approx(0.55 * 1e9 / TRUE_HZ_100K[2], abs=TICK_NS)
    assert stretched.timing["high_ns"] + stretched.timing["low_ns"] == pytest.approx(stretched.timing["period_ns"], rel=0.01)


def test_partial_byte_and_aborted_transactions_never_crash_or_invent_cycles():
    t = Transaction(id=1, t_start=0.0, t_end=60_000.0)
    t.bytes = [ByteRec(0xEC, True, "addr", 1000, 9000, 0, bit_times=(1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000)),
               ByteRec(0xF7, None, "data", 10000, None, 0, bit_times=(10000, 11000, 12000)),     # ACK clock missing
               ByteRec(0x55, True, "data", 30000, 38000, 0,
                       bit_times=(30000, 31000, 32000, 33000, 34000, 35000, 36000, 37000))]
    groups = scl_timing.rise_groups(t)
    assert all(None not in g for g in groups)
    assert groups[0][-3:] == [10000, 11000, 12000] and groups[1][0] == 30000      # closed at the missing 9th clock
    periods = [p for *_x, p in scl_timing.cycles(t)]
    assert periods == [1000] * 19 and all(not (a <= 12000 < bb and bb >= 30000) for a, bb, _ in scl_timing.cycles(t))
    m = scl_timing.packet_measurement(t)
    assert m["frequency_hz"] == pytest.approx(1e6) and m["cycles"] == 19
    empty = Transaction(id=2, t_start=0.0, t_end=1000.0, events=[(0.0, "S"), (1000.0, "P")])     # START then STOP
    m = scl_timing.packet_measurement(empty)
    assert m["frequency_hz"] is None and m["source"] == "UNKNOWN"
    one = Transaction(id=3, t_start=0.0, t_end=3000.0)
    one.bytes = [ByteRec(0x01, None, "addr", 1000, None, 0, bit_times=(1000,))]  # a single captured bit
    assert scl_timing.cycles(one) == [] and scl_timing.packet_measurement(one)["source"] == "UNKNOWN"


def test_setup_hold_and_long_low_ignore_start_sr_stop_conditions():
    # states: bit1 = SCL, bit0 = SDA.  Two data bits with 2.0 µs setup, then an Sr whose SDA rises only 0.5 µs before
    # its SCL rise (t_SU;STA, not t_SU;DAT), a stale SCL fall before the START, and a STOP whose SCL rise follows a 0.5 µs
    # SDA change.  Before the fix the Sr / STOP rises were taken for data bits: setup_min 500 ns, a long "LOW".
    samples = [(0, 3), (500, 1),            # stale: SCL fall BEFORE the START (must not feed hold / LOW)
               (1000, 3), (1500, 2),         # SCL up, START (SDA falls while SCL HIGH)
               (5000, 0),                    # SCL fall (end of START hold)
               (6000, 1), (8000, 3), (13000, 1),      # data bit 1: SDA change 1.0 µs after fall, setup 2.0 µs
               (14000, 0), (16000, 2), (21000, 0),    # data bit 0: hold 1.0 µs, setup 2.0 µs
               (60000, 1), (60500, 3), (65000, 2), (70000, 0),   # Sr: SDA up 0.5 µs before SCL up (long LOW 39.5 µs)
               (71000, 1), (73000, 3), (78000, 1),    # data bit after Sr: setup 2.0 µs
               (79000, 0), (79500, 2), (84000, 3)]    # STOP: SDA low 0.5 µs before SCL up, SDA up while SCL HIGH
    out = observed_data_timing(samples, TICK_NS)
    assert out["setup_min_ns"] == 2000
    assert out["hold_min_ns"] == 1000
    assert out["long_low_ns"] == []                                     # the Sr / STOP preparation is not a data LOW
    assert out["timing_resolution_ns"] == TICK_NS


def test_tbuf_is_checked_on_raw_edge_captures_too():
    b, e = raw_capture(TRUE_HZ_100K)
    short = [t for t in e.packets if any("tBUF" in n for n in t.notes)]
    assert e.tbuf_violations == len(short) == 1
    assert short[0].timing["bus_free_ns"] == pytest.approx(SHORT_BUF_NS, abs=TICK_NS)
    assert short[0].timing["frequency_hz"] == pytest.approx(TRUE_HZ_100K[1], rel=0.002)
    free = [x for x in b.true_bus_free_ns() if x is not None]
    assert sorted(t.timing["bus_free_ns"] for t in e.packets[1:]) == pytest.approx(sorted(free))


def test_late_raw_match_replaces_the_firmware_average_in_every_statistic():
    b, e = native_capture(TRUE_HZ_400K)
    s = e.perf.compute()
    pk = e.packets
    assert len(pk) == len(b.txns) and e.perf.retimed == len(pk)        # every segment packet got its raw cycles
    assert all(t.timing["frequency_source"] == "captured raw edges" for t in pk if scl_timing.robust_periods(t)[0])
    assert s.freq_avg_hz == pytest.approx(pooled_hz(pk), rel=1e-9)                        # = the Packet Analyzer's cycles
    eligible = [t.timing["frequency_hz"] for t in pk if t.timing.get("cycles_used", 0) >= 8]
    assert s.freq_min_hz == pytest.approx(min(eligible)) and s.freq_max_hz == pytest.approx(max(eligible))
    assert s.inst_freq_hz == pytest.approx(pk[-1].timing["frequency_hz"])
    hist = [h for h in e.perf.history if h["observed"] > 0.5 and h["freq_khz"] == h["freq_khz"]]
    for h in hist:                                                     # every completed second: captured cycles only
        sec = [t for t in pk if int(t.t_start // 1e9) == h["t"]]
        if sec:
            assert h["freq_khz"] == pytest.approx(pooled_hz(sec) / 1e3, rel=1e-6)
    # per master: strictly the telemetry attribution, never guessed; groups add up to the cumulative figures
    truth = [tx["master"] for tx in b.txns]
    assert [t.master_id for t in pk] == truth
    for mid in (1, 2):
        g = s.by_master[mid]
        assert g.freq_mean_hz == pytest.approx(pooled_hz([t for t in pk if t.master_id == mid]), rel=1e-9)
        assert g.freq_mean_hz == pytest.approx(TRUE_HZ_400K[mid], rel=0.005)
    assert s.by_master[None].transactions == 0
    assert sum(g.transactions for g in s.by_master.values()) == s.total
    assert sum(g.busy_ns for g in s.by_master.values()) == pytest.approx(s.busy_ns)
    assert sum(g.data_bytes for g in s.by_master.values()) == s.total_bytes
    assert sum(g.unexpected_nacks for g in s.by_master.values()) == s.unexpected_nacks == 2


def test_a_dropped_stream_batch_leaves_that_packet_on_the_labelled_firmware_average():
    b, e = native_capture(TRUE_HZ_400K, drop_batch=40)
    assert any(ev.kind == "Edge stream gap" and ev.t_hw is not None for ev in e.events)
    fw = [t for t in e.packets if t.timing["frequency_source"] == "analyzer firmware segment"]
    raw = [t for t in e.packets if t.timing["frequency_source"] == "captured raw edges"]
    assert fw and raw and len(fw) + len(raw) == len(e.packets)
    assert all("individual cycles not captured" in t.timing["frequency_scope"] for t in fw)
    s = e.perf.compute()
    # cumulative mean = captured cycles of the matched packets + the firmware averages of the unmatched ones (labelled)
    ps = sum(sum(scl_timing.robust_periods(t)[0]) for t in raw)
    pn = sum(len(scl_timing.robust_periods(t)[0]) for t in raw)
    for t in fw:
        for f in t._perf_clk["entries"]:
            ps, pn = ps + f[1], pn + f[2]
    assert s.freq_avg_hz == pytest.approx(pn / ps * 1e9, rel=1e-9)


def test_unattributed_packets_get_their_own_group_and_are_never_given_a_master():
    b, e = raw_capture(TRUE_HZ_100K, telemetry=False)
    s = e.perf.compute()
    # no controller telemetry: every packet (the OLED 0x3C too - Master 2 also writes it since firmware 2.1, so no
    # target is owned by one master) stays unattributed, in its own group
    assert e.packets and any(t.address == 0x3C for t in e.packets)
    assert all(t.master_id is None and t.master_method == "" for t in e.packets)
    assert s.by_master[None].transactions == len(e.packets)
    assert s.by_master[1].transactions == 0 and s.by_master[2].transactions == 0 and s.by_master[2].freq_mean_hz is None
