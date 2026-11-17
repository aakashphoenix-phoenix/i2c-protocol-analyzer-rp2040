"""Movement dots: every stage dot crosses the COMPLETE controller <-> rail <-> target wire, whatever the master, speed,
tab, burst length or moment the controller telemetry attributes the packet.

Bugs guarded (2026-11-15):
  * Master 2 -> RTC / BMP280 dots covered only part of the bus: Master 2's packets are attributed AFTER the analyzer decoded
    them (controller telemetry arrives on another COM port), so they started playing unattributed - drawn from the analyzer
    tap (7.6 of the 24.45 scene units to the RTC) - and when the attribution arrived mid-stage no dot was spawned on the
    master's real wire for the rest of that stage.
  * Master 1 <-> OLED dots vanished intermittently: a fast stage (2x, repeated bytes of a long OLED burst) gave the dot 3-4
    frames for a 21-unit wire; a view that was minimised piled up frozen dots (and refused new ones at 120); a view
    switched away from kept frozen dots that resumed as ghosts of an old stage when its tab came back.
Existing attribution rules are untouched: a master is never guessed (unattributed packets stay unattributed)."""
import time

import numpy as np
import pytest

from analysis.capture_engine import CaptureEngine
from analysis.multi_master import MultiMasterTracker
from analysis.playback import FollowQueue, Playback
from tests.helpers import lines_for, seq_of, synth, write, write_read
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)
from tests.test_playback_flow import FRAME, check_packet, make_net, play


def engine_with(*builders):
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines(lines_for(synth(seq_of(*builders))))
    assert len(e.packets) == len(builders)
    return e


def oled_burst(nbytes=16):
    return write(0x3C, [0x40] + [(0x11 * i) & 0xFF for i in range(nbytes)])        # control byte + framebuffer chunk


def rtc_read():
    return write_read(0x68, 0x00, [0x12, 0x34, 0x56, 0x07, 0x15, 0x11, 0x26])        # DS3231 time registers


def pins(model, addr, master):
    """(controller pin, target pin) of the SDA wire, from the geometry itself (independent of path_to)."""
    import gui.network_3d as net
    target = (model.drop_x(addr, "SDA"), model.target_top(addr), net.RAIL_Z)
    if master == 2:
        ctrl = (model.feed_x("SDA"), model.master2_bottom(), net.RAIL_Z)
    else:
        ctrl = (net.CTRL_X + net.board_size("pico", False)[0] / 2, net.RAIL_Y["SDA"], net.RAIL_Z)
    return np.array(ctrl), np.array(target)


# ------------------------------------------------------------------ 1. full-path traversal, controller -> target and back
@pytest.mark.parametrize("master,addr", [(1, 0x3C), (2, 0x68), (2, 0x76), (2, 0x3C)])
@pytest.mark.parametrize("direction", ["W", "R"])
def test_a_dot_travels_pin_to_pin_the_whole_wire_in_both_directions(qt_app, master, addr, direction):
    import gui.network_3d as net
    n = make_net(qt_app)
    m = n.model
    ctrl, tgt = pins(m, addr, master)
    path = m.path_to(addr, "SDA", master=master)
    assert np.allclose(path[0], ctrl) and np.allclose(path[-1], tgt)
    if direction == "R":
        path = path[::-1]
    p = net.Particle(path, "#ffffff", travel_s=0.3)
    start, end = (ctrl, tgt) if direction == "W" else (tgt, ctrl)
    assert np.allclose(p.pos(), start) and p.s == 0.0
    n.particles.append(p)
    visited = []
    for _ in range(100):
        n._tick(FRAME)
        visited.append(p.s)
        if p.done:
            break
    assert p.done and p.s == pytest.approx(p.total)                                # s reached total: 100 % of the wire
    assert np.allclose(p.pos_at(p.total), end)
    assert n.particles == [] and n.particles_arrived == 1 and n.particles_cut == 0
    assert all(b >= a for a, b in zip(visited, visited[1:]))                         # monotonic: it never jumps back
    # at most ~11 % of the wire per 33 ms frame (a 0.3 s crossing): a smooth glide, never a teleport
    assert max(b - a for a, b in zip([0.0] + visited, visited)) < 0.2 * p.total
    n.close()


