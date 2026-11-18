"""Fair follow-mode scheduling, readable dot pacing, path alignment and particle rendering of the network view.

Bug being guarded: follow mode kept only `act[-1]` while one packet played for 6-8 s, so the busy BMP280 (0x76, 5 Hz) starved
the DS3231 (0x68, 1 Hz) and the SSD1306 (0x3C); dots crossed their wire in ~0.16 s (too fast to tell master->slave from
slave->master); the R/W bit fired a second overlapping dot on top of its address byte; Master 1's route started 0.45 units
right of the board pin where its rails are drawn; and the 3D scatter was one plain additive 12 px dot."""
import copy
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from PyQt6.QtGui import QColor

import config
from analysis.capture_engine import CaptureEngine
from analysis.playback import FollowQueue, Playback, stages
from sources.demo_source import DemoGenerator
from tests.test_hardware_pipeline import qt_app  # noqa: F401  (module-scoped QApplication + temp QSettings)
from tests.test_playback_flow import FRAME, make_net, packets


# ====================================================================== the queue itself
def T(addr, mid=None, n=0):
    return SimpleNamespace(address=addr, master_id=mid, n=n)


def test_queue_serves_every_address_in_turn_whatever_fired_last():
    q = FollowQueue()
    q.offer([T(0x76, 1, 0), T(0x68, 1, 0), T(0x3C, 1, 0), T(0x76, 1, 1), T(0x76, 1, 2)])    # 0x76 fired three times, last
    order = [q.pop() for _ in range(3)]
    assert [t.address for t in order] == [0x76, 0x3C, 0x68]       # newest's bucket first, then ascending round robin
    assert order[0].n == 2                                          # only the NEWEST unplayed packet of a bucket is kept
    assert q.pop() is None and len(q) == 0
    served = {0x3C: 0, 0x68: 0, 0x76: 0}
    for _ in range(30):                                             # 0x76 bursts 5x, 0x68 / 0x3C once, every cycle
        q.offer([T(0x68), T(0x3C)] + [T(0x76, n=i) for i in range(5)])
        served[q.pop().address] += 1
    assert max(served.values()) - min(served.values()) <= 1 and min(served.values()) >= 9, served


def test_queue_rotates_the_masters_of_an_address_and_never_guesses_one():
    q = FollowQueue()
    for _ in range(3):
        q.offer([T(0x76, 1), T(0x76, 2), T(0x76, None)])
    got = [q.pop().master_id for _ in range(3)]
    assert sorted(got, key=lambda m: (m is None, m)) == [1, 2, None]      # M1, M2 and the unattributed bucket all play
    q.offer([T(0x76, 1), T(0x76, 2)])
    two = [q.pop().master_id for _ in range(2)]
    assert sorted(two) == [1, 2]
    q.offer([T(0x68, None)])
    assert q.pop().master_id is None                                 # unattributed stays unattributed


def test_queue_is_bounded_and_empty_safe():
    q = FollowQueue()
    assert q.pop() is None
    for i in range(10_000):
        q.offer([T(0x3C + (i % 3) * 0x2D, 1 + i % 2, i)])
    assert len(q) <= 6                                              # (addresses x masters), not the number of packets
    q.clear()
    assert len(q) == 0 and q.pop() is None


# ====================================================================== through the real MainWindow
@pytest.fixture
def window(qt_app):
    import gui.network_3d as network
    from gui import theme
    from gui.main_window import MainWindow
    theme.apply_theme(qt_app, "dark")
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow()
        w.visual_speed.setCurrentIndex(w.visual_speed.findData(0.25))   # these tests cover the review pace + fair queue
        w.engine.set_source("demo")
        w.resize(1280, 720)
        w.show()
        qt_app.processEvents()
        yield w
        w.close()
        qt_app.processEvents()


