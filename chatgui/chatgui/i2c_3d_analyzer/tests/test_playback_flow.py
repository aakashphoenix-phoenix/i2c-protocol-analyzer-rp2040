"""Network playback: every stage's dot runs its WHOLE wire (sender -> receiver) and a packet plays START -> STOP intact.

Frame-steps the real Network3D (2D renderer, headless) against the real Playback exactly like MainWindow._refresh does
(playback advances at the GUI refresh, the dots tick at ~30 Hz), on multi-master read and write packets with ACK and NACK.

Bug being guarded (2026-11-14): following live traffic gave a 23-stage packet 65 ms per stage, a dot needs ~830 ms to cross
the wire, and show_stage() cleared every dot at each stage change - so each dot died after ~8 % of its path; NACK stages and
START/STOP never drew a dot at all, and the last stage (STOP) was never played because the packet counted as finished the
moment it ARRIVED at it."""
import time
from unittest.mock import patch

import numpy as np
import pytest

from analysis.capture_engine import CaptureEngine
from analysis.playback import Playback, stages
from tests.helpers import lines_for, seq_of, synth, write, write_read
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)

FRAME = 1 / 30           # dot tick (Network3D's QTimer is ~33 ms)
REFRESH = 0.1            # GUI refresh (config.GUI_REFRESH_MS = 100): playback advances + show_stage()


def data_write_nack(s):                                   # DS3231 0x68: register write whose data byte is NACKed
    s.start(); s.byte(0x68 << 1); s.byte(0x0E); s.byte(0x1C, ack=False); s.stop()


def packets():
    """OLED write (M1) · BMP280 write+read, last read byte NACKed = end of read (M2) · RTC data NACK (M1) · BMP address NACK (M2)."""
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines(lines_for(synth(seq_of(write(0x3C, [0x00, 0xAF]), write_read(0x76, 0xF7, [0x51, 0x60, 0x2C]),
                                          data_write_nack, write(0x76, [], nack_addr=True)))))
    assert len(e.packets) == 4, len(e.packets)
    for t, mid in zip(e.packets, (1, 2, 1, 2)):
        t.master_id, t.master_method = mid, "telemetry"
    return e


def make_net(qt_app):
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False):
        n = network.Network3D(prefer_3d=False)
    n.resize(1100, 600)
    n.show()
    qt_app.processEvents()
    assert n.isVisible()
    return n


def expected_direction(stage, previous_byte_stage):
    """Who transmits this stage, from the I2C protocol itself (independent of Stage.direction)."""
    if stage.kind in ("cond", "addr", "rw"):
        return "W"                                         # controller drives START/STOP and the address byte
    if stage.kind == "data":
        return stage.frame_rw                              # W: controller sends · R: target sends
    # ACK/NACK slot: the RECEIVER of the byte answers: target for address / written data, controller for read data
    return "W" if previous_byte_stage.kind == "data" and previous_byte_stage.frame_rw == "R" else "R"


def play(n, e, txn, speed, pinned):
    """Step playback + dots like the GUI.  Returns per-stage records."""
    pb = Playback()
    pb.speed = speed
    pb.select(txn, pinned=pinned)
    assert n.particles_cut == 0
    n.show_stage(pb, e)                                    # what select_transaction() does: shows stage 0 at once
    records, known, seen, byte_stage = {}, set(), [], None
    t, next_refresh, steps = 0.0, REFRESH, 0

    def harvest(idx):
        nonlocal byte_stage
        stage = pb.events[idx]
        new = [p for p in n.particles if id(p) not in known]
        known.update(id(p) for p in new)
        records[idx] = dict(stage=stage, particles=new, t_spawn=t, label=n.stage_text, arrived=None,
                            previous=byte_stage)
        if stage.kind in ("addr", "rw", "data"):
            byte_stage = stage

    seen.append(pb.stage_index)
    harvest(pb.stage_index)
    while (not pb.finished or n.particles) and steps < 200_000:
        steps += 1
        if t + 1e-9 >= next_refresh:
            pb.advance(min(REFRESH, pb.max_step_seconds()))
            n.show_stage(pb, e)
            if pb.stage_index != seen[-1]:
                seen.append(pb.stage_index)
                harvest(pb.stage_index)
            next_refresh += REFRESH
        n._tick(FRAME)
        t += FRAME
        for r in records.values():
            for p in r["particles"]:
                if p.done and r["arrived"] is None:
                    r["arrived"] = t
    assert steps < 200_000, "playback never finished"
    return pb, records, seen


