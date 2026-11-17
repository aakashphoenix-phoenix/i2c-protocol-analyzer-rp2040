"""Review presentation: slow speeds, Play/Pause + Step, ONE transfer and ONE phase at a time, both masters with all three
targets (Master 2 (Pico 3) <-> OLED 0x3C included), arbitration, NACK toggle and a Packet Analyzer in lockstep.

The presentation sequence is scripted and SIMULATED (sources/presentation_source.py); these tests drive the real window."""
from unittest.mock import patch

import pytest

from analysis.playback import PHASES, phase_info, phase_of, stages
from models.packet_model import C_ACK, C_DATA, C_STAT
from sources.presentation_source import OLED, RTC, BMP, SCRIPT
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)

# every scripted transfer is ONE uniform 2-data-byte transfer (9 stages): RTC / BMP280 are written (transmission) and
# read with a plain 2-byte read (reception); the SSD1306 is write-only on I2C (a command and a display-data transfer)
RD = "READ"
EXPECTED = [(1, RTC, "WRITE"), (1, RTC, RD), (1, BMP, "WRITE"), (1, BMP, RD), (1, OLED, "WRITE"), (1, OLED, "WRITE"),
            (2, RTC, "WRITE"), (2, RTC, RD), (2, BMP, "WRITE"), (2, BMP, RD), (2, OLED, "WRITE"), (2, OLED, "WRITE")]


@pytest.fixture
def win(qt_app):
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    w.show()
    w.nav.setCurrentRow(1)                                   # 3D I²C Network page
    w.timer.stop()
    for n in w._nets():
        n.timer.stop()
    yield w
    w.close()


def step(w, dt=0.1):
    w._playback_wall -= dt
    w._refresh()
    for n in w._nets():
        n._tick(dt)


def run_until(w, n_packets, check=None, limit=40000):
    played = []
    for _ in range(limit):
        step(w)
        t = w.playback.txn
        if t is not None and (not played or played[-1] is not t):
            played.append(t)
        if check is not None:
            check(w)
        if len(played) > n_packets:
            break
    return played


def test_speed_options_are_slow_and_default_below_half(win):
    speeds = [win.visual_speed.itemData(i) for i in range(win.visual_speed.count())]
    assert speeds == ["rt", 0.1, 0.25, 0.4, 0.5] and 2.0 not in speeds and 1.0 not in speeds   # Real-time = live default
    assert win.visual_speed.currentData() == "rt"
    win.btn_present.setChecked(True)                                   # the scripted review always starts slow
    assert win.visual_speed.currentData() == 0.25 < 0.5 and win.playback.speed == 0.25
    win.btn_present.setChecked(False)
    assert win.btn_play.text() == "⏸ Pause" and win.btn_step.text().startswith("Step")


def test_presentation_runs_both_masters_with_all_three_targets_one_transfer_at_a_time(win):
    win.btn_present.click()
    assert win.presentation is not None and win.engine.source == "demo"          # SIMULATED everywhere

    def strict(w):
        # the Packet Analyzer never gets ahead of the 3D view: the playing packet is always the newest row
        if w.engine.packets:
            assert w.playback.txn is w.engine.packets[-1]
        # zero overlap: at most one stage dot in flight (an arbitration loser's amber pulse is the only extra)
        net = w.p_net.net
        assert sum(1 for q in net.particles if not q.done and not q.partial) <= 1

    played = run_until(win, 12, strict)
    got = [(t.master_id, t.address, t.operation) for t in played[:12]]
    assert got == EXPECTED
    # attributed by the scripted master, confirmed by the master's own (SIMULATED) controller transfer report
    assert all(t.master_method in ("presentation", "telemetry") and t.demo for t in played)
    m2_oled = [t for t in played if t.master_id == 2 and t.address == OLED]
    assert len(m2_oled) == 2 and {t.operation for t in m2_oled} == {"WRITE"}       # Master 2 (Pico 3) -> OLED 0x3C
    assert not any(f.rw == "R" for t in played if t.address == OLED for f in t.frames)   # never a fake OLED read
    net = win.p_net.net
    assert net.particles_cut == 0
    # every Master 2 OLED dot ran on Master 2's own wire: from its feeder (write) or back to it (read data / ACK)
    feeder = (net.model.feed_x("SDA"), net.model.master2_bottom())
    assert any(abs(p - feeder[0]) < 1e-6 for p in [net.model.path_to(OLED, "SDA", master=2)[0][0]])
    # the Packet Analyzer shows exactly the packets played so far (released one by one, never dumped)
    assert len(win.engine.packets) == len(played)