def scripted_traffic(w):
    """Real decoded transactions of the three devices; `batch(k)` is what the analyzer delivered during GUI refresh k:
    BMP280 (0x76) every refresh (it is the busiest, like the 5 Hz burst), the OLED (0x3C) every 3rd, the RTC (0x68) every
    10th - so the newest packet of a tick is almost always a 0x76 one (the old `act[-1]` starved the other two)."""
    from tests.test_stage_pills_semantic import load_traffic
    if hasattr(w, "_scripted"):
        return w._scripted
    p = load_traffic(w.engine)
    pool = {0x76: p["bmp"], 0x3C: p["oled_frame"], 0x68: p["rtc"]}
    pool[0x76].master_id, pool[0x3C].master_id, pool[0x68].master_id = 1, 1, 2
    unattributed = copy.copy(p["rtc"])
    unattributed.master_id, unattributed.master_method = None, ""

    def batch(k):
        out = [copy.copy(pool[0x3C])] if k % 3 == 0 else []
        if k % 10 == 0:
            out.append(copy.copy(pool[0x68]))
            if k % 50 == 0:
                out.append(copy.copy(unattributed))
        out.append(copy.copy(pool[0x76]))                                   # the busiest device always fired LAST
        return out
    w._scripted = (pool, batch)
    return w._scripted


def run_scripted(w, seconds, refresh=0.2):
    w.visual_speed.setCurrentIndex(w.visual_speed.count() - 1)           # 0.5x: the fastest review speed
    pool, batch = scripted_traffic(w)
    w.engine.take_activity()
    w.nav.setCurrentRow(9)
    k, played, last = 0, [], None
    with patch.object(w.engine, "take_activity", side_effect=lambda: batch(k)):
        for k in range(1, int(seconds / refresh) + 1):
            w._playback_wall = time.monotonic() - refresh
            w._refresh()
            t = w.playback.txn
            if t is not None and t is not last:
                played.append(t)
                last = t
    return played


def test_follow_mode_plays_rtc_bmp280_and_oled_in_turn_instead_of_only_the_busiest(window):
    played = run_scripted(window, 560)              # uniform pace at 0.5x: a 17-byte OLED frame alone takes ~1 min
    addrs = [t.address for t in played]
    for a in (0x3C, 0x68, 0x76):
        assert addrs.count(a) >= 3, f"0x{a:02X} starved: {[hex(x) for x in addrs]}"
    # every (target, master) PAIR takes its turn: the least recently shown pair goes next (0x68 has two buckets here -
    # Master 2 and the unattributed one - so it is shown twice as often as a target only one master addresses)
    pairs = [(t.address, t.master_id) for t in played]
    pcounts = {q: pairs.count(q) for q in set(pairs)}
    assert max(pcounts.values()) - min(pcounts.values()) <= 1, pcounts      # a fair rotation, not 'newest wins'
    assert all(a != b for a, b in zip(addrs, addrs[1:]))                    # never the same device twice in a row
    masters = {(t.address, t.master_id) for t in played}
    assert (0x68, 2) in masters and (0x68, None) in masters and (0x76, 1) in masters       # both masters + unattributed play
    unattributed = [t for t in played if t.master_id is None]
    assert unattributed and all(t.master_method == "" for t in unattributed)               # never assigned a guessed master
    ops = {t.operation for t in played}
    assert "WRITE" in ops and "WRITE→READ" in ops                                          # OLED write, register reads
    acks = [s.ack for t in played for s in stages(t) if s.kind == "ack"]
    assert True in acks and False in acks                                                  # ACK and the end-of-read NACK
    assert len(window._follow_queue) <= 6                                                  # bounded however much traffic arrives


