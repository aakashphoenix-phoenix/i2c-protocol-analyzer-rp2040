"""Strict turn taking (I2C.c 2.2-turns): the REAL capture of 2026-11-17 and the SIMULATED turn profile.

  Master 1: RTC 0x68 -> BMP280 0x76 -> OLED 0x3C · 4 s idle bus · Master 2: RTC -> BMP280 -> OLED · 4 s idle · ...

Real data: work/measurements/mm_turns.i2ccap (analyzer COM28 + both controller logs, 70 s).  Simulation ground truth:
the simulator's own bus log / turn log (which controller really drove each transaction)."""
import json
import statistics
from collections import Counter
from pathlib import Path

import pytest

from analysis.capture_engine import CaptureEngine
from analysis.communication_health import acknowledgment_counts
from analysis.multi_master import attribution_text
from communication.master_protocol import MasterProtocolError, parse_master_line
from sources.demo_source import BENCH_TURNS_MEASURED, DemoGenerator, DemoSource
from storage.capture_storage import load_capture

ROOT = Path(__file__).resolve().parents[1]
REAL_TURNS = ROOT / "work" / "measurements" / "mm_turns.i2ccap"
RANK = {0x68: 0, 0x76: 1, 0x3C: 2}


def bursts(packets, gap_ns=1e9):
    out = [[packets[0]]]
    for a, b in zip(packets, packets[1:]):
        (out[-1].append(b) if b.t_start - a.t_end < gap_ns else out.append([b]))
    return out


def counts(packets):
    c = Counter()
    for t in packets:
        c.update(acknowledgment_counts(t))
    return c


def replay(path, chunk):
    _meta, lines = load_capture(str(path))
    e = CaptureEngine()
    e.set_source("replay")
    for i in range(0, len(lines), chunk):
        e.ingest_lines(lines[i:i + chunk])
    return e


# ====================================================================== the REAL turn-taking capture
@pytest.fixture(scope="module")
def real():
    return replay(REAL_TURNS, 2000)


def test_real_turns_alternate_in_order_with_4s_idle_gaps(real):
    bs = bursts(real.packets)
    assert len(bs) == BENCH_TURNS_MEASURED["turns"] == 16
    owners = [{t.master_id for t in b} for b in bs]
    assert all(len(o) == 1 for o in owners)
    seq = [o.pop() for o in owners]
    assert seq == [1, 2] * 8                                              # Master 1, Master 2, Master 1, ...
    for b in bs:
        ranks = [RANK[t.address] for t in b]
        assert ranks == sorted(ranks) and set(ranks) == {0, 1, 2}         # RTC -> BMP280 -> OLED, every turn
        assert len(b) in BENCH_TURNS_MEASURED["transfers_per_turn"]
    gaps = [(y[0].t_start - x[-1].t_end) / 1e9 for x, y in zip(bs, bs[1:])]
    lo, med, hi = BENCH_TURNS_MEASURED["gap_s"]
    assert (round(min(gaps), 4), round(statistics.median(gaps), 4), round(max(gaps), 4)) == (lo, med, hi)


def test_real_turns_every_nack_is_a_read_end_nack(real):
    c = counts(real.packets)
    assert len(real.packets) == 1168
    assert (c["acks"], c["raw_nacks"], c["expected_read_nacks"], c["unexpected_nacks"]) == (19368, 48, 48, 0)
    assert real.lost_records == real.transport_errors == real.stream_gaps == 0
    assert real.master_errors == 0                                        # turn records are valid controller records


@pytest.mark.parametrize("chunk", [5000, 2000, 300, 50])
def test_real_turns_attribution_is_complete_at_any_replay_chunk_size(chunk):
    e = replay(REAL_TURNS, chunk)
    assert Counter(t.master_id for t in e.packets) == {1: 584, 2: 584}
    assert {t.master_method for t in e.packets} == {"telemetry"}
    assert len([ev for ev in e.events if ev.kind == "Controller clock aligned"]) == 2      # one correct lock each
    slopes = {mid: s.clock.slope * 1e3 for mid, s in e.masters.state.items()}         # ppm
    assert slopes[1] == pytest.approx(-30, abs=3) and slopes[2] == pytest.approx(13, abs=3)


def test_real_turns_are_reported_per_master(real):
    rows = {r["master_id"]: r for r in real.masters.rows()}
    for mid in (1, 2):
        assert rows[mid]["schedule"] == "turns" and rows[mid]["turns"] >= 8
        assert rows[mid]["turn_last"]["rtc"] == rows[mid]["turn_last"]["oled"] == "ok"
        assert "turn-taking (4 s idle between turns)" in rows[mid]["detail"]
        assert not rows[mid]["simulated"]
    assert not [ev for ev in real.events if ev.kind in ("Turn incomplete", "Peer master silent")]