def test_each_phase_waits_for_the_previous_phase_dot(win):
    win.btn_present.click()
    seen, flying = [], [False]

    def watch(w):
        pb = w.playback
        if pb.txn is not None and pb.events:
            key = (id(pb.txn), pb.stage_index)
            if not seen or seen[-1] != key:
                if seen and seen[-1][0] == key[0]:
                    assert key[1] == seen[-1][1] + 1                              # never skips a phase
                    assert not flying[0]                                          # the previous phase's dot had arrived
                seen.append(key)
        flying[0] = w._dots_in_flight()                                           # state the NEXT refresh decides on

    run_until(win, 3, watch)
    assert len(seen) > 20


def test_phase_breakdown_write_and_read():
    from analysis.capture_engine import CaptureEngine
    from sources.presentation_source import PresentationBus
    e = CaptureEngine(); e.set_source("demo")
    bus = PresentationBus()
    e.ingest_lines([bus.hello()])
    for _ in range(6):
        e.ingest_lines(bus.next_transfer()[0])
    write, read = e.packets[4], e.packets[1]                     # Master 1 -> OLED command write; Master 1 RTC read
    ev = stages(write)
    assert [phase_of(ev, i) for i in range(len(ev))] == [1, 2, 2, 3, 4, 5, 4, 5, 6]
    info = [phase_info(ev, i, 0.5, "Master 1 (Pico 1)", "OLED 0x3C") for i in range(len(ev))]
    assert info[0]["sda"] == "1 → 0" and info[0]["scl"] == "1" and info[-1]["sda"] == "0 → 1"
    assert info[1]["name"] == "Address 0x3C + W=0" and info[3]["name"] == "Address ACK"
    assert info[3]["direction"] == "Slave → Master" and info[3]["sda"] == "0"
    assert info[4]["direction"] == "Master → Slave" and info[4]["value"] == 0x00 and info[4]["bits"] == "00000000"
    ev = stages(read)                     # plain read from the pointer 0x01: minutes, hours (BCD) - same 9 stages
    assert [phase_of(ev, i) for i in range(len(ev))] == [1, 2, 2, 3, 4, 5, 4, 5, 6]
    info = [phase_info(ev, i, 0.5, "Master 1 (Pico 1)", "RTC 0x68") for i in range(len(ev))]
    assert info[1]["name"] == "Address 0x68 + R=1"
    data = next(x for x in info if x["num"] == 4 and x["direction"] == "Slave → Master")
    assert data["sender"] == "RTC 0x68" and data["value"] == 0x22                 # minutes = 22 (BCD)
    assert [x["name"].split(" = ")[0] for x in info if x["num"] == 4] == ["Data byte 1/2", "Data byte 2/2"]
    last_ack = [x for x in info if x["num"] == 5][-1]
    assert last_ack["name"] == "Data NACK" and last_ack["sender"] == "Master 1 (Pico 1)" and last_ack["sda"] == "1"
    assert len(PHASES) == 6


