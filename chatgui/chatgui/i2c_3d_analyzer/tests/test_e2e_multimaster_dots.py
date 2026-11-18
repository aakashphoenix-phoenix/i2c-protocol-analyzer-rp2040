"""End-to-end movement dots: real analyzer segments + real Master 1 / Master 2 `txn` telemetry (tests.test_multi_master.Bench,
two controllers with independent clocks and their own USB streams) -> CaptureEngine.ingest_lines / ingest_master_lines ->
MultiMasterTracker -> FollowQueue -> MainWindow._refresh() -> Network3D (2D renderer, headless).

Nothing here assigns a master by hand: every master_id seen below was produced by the tracker.  The GUI clock is simulated
(time.monotonic is patched), so the 0.8 s attribution grace of the fair queue is exercised deterministically.

What it guards (2026-11-15 audit):
  * start-of-capture warm-up: before a controller clock is aligned nothing can attribute a packet - it plays UNATTRIBUTED from
    the analyzer tap, a master is never guessed - and the later alignment upgrades it mid-play without cutting a dot;
  * telemetry arriving after decoding (a stalled port): inside the grace period the queue holds the packet, beyond it the dot
    upgrades None -> 2 mid-stage;
  * Master 1 oled_show() bursts (6 commands + 64 x 16-byte chunks) interleaved with Master 2 polling 0x68 / 0x76 and a custom
    Master 2 -> 0x3C packet: ownership never blocks the telemetry override (1 -> 2) and no address is starved;
  * the toolbar speed survives capture / decoder resets;
  * every dot completes its whole wire, nothing is cut, ghosted or left frozen."""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)
from tests.test_multi_master import HELLO as MM_HELLO, Bench

REFRESH = 0.1
FRAME = 1 / 30


# ------------------------------------------------------------------ traffic
def oled_show(bench, t):
    """firmware oled_show(): 6 command writes + 64 framebuffer chunks of (0x40 + 16 bytes) = a 70-packet burst."""
    for c in (0x21, 0x00, 0x7F, 0x22, 0x00, 0x07):
        bench.transfer(1, t, 0x3C, [0x00, c])
    for k in range(64):
        bench.transfer(1, t, 0x3C, [0x40] + [(7 * k + i) & 0xFF for i in range(16)])


def mm_traffic(bench, seconds=12.0, custom_m2_oled=True):
    """Master 1: BMP280 every 250 ms + an oled_show() burst every 4 s.  Master 2: BMP280 every 330 ms, RTC every 1.1 s and
    (optionally) one custom write to the OLED.  Shared BMP280 / DS3231 / OLED bus."""
    T0 = 10_000_000_000.0
    sched = [(T0 + k * 250e6, 1, "bmp") for k in range(int(seconds / 0.25))]
    sched += [(T0 + t * 1e9, 1, "oled") for t in (1.0, 5.0, 9.0) if t < seconds]
    sched += [(T0 + 97e6 + k * 330e6, 2, "bmp") for k in range(int(seconds / 0.33))]
    sched += [(T0 + 150e6 + k * 1100e6, 2, "rtc") for k in range(int(seconds / 1.1))]
    if custom_m2_oled:
        sched.append((T0 + 2.5e9, 2, "custom"))
    for t, mid, what in sorted(sched):
        if what == "bmp":
            bench.transfer(mid, t, 0x76, [0xF7], 6)
        elif what == "rtc":
            bench.transfer(mid, t, 0x68, [0x00], 16)
        elif what == "oled":
            oled_show(bench, t)
        else:
            bench.transfer(2, t, 0x3C, [0x40, 0xAA, 0xBB, 0xCC])         # Master 2 addressing the Master-1-owned OLED


# ------------------------------------------------------------------ simulation of the real GUI loop
def live_window_mm():
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    w.timer.stop()                                  # the test drives _refresh() itself (simulated clock)
    w.worker = type("W", (), {"send": lambda self, c: None, "isRunning": lambda self: True, "port": "REPLAY",
                               "stop": lambda self: None, "wait": lambda self, t=0: True})()
    w.capturing = True
    w.engine.set_source("live"); w.engine.stream_connected = True; w.engine.capture_active = True
    w._on_lines([json.dumps(MM_HELLO).encode()])
    for mid in (1, 2):
        w.engine.masters.set_connection(mid, f"COM3{1 + mid}", True)
    return w