def check_packet(n, e, txn, speed, pinned, coverage):
    pb, records, seen = play(n, e, txn, speed, pinned)
    count = len(pb.events)
    assert seen == list(range(count)), f"stage order/skips: {seen}"              # every stage shown, in order, STOP included
    assert pb.events[-1].text == "STOP"
    assert n.particles_cut == 0, "a dot was cut off before reaching its receiver"
    for idx, r in records.items():
        st = r["stage"]
        stage_wall = pb.stage_wall_seconds(idx)
        if st.kind == "rw":
            # the R/W bit is a bit of the ADDR byte whose dot just crossed: NO second, overlapping M->S dot for it
            assert r["particles"] == [], f"stage {idx} {st.text}: the R/W bit must not fire a second dot"
            want = expected_direction(st, r["previous"])
            assert r["label"].endswith("▶") and ("READ R=1" if st.frame_rw == "R" else "WRITE W=0") in r["label"]
            coverage.add((st.kind, want, st.ack, txn.master_id))
            continue
        assert len(r["particles"]) == 1, f"stage {idx} {st.text}: expected exactly one dot, got {len(r['particles'])}"
        p = r["particles"][0]
        assert p.done and p.s >= p.total, f"stage {idx} {st.text}: dot stopped at {p.s / p.total:.0%}"
        import gui.network_3d as _net
        assert r["arrived"] is not None and \
            r["arrived"] - r["t_spawn"] <= max(0.85 * stage_wall, _net.MIN_TRAVEL_S) + 2 * FRAME, \
            f"stage {idx} {st.text}: dot needed {r['arrived'] - r['t_spawn']:.3f}s of a {stage_wall:.3f}s stage"
        # direction + route: sender node -> receiver node along SDA
        want = expected_direction(st, r["previous"])
        route = n.model.path_to(st.address if st.address is not None else txn.address, "SDA", master=txn.master_id)
        route = route[::-1] if want == "R" else route
        assert np.allclose(p.path, np.asarray(route, float)), f"stage {idx} {st.text}: wrong wire direction"
        # colour: ACK green, NACK red, everything else in the owning master's write/read colour
        import gui.network_3d as network
        expected_col = (network.ACK_COL if st.ack is True else network.NACK_COL if st.ack is False else
                        network.master_color(txn.master_id, want))
        assert p.color == expected_col, f"stage {idx} {st.text}: colour {p.color} != {expected_col}"
        # label on the diagram: what is on the wire + which way it flows
        label, arrow = r["label"], ("▶" if want == "W" else "◀")
        assert label.endswith(arrow), f"stage {idx}: label {label!r} does not point {want}"
        if st.kind == "addr":
            assert f"ADDR 0x{st.address:02X}" in label and ("R=1" if st.frame_rw == "R" else "W=0") in label
        elif st.kind == "rw":
            assert ("READ R=1" if st.frame_rw == "R" else "WRITE W=0") in label
        elif st.kind == "data":
            assert f"DATA 0x{st.byte:02X}" in label
        elif st.kind == "ack":
            assert ("ACK" in label) if st.ack else ("NACK" in label), label
        else:
            assert st.text in label
        coverage.add((st.kind, want, st.ack, txn.master_id))
    return pb, records


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("pinned", [True, False])
def test_every_stage_dot_runs_its_whole_wire_with_the_right_direction_colour_and_label(qt_app, speed, pinned):
    e = packets()
    n = make_net(qt_app)
    coverage = set()
    for txn in e.packets:
        check_packet(n, e, txn, speed, pinned, coverage)
    kinds = {(k, d, a) for k, d, a, _m in coverage}
    assert ("cond", "W", None) in kinds                                           # START / Repeated START / STOP
    assert ("addr", "W", None) in kinds and ("rw", "W", None) in kinds           # address: master -> slave
    assert ("data", "W", None) in kinds                                           # data write: master -> slave
    assert ("data", "R", None) in kinds                                           # data read: slave -> master
    assert ("ack", "R", True) in kinds                                            # ACK from the target
    assert ("ack", "W", True) in kinds                                            # ACK from the controller (read data)
    assert ("ack", "W", False) in kinds                                           # NACK: controller ends the read
    assert ("ack", "R", False) in kinds                                           # NACK: target did not answer (addr/data)
    assert {m for *_x, m in coverage} == {1, 2}                                   # both masters drive their own wire
    n.close()


