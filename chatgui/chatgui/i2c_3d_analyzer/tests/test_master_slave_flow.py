"""Master 1 / Master 2 <-> slave TX / RX flow in the network view; the passive analyzer is never an endpoint."""
import numpy as np
import pytest

import gui.network_3d as net
from analysis.playback import Playback, stages
from tests.test_dot_full_path import engine_with, rtc_read
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)
from tests.test_playback_flow import make_net


def _play_all(n, e, txn):
    """Show every stage of `txn`; returns [(stage, dot or None)] where dot is the particle spawned by that stage."""
    out = []
    pb = Playback()
    pb.select(txn, pinned=True)
    for i, st in enumerate(stages(txn)):
        before = list(n.particles)
        pb.position = float(i)
        n.show_stage(pb, e)
        new = [p for p in n.particles if p not in before]
        out.append((st, new[0] if new else None))
        n._tick(0.05)
    return out


@pytest.mark.parametrize("mid,key", [(1, "master"), (2, "master2")])
def test_each_master_transmits_to_and_receives_from_its_target_never_the_analyzer(qt_app, mid, key):
    e = engine_with(rtc_read())
    t = e.packets[0]
    t.master_id, t.master_method = mid, "telemetry"
    n = make_net(qt_app)
    played = _play_all(n, e, t)
    tx = [(s, d) for s, d in played if d is not None and s.direction == "W" and s.kind != "ack"]
    rx = [(s, d) for s, d in played if d is not None and s.direction == "R" and s.kind == "data"]
    assert tx and rx                                                          # a register read: address out, data back
    for s, d in tx:                                                           # TX: master -> RTC
        assert (d.src, d.dst) == (key, t.address) and d.color == net.master_color(mid, "W")
        assert np.allclose(d.path[0], n.model.path_to(t.address, "SDA", master=mid)[0])
        assert net.flow_tag(s, t) == f"M{mid} TX"
    for s, d in rx:                                                           # RX: RTC -> master (reverse wire, read colour)
        assert (d.src, d.dst) == (t.address, key) and d.color == net.master_color(mid, "R")
        assert np.allclose(d.path[-1], n.model.path_to(t.address, "SDA", master=mid)[0])
        assert net.flow_tag(s, t) == f"M{mid} RX"
    assert all("analyzer" not in (d.src, d.dst) for _s, d in played if d is not None)
    other = "master2" if key == "master" else "master"
    assert all(other not in (d.src, d.dst) for _s, d in played if d is not None)   # the other master is not involved


def test_master_1_and_master_2_flows_are_visibly_different(qt_app):
    n = make_net(qt_app)
    m = n.model
    p1, p2 = m.path_to(0x76, "SDA", master=1), m.path_to(0x76, "SDA", master=2)
    assert p1[0] != p2[0] and p1[-1] == p2[-1]                                # different origin pins, same target pin
    assert len({net.master_color(1, "W"), net.master_color(1, "R"), net.master_color(2, "W"), net.master_color(2, "R")}) == 4


def test_unattributed_packet_is_bus_only_and_the_analyzer_only_observes(qt_app):
    e = engine_with(rtc_read())
    t = e.packets[0]
    t.master_id, t.master_method = None, ""
    n = make_net(qt_app)
    played = _play_all(n, e, t)
    dots = [d for _s, d in played if d is not None]
    assert dots and all(d.color in (net.UNATTR_COL, net.ACK_COL, net.NACK_COL) for d in dots)
    assert all("analyzer" not in (d.src, d.dst) and "master" not in (d.src, d.dst) and "master2" not in (d.src, d.dst)
               for d in dots)                                                 # no board is named as the transmitter
    tap = np.array((n.model.tap_x("SDA"), net.RAIL_Y["SDA"], net.RAIL_Z))
    assert all(not np.allclose(d.path[0], tap) and not np.allclose(d.path[-1], tap) for d in dots)
    assert n.owner is None and n.flash_color["analyzer"] == net.OBS_COL      # dim neutral 'observing' outline only
    assert net.flow_tag(played[1][0], t) == "bus TX"
    assert "passive tap" in n.hud_row2.text() and "never transmits" in n.hud_row2.text()