@pytest.mark.parametrize("master,addr", [(1, 0x3C), (2, 0x68), (2, 0x76), (2, 0x3C)])
def test_2d_topology_shows_the_whole_wire_inside_the_canvas(qt_app, master, addr):
    n = make_net(qt_app)
    for _ in range(60):                                       # let the resize 'settle' timer fire like the real event loop
        n.view.grab(); qt_app.processEvents(); time.sleep(0.01)
        if not getattr(n.view, "_settling", False):
            break
    ctrl, tgt = pins(n.model, addr, master)
    import gui.network_3d as net
    p = net.Particle(n.model.path_to(addr, "SDA", master=master), "#ffffff", travel_s=0.3)
    n.particles.append(p)
    rect = n.view.rect()
    for frame in range(40):
        for s in (p.pos(),):
            q = n.view._map(float(s[0]), float(s[1]))
            assert rect.contains(int(q.x()), int(q.y())), f"dot left the 2D canvas at frame {frame}"
        n.view.grab()                                          # the real QPainter path draws the dot without error
        n._tick(FRAME)
        if p.done:
            break
    assert p.done
    for pt in (ctrl, tgt):                                     # both ends of the wire are on the canvas
        q = n.view._map(float(pt[0]), float(pt[1]))
        assert rect.contains(int(q.x()), int(q.y()))
    n.close()


# ------------------------------------------------------------------ 2. no skipped / dropped dots in OLED bursts
@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("pinned", [True, False])
def test_oled_multi_byte_burst_gives_every_stage_a_complete_dot_for_master_1_and_master_2(qt_app, speed, pinned):
    e = engine_with(oled_burst(16), oled_burst(16), rtc_read())
    for t, mid in zip(e.packets, (1, 2, 2)):               # Master 1 owns the OLED; Master 2 may also address it in a custom stream
        t.master_id, t.master_method = mid, "telemetry"
    n = make_net(qt_app)
    coverage = set()
    for txn in e.packets:
        pb, records = check_packet(n, e, txn, speed, pinned, coverage)
        dotted = [r for r in records.values() if r["stage"].kind != "rw"]
        assert len(dotted) >= 30 or txn.address == 0x68                            # a real burst, not a fragment
        assert all(len(r["particles"]) == 1 for r in dotted)
        assert all(r["particles"][0].done for r in dotted)                          # every dot reached the receiver
    assert n.particles == [] and n.particles_cut == 0
    assert {mid for *_x, mid in coverage} == {1, 2}
    n.close()


def test_a_fast_stage_still_gets_enough_frames_to_be_seen(qt_app):
    import gui.network_3d as net
    e = engine_with(oled_burst(16))
    e.packets[0].master_id = 1
    n = make_net(qt_app)
    pb = Playback()
    pb.select(e.packets[0], pinned=False)
    pb.speed = 4.0           # far above every offered speed (max 0.5x): forces a stage shorter than the dot floor
    fast = min((i for i, s in enumerate(pb.events) if s.kind in ("data", "ack")), key=lambda i: pb.stage_wall_seconds(i))
    pb.position = float(fast)
    n.show_stage(pb, e)
    p = n.particles[-1]
    assert 0.8 * pb.stage_wall_seconds(fast) < net.MIN_TRAVEL_S            # the stage itself is shorter than the floor ...
    assert p.travel_s == pytest.approx(net.MIN_TRAVEL_S) and p.travel_s / FRAME >= 5.5    # ... the dot still shows >= ~6 frames
    n.close()


def test_the_particle_cap_retires_the_oldest_dot_instead_of_refusing_a_new_stage(qt_app):
    import gui.network_3d as net
    e = engine_with(oled_burst(4))
    e.packets[0].master_id = 1
    n = make_net(qt_app)
    pb = Playback(); pb.select(e.packets[0], pinned=True)
    pb.position = 1.0
    n.particles = [net.Particle(n.model.path_to(0x3C, "SDA", 1), "#fff", travel_s=5.0) for _ in range(net.MAX_PARTICLES)]
    oldest = n.particles[0]
    n.show_stage(pb, e)
    assert len(n.particles) == net.MAX_PARTICLES and oldest not in n.particles
    assert n.particles[-1].color != "#fff"                                  # the NEW stage's dot is there
    n.close()


