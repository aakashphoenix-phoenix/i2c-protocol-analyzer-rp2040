"""2026-11-15 audit of the movement dots: contention pulses, paused views, fair-queue starvation, ownership vs telemetry,
the device-filter path and the per-frame smoothness of the longest wires.  (Complements tests/test_dot_full_path.py.)"""
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from analysis.playback import FollowQueue, Playback
from tests.test_dot_full_path import P, engine_with, oled_burst, rtc_read
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)
from tests.test_playback_flow import FRAME, make_net


def arb_event(master_id=2, addr=0x68):
    return SimpleNamespace(master_id=master_id, addr=addr, winner=SimpleNamespace(master_id=3 - master_id))


# ------------------------------------------------------------------ contention (arbitration loss)
def test_contention_pulse_is_a_truncated_amber_partial_that_never_counts_as_a_delivered_bit(qt_app):
    import gui.network_3d as net
    n = make_net(qt_app)
    full = net.Particle(n.model.path_to(0x68, "SDA", master=2), "#fff")
    n.show_contention([arb_event(2, 0x68)])
    pulse = n.particles[-1]
    assert pulse.partial and pulse.color == net.ARB_COL
    assert pulse.color not in (net.master_color(2, "W"), net.master_color(2, "R"), net.UNATTR_COL, net.ACK_COL, net.NACK_COL)
    assert pulse.total == pytest.approx(0.45 * full.total)                          # Master 2 wire, cut at 45 %
    assert np.allclose(pulse.path[0], full.path[0])                                 # leaves the LOSER pin ...
    assert np.linalg.norm(pulse.path[-1] - full.path[-1]) > 0.4 * full.total        # ... and stops far short of the target
    assert pulse.travel_s >= net.MIN_TRAVEL_S
    for _ in range(80):
        n._tick(FRAME)
    assert n.particles == [] and n.particles_arrived == 0 and n.particles_cut == 0   # finished, but NOT a delivered transfer
    assert 0x68 not in n.flash                                                       # the target never lit up as a receiver
    e = engine_with(rtc_read())
    e.packets[0].master_id = 2
    pb = Playback(); pb.select(e.packets[0], pinned=True); pb.position = 1.0
    n.show_stage(pb, e)                                                              # a REAL transfer does count
    for _ in range(80):
        n._tick(FRAME)
    assert n.particles_arrived == 1
    n.close()


def test_contention_pulse_is_never_refused_when_the_dot_list_is_full(qt_app):
    import gui.network_3d as net
    n = make_net(qt_app)
    n.particles = [net.Particle(n.model.path_to(0x68, "SDA", 2), "#fff", travel_s=9.0) for _ in range(net.MAX_PARTICLES)]
    for q in n.particles:
        q.s = q.total                                     # arrived: the wire is free, only old dots fill the list
    oldest = n.particles[0]
    n.show_contention([arb_event(1, 0x3C)])
    assert len(n.particles) == net.MAX_PARTICLES and oldest not in n.particles and n.particles[-1].partial
    n.close()


def test_contention_pulse_never_overlaps_a_stage_dot_in_flight(qt_app):
    """One activity on the wire at a time (review requirement): while a stage dot travels, a lost arbitration is shown
    by the loser's amber flash and the HUD note only - its pulse would overlap the transfer being explained."""
    import gui.network_3d as net
    n = make_net(qt_app)
    n.particles = [net.Particle(n.model.path_to(0x68, "SDA", 2), "#fff", travel_s=9.0)]
    n.show_contention([arb_event(1, 0x3C)])
    assert len(n.particles) == 1 and not n.particles[0].partial
    assert n.flash_color.get("master") == net.ARB_COL and "ARBITRATION" in n._arb_note[0]
    n.close()


def test_contention_pulse_leaves_no_ghost_in_a_hidden_minimised_or_paused_view(qt_app):
    n = make_net(qt_app)
    n.set_active(False)                                                             # minimised
    n.show_contention([arb_event()])
    assert n.particles == []
    n.set_active(True)
    n.playback_paused = True                                                        # Pause display
    n._last_arb_spawn = 0.0
    n.show_contention([arb_event()])
    assert n.particles == []
    n.playback_paused = False
    n._last_arb_spawn = 0.0
    n.hide()                                                                        # another tab: queued, but dropped on show
    n.show_contention([arb_event()])
    n.show()
    assert n.particles == []
    n.close()


# ------------------------------------------------------------------ paused display
def test_paused_view_spawns_no_frozen_dot_and_resumes_the_stage_on_display(qt_app):
    e = engine_with(oled_burst(4))
    e.packets[0].master_id = 1
    n = make_net(qt_app)
    pb = Playback(); pb.select(e.packets[0], pinned=True)
    n.playback_paused = True                                                        # MainWindow._pause_display(True)
    for k in (4.0, 5.0, 6.0):                                                       # stage stepping while paused
        pb.position = k
        n.show_stage(pb, e)
        n._tick(0.1)
        assert n.particles == []                                                    # no frozen s = 0 dot
    n.hide(); n.show(); qt_app.processEvents()                                      # tab switches while paused
    n.show_stage(pb, e)
    assert n.particles == []
    n.playback_paused = False                                                       # resume
    n.show_stage(pb, e)
    assert len(n.particles) == 1 and n.particles[0].s == 0.0
    n._tick(FRAME)
    assert n.particles[0].s > 0                                                     # and it moves
    n.close()