def test_stage_change_inside_a_packet_never_clears_a_dot_in_flight(qt_app):
    e = packets()
    n = make_net(qt_app)
    pb = Playback()
    pb.select(e.packets[1], pinned=True)
    pb.position = 1.0
    n.show_stage(pb, e)
    n._tick(0.05)
    first = list(n.particles)
    assert len(first) == 1 and 0 < first[0].s < first[0].total
    pb.position = 3.0                                                              # next dotted stage (ACK) starts while it flies
    n.show_stage(pb, e)
    assert first[0] in n.particles and len(n.particles) == 2 and n.particles_cut == 0
    # the NEXT packet starting does not kill dots still in flight (the STOP dot of a finished packet): they arrive
    pb.select(e.packets[2], pinned=True)
    n.show_stage(pb, e)
    assert first[0] in n.particles and n.particles_cut == 0 and len(n.particles) == 3
    for _ in range(80):
        n._tick(FRAME)
    assert n.particles == [] and n.particles_cut == 0                              # every one of them arrived
    # a NEW CAPTURE (no stage any more) is a reset: whatever is still flying is dropped, and counted as cut
    pb.select(e.packets[1], pinned=True)
    n._stage_key = None
    n.show_stage(pb, e)
    assert len(n.particles) == 1
    n.show_stage(Playback(), e)
    assert n.particles == [] and n.particles_cut == 1
    n.close()


def test_dots_freeze_while_paused_and_a_hidden_view_spawns_none(qt_app):
    e = packets()
    n = make_net(qt_app)
    pb = Playback()
    pb.select(e.packets[1], pinned=True)
    n.show_stage(pb, e)
    n._tick(0.05)
    s0 = n.particles[0].s
    n.playback_paused = True
    n._tick(0.2)
    assert n.particles[0].s == s0
    n.playback_paused = False
    hidden = make_net(qt_app)
    hidden.hide()
    pb.position = 3.0
    hidden.show_stage(pb, e)
    assert hidden.particles == []
    n.close(); hidden.close()


def test_dot_travel_follows_the_playback_pace_and_speed(qt_app):
    import gui.network_3d as network
    e = packets()
    n = make_net(qt_app)
    txn = e.packets[1]
    for speed in (0.5, 1.0, 2.0):
        for pinned in (True, False):
            pb = Playback()
            pb.speed = speed
            pb.select(txn, pinned=pinned)
            n._stage_key = None                                                      # same packet + stage: force a respawn
            n.show_stage(pb, e)
            p = n.particles[-1]
            floor = network.MIN_TRAVEL_S                                                  # never fewer than ~6 dot frames
            assert p.travel_s == pytest.approx(max(floor, 0.8 * pb.stage_wall_seconds(0)))   # stage 0 = START
            if 0.8 * pb.stage_wall_seconds(0) >= floor:
                assert p.travel_s < pb.stage_wall_seconds(0)                              # done before the stage ends
            pb.position = 1.0                                                             # stage 1 = the ADDR byte
            n.show_stage(pb, e)
            q = n.particles[-1]
            assert q.travel_s == pytest.approx(max(floor, 0.8 * pb.stage_wall_seconds(1)))
            assert q.travel_s == pytest.approx(p.travel_s)                                # one uniform pace: START = a byte
            assert 0.44 <= q.travel_s * speed <= 0.66                                     # 0.45-0.65 s per crossing at 1x
    n.close()