# ------------------------------------------------------------------ 3. tab switches, minimising: no ghosts, no missing dot
def test_a_hidden_view_drops_its_frozen_dots_and_respawns_the_current_stage_when_shown(qt_app):
    e = engine_with(oled_burst(8))
    e.packets[0].master_id = 1
    n = make_net(qt_app)
    pb = Playback(); pb.select(e.packets[0], pinned=True)
    pb.position = 3.0
    n.show_stage(pb, e)
    n._tick(0.05)
    assert len(n.particles) == 1
    n.hide()
    assert n.particles == [] and n.particles_cut == 1                          # nothing frozen is kept
    pb.position = 9.0                                                          # the packet moves on while the tab is away
    n._tick(0.2)
    assert n.particles == []
    n.show()                                                                   # (showEvent is synchronous)
    assert n._tick_wall is None                                                # no catch-up jump
    n.show_stage(pb, e)                                                        # the next refresh: current stage, dot at once
    assert len(n.particles) == 1 and n.particles[0].s == 0.0
    assert n._stage_idx == 9
    n.close()


def test_a_minimised_view_neither_piles_up_frozen_dots_nor_loses_the_current_one(qt_app):
    import gui.network_3d as net
    e = engine_with(oled_burst(16))
    e.packets[0].master_id = 1
    n = make_net(qt_app)
    pb = Playback(); pb.select(e.packets[0], pinned=True)
    n.set_active(False)                                                        # MainWindow.changeEvent: window minimised
    for i in range(len(pb.events)):
        pb.position = float(i)
        n.show_stage(pb, e)
        n._tick(0.05)
    assert n.particles == []                                                   # (used to pile up to 120 frozen dots)
    pb.position = 6.0
    n.set_active(True)
    n.show_stage(pb, e)
    assert len(n.particles) == 1 and n.particles[0].s == 0.0
    for _ in range(40):
        n._tick(FRAME)
    assert n.particles == [] and n.particles_arrived == 1
    n.close()


def test_switching_tabs_shows_the_current_stage_dot_at_once(qt_app):
    """MainWindow._goto() used to leave the newly shown diagram stale until the next 100 ms refresh (and with the stage
    already shown by a previous refresh, nothing was ever spawned for it)."""
    from tests.test_inspect_follow import live_window
    w, _ = live_window()
    w.show()
    qt_app.processEvents()
    e = engine_with(oled_burst(8))
    t = e.packets[0]
    t.master_id = 1
    w.playback.select(t, pinned=True)
    w.playback.position = 4.0
    w.nav.setCurrentRow(1)                                                     # 3D Network page
    qt_app.processEvents()
    net = w.p_net.net
    assert net.isVisible() and len(net.particles) == 1 and net.particles[0].s < net.particles[0].total
    w.nav.setCurrentRow(0)                                                     # away: its dots are dropped, not frozen
    qt_app.processEvents()
    assert not net.isVisible() and net.particles == []
    w.playback.position = 7.0                                                  # playback moves on while away
    w.nav.setCurrentRow(1)
    qt_app.processEvents()
    assert len(net.particles) == 1 and net._stage_idx == 7                     # back: the CURRENT stage, not a stale dot
    w.close()