def test_stage_text_says_tx_or_rx_and_which_master():
    from types import SimpleNamespace as NS
    st_w = NS(kind="data", direction="W", address=0x76, ack=None, text="DATA 0xF7")
    st_r = NS(kind="data", direction="R", address=0x76, ack=None, text="DATA 0x51")
    names = {0x76: "BMP280 0x76"}
    for mid in (1, 2):
        t = NS(master_id=mid)
        assert stage_text(st_w, t, names).startswith(f"TX Pico") and f"Master {mid}" in stage_text(st_w, t, names)
        assert stage_text(st_r, t, names).startswith("RX BMP280") and f"Master {mid}" in stage_text(st_r, t, names)


def stage_text(st, t, names):
    return net.stage_description(st, t, names)[0]


def test_follow_queue_never_starves_master_2_behind_master_1_oled_bursts():
    """Live-bench mix (measured: ~3200 M1 vs ~170 M2 packets per minute): Master 2 still gets a turn at least once every
    MASTER_GUARD + 1 played packets, while no address is played twice in a row."""
    from types import SimpleNamespace as NS
    from analysis.playback import FollowQueue
    q = FollowQueue()
    played, now = [], 0.0
    for k in range(200):
        batch = [NS(address=0x3C, master_id=1, master_method="ownership") for _ in range(15)]
        batch += [NS(address=0x76, master_id=1, master_method="telemetry"), NS(address=0x68, master_id=1, master_method="telemetry")]
        if k % 2 == 0:
            batch.append(NS(address=(0x76, 0x68)[k // 2 % 2], master_id=2, master_method="telemetry"))
        q.offer(batch, now=now)
        if k % 3 == 0:                                   # one packet finishes playing every few batches
            t = q.pop(now=now + 5)
            if t is not None:
                played.append(t)
        now += 0.2
    mids = [t.master_id for t in played]
    addrs = [t.address for t in played]
    assert 2 in mids and all(a != b for a, b in zip(addrs, addrs[1:]))
    gaps = [j - i for i, j in zip([i for i, m in enumerate(mids) if m == 2], [i for i, m in enumerate(mids) if m == 2][1:])]
    assert max(gaps) <= FollowQueue.MASTER_GUARD + 1, mids
    assert {0x3C, 0x68, 0x76} <= set(addrs)


def test_every_master_target_pair_is_shown_within_a_bounded_number_of_transfers():
    """Live bench: Master 2 polls the RTC and the BMP280 far less than Master 1 drives the OLED, and at the uniform review
    pace only a few transfers play per minute: the least recently shown (target, master) pair goes next, so every one of
    the 5 bench pairs (Master 2 <-> RTC and Master 2 <-> BMP280 included) is shown within 5 transfers."""
    from types import SimpleNamespace as NS
    from analysis.playback import FollowQueue
    q = FollowQueue()
    played, now = [], 0.0
    for k in range(400):
        batch = [NS(address=0x3C, master_id=1, master_method="ownership") for _ in range(10)]
        batch += [NS(address=0x76, master_id=1, master_method="telemetry"), NS(address=0x68, master_id=1, master_method="telemetry")]
        batch += [NS(address=0x76, master_id=2, master_method="telemetry"), NS(address=0x68, master_id=2, master_method="telemetry")]
        q.offer(batch, now=now)
        if k % 4 == 0:
            t = q.pop(now=now + 5)
            if t is not None:
                played.append((t.master_id, t.address))
        now += 0.2
    pairs = {(1, 0x3C), (1, 0x68), (1, 0x76), (2, 0x68), (2, 0x76)}
    assert pairs <= set(played)
    for q in pairs:                                                              # bounded wait for every pair
        idx = [i for i, p in enumerate(played) if p == q]
        assert idx[0] <= len(pairs) and max(b - a for a, b in zip(idx, idx[1:])) <= len(pairs), (q, idx)   # 1st pick = newest