def run_window(qt_app, events, speed, seconds, ports=(1, 2), hooks=None):
    """Feed `events` (wall, kind, line) in simulated time and drive MainWindow._refresh() every 100 ms, ticking the dots at
    30 Hz.  hooks: {sim_seconds: fn(window)} run once when the simulated clock reaches that time.  Returns a log of every
    played packet and every spawned stage dot."""
    w = live_window_mm()
    w.show()
    w.nav.setCurrentRow(1)                          # 3D Network page: this diagram is the visible one
    qt_app.processEvents()
    w.timer.stop()
    net = w.p_net.net
    net.timer.stop()
    assert net.isVisible() and not net.use_gl
    w.visual_speed.setCurrentIndex(w.visual_speed.findData(speed))
    events = sorted(events, key=lambda e: e[0])
    t0 = events[0][0]
    clk = [1000.0]
    log = SimpleNamespace(w=w, net=net, plays=[], dots=[], lock_at=None, sim=0.0, t0=t0)
    real_spawn = net._spawn_stage_particle
    state = {"last": None}
    hooks = dict(hooks or {})

    def spy(stage, txn):
        before = len(net.particles)
        real_spawn(stage, txn)
        if len(net.particles) > before:
            log.dots.append(SimpleNamespace(p=net.particles[-1], txn=txn, stage=stage, mid=txn.master_id,
                                            method=getattr(txn, "master_method", ""), t=log.sim,
                                            idx=w.playback.stage_index, speed=w.playback.speed))

    net._spawn_stage_particle = spy
    i = 0
    with patch.object(time, "monotonic", lambda: clk[0]):
        for step in range(int(seconds / REFRESH)):
            clk[0] += REFRESH
            log.sim = (step + 1) * REFRESH
            for at in [k for k in hooks if k <= log.sim]:
                hooks.pop(at)(w)
            while i < len(events) and events[i][0] - t0 <= log.sim:
                wall, kind, line = events[i]
                w.engine.wall_clock = lambda ww=wall: ww
                if kind == "an":
                    w.engine.ingest_lines([line])
                elif kind in ports:
                    w.engine.ingest_master_lines(kind, [line])
                i += 1
            w._refresh()
            if log.lock_at is None and all(s.clock.locked for s in w.engine.masters.state.values()):
                log.lock_at = log.sim
            txn = w.playback.txn
            if txn is not state["last"] and txn is not None:
                state["last"] = txn
                log.plays.append(SimpleNamespace(txn=txn, t=log.sim, mid=txn.master_id,
                                                 method=getattr(txn, "master_method", ""),
                                                 aligned=w.engine.masters.can_attribute(), speed=w.playback.speed))
            for _ in range(3):
                net._tick(FRAME)
        for _ in range(600):                        # let every last dot arrive
            net._tick(FRAME)
    return log


def expected_path(net, d, mid):
    addr = d.stage.address if d.stage.address is not None else d.txn.address
    return np.asarray(net.model.path_to(addr, "SDA", master=mid), float)


def same_wire(path, want):
    return np.allclose(path, want) or np.allclose(path, want[::-1])


def quiesce(w):
    """Nothing queued, nothing playing: the next decoded packet is the next one shown (state reset only - the packets and
    their attribution still come from the real engine)."""
    w.engine.take_activity()
    w._follow_queue.clear()
    w._follow_shown.clear()
    w._follow_latest_txn = None
    w.playback.position = float(len(w.playback.events))