# ------------------------------------------------------------------ 4. late attribution animates the master's full wire
def test_attribution_upgrade_none_to_master_2_animates_master_2s_full_path(qt_app):
    import gui.network_3d as net
    e = engine_with(rtc_read())
    t = e.packets[0]
    t.master_id, t.master_method = None, ""
    n = make_net(qt_app)
    pb = Playback(); pb.select(t, pinned=True)
    pb.position = 1.0                                                          # ADDR 0x68 W: controller -> RTC
    n.show_stage(pb, e)
    stub = n.particles[0]
    full_len = net.Particle(n.model.path_to(0x68, "SDA", master=2), "#fff").total
    assert stub.total < 0.4 * full_len                                         # unattributed: only the shared-bus -> target stub
    assert stub.color == net.UNATTR_COL and np.allclose(stub.path[0], (n.model.drop_x(0x68, "SDA"), net.RAIL_Y["SDA"], net.RAIL_Z))
    assert stub.src is None and stub.dst == 0x68                              # the passive analyzer is never the sender
    n._tick(0.05)
    t.master_id, t.master_method = 2, "telemetry"                              # the controller's telemetry arrives mid-stage
    n.show_stage(pb, e)
    assert n.particles[0] is stub and n.particles_cut == 0                     # the dot in flight is never cut
    m2 = n.particles[1]
    assert m2.color == net.master_color(2, "W") and m2.total == pytest.approx(full_len)
    ctrl, tgt = pins(n.model, 0x68, 2)
    assert np.allclose(m2.path[0], ctrl) and np.allclose(m2.path[-1], tgt)     # Master 2's pin ... the RTC pin
    for _ in range(60):
        n._tick(FRAME)
    assert n.particles == [] and n.particles_cut == 0 and n.particles_arrived == 2
    # later stages of the same packet run the full Master 2 wire from their first frame
    pb.position = 4.0                                                          # DATA 0x00: controller -> RTC
    n.show_stage(pb, e)
    assert np.allclose(n.particles[-1].path[0], ctrl)
    n.close()


def test_attribution_upgrade_between_masters_also_spawns_on_the_new_masters_wire(qt_app):
    import gui.network_3d as net
    e = engine_with(oled_burst(4))
    t = e.packets[0]
    t.master_id, t.master_method = 1, "ownership"                              # configured owner of the OLED ...
    n = make_net(qt_app)
    pb = Playback(); pb.select(t, pinned=True)
    pb.position = 1.0
    n.show_stage(pb, e)
    assert np.allclose(n.particles[0].path[0], pins(n.model, 0x3C, 1)[0])
    t.master_id, t.master_method = 2, "telemetry"                              # ... but Master 2's own log claims this packet
    n.show_stage(pb, e)
    assert len(n.particles) == 2 and n.particles_cut == 0
    assert np.allclose(n.particles[1].path[0], pins(n.model, 0x3C, 2)[0])
    n.close()


def test_an_unattributed_packet_stays_unattributed_and_visibly_distinct_from_a_real_transfer(qt_app):
    import gui.network_3d as net
    e = engine_with(rtc_read())
    t = e.packets[0]
    t.master_id, t.master_method = None, ""                                    # (the demo source pre-attributes to Master 1)
    n = make_net(qt_app)
    pb = Playback(); pb.select(t, pinned=True)
    for i in (1, 4, 6):
        pb.position = float(i)
        n.show_stage(pb, e)
    assert all(p.color in (net.UNATTR_COL, net.ACK_COL, net.NACK_COL) for p in n.particles)
    assert n.particles[0].color == net.UNATTR_COL != net.master_color(2, "W")
    stub = n.particles[0]                                                      # drawn on the shared bus: no master pin, no analyzer
    assert np.allclose(stub.path[0][:2], (n.model.drop_x(0x68, "SDA"), net.RAIL_Y["SDA"]))
    assert "analyzer" not in (stub.src, stub.dst)
    assert t.master_id is None and getattr(t, "master_method", "") == ""        # showing it never assigned a master
    # arbitration (amber) is a different thing again: a deliberately truncated path
    class Ev:
        master_id, addr, winner = 2, 0x68, None
    n.particles.clear()
    n._last_arb_spawn = 0.0
    n.show_contention([Ev])
    arb = n.particles[-1]
    assert arb.color == net.ARB_COL and arb.total == pytest.approx(0.45 * net.Particle(n.model.path_to(0x68, "SDA", 2), "#f").total)
    n.close()


# ------------------------------------------------------------------ 5. follow queue: late telemetry
class P:
    """A queued packet stand-in: the queue only reads .address and .master_id (the real object is updated in place)."""
    def __init__(self, address, master_id=None, master_method=""):
        self.address, self.master_id, self.master_method = address, master_id, master_method


