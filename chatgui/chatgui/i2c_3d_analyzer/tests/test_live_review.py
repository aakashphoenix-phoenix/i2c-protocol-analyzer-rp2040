"""Live review features (2026-11-17): real-time follow in capture order, the I²C data-frame breakdown, error and
arbitration explanations, and the SEQ / PROBE / ARB controller records.  Built from hand-specified bus traffic
(tests/helpers.py) - no demo stream, no simulated controller."""
import json
from types import SimpleNamespace

import pytest

from analysis.capture_engine import CaptureEngine
from analysis.frame_view import (arbitration_details, arbitration_text, collision_point, error_cause, frame_rows,
                                 is_error)
from analysis.playback import Playback, RealtimeQueue, condensed_stages, stages
from communication.master_protocol import MasterProtocolError, parse_master_line
from tests.helpers import lines_for, seq_of, synth, write, write_read


def engine_for(*builders):
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_lines(lines_for(synth(seq_of(*builders))))
    return e


# ---------------------------------------------------------------- frame breakdown + error cause
def test_address_nack_frame_is_an_invalid_address_error():
    e = engine_for(write(0x2A, [0x00], nack_addr=True))
    t = e.packets[0]
    rows = frame_rows(e, t)
    fields = [r["field"] for r in rows]
    assert fields == ["START (S)", "ADDRESS (7-bit)", "R/W bit", "Address NACK", "STOP (P)"]
    addr = rows[1]
    assert addr["value"] == "0x2A" and "b6..b0 = 0101010" in addr["detail"]
    assert rows[2]["value"].startswith("W = 0") and "0x54 = 01010100" in rows[2]["detail"]
    assert rows[3]["ok"] is False and "Invalid / unacknowledged slave address 0x2A" in rows[3]["detail"]
    assert "SDA 0→1 while SCL = 1" in rows[4]["detail"] and "measured f_SCL" in rows[4]["detail"]
    err = error_cause(e, t)
    assert err["kind"] == "invalid_address"
    assert err["text"].startswith("INVALID ADDRESS ERROR: an unattributed master sent 0x2A (W), no slave answered")
    ev = [x for x in e.events if x.kind == "Address NACK"]
    assert ev and ev[0].severity == "error" and "Invalid / unacknowledged slave address 0x2A" in ev[0].message


def test_read_frame_breakdown_keeps_the_end_of_read_nack_and_is_no_error():
    e = engine_for(write_read(0x76, 0xF7, [0x51, 0x2A, 0x00]))
    t = e.packets[0]
    rows = frame_rows(e, t)
    fields = [r["field"] for r in rows]
    assert fields[:4] == ["START (S)", "ADDRESS (7-bit)", "R/W bit", "Address ACK"]
    assert "Repeated START (Sr)" in fields and fields[-1] == "STOP (P)"
    last = [r for r in rows if r["field"] == "DATA [2] NACK"][0]
    assert last["value"] == "NACK (SDA = 1)" and "normal end-of-read NACK" in last["detail"]   # kept as a NACK
    assert last["ok"] is True
    data = [r for r in rows if r["field"] == "DATA [0]"]
    assert data[-1]["value"] == "0x51" and "01010001" in data[-1]["detail"] and "Slave → Master" in data[-1]["detail"]
    assert error_cause(e, t) is None and not is_error(e, t)


def test_written_byte_nack_is_reported():
    def b(s):
        s.start(); s.byte(0x3C << 1); s.byte(0x00); s.byte(0xAF, ack=False); s.stop()
    e = engine_for(b)
    err = error_cause(e, e.packets[0])
    assert err["kind"] == "data_nack" and "refused byte [1] 0xAF" in err["text"]


# ---------------------------------------------------------------- arbitration explanation
def test_arbitration_text_names_winner_loser_collision_bit_and_outcome():
    e = engine_for(write_read(0x68, 0x00, [0x12, 0x34]))
    win = e.packets[0]
    win.master_id, win.master_method = 2, "telemetry"
    ev = SimpleNamespace(master_id=1, addr=0x76, attempt=1, byte=0, backoff_us=287, retry=True, winner=win)
    txt = arbitration_text(e, ev)
    assert txt.startswith("⚡ ARBITRATION LOST: Master 2 (0x68) vs Master 1 (0x76) — wired-AND SDA=0 won")
    assert "backed off 287 µs & retried, NOT a NACK" in txt
    t_ns, bit, where = collision_point(win, ev)
    # 0x68<<1 = 1101 0000, 0x76<<1 = 1110 1100: first difference at the 3rd bit (b5), where the winner sends 0
    assert bit == 2 and "b5" in where and "winner sent 0, loser sent 1" in where
    addr_b = next(b for b in win.bytes if b.kind == "addr")
    assert t_ns == addr_b.bit_times[2]
    lines = arbitration_details(e, ev)
    assert any(l.startswith("Winner: Master 2") for l in lines) and any("back-off 287 µs" in l for l in lines)