# ------------------------------------------------------------------ fair queue
def test_a_device_polled_faster_than_the_grace_period_still_gets_its_turn():
    q = FollowQueue()
    popped = []
    for k in range(60):                                                             # an unattributed packet every 200 ms
        now = k * 0.2
        q.offer([P(0x76)], now=now)
        if q.pop(now=now, can_attribute=lambda: True) is not None:
            popped.append(now)
    assert popped and popped[0] == pytest.approx(0.8, abs=0.21)                    # waited at most the grace period ONCE
    assert len(popped) >= 8                                                         # ... and keeps being served afterwards


def test_ownership_label_alone_is_held_for_telemetry_but_telemetry_and_demo_are_not():
    q = FollowQueue()
    owned, tele, demo = P(0x3C, 1, "ownership"), P(0x68, 2, "telemetry"), P(0x76, 1, "demo")
    q.offer([owned, tele, demo], now=0.0)
    first, second = q.pop(now=0.1, can_attribute=lambda: True), q.pop(now=0.1, can_attribute=lambda: True)
    assert {id(first), id(second)} == {id(tele), id(demo)}
    assert q.pop(now=0.2, can_attribute=lambda: True) is None                      # Master 2 might claim the owned OLED
    owned.master_id, owned.master_method = 2, "telemetry"                          # 1 -> 2: telemetry overrides ownership
    assert q.pop(now=0.3, can_attribute=lambda: True) is owned
    again = P(0x3C, 1, "ownership")
    q.offer([again], now=1.0)
    assert q.pop(now=1.0 + FollowQueue.ATTRIBUTION_SETTLE_S + 0.01, can_attribute=lambda: True) is again
    assert again.master_id == 1 and again.master_method == "ownership"             # nobody else claimed it: stays labelled


def test_device_filter_path_waits_for_telemetry_too_but_only_once(qt_app):
    from tests.test_inspect_follow import live_window
    w, _ = live_window()
    e = engine_with(rtc_read())
    t = e.packets[0]
    t.master_id, t.master_method = None, ""
    w.engine.masters.set_connection(2, "COM34", True)
    w.engine.masters.state[2].clock.locked = True
    clk = [500.0]
    with patch.object(time, "monotonic", lambda: clk[0]):
        w._follow_latest_txn = t
        assert w._follow_next(0x68) is None                                         # held: telemetry may still arrive
        clk[0] += 0.5
        assert w._follow_next(0x68) is None
        clk[0] += 0.4
        assert w._follow_next(0x68) is None                                         # grace over: still held - never
        clk[0] += 5.0                                                               # animated as an unnamed master
        assert w._follow_next(0x68) is None and t.master_id is None
        t.master_id, t.master_method = 2, "telemetry"
        w._follow_shown.clear()
        w.playback.txn = None
        assert w._follow_next(0x68) is t                                            # attributed: no wait at all
    w.close()


# ------------------------------------------------------------------ smoothness
@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("pinned", [True, False])
def test_dots_glide_pin_to_pin_in_small_pixel_steps_on_the_longest_wires(qt_app, speed, pinned):
    import gui.network_3d as net
    e = engine_with(oled_burst(16), rtc_read())
    e.packets[0].master_id, e.packets[1].master_id = 1, 2
    n = make_net(qt_app)
    for _ in range(60):                                       # let the resize 'settle' timer fire like the real event loop
        n.view.grab(); qt_app.processEvents(); time.sleep(0.01)
        if not getattr(n.view, "_settling", False):
            break
    lengths = {(m, a): net.Particle(n.model.path_to(a, "SDA", master=m), "#fff").total for m, a in ((1, 0x3C), (2, 0x68))}
    assert lengths[(1, 0x3C)] == pytest.approx(21.15, abs=0.01) and lengths[(2, 0x68)] == pytest.approx(24.45, abs=0.01)
    worst = 0.0
    for txn in e.packets:
        pb = Playback(); pb.speed = speed; pb.select(txn, pinned=pinned)
        for idx in range(len(pb.events)):
            pb.position = float(idx)
            n.particles.clear(); n._stage_key = None
            n.show_stage(pb, e)
            if not n.particles:                               # the R/W bit has no dot of its own
                continue
            p = n.particles[-1]
            pts = [n.view._map(float(a[0]), float(a[1])) for a in p.path]
            px_total = sum(np.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(pts, pts[1:]))
            prev, steps = n.view._map(*[float(v) for v in p.pos()[:2]]), []
            while p in n.particles:
                n._tick(FRAME)
                cur = n.view._map(*[float(v) for v in p.pos()[:2]])
                steps.append(np.hypot(cur.x() - prev.x(), cur.y() - prev.y()))
                prev = cur
            assert len(steps) >= 6, f"stage {idx}: only {len(steps)} frames for the whole wire"
            end = n.view._map(float(p.path[-1][0]), float(p.path[-1][1]))
            assert np.hypot(prev.x() - end.x(), prev.y() - end.y()) < 0.5       # the last frame sits ON the receiving pin
            assert 0.85 * px_total <= sum(steps) <= px_total + 0.5               # (chords cut the bends of the wire a little)
            assert all(s > 0 for s in steps[:-1])                              # never stalls mid-wire (the last tick may just land)
            assert max(steps) <= px_total * (FRAME / net.MIN_TRAVEL_S + 0.02)  # never more than ~1/6 of the wire per frame
            worst = max(worst, max(steps) / px_total)
    assert worst <= 1 / 6 + 0.02
    n.close()