def test_a_played_packet_is_never_replayed_and_the_queue_keeps_collecting_while_held(window):
    played = run_scripted(window, 40)
    assert len({id(t) for t in played}) == len(played)
    window.set_stage_hold(True)                                             # held: nothing plays, nothing is lost or unbounded
    shown = window.playback.txn
    pool, batch = scripted_traffic(window)
    with patch.object(window.engine, "take_activity", side_effect=lambda: batch(30)):
        for _ in range(60):
            window._playback_wall = time.monotonic() - 0.2
            window._refresh()
    assert window.playback.txn is shown and 0 < len(window._follow_queue) <= 6
    window.set_stage_hold(False)


def test_device_filter_still_snaps_to_the_newest_matching_packet(window):
    pool, batch = scripted_traffic(window)
    window.visual_speed.setCurrentIndex(window.visual_speed.count() - 1)   # 0.5x
    window.nav.setCurrentRow(9)
    window.focus_device(0x68)
    assert window.playback.txn.address == 0x68 and len(window._follow_queue) == 0
    seen = []
    with patch.object(window.engine, "take_activity", side_effect=lambda: batch(k)):
        for k in range(1, 1100):
            window._playback_wall = time.monotonic() - 0.2
            window._refresh()
            if not seen or window.playback.txn is not seen[-1]:
                seen.append(window.playback.txn)
            assert len(window._follow_queue) == 0                            # filtered: the fair queue is bypassed
    assert len(seen) >= 3 and all(t.address == 0x68 for t in seen)           # only the filtered device plays
    window.focus_device(None)
    assert window.device_filter is None


# ====================================================================== animate everything, start to end (DemoGenerator)
def test_demo_traffic_animates_all_three_targets_both_directions_ack_and_nack(window):
    """DemoGenerator traffic (BMP280 5 Hz, RTC 1 Hz, OLED framebuffer writes) through _refresh() + _tick() at 2x: logs every
    (address, direction, stage, ack/nack) that actually animated and proves each ran its whole wire."""
    n = window.p_net.net
    window.playback.speed = 2.0
    log = []
    real = n._spawn_stage_particle

    def spy(stage, txn):
        before = len(n.particles)
        real(stage, txn)
        if len(n.particles) > before:
            idx = window.playback.stage_index
            log.append(dict(addr=stage.address if stage.address is not None else txn.address, kind=stage.kind,
                            direction=stage.direction, ack=stage.ack, p=n.particles[-1], idx=idx, mid=txn.master_id,
                            weight=window.playback._weights()[idx], wall=window.playback.stage_wall_seconds(idx)))
    g = DemoGenerator(seed=5)
    window.engine.ingest_lines([g.hello()])
    window.nav.setCurrentRow(1)
    with patch.object(n, "_spawn_stage_particle", side_effect=spy):
        for k in range(1, 221):                                                           # 55 s of simulated bus time
            window.engine.ingest_lines(g.lines_until(k * 0.25))
            window._playback_wall = time.monotonic() - 0.25
            window._refresh()
            for _ in range(8):
                n._tick(FRAME)
        for _ in range(400):                                                              # let every last dot arrive
            n._tick(FRAME)
    assert n.particles == [] and n.particles_cut == 0
    assert all(r["p"].done and r["p"].s >= r["p"].total for r in log), "a dot stopped before the end of its wire"
    seen = {(r["addr"], r["kind"], r["direction"], r["ack"]) for r in log}
    for a in (0x3C, 0x68, 0x76):
        assert (a, "addr", "W", None) in seen, f"0x{a:02X}: no master -> slave address dot"
        assert (a, "data", "W", None) in seen, f"0x{a:02X}: no master -> slave write data dot"
        assert (a, "ack", "R", True) in seen, f"0x{a:02X}: no slave -> master ACK dot"
    for a in (0x68, 0x76):
        assert (a, "data", "R", None) in seen, f"0x{a:02X}: no slave -> master read data dot"
        assert (a, "ack", "W", True) in seen, f"0x{a:02X}: no master ACK for read data"
        assert (a, "ack", "W", False) in seen, f"0x{a:02X}: no end-of-read NACK dot"
    assert not any(r["kind"] == "rw" for r in log)                                        # the R/W bit never fires a second dot
    speed = window.playback.speed
    from gui.network_3d import MIN_TRAVEL_S
    for r in log:
        assert r["p"].travel_s == pytest.approx(max(MIN_TRAVEL_S, 0.8 * r["wall"]))
        if 0.8 * r["wall"] < MIN_TRAVEL_S:      # fast stage at 2x: the dot keeps its minimum number of visible frames
            continue
        if r["weight"] == 1.0:                  # a full-pace byte stage (ADDR, first data bytes, their ACKs): 0.45-0.65 s at 1x
            assert 0.44 <= r["p"].travel_s * speed <= 0.66, (r["kind"], r["p"].travel_s * speed)
        elif r["kind"] == "cond":               # START / Sr / STOP: briefer, so they open and close the packet quickly
            assert r["p"].travel_s * speed < 0.35
        else:                                   # repetitive tail of a long frame (follow mode only): brisker, never < 0.2 s
            assert r["p"].travel_s * speed >= 0.2 - 1e-9
    assert sum(1 for r in log if r["weight"] == 1.0) >= 30
    for r in log:                               # sender AND receiver boards named for every dot, in the direction of flow
        key = {1: "master", 2: "master2"}.get(r["mid"], "analyzer")
        assert {r["p"].src, r["p"].dst} == {key, r["addr"]}
        # START / Sr / STOP (direction None) are driven by the controller; ADDR / write data / read-data ACK flow master -> target
        master_sends = r["direction"] == "W" or r["kind"] == "cond"
        assert r["p"].src == (key if master_sends else r["addr"])


