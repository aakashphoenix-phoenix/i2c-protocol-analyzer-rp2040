"""Guided flow (Real Hardware Mode, firmware 2.5-turns CONDUCT): the GUI sends one step at a time, waits for that exact
packet from that exact master, plays it completely, then sends the next - Master 1 RTC -> BMP280 -> OLED, Master 2
RTC -> BMP280 -> OLED, then an injected error (rotating invalid addresses / arbitration pairs)."""
from types import SimpleNamespace

from gui.guided_flow import ARB_PAIRS, PROBE_ADDRS, GuidedFlow


class FakeWin:
    def __init__(self):
        self.sent, self.played, self.logs = [], [], []
        self.engine = SimpleNamespace(packets=[], log=lambda *a: self.logs.append(a),
                                      masters=SimpleNamespace(arb_total=0, arb_events=[], _resolve_winner=lambda ev: None))
        self._id = 0

    def _review_masters(self):
        return [1, 2]

    def _review_send(self, mids, cmd):
        self.sent.append((tuple(mids), cmd))
        return mids

    def play_guided(self, txn, stage_s, step):
        self.played.append((txn, step))

    def bus(self, addr, mid, status="ok"):
        self._id += 1
        t = SimpleNamespace(id=self._id, address=addr, master_id=mid, status=status, t_start=float(self._id))
        self.engine.packets.append(t)
        return t


def drive(g, w, now, answer):
    """One step: tick until a command is sent, let the 'bus' answer, tick until it is played."""
    n = len(w.sent)
    while len(w.sent) == n:
        now += 0.5
        g.tick(now)
    answer(w.sent[-len(w.sent) + n:], now)
    m = len(w.played)
    for _ in range(20):
        now += 0.3
        g.tick(now)
        if len(w.played) > m:
            break
    return now


def test_one_cycle_in_order_then_rotating_errors():
    w = FakeWin()
    g = GuidedFlow(w, stage_s=0.25)
    g.start(now=0.0)
    assert w.sent[0] == ((1, 2), "CONDUCT ON")
    now = 0.0

    def answer(cmds, now):
        for mids, cmd in cmds:
            if cmd.startswith("DO "):
                addr = {"RTC": 0x68, "BME": 0x76, "OLED": 0x3C}[cmd[3:]]
                w.bus(0x76 if addr == 0x68 and False else addr, mids[0])
            elif cmd.startswith("PROBE "):
                w.bus(int(cmd[6:], 16), mids[0], status="ok")
    for _ in range(7):                                   # cycle 1: 6 steps + invalid address
        now = drive(g, w, now, answer)
    cmds = [c for _m, c in w.sent[1:]]
    assert cmds[:6] == ["DO RTC", "DO BME", "DO OLED", "DO RTC", "DO BME", "DO OLED"]
    assert [m for m, _c in w.sent[1:7]] == [(1,), (1,), (1,), (2,), (2,), (2,)]          # Master 1 then Master 2
    assert cmds[6] == f"PROBE {PROBE_ADDRS[0]:02X}" and w.sent[7][0] == (1,)
    assert [(t.address, t.master_id) for t, _s in w.played] == [
        (0x68, 1), (0x76, 1), (0x3C, 1), (0x68, 2), (0x76, 2), (0x3C, 2), (PROBE_ADDRS[0], 1)]
    # cycle 2 ends with an arbitration between different slaves; cycle 3 probes another address from Master 2
    for _ in range(6):
        now = drive(g, w, now, answer)
    n = len(w.sent)
    while len(w.sent) < n + 2:
        now += 0.5
        g.tick(now)
    assert w.sent[-2:] == [((1,), f"ARB {ARB_PAIRS[0][0]:02X}"), ((2,), f"ARB {ARB_PAIRS[0][1]:02X}")]
    winner = w.bus(ARB_PAIRS[0][1], 2)
    ev = SimpleNamespace(master_id=1, winner=winner)
    w.engine.masters.arb_events.append(ev)
    w.engine.masters.arb_total += 1
    for _ in range(12):
        now += 0.3
        g.tick(now)
    assert w.played[-1][0] is winner and w.played[-1][1]["kind"] == "ARB"
    for _ in range(7):
        now = drive(g, w, now, answer)
    probes = [(m, c) for m, c in w.sent if c.startswith("PROBE")]
    assert probes == [((1,), f"PROBE {PROBE_ADDRS[0]:02X}"), ((2,), f"PROBE {PROBE_ADDRS[1]:02X}")]


