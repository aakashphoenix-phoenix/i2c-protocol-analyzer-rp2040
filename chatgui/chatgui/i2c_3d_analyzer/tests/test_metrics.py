"""Decoder and performance-calculation tests on exactly known traffic."""
import random

import pytest

from analysis.capture_engine import CaptureEngine
from analysis.sensor_decoder import bmp280_compensate
from sources.demo_source import CALIB
from tests.helpers import lines_for, seq_of, synth, write, write_read


@pytest.fixture(autouse=True)
def no_jitter(monkeypatch):
    monkeypatch.setattr(random, "randint", lambda a, b: 0)   # exact 100 kHz timing


def run(edges, **kw):
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(lines_for(edges, **kw))
    return e


def test_write_decoded_exactly():
    e = run(synth(write(0x76, [0xF4, 0x27])))
    assert len(e.packets) == 1
    t = e.packets[0]
    assert (t.address, t.operation, t.frames[0].data, t.status) == (0x76, "WRITE", [0xF4, 0x27], "ok")
    assert t.ack_count == 3 and t.nack_count == 0 and t.event_seq() == "S P"


def test_repeated_start_is_one_transaction():
    e = run(synth(write_read(0x76, 0xD0, [0x58])))
    t = e.packets[0]
    assert len(e.packets) == 1 and t.event_seq() == "S Sr P"
    assert [(f.addr, f.rw) for f in t.frames] == [(0x76, "W"), (0x76, "R")]
    assert t.frames[1].data == [0x58] and t.frames[1].acks == [False]   # master NACKs last read byte
    s = e.perf.compute()
    assert s.read_end_nacks == 1 and s.write_data_nacks == 0 and s.addr_nacks == 0


def test_address_nack():
    e = run(synth(write(0x2A, [], nack_addr=True)))
    t = e.packets[0]
    assert t.addr_nack and t.frames[0].addr == 0x2A
    assert e.perf.compute().addr_nacks == 1
    assert e.registry.node_list(e.hw_now) == []        # never-ACKed address is not drawn as a device


def test_scl_frequency_and_timing():
    e = run(synth(write(0x3C, list(range(20)))))
    s = e.perf.compute()
    assert s.freq_avg_hz == pytest.approx(100_000, rel=1e-3)
    # Simulated Standard-mode timing: HIGH 4.5 us, LOW 5.5 us (SIM_TIMING, UM10204 Table 10 minima met)
    assert s.t_high_ns == pytest.approx(4500, rel=0.02) and s.t_low_ns == pytest.approx(5500, rel=0.02)


def test_rate_and_utilization_math():
    n = 10
    e = run(synth(seq_of(*[write(0x76, [0x10]) for _ in range(n)], gap=10_000)))   # 1 ms gaps
    s = e.perf.compute()
    assert s.complete == n
    busy = sum(t.duration_ns for t in e.packets)
    assert s.busy_ns == pytest.approx(busy)
    assert s.util == pytest.approx(100 * busy / s.observed_ns)
    assert s.tps == pytest.approx(n / (s.observed_ns / 1e9))


def test_gap_not_counted_as_idle():
    edges = synth(seq_of(*[write(0x76, [1]) for _ in range(6)], gap=10_000))
    lines = lines_for(edges, chunk=20)
    del lines[3]                                    # lose one edges record
    e = CaptureEngine()
    e.ingest_lines(lines)
    s = e.perf.compute()
    assert e.lost_records == 1 and s.gaps >= 1
    full = CaptureEngine()
    full.ingest_lines(lines_for(edges, chunk=20))
    assert s.observed_ns < full.perf.compute().observed_ns          # missing interval excluded
    assert len(e.packets) < 6 or any(t.status == "gap" for t in e.packets)   # nothing decoded across the gap


def test_stop_inside_byte_is_incomplete():
    def b(s):
        s.start(); s.byte(0x76 << 1)
        for bit in (1, 0, 1):
            s.bit(bit)
        s.stop()
    e = run(synth(b))
    assert e.packets[0].status == "incomplete"


def test_ten_bit_address():
    def b(s):
        s.start(); s.byte(0xF0 | (0x2 << 1)); s.byte(0x34); s.byte(0xAA); s.stop()
    t = run(synth(b)).packets[0]
    assert t.ten_bit and t.address == 0x234 and t.frames[0].data == [0xAA]


def test_bmp280_compensation_matches_datasheet_example():
    temp, press = bmp280_compensate(CALIB, 519888, 415148)
    assert temp == pytest.approx(25.08, abs=0.01)
    assert press == pytest.approx(1006.53, abs=0.05)


def test_sensor_values_only_from_observed_bytes():
    raw = [0x65, 0x5A, 0xC0, 0x7E, 0xED, 0x00]
    e = run(synth(write_read(0x76, 0xF7, raw)))
    assert e.sensors.bmp.temperature is None            # no calibration captured -> no value invented
    assert "calibration" in e.sensors.bmp.reason