def test_sender_pulses_when_the_dot_leaves_and_receiver_when_it_arrives(qt_app):
    e = packets()
    n = make_net(qt_app)
    txn = e.packets[1]                                              # BMP280 write+read, Master 2
    idx_w = next(i for i, s in enumerate(stages(txn)) if s.kind == "data" and s.frame_rw == "W")
    idx_r = next(i for i, s in enumerate(stages(txn)) if s.kind == "data" and s.frame_rw == "R")
    for idx, (src, dst) in ((idx_w, ("master2", 0x76)), (idx_r, (0x76, "master2"))):
        n.flash.clear(); n.flash_color.clear(); n.particles.clear(); n._stage_key = None
        pb = Playback(); pb.select(txn, pinned=True); pb.position = float(idx)
        n.show_stage(pb, e)
        p = n.particles[-1]
        assert (p.src, p.dst) == (src, dst)
        assert n.flash[src] >= 0.99 and n.flash_color[src] == p.color                 # sender lights up as the dot leaves
        n.flash[dst] = 0.0
        while not p.done:
            n._tick(FRAME)
        assert n.flash[dst] > 0.9 and n.flash_color[dst] == p.color                   # receiver lights up on ARRIVAL
    n.close()


def test_rw_bit_fires_no_second_dot_but_both_boards_pulse(qt_app):
    e = packets()
    n = make_net(qt_app)
    txn = e.packets[0]                                              # OLED write, Master 1
    st = stages(txn)
    assert st[1].kind == "addr" and st[2].kind == "rw"
    pb = Playback(); pb.select(txn, pinned=True); pb.position = 1.0
    n.show_stage(pb, e)
    assert len(n.particles) == 1                                    # the ADDR byte's dot
    pb.position = 2.0                                               # 'WRITE W=0': the last bit of the same byte
    n.flash.clear()
    n.show_stage(pb, e)
    assert len(n.particles) == 1                                    # still ONE dot: no overlapping duplicate
    assert n.flash["master"] >= 0.99 and n.flash[0x3C] >= 0.99      # both ends pulse
    assert n.stage_text == "0x3C · WRITE W=0 ▶" or n.stage_text.endswith("W=0 ▶")
    n.close()