def test_waits_for_the_right_master_and_skips_a_missing_packet():
    w = FakeWin()
    g = GuidedFlow(w, inject_errors=False)
    g.start(now=0.0)
    now = 1.0
    g.tick(now)                                          # DO RTC to Master 1
    w.bus(0x68, 2)                                       # Master 2's packet is not this step's
    g.tick(now + 0.3)
    assert not w.played
    w.bus(0x68, 1)
    g.tick(now + 0.6)
    assert w.played and w.played[0][0].master_id == 1
    g.tick(now + 0.7)                                    # DO BME ... and nothing comes back
    g.tick(now + 6.0)
    assert any("no packet" in str(a) for a in w.logs)
    g.stop()
    assert w.sent[-1] == ((1, 2), "CONDUCT OFF") and not g.active


def test_nack_next_transfer_queues_a_real_invalid_address_for_the_master_due_next():
    """Presentation mode's 'NACK next transfer' on real hardware: the next step becomes a PROBE by the master due next."""
    w = FakeWin()
    g = GuidedFlow(w, inject_errors=False)
    g.start(now=0.0)
    for k, addr in enumerate((0x68, 0x76, 0x3C)):        # Master 1's three steps
        g.tick(1.0 + k)
        w.bus(addr, 1)
        g.tick(1.1 + k)
    st = g.queue_nack()
    assert st["kind"] == "PROBE" and st["mid"] == 2 and st["addr"] == PROBE_ADDRS[0] and st["user"]
    g.tick(4.0)
    assert w.sent[-1] == ((2,), f"PROBE {PROBE_ADDRS[0]:02X}")
    w.bus(PROBE_ADDRS[0], 2)
    g.tick(4.1)
    assert w.played[-1][1].get("user")
    g.tick(4.2)
    assert w.sent[-1] == ((2,), "DO RTC")                # the normal order resumes
    assert g.queue_nack()["addr"] == PROBE_ADDRS[1]      # rotates


def test_step_forces_the_next_command_and_the_arbitration_winner_carries_the_phase1_picture():
    w = FakeWin()
    g = GuidedFlow(w)
    g.start(now=0.0)
    g.tick(0.1)
    assert len(w.sent) == 1                              # the 0.4 s settle after CONDUCT ON
    g.tick(0.1, force=True)                              # Step ▸
    assert w.sent[-1] == ((1,), "DO RTC")
    g.steps = [dict(kind="ARB", a1=0x76, a2=0x68)]
    w.bus(0x68, 1)
    g.tick(0.2)
    g.tick(0.3)                                          # ARB sent
    winner = w.bus(0x68, 2)
    w.engine.masters.arb_events.append(SimpleNamespace(master_id=1, winner=winner))
    w.engine.masters.arb_total += 1
    for k in range(12):
        g.tick(0.5 + k * 0.3)
    assert w.played[-1][0] is winner
    arb = winner.presentation["arbitration"]
    assert arb["winner"] == 2 and arb["loser"] == 1 and arb["live"] and "REAL arbitration" in arb["text"]


def test_guided_playback_caps_a_long_oled_text_write_but_keeps_short_packets_whole():
    """2.6-live OLED step = one 251-byte write: the guided flow plays its first 8 bytes byte by byte (each ACK), the rest
    as one condensed stage + its ACK; RTC / BMP280 reads stay complete, and a NACK is never condensed away."""
    from analysis.capture_engine import CaptureEngine
    from analysis.playback import Playback, capped_stages, stages
    from tests.helpers import lines_for, seq_of, synth, write, write_read

    def packet(builder):
        e = CaptureEngine()
        e.set_source("live")
        e.ingest_lines(lines_for(synth(seq_of(builder))))
        return e.packets[0]

    oled = packet(write(0x3C, [0x80, 0x20] * 8 + [0x40] + [0x7F] * 60))
    full, cap = stages(oled), capped_stages(oled)
    assert len(full) > 150 and len(cap) < 30
    data = [s for s in cap if s.kind == "data"]
    assert len(data) == 9 and "condensed" in data[-1].text and "×69" in data[-1].text
    assert [s.kind for s in cap[-3:]] == ["data", "ack", "cond"] and cap[-1].text == "STOP"
    rtc = packet(write_read(0x68, 0x00, [1, 2, 3, 4, 5, 6, 7]))
    assert capped_stages(rtc) == stages(rtc)
    pb = Playback()
    pb.select(oled, pinned=False, stage_s=0.25)
    assert pb.events == cap and pb.total_seconds() < 8 / max(pb.speed, 0.05)