def test_playback_pace_is_readable_and_the_last_stage_gets_its_time():
    e = packets()
    for t in e.packets:
        n = len(stages(t))
        f = Playback(); f.select(t, pinned=False)
        assert f.stage_seconds() == Playback.STAGE_S                              # one uniform pace (no follow compression)
        assert 0.8 * f.stage_seconds() >= 0.44                                    # a byte dot takes >= ~0.45 s (was 0.16 s)
        pin = Playback(); pin.select(t, pinned=True)
        assert pin.stage_seconds() == Playback.STAGE_S == 0.8
        assert 0.44 <= 0.8 * pin.stage_seconds() <= 0.66                          # 0.64 s per crossing
        for pb in (f, pin):
            # START / STOP are full steps (COND_W = 1), the R/W bit half a step; every other stage a full step
            assert pb.stage_duration(0) == pytest.approx(Playback.COND_W * pb.stage_seconds())
            assert pb.stage_duration(n - 1) == pytest.approx(Playback.COND_W * pb.stage_seconds())
            assert pb.stage_duration(2) == pytest.approx(Playback.RW_W * pb.stage_seconds())      # 'WRITE W=0'
            assert pb.stage_duration(1) == pytest.approx(pb.stage_seconds())                        # ADDR
            pb.advance(pb.total_seconds() - 0.5 * pb.stage_wall_seconds(n - 1))   # half way through the LAST stage
            assert pb.stage_index == n - 1 and not pb.finished                    # STOP is playing, packet not over
            assert pb.current.text == "STOP"
            pb.advance(pb.stage_wall_seconds(n - 1))
            assert pb.finished and pb.stage_index == n - 1 and pb.stage_fraction == 1.0
        # a GUI stall can never jump over a stage
        s = Playback(); s.select(t, pinned=True)
        before = s.stage_index
        s.advance(min(5.0, s.max_step_seconds()))
        assert s.stage_index - before <= 1


# ------------------------------------------------------------------ the real MainWindow loop: whole packets, in order, no interruption
@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_a_packet_plays_start_to_stop_before_the_next_one_starts(qt_app, speed):
    from tests.test_inspect_follow import live_window, Bus
    w, _ = live_window()
    bus, t_us = Bus(), 2_000_000
    w._on_lines(bus.until(t_us))
    w.playback.speed = speed
    log, sim = [], 0.0                                     # (txn, stage index, finished-before-this-refresh)
    switches = []
    last = None
    packets_seen = 0
    while packets_seen < 3 and sim < 120:
        if int(sim * 10) % 10 == 0:                        # keep fresh traffic arriving every second of simulated time
            t_us += 400_000
            w._on_lines(bus.until(t_us))
        was_finished = w.playback.finished
        w._playback_wall = time.monotonic() - REFRESH      # exactly one GUI refresh (100 ms) of wall time has passed
        w._refresh()
        sim += REFRESH
        txn, idx = w.playback.txn, w.playback.stage_index
        if txn is not last:
            if last is not None:
                switches.append(was_finished)              # the previous packet must have finished before this switch
            packets_seen += 1
            last = txn
        log.append((id(txn), idx, w.playback.finished))
    assert switches and all(switches), "a packet was replaced before its STOP had been played"
    by_packet = {}
    for key, idx, _fin in log:
        by_packet.setdefault(key, []).append(idx)
    complete = list(by_packet.values())[:-1]               # the last one may still be playing when the loop ends
    assert complete
    for seq in complete:
        dedup = [i for k, i in enumerate(seq) if k == 0 or i != seq[k - 1]]
        assert dedup == list(range(len(dedup))) and dedup[-1] == len(dedup) - 1, f"stages skipped/reordered: {dedup}"
        assert len(dedup) >= 8                             # whole transactions (START .. STOP), not a fragment
    w.close()


# ------------------------------------------------------------------ live-GUI findings (2026-11-14): the GUI thread must not starve
def test_static_scene_is_painted_once_not_every_frame(qt_app):
    """A 2D frame used to repaint wires, scaled board photos and ~25 captions with antialiasing (~27 ms on a 125 % display
    = the whole 33 ms frame budget), so the dot timer ran at ~12 fps and the GUI thread starved."""
    e = packets()
    n = make_net(qt_app)
    pb = Playback()
    pb.select(e.packets[1], pinned=True)
    pb.position = 4.0
    n.show_stage(pb, e)
    for _ in range(60):                                     # let the resize 'settle' timer fire like the real event loop
        n.view.grab(); qt_app.processEvents(); time.sleep(0.01)
        if not getattr(n.view, "_settling", False):
            break
    n.view.grab()
    layers = n.view._layer_cache
    for k in range(6, 12):                                  # stage changes, dots moving, glows decaying: same static layers
        pb.position = float(k)
        n.show_stage(pb, e)
        n._tick(FRAME)
        n.view.grab()
        assert n.view._layer_cache is layers, "static layers were rebuilt for an animation frame"
    n.resize(n.width() + 80, n.height() + 40)                # a different size DOES rebuild them
    for _ in range(60):
        qt_app.processEvents(); time.sleep(0.01)
    n.view.grab()
    assert n.view._layer_cache is not layers
    n.close()