def check_all_dots(log):
    """Every spawned dot ran the complete wire of the master the packet had WHEN it spawned; nothing cut, ghosted, frozen."""
    net = log.net
    assert log.dots
    for d in log.dots:
        assert same_wire(d.p.path, expected_path(net, d, d.mid)), (d.txn.id, d.stage.text, d.mid)
        assert d.p.done and d.p.s == pytest.approx(d.p.total)                      # 100 % of pin-to-pin
        if d.mid is None:
            assert d.method == ""                                                 # never a guessed master
    assert net.particles == [] and net.particles_cut == 0
    assert net.particles_arrived == len(log.dots)


def truth(b, e):
    by_start = {int(t // 500): mid for t, mid in b.expect}
    assert len(e.packets) == len(b.expect)
    assert [t for t in e.packets if t.master_id is not None and by_start[int(t.t_start // 500)] != t.master_id] == []
    assert all(t.master_method == "" for t in e.packets if t.master_id is None)   # nothing guessed


@pytest.fixture
def cleanup():
    made = []
    yield made
    for w in made:
        w.close()


# ------------------------------------------------------------------ scenarios
@pytest.mark.parametrize("speed", [0.25, 0.5])                # review speeds (no 1x / 2x any more)
def test_oled_bursts_interleaved_with_master_2_polling_starve_nothing_and_every_dot_completes(qt_app, cleanup, speed):
    b = Bench()
    mm_traffic(b, seconds=12.0, custom_m2_oled=True)
    log = run_window(qt_app, b.events, speed, seconds=400.0 / speed)   # one uniform pace: a 17-byte OLED frame ~37 steps
    cleanup.append(log.w)
    w, e = log.w, log.w.engine
    truth(b, e)                                                                    # attribution by the tracker only
    oled_m2 = [t for t in e.packets if t.address == 0x3C and t.master_id == 2]
    assert len(oled_m2) == 1 and oled_m2[0].master_method == "telemetry"           # ownership (M1) overridden: 1 -> 2
    assert sum(1 for t in e.packets if t.address == 0x3C and t.master_id == 1) == 70 * 3     # three 70-packet bursts
    assert log.lock_at is not None and log.lock_at < 6.0                           # both clocks aligned in the warm-up
    # the speed picked in the toolbar survived the capture-context reset of the first _refresh()
    assert log.plays and all(p.speed == speed for p in log.plays) and w.playback.speed == speed
    # no address and no master of a shared address starved: every (address, master) bucket took a turn, the fair queue
    # alternates addresses (never the same one twice in a row while others wait) and drained completely
    played = {(p.txn.address, p.txn.master_id) for p in log.plays}
    assert {(0x3C, 1), (0x3C, 2), (0x68, 2), (0x76, 1), (0x76, 2)} <= played, sorted(played)
    pairs = [(p.txn.address, p.txn.master_id) for p in log.plays]
    assert all(x != y for x, y in zip(pairs, pairs[1:])), pairs                   # never the same pair twice in a row
    assert len(w._follow_queue) == 0 and w.playback.finished
    check_all_dots(log)
    # a packet attributed by telemetry never plays its stages from the analyzer tap after the warm-up
    late = [d for d in log.dots if d.t > log.lock_at + 2.0]
    assert late and [d for d in late if d.mid is None and d.txn.master_id is not None] == []
    # Master 2 dots (RTC / BMP280 / custom OLED) start at Master 2's feeder pin or end there (reads / slave ACKs)
    m2 = [d for d in log.dots if d.mid == 2]
    assert {d.txn.address for d in m2} == {0x68, 0x76, 0x3C}
    feeder = np.array((log.net.model.feed_x("SDA"), log.net.model.master2_bottom(), 0.25))
    assert all(np.allclose(d.p.path[0], feeder) or np.allclose(d.p.path[-1], feeder) for d in m2)


def test_warm_up_before_clock_alignment_plays_unattributed_then_upgrades_without_a_cut_dot(qt_app, cleanup):
    b = Bench()
    mm_traffic(b, seconds=6.0, custom_m2_oled=False)
    log = run_window(qt_app, b.events, 0.5, seconds=60.0)                          # 1x is no longer offered
    cleanup.append(log.w)
    truth(b, log.w.engine)
    assert log.lock_at is not None
    warm = [d for d in log.dots if d.t < log.lock_at]                              # before the refresh that locked
    assert warm and all(d.mid is None or d.method in ("ownership", "telemetry") for d in warm)   # never guessed
    # (one master may align before the other: its own telemetry then attributes legitimately before lock_at)
    assert [d for d in warm if d.mid is None]                                      # (the shared BMP280 played from the tap)
    first = log.plays[0]
    assert first.txn.address in (0x68, 0x76) and not first.aligned                 # nothing held: playing started at once
    first_dots = [d for d in log.dots if d.txn is first.txn]
    assert first_dots[0].mid is None and first.txn.master_id in (1, 2)             # attributed retroactively (None -> M)
    assert first_dots[-1].mid == first.txn.master_id                               # the later stages use the master's wire
    check_all_dots(log)


def late_master_2_scenario(qt_app, lag, cleanup):
    """Lock both clocks, quiesce the GUI queue, then ONE Master 2 RTC read whose controller report arrives `lag` seconds after
    the analyzer decoded the packet (a stalled USB port)."""
    b = Bench()
    mm_traffic(b, seconds=4.0, custom_m2_oled=False)
    b.transfer(2, 10_000_000_000.0 + 6.0e9, 0x68, [0x00], 6)                      # 6 data bytes: 23 stages
    wall, kind, line = b.events[-1]
    assert kind == 2
    b.events[-1] = (wall + lag, 2, line)
    log = run_window(qt_app, b.events, 0.5, seconds=90.0, hooks={5.5: quiesce})   # 1x is no longer offered
    cleanup.append(log.w)
    truth(b, log.w.engine)
    target = [p for p in log.plays if p.t > 5.5]
    assert len(target) == 1 and target[0].txn.address == 0x68
    return log, target[0], [d for d in log.dots if d.txn is target[0].txn]


def test_telemetry_inside_the_grace_period_means_the_first_stage_already_uses_master_2s_full_wire(qt_app, cleanup):
    log, play, dots = late_master_2_scenario(qt_app, 0.4, cleanup)
    assert play.mid == 2 and play.method == "telemetry"                            # held until the report arrived: no tap stub
    assert {d.mid for d in dots} == {2} and len(dots) >= 20
    check_all_dots(log)


@pytest.mark.parametrize("lag", [4.3, 5.2])
def test_telemetry_after_the_grace_period_holds_the_packet_until_master_2_is_known(qt_app, cleanup, lag):
    """A controller report that arrives seconds late (a stalled USB port): while a master is connected and aligned the
    3D view never animates the packet as an unnamed master ('?') - it is held and, once Master 2's report arrives, plays
    entirely on Master 2's full wire (no bus-stub dots, none cut)."""
    log, play, dots = late_master_2_scenario(qt_app, lag, cleanup)
    assert play.mid == 2 and play.method == "telemetry"
    assert dots and {d.mid for d in dots} == {2} and len(dots) >= 20
    check_all_dots(log)


def test_toolbar_speed_survives_new_capture_and_decoder_resets(qt_app, cleanup):
    w = live_window_mm()
    cleanup.append(w)
    w.timer.stop()
    # "Real-time" (live packets in capture order) is the default since 2026-11-17; the review speeds stay slow (no 2x)
    assert [w.visual_speed.itemData(i) for i in range(w.visual_speed.count())] == ["rt", 0.1, 0.25, 0.4, 0.5]
    assert w.visual_speed.currentData() == "rt" and w.playback.realtime and w.playback.speed == 1.0
    for speed in (0.1, 0.4, 0.5):
        w.visual_speed.setCurrentIndex(w.visual_speed.findData(speed))
        assert w.playback.speed == speed
        w._refresh()                                                               # first call after a new decoder context
        assert w.playback.speed == speed
        w._playback_context = None                                                 # e.g. a new demo / replay / capture
        w._refresh()
        assert w.playback.speed == speed
        w.clear_capture()                                                          # "Clear": a brand-new decoder
        w._refresh()
        assert w.playback.speed == speed and w.visual_speed.currentData() == speed