def test_banner_and_packet_analyzer_follow_the_same_phase(win):
    from gui.phase_panel import banner_text
    win.btn_present.click()
    for _ in range(4000):
        step(win)
        pb = win.playback
        if pb.txn is not None and pb.txn.id == 2 and pb.current.kind == "data" and pb.current.frame_rw == "R":
            break
    pb = win.playback
    txt = banner_text(pb)
    assert txt == "[Master 1 (Pico 1) → RTC 0x68 · READ] Phase 4/6: Data byte 1/2 = 0x22"
    assert win.p_net.net.phase_panel.text == txt == win.p_pk.phase.text            # 3D and Packet Analyzer agree
    m = win.packet_model
    row = len(m.rows) - 1
    assert m.data(m.index(row, C_DATA)).startswith("R: 22") and "08" not in m.data(m.index(row, C_DATA))
    assert m.data(m.index(row, C_ACK)) == "A"                        # the read address so far
    assert m.data(m.index(row, C_STAT)).startswith("▶ Phase 4/6")
    for _ in range(4000):                                                           # finished: the full row
        step(win)
        if win.playback.txn.id == 3:
            break
    assert m.data(m.index(1, C_ACK)) == "A A N"
    assert m.data(m.index(1, C_DATA)) == "R: 22 08"


def test_step_next_phase_pauses_and_walks_into_the_next_transfer(win):
    win.btn_present.click()
    step(win)
    first = win.playback.txn
    win.btn_step.click()
    assert win.stage_hold and win.btn_play.text() == "▶ Play" and win.playback.stage_index == 1
    idx = win.playback.stage_index
    for _ in range(30):
        step(win, 0.5)                                                              # held: nothing advances
    assert win.playback.stage_index == idx and win.playback.txn is first
    for _ in range(len(win.playback.events) - 1):                                  # phase 2 .. STOP, then the next one
        win.btn_step.click()
    assert win.playback.txn is not first and win.playback.stage_index == 0          # the next transfer, phase 1
    assert win.playback.txn.master_id == 1 and win.playback.txn.operation == RD
    win.btn_play.click()
    assert not win.stage_hold and win.btn_play.text() == "⏸ Pause"


def test_nack_toggle_makes_the_next_target_nack_its_address(win):
    win.btn_present.click()
    step(win)
    win.btn_nack.setChecked(True)
    played = run_until(win, 2)
    t = played[1]
    assert t.addr_nack and t.ack_seq() == "N" and not win.btn_nack.isChecked()      # one-shot
    ev = stages(t)
    i = next(k for k, s in enumerate(ev) if s.kind == "ack")
    assert phase_info(ev, i)["name"] == "Address NACK"


def test_first_transfer_shows_arbitration_and_the_loser_waits(win):
    win.btn_present.click()
    step(win, 0.05)
    net = win.p_net.net
    t = win.playback.txn
    assert t.presentation["arbitration"]["loser"] == 2 and any("arbitration" in n for n in t.notes)
    amber = [q for q in net.particles if q.partial]
    assert amber and amber[0].src == "master2"                                      # Master 2's pulse stops short
    assert "Arbitration & START" in net.phase_panel.text
    assert win.packet_model.data(win.packet_model.index(0, C_STAT)).endswith("Arbitration & START")


def test_stop_during_presentation_drains_then_freezes(win):
    win.btn_present.click()
    for _ in range(10):
        step(win)
    playing = win.playback.txn
    win.btn_stop.click()
    assert win.net_run_state == "draining" and win.presentation is None
    for _ in range(4000):
        step(win)
        if win.net_run_state == "stopped":
            break
    assert win.net_run_state == "stopped" and win.playback.txn is playing and win.playback.finished
    n = len(win.engine.packets)
    for _ in range(50):
        step(win)
    assert len(win.engine.packets) == n                                             # no further transfer released


def test_script_is_uniform_and_covers_every_pair():
    """Every master with every target; RTC / BMP280 written AND read; the SSD1306 only written (write-only on I2C): a
    command transfer [0x00, cmd] and a display-data transfer [0x40, column].  Every transfer: exactly 2 data bytes."""
    pairs = {(m, a) for m, a, *_rest in SCRIPT}
    assert pairs == {(m, a) for m in (1, 2) for a in (RTC, BMP, OLED)}
    assert all(len(row) == 5 and len(row[3]) == 2 for row in SCRIPT)               # (master, target, op, data, what)
    for m in (1, 2):
        for a in (RTC, BMP):
            assert [op for mm, aa, op, *_r in SCRIPT if (mm, aa) == (m, a)] == ["W", "R"]
        oled = [d for mm, aa, op, d, _t in SCRIPT if (mm, aa) == (m, OLED)]
        assert [op for mm, aa, op, *_r in SCRIPT if (mm, aa) == (m, OLED)] == ["W", "W"]
        assert [d[0] for d in oled] == [0x00, 0x40]