# ====================================================================== controller protocol
def test_turn_records_are_valid_controller_records():
    ok = [{"type": "turn_start", "master_id": 1, "turn": 3, "t_us": 10, "reason": "after_peer_turn", "idle_before_us": 4000900},
          {"type": "turn_end", "master_id": 2, "turn": 3, "t_us": 20, "dur_us": 148000, "transfers": 72,
           "rtc": "ok", "bmp": "ok", "oled": "ok"},
          {"type": "peer_absent", "master_id": 2, "t_us": 30, "silent_ms": 13501}]
    for d in ok:
        assert parse_master_line(json.dumps(d)) == d
    for bad in ({"type": "turn_start", "turn": 1, "t_us": 1, "reason": "whenever"},
                {"type": "turn_end", "turn": 1, "t_us": 1, "dur_us": 1, "transfers": -1, "rtc": "ok", "bmp": "ok", "oled": "ok"},
                {"type": "peer_absent", "t_us": 1}):
        with pytest.raises(MasterProtocolError):
            parse_master_line(json.dumps(bad))


# ====================================================================== the SIMULATED turn profile
def run_turns(seconds, seed=8, step=0.05, **kw):
    g = DemoGenerator(seed=seed, masters=2, schedule="turns", **kw)
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
def sim():
    return run_turns(42.0)


def test_simulated_turns_match_the_measured_bench(sim):
    g, e, _ctl = sim
    bs = bursts(e.packets)
    seq = [{t.master_id for t in b} for b in bs]
    assert all(len(s) == 1 for s in seq) and [s.pop() for s in seq] == [1, 2] * (len(bs) // 2) + [1] * (len(bs) % 2)
    assert len(bs) >= 10
    for b in bs:
        ranks = [RANK[t.address] for t in b]
        assert ranks == sorted(ranks) and set(ranks) == {0, 1, 2}
    assert [len(b) for b in bs[:2]] == [103, 101]                        # init turns (BMP config written once, by M1)
    assert {len(b) for b in bs[2:]} == set(BENCH_TURNS_MEASURED["transfers_per_turn"])
    gaps = [(y[0].t_start - x[-1].t_end) / 1e9 for x, y in zip(bs, bs[1:])]
    lo, _med, hi = BENCH_TURNS_MEASURED["gap_s"]
    assert lo - 0.0002 <= min(gaps) and max(gaps) <= hi + 0.0002
    for mid in (1, 2):
        dur = [(b[-1].t_end - b[0].t_start) / 1e6 for b in bs[2:] if b[0].master_id == mid]
        assert statistics.median(dur) == pytest.approx(statistics.mean(BENCH_TURNS_MEASURED["turn_ms"][mid]), abs=4)
    hz = statistics.median(t.timing["frequency_hz"] for t in e.packets)
    assert hz == pytest.approx(94_570, rel=0.01)


def test_simulated_turns_nacks_and_attribution_are_honest(sim):
    g, e, ctl = sim
    c = counts(e.packets)
    assert c["raw_nacks"] == c["expected_read_nacks"] > 0 and c["unexpected_nacks"] == 0
    assert [m for (m, *_rest) in g.bench.bus_log] == [t.master_id for t in e.packets]   # simulator ground truth
    assert {t.master_method for t in e.packets} == {"telemetry"} and all(t.demo for t in e.packets)
    assert set(Counter(attribution_text(t) for t in e.packets)) == {"Master 1 (SIMULATED)", "Master 2 (SIMULATED)"}
    assert len([t for t in e.packets if t.master_id == 2 and t.address == 0x3C]) >= 70 * 4   # Master 2 draws now
    for mid, lines in ctl.items():
        recs = [json.loads(x) for x in lines]
        assert all(r["simulated"] is True and r["master_id"] == mid for r in recs)
        hello = next(r for r in recs if r["type"] == "hello")
        assert hello["schedule"] == "turns" and "SIMULATED" in hello["fw_version"]
        starts = [r for r in recs if r["type"] == "turn_start"]
        ends = [r for r in recs if r["type"] == "turn_end"]
        assert starts and len(ends) >= len(starts) - 1
        assert starts[0]["reason"] == ("first_turn" if mid == 1 else "after_peer_turn")
    st = {mid: s.session_stats() for mid, s in e.masters.state.items()}
    assert all(v["arb_lost"] == v["busy_waits"] == v["addr_nack"] == 0 for v in st.values())   # as measured
    rows = {r["master_id"]: r for r in e.masters.rows(now_wall=42.0)}
    assert all(r["simulated"] and r["schedule"] == "turns" and r["turns"] >= 4 for r in rows.values())


def test_absent_probe_stays_opt_in_in_the_turn_profile():
    _g, e, _ctl = run_turns(60.0, inject_absent_probe=True)
    probes = [t for t in e.packets if t.address == 0x2A]
    c = counts(e.packets)
    assert probes and all(t.master_id == 1 for t in probes)
    assert c["address_nacks"] == c["unexpected_nacks"] == len(probes)          # reported, never hidden


def test_profiles_and_defaults():
    assert DemoGenerator(seed=1, masters=2).schedule == "free"                 # measured 2026-11-15 profile unchanged
    assert DemoSource.DEFAULT_SCHEDULE == "turns"                              # Demo mode: the flashed turn firmware
    with pytest.raises(ValueError):
        DemoGenerator(masters=1, schedule="turns")