# ====================================================================== wires start where they are drawn
def test_paths_start_at_the_visible_rail_and_end_at_each_target_for_both_masters(qt_app):
    import gui.network_3d as network
    n = make_net(qt_app)
    m = n.model
    assert m.two_masters and set(m.slaves) >= {0x3C, 0x68, 0x76}
    segs = {name: pts for name, pts, _c, _d in m.segments()}
    pin_edge = network.CTRL_X + network.board_size("pico", False)[0] / 2
    for a in (0x3C, 0x68, 0x76):
        p1 = m.path_to(a, "SDA", master=1)
        p2 = m.path_to(a, "SDA", master=2)
        pn = m.path_to(a, "SDA", master=None)
        drop = segs[f"drop:{a}:SDA"]
        # Master 1: leaves its board at the pin edge = the first point of the drawn rail (it used to start 0.45 right of it)
        assert p1[0] == segs["rail:SDA"][0] and p1[0][0] == pytest.approx(pin_edge)
        # Master 2: leaves from the feeder at its bottom edge, exactly like the drawn feeder wire
        assert p2[:2] == segs["feed:master2:SDA"] and p2[0][1] == pytest.approx(m.master2_bottom())
        # unattributed: shared bus only (rail junction -> target); never a guessed master, never the analyzer tap
        assert pn[0] == drop[0] and pn[0] != segs["tap:SDA"][0] and len(pn) == 2
        for p in (p1, p2, pn):
            assert p[-2:] == drop                                     # every route ends on the target's drop wire, at its pin
            assert p[-1][:2] == (m.drop_x(a, "SDA"), m.target_top(a))
            pts = [np.asarray(q, float) for q in p]
            assert all(np.linalg.norm(b - c) > 1e-9 for b, c in zip(pts, pts[1:]))      # no zero-length hops
    n.close()


# ====================================================================== particle rendering (3D layers + 2D painter)
def test_particle_render_layers_are_bright_core_halo_and_comet_trail():
    import gui.network_3d as network
    path = [(0, 0, 0.25), (10, 0, 0.25), (10, 5, 0.25)]
    p = network.Particle(path, config.COLORS["cyan"], travel_s=0.5)
    p.s = 0.6 * p.total
    q = network.Particle(path, config.COLORS["green"], travel_s=0.5)
    q.s = 0.02 * q.total                                            # just left the start: a SHORT tail only
    data = network.particle_render_data([p, q])
    pos, halo_c, halo_sz = data["halo"]
    assert len(pos) == 2 and pos.shape == (2, 3)
    core_pos, core_c, core_sz = data["core"]
    spark_pos, spark_c, spark_sz = data["spark"]
    assert halo_sz[0] > 2 * core_sz[0] > 2 * spark_sz[0]                    # larger outer halo, solid dot, small bright centre
    assert halo_c[0, 3] < 0.4 and core_c[0, 3] == 1.0 and spark_c[0, 3] > 0.9   # translucent halo, solid core
    assert spark_c[0, :3].sum() > core_c[0, :3].sum()                       # the centre is lighter than the dot
    tpos, tcol, tsz = data["trail"]
    assert 1 <= len(tpos) <= 2 * network.TRAIL_POINTS and len(tpos) == len(tcol) == len(tsz)
    mine = tpos[:network.TRAIL_POINTS]                                      # p: full tail, strictly BEHIND the dot on its path
    assert len(mine) == network.TRAIL_POINTS
    assert np.all(mine[:, 0] < pos[0, 0] + 1e-9) and np.all(np.diff(mine[:, 0]) < 0)
    assert np.all(np.diff(tcol[:network.TRAIL_POINTS, 3]) < 0) and np.all(np.diff(tsz[:network.TRAIL_POINTS]) < 0)  # fades + shrinks
    assert network.particle_render_data([]) is None
    # colours come from the dot, so the same layers work on the dark and on the presentation palette
    for name in ("dark", "presentation"):
        c = QColor(config.THEMES[name]["red"])
        r = network.Particle(path, config.THEMES[name]["red"], travel_s=0.5)
        r.s = 0.5 * r.total
        d = network.particle_render_data([r])
        assert d["core"][1][0, 0] == pytest.approx(c.redF()) and d["halo"][1][0, 3] < 0.4