def test_every_transfer_has_the_same_nine_stages_and_duration():
    from analysis.capture_engine import CaptureEngine
    from analysis.playback import Playback
    from sources.presentation_source import PresentationBus
    e = CaptureEngine(); e.set_source("demo")
    bus = PresentationBus()
    e.ingest_lines([bus.hello()])
    for _ in SCRIPT:
        e.ingest_lines(bus.next_transfer()[0])
    totals = set()
    for t in e.packets:
        ev = stages(t)
        assert [phase_of(ev, i) for i in range(len(ev))] == [1, 2, 2, 3, 4, 5, 4, 5, 6]
        pb = Playback(); pb.select(t, pinned=False, full_pace=True); pb.speed = 0.25
        totals.add(round(pb.total_seconds(), 6))
    assert totals == {round(8.5 * 0.8 / 0.25, 6)}                                    # 27.2 s each at 0.25x


def test_sensor_monitoring_decodes_from_the_seeded_registers(win):
    win.btn_present.click()
    run_until(win, 4)
    s = win.engine.sensors
    assert s.rtc.time is not None and s.rtc.time.value[:3] == (8, 22, 30)
    assert s.bmp.temperature is not None and 20 < s.bmp.temperature.value < 30
    assert s.bmp.pressure is not None and 950 < s.bmp.pressure.value < 1050


def test_bench_timing_and_no_cloned_packets():
    """The waveform carries the MEASURED bench clock (HIGH 4.44 us / LOW 6.13 us, ~94.6 kHz) with natural jitter, and
    two transfers never share a cloned timing pattern."""
    from analysis.capture_engine import CaptureEngine
    from analysis.scl_timing import cycles
    from sources.presentation_source import PresentationBus
    e = CaptureEngine(); e.set_source("demo")
    bus = PresentationBus()
    e.ingest_lines([bus.hello()])
    for _ in range(len(SCRIPT) + 1):
        e.ingest_lines(bus.next_transfer()[0])
    for t in e.packets:
        assert 93_000 <= t.timing["frequency_hz"] <= 96_500, t.timing["frequency_hz"]
        assert 4.2e3 <= t.timing["high_ns"] <= 4.7e3 and 5.9e3 <= t.timing["low_ns"] <= 6.4e3
    patterns = [tuple(round(p) for _a, _b, p in cycles(t))[:8] for t in e.packets]
    assert len(set(patterns)) == len(patterns)                                       # no two packets cloned
    first, again = e.packets[0], e.packets[len(SCRIPT)]                               # same script entry, next cycle
    assert (first.data_hex(), first.ack_seq()) == (again.data_hex(), again.ack_seq())
    assert patterns[0] != patterns[len(SCRIPT)]


def test_every_pair_runs_at_one_uniform_pace():
    """Transfers differ in length (a 6-byte ADC burst vs a 2-byte command), but every step of every pair lasts the same:
    nothing is compressed, no device is fast-forwarded."""
    from analysis.capture_engine import CaptureEngine
    from analysis.playback import Playback
    from sources.presentation_source import PresentationBus
    e = CaptureEngine(); e.set_source("demo")
    bus = PresentationBus()
    e.ingest_lines([bus.hello()])
    for _ in SCRIPT:
        e.ingest_lines(bus.next_transfer()[0])
    per_step = set()
    for t in e.packets:
        pb = Playback(); pb.select(t, pinned=False, full_pace=True); pb.speed = 0.25
        steps = sum(0.5 if s.kind == "rw" else 1.0 for s in pb.events)
        per_step.add(round(pb.total_seconds() / steps, 9))
    assert per_step == {round(0.8 / 0.25, 9)}                                        # 3.2 s per step at 0.25x