# ---------------------------------------------------------------- real-time follow
def test_condensed_stages_keep_every_ack_class_and_order():
    e = engine_for(write_read(0x76, 0xF7, [1, 2, 3, 4, 5, 6]))
    t = e.packets[0]
    short = condensed_stages(t)
    assert len(short) <= 5 < len(stages(t))
    texts = [s.text for s in short]
    assert texts[0] == "START · ADDR 0x76 W" and texts[-1] == "STOP" and texts[2].startswith("DATA ×7 · Sr · READ")
    assert short[-2].kind == "ack" and short[-2].ack is False                 # the end-of-read NACK is still shown
    e2 = engine_for(write(0x2A, [0x00], nack_addr=True))
    assert [s.text for s in condensed_stages(e2.packets[0])] == ["START · ADDR 0x2A W", "NACK", "STOP"]


def test_realtime_queue_is_fifo_skips_to_stay_current_and_budgets_by_capture_gap():
    q = RealtimeQueue()
    pk = [SimpleNamespace(t_start=i * 0.5e9, address=0x68, master_id=1, master_method="telemetry") for i in range(12)]
    q.offer(pk[:3], now=0.0)
    assert q.pop(now=0.0) is pk[0]
    assert q.budget_s(pk[0]) == pytest.approx(0.5)                          # the real time to the next packet
    assert q.pop(now=0.0) is pk[1] and q.pop(now=0.0) is pk[2]               # strictly in capture order
    q.offer(pk[3:], now=1.0)                                                 # 4.5 s of traffic arrived at once
    nxt = q.pop(now=1.0)
    assert nxt.t_start >= pk[-1].t_start - RealtimeQueue.MAX_LAG_NS and q.skipped > 0   # skipped, never reordered
    left = [t.t_start for t, _ in q.q]
    assert left == sorted(left) and all(x > nxt.t_start for x in left)    # what remains is still in capture order
    pb = Playback()
    pb.realtime = True
    e = engine_for(write_read(0x76, 0xF7, [1, 2, 3, 4, 5, 6]))
    pb.select(e.packets[0], pinned=False, packet_s=0.6)
    assert pb.total_seconds() == pytest.approx(0.6)                          # the packet plays in its real-time budget
    pb.select(e.packets[0], pinned=True)
    assert pb.rt_packet_s is None and pb.stage_seconds() == Playback.STAGE_S  # a pinned packet: full review pace


def test_unattributed_packet_waits_briefly_then_plays_unattributed():
    q = RealtimeQueue()
    t = SimpleNamespace(t_start=0.0, address=0x68, master_id=None, master_method="")
    q.offer([t], now=0.0)
    assert q.pop(now=0.1, can_attribute=lambda: True) is None                 # telemetry may still name its master
    assert q.pop(now=0.1 + RealtimeQueue.SETTLE_S, can_attribute=lambda: True) is t   # then shown as unattributed


# ---------------------------------------------------------------- controller records of SEQ / PROBE / ARB
def test_review_records_are_valid_and_logged():
    recs = [{"type": "seq", "master_id": 1, "enabled": True, "gap_ms": 500},
            {"type": "probe", "master_id": 1, "state": "queued", "addr": 42},
            {"type": "probe", "master_id": 1, "state": "done", "addr": 42, "t_us": 5, "result": "addr_nack"},
            {"type": "arb_test", "master_id": 1, "target": 0x76, "t_us": 7, "transfers": 3, "arb_lost": 1,
             "gave_up": 0, "result": "lost_arbitration"}]
    for d in recs:
        assert parse_master_line(json.dumps(d)) == d
    for bad in ({"type": "seq", "enabled": "yes", "gap_ms": 5}, {"type": "probe", "state": "done", "addr": 42},
                {"type": "arb_test", "target": 1, "t_us": 1, "transfers": 1, "arb_lost": 0, "gave_up": 0, "result": "?"}):
        with pytest.raises(MasterProtocolError):
            parse_master_line(json.dumps(bad))
    e = CaptureEngine()
    e.set_source("live")
    e.ingest_master_lines(1, [json.dumps(d).encode() for d in recs], wall=1.0)
    kinds = [(ev.kind, ev.severity) for ev in e.events]
    assert ("SEQ mode", "info") in kinds and ("Probe", "warning") in kinds and ("Arbitration test", "info") in kinds
    s = e.masters.state[1]
    assert s.seq == (True, 500) and s.last_probe["result"] == "addr_nack" and s.last_arb_test["arb_lost"] == 1
    assert e.master_errors == 0