class FakeScatter:
    """Records what the 3D code hands to a GLScatterPlotItem (pyqtgraph.opengl is not installed everywhere)."""
    def __init__(self, **kw):
        self.opts, self.calls = [], []

    def setGLOptions(self, o):
        self.opts.append(o)

    depth = None

    def setDepthValue(self, v):
        self.depth = v

    def setData(self, **kw):
        self.calls.append(kw)


def test_gl_scatter_layers_are_translucent_and_fed_every_tick(qt_app):
    import gui.network_3d as network
    # _build_static creates halo, trail, dot and bright-centre layers, ALL 'translucent' (additive blending vanishes on the
    # light presentation background) and adds them in drawing order
    added = []
    stub = SimpleNamespace(_add=lambda item, *a, **k: added.append(item), _rebuild_dynamic=lambda: None)
    with patch.object(network, "gl", SimpleNamespace(GLScatterPlotItem=FakeScatter), create=True):
        network.Network3D._build_static(stub)
    assert len(added) == 4 and all(x.opts == ["translucent"] for x in added)
    assert [stub.scatter_halo, stub.scatter_trail, stub.scatter, stub.scatter_spark] == added
    assert all(x.depth == network.DOT_DEPTH_VALUE > 0 for x in added)              # drawn after every wire / board / overlay
    # _tick (GL branch) pushes the same dots to all four layers; with no tail yet the trail layer is blanked, not stale
    e = packets()
    n = make_net(qt_app)
    pb = Playback(); pb.select(e.packets[1], pinned=True); pb.position = 1.0
    n.show_stage(pb, e)                                                                # (2D scene: no OpenGL stage lines)
    n.use_gl = True                                                                    # ... now run the GL branch of _tick
    n.scatter_halo, n.scatter_trail, n.scatter, n.scatter_spark = (FakeScatter() for _ in range(4))
    n.auto_rotate = False
    n._tick(0.0001)                                                                    # the dot has only just left: no tail
    assert n.scatter_trail.calls[-1]["size"] == 0.0 and len(n.scatter.calls[-1]["pos"]) == 1
    for _ in range(10):
        n._tick(FRAME)
    halo, trail, core, spark = (x.calls[-1] for x in (n.scatter_halo, n.scatter_trail, n.scatter, n.scatter_spark))
    assert len(halo["pos"]) == len(core["pos"]) == len(spark["pos"]) == 1 and len(trail["pos"]) >= 2
    assert halo["size"][0] > core["size"][0] > spark["size"][0] and core["color"][0, 3] == 1.0
    while n.particles:
        n._tick(FRAME)
    assert n.scatter.calls[-1]["size"] == 0.0 and n.scatter_halo.calls[-1]["size"] == 0.0   # all dots gone: layers blanked
    n.use_gl = False
    n.close()


def test_2d_view_draws_halo_core_and_trail(qt_app):
    import gui.network_3d as network
    e = packets()
    n = make_net(qt_app)
    pb = Playback(); pb.select(e.packets[1], pinned=True); pb.position = 4.0           # a data byte
    n.show_stage(pb, e)
    for _ in range(30):                                                                  # let the resize 'settle' timer fire
        n.view.grab(); qt_app.processEvents(); time.sleep(0.01)
        if not getattr(n.view, "_settling", False):
            break
    p = n.particles[-1]
    p.s = 0.0
    n._tick(0.0001)
    for _ in range(12):                                                                  # fly part of the wire: a tail exists
        n._tick(FRAME * 0.5)
    assert p.s > p.total * TRAIL_NEEDED() and not p.done
    img_with = n.view.grab().toImage()
    x, y, _ = p.pos()
    c = n.view._map(x, y)
    behind = p.pos_at(p.s - p.total * network.TRAIL_STEP * 2)
    cb = n.view._map(behind[0], behind[1])
    n.particles.clear()
    n.view.update()
    img_without = n.view.grab().toImage()
    def px(img, pt):
        return img.pixelColor(int(pt.x()), int(pt.y()))
    assert px(img_with, c) != px(img_without, c)                                         # the dot (halo + core + centre)
    assert px(img_with, cb) != px(img_without, cb)                                       # the comet tail behind it
    n.close()