def test_follow_queue_files_a_late_attributed_packet_under_its_master():
    q = FollowQueue()
    a = P(0x68)
    q.offer([a], now=0.0)
    a.master_id, a.master_method = 2, "telemetry"                              # telemetry attributes it after it was queued
    b = P(0x68)                                                                # a newer UNATTRIBUTED packet of the same address
    q.offer([b], now=0.1)
    assert len(q) == 2                                                         # the attributed one was not superseded
    first, second = q.pop(now=1.0), q.pop(now=1.0)
    assert {id(first), id(second)} == {id(a), id(b)} and q.pop(now=1.0) is None
    assert a.master_id == 2 and b.master_id is None                           # nothing was guessed or reassigned


def test_follow_queue_collapses_two_packets_that_are_both_master_2_now():
    q = FollowQueue()
    a, b = P(0x76), P(0x76)
    q.offer([a], now=0.0); q.offer([b], now=0.1)
    a.master_id, a.master_method = 2, "telemetry"
    b.master_id, b.master_method = 2, "telemetry"                              # both attributed to the same master meanwhile
    assert q.pop(now=1.0) is b and q.pop(now=1.0) is None                      # only the newest of the bucket is played
    assert not q._meta


def test_unattributed_packet_waits_for_telemetry_only_while_a_controller_could_claim_it():
    q = FollowQueue()
    a = P(0x68)
    q.offer([a], now=10.0)
    assert q.pop(now=10.1) is a                                                # no aligned controller: plays at once
    q.offer([a], now=10.0)
    assert q.pop(now=10.1, can_attribute=lambda: False) is a
    q.offer([a], now=10.0)
    assert q.pop(now=10.1, can_attribute=lambda: True) is None                 # held: telemetry may still arrive
    assert len(q) == 1
    a.master_id, a.master_method = 2, "telemetry"                              # it did: plays with the real master at once
    assert q.pop(now=10.2, can_attribute=lambda: True) is a
    b = P(0x68)
    q.offer([b], now=20.0)
    assert q.pop(now=20.0 + FollowQueue.ATTRIBUTION_SETTLE_S - 0.01, can_attribute=lambda: True) is None
    assert q.pop(now=20.0 + FollowQueue.ATTRIBUTION_SETTLE_S + 0.01, can_attribute=lambda: True) is b
    assert b.master_id is None                                                 # grace over: plays UNATTRIBUTED, never guessed


def test_a_held_packet_does_not_block_a_packet_that_is_ready():
    q = FollowQueue()
    held, ready = P(0x68), P(0x3C, 1, "telemetry")                             # the OLED is attributed by Master 1's own log
    q.offer([held, ready], now=5.0)
    assert q.pop(now=5.1, can_attribute=lambda: True) is ready
    assert q.pop(now=5.2, can_attribute=lambda: True) is None and len(q) == 1


def test_tracker_can_attribute_needs_a_connected_master_with_an_aligned_clock():
    mm = MultiMasterTracker()
    assert not mm.can_attribute()
    mm.set_connection(2, "COM34", True)
    assert not mm.can_attribute()                                              # connected but clock not aligned yet
    mm.state[2].clock.locked = True
    assert mm.can_attribute()
    mm.set_connection(2, "COM34", False)
    assert not mm.can_attribute()


def test_main_window_holds_an_unattributed_packet_briefly_but_never_skips_past_it(qt_app):
    from tests.test_inspect_follow import live_window
    w, _ = live_window()
    e = engine_with(rtc_read())
    t = e.packets[0]
    t.master_id, t.master_method = None, ""
    w.engine.masters.set_connection(2, "COM34", True)
    w.engine.masters.state[2].clock.locked = True                              # Master 2 telemetry is aligned: wait for it
    w._follow_queue.clear()
    w._follow_latest_txn = t                                                   # (the fallback that used to skip the queue)
    w._follow_queue.offer([t])
    assert w._follow_next(None) is None and len(w._follow_queue) == 1          # held, not bypassed through _follow_latest_txn
    t.master_id, t.master_method = 2, "telemetry"                              # telemetry arrives
    assert w._follow_next(None) is t and len(w._follow_queue) == 0
    w.close()