def test_badge_slot_search_runs_once_per_width_bucket_not_per_stage(qt_app):
    e = packets()
    n = make_net(qt_app)
    pb = Playback()
    pb.select(e.packets[1], pinned=True)
    n.show_stage(pb, e)
    n.view.grab()
    rects = list(n.view.label_rects.values())
    base = len(getattr(n, "_badge_slots", {}))
    for w in (101, 110, 123, 131, 140, 96, 100, 125):       # eight different stage-text widths, two 48 px buckets
        r = n.place_badge(w, 20, n.view.project, n.view.width(), n.view.height(), rects)
        assert r.width() == w and r.height() == 20           # the badge keeps the text's own size
    assert len(n._badge_slots) - base <= 3
    n.close()


def test_a_stalled_gui_thread_makes_dots_lag_instead_of_teleporting(qt_app):
    e = packets()
    n = make_net(qt_app)
    pb = Playback()
    pb.select(e.packets[1], pinned=True)
    n.show_stage(pb, e)
    p = n.particles[-1]
    n._tick_wall = time.monotonic() - 3.0                   # the previous tick was 3 s ago (GUI thread was blocked)
    n._tick()
    import gui.network_3d as network
    assert 0 < p.s / p.total <= network.STEP_CAP + 1e-9     # moved one capped step (<= STEP_CAP of the wire), not all of it
    assert not p.done
    n.close()


def test_hidden_diagram_tabs_are_not_updated_every_refresh(qt_app):
    from tests.test_inspect_follow import live_window
    w, _ = live_window()
    w.show()
    qt_app.processEvents()
    calls = []
    for name in ("p_dash", "p_net", "p_dev"):
        net = getattr(w, name).net
        orig = net.show_stage
        net.show_stage = (lambda pb, eng, _n=name, _o=orig: (calls.append(_n), _o(pb, eng))[1])
    w._sync_playback()
    visible = {n for n in ("p_dash", "p_net", "p_dev") if getattr(w, n).net.isVisible()}
    assert set(calls) == visible and len(visible) <= 1       # only the diagram on the current tab is refreshed
    w.close()


def test_attribution_arriving_mid_stage_does_not_cut_the_dot_and_adds_one_on_the_masters_full_wire(qt_app):
    """Live hardware: the first packet after connecting starts unattributed; when the controller's telemetry attributes it
    the stage used to be 'restarted' (the dot in flight was dropped = cut, a second one spawned).  Now the dot in flight
    FINISHES (never cut) and the stage also gets ONE dot on the attributed master's complete wire - the analyzer-tap stub
    of an unattributed packet is only a fraction of it."""
    e = packets()
    n = make_net(qt_app)
    txn = e.packets[1]
    pb = Playback()
    pb.select(txn, pinned=True)
    pb.position = 5.0
    txn.master_id = None
    n.show_stage(pb, e)
    n._tick(0.05)
    assert len(n.particles) == 1
    dot = n.particles[0]
    txn.master_id = 2                                           # attribution arrives while the dot flies
    n.show_stage(pb, e)
    assert n.particles[0] is dot and n.particles_cut == 0       # the same dot is still flying: nothing was dropped
    assert len(n.particles) == 2                                # + one on master 2's full wire
    full = n.particles[1]
    want = np.asarray(n.model.path_to(txn.address, "SDA", master=2), float)
    assert np.allclose(full.path, want) or np.allclose(full.path, want[::-1])      # Master 2 pin <-> target pin, whole wire
    assert len(full.path) == len(want) and len(dot.path) == 2                     # (the stub is the shared-bus drop only: no feeder, no analyzer tap)
    assert full.s == 0.0 and full.total > dot.total             # starts at Master 2's pin, longer than the tap stub
    assert n._stage_paths(n._stage)[0] is not None              # the highlighted path now belongs to master 2
    for _ in range(60):
        n._tick(FRAME)
    assert dot.done and full.done and n.particles == [] and n.particles_cut == 0
    pb.position = 6.0                                           # and the next stage's dot runs on the master's wire
    n.show_stage(pb, e)
    assert len(n.particles) == 1
    n.close()