def TRAIL_NEEDED():
    import gui.network_3d as network
    return network.TRAIL_STEP * 2


# ====================================================================== HUD row 1: concise direction / byte / ACK text
def test_hud_row1_names_what_is_on_the_wire_and_which_way_without_repeating_ack(qt_app):
    import re
    e = packets()
    n = make_net(qt_app)
    txn = e.packets[1]                                              # BMP280 write+read, last read byte NACKed (end of read)
    st = stages(txn)
    want = {
        next(i for i, s in enumerate(st) if s.kind == "addr"): "ADDR 0x76 W=0 ▶",
        next(i for i, s in enumerate(st) if s.kind == "ack"): "ACK ◀",                              # target answers the address
        next(i for i, s in enumerate(st) if s.kind == "data" and s.frame_rw == "W"): "DATA 0xF7 ▶",
        next(i for i, s in enumerate(st) if s.kind == "data" and s.frame_rw == "R"): "DATA 0x51 ◀",
        next(i for i, s in enumerate(st) if s.kind == "ack" and s.ack and s.frame_rw == "R" and s.direction == "W"): "ACK ▶",
        next(i for i, s in enumerate(st) if s.kind == "ack" and s.ack is False): "NACK (end) ▶",  # controller ends the read
    }
    for idx, text in want.items():
        pb = Playback(); pb.select(txn, pinned=True); pb.position = float(idx)
        n._stage_key = None
        n.show_stage(pb, e)
        row1 = re.sub(r"<[^>]+>", "", n.hud_row1.text())
        assert text in row1, (idx, text, row1)
        assert n.stage_text.endswith(text)                          # the badge on the diagram says the same
        if st[idx].kind == "ack":
            assert len(re.findall(r"N?ACK", row1)) == 1, row1       # not 'ACK · ACK'
        assert ("C→T" in row1) == text.endswith("▶") or st[idx].kind == "ack"
    n.close()


# ====================================================================== pacing details
def test_one_uniform_pace_for_every_device_and_mode_nothing_is_fast_forwarded():
    """Review requirement: BMP280 bursts (6 bytes) and OLED frames used to play bytes 4+ at 0.55x and were squeezed into
    14 s per packet while short RTC transfers were not - they visibly fast-forwarded.  Now every stage of every packet,
    followed or pinned, is one full step (the R/W bit half a step: it is the 8th bit of the address phase)."""
    from tests.test_stage_pills_semantic import load_traffic
    e = CaptureEngine()
    e.set_source("demo")
    p = load_traffic(e)
    for name in ("bmp", "rtc", "oled_frame"):
        t = p[name]
        st = stages(t)
        follow, pinned = Playback(), Playback()
        follow.select(t, pinned=False)
        pinned.select(t, pinned=True)
        wf, wp = follow._weights(), pinned._weights()
        assert wf == wp == [Playback.RW_W if s.kind == "rw" else 1.0 for s in st], name
        assert follow.stage_seconds() == pinned.stage_seconds() == Playback.STAGE_S
        assert follow.total_seconds() == pytest.approx(pinned.total_seconds())
    # speed scales the time base only: every duration halves at 2x the speed, the weights stay
    follow.speed = 0.5
    assert follow.stage_wall_seconds(1) == pytest.approx(follow.stage_seconds() / 0.5)
    assert follow._weights() == wf
