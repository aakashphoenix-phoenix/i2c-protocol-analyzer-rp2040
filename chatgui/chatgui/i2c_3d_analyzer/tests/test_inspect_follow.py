"""Regression: inspecting devices must never freeze the diagram or touch capture.

Bug (2026-11-09, reported on hardware): after inspecting two devices, the third one
"stopped updating" until Stop/Start capture.  Causes: (1) one long packet held the
diagram 13.6-15 s at 1x; (2) a packet-row selection pinned playback forever and
device inspection never un-pinned it; Start capture "fixed" it by clearing all data.
Replayed analyzer-format records only — not a hardware test.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

from analysis.playback import Playback, stages
from tests.test_hardware_pipeline import qt_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from repro_inspect_freeze import Bus, HELLO   # noqa: E402


def live_window():
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    w.visual_speed.setCurrentIndex(w.visual_speed.findData(0.25))   # review pace + fair queue (Real-time is tested apart)
    sent = []
    w.worker = type("W", (), {"send": lambda self, c: sent.append(c), "isRunning": lambda self: True,
                               "port": "REPLAY", "stop": lambda self: None, "wait": lambda self, t=0: True})()
    w.capturing = True
    w.engine.set_source("live"); w.engine.stream_connected = True; w.engine.capture_active = True
    w._on_lines([json.dumps(HELLO).encode()])
    return w, sent


def test_follow_mode_plays_long_packets_in_bounded_readable_time_pinned_stays_readable():
    from analysis.capture_engine import CaptureEngine
    e = CaptureEngine(); e.set_source("live"); e.ingest_lines([json.dumps(HELLO).encode()] + Bus().until(2_000_000))
    rtc = next(t for t in e.packets if t.address == 0x68)
    assert len(stages(rtc)) == 43
    follow = Playback(); follow.select(rtc, pinned=False)
    # 43 stages at the ONE uniform pace (no compressed / brisk tail any more: that fast-forwarded long frames): every
    # stage a full step, the R/W bit half a step; the packet still finishes, it is never frozen
    assert follow.stage_seconds() == Playback.STAGE_S
    assert follow.total_seconds() == sum(0.5 if s.kind == "rw" else 1.0 for s in stages(rtc)) * Playback.STAGE_S
    follow.advance(1.5)
    assert not follow.finished
    follow.advance(follow.total_seconds())
    assert follow.finished and follow.position == 43
    pinned = Playback(); pinned.select(rtc, pinned=True); pinned.advance(1.5)
    # pinned: every stage keeps its full-pace duration (stepping / pinned playback never compresses a packet)
    left, idx = 1.5, 0
    while left >= pinned.stage_duration(idx):
        left -= pinned.stage_duration(idx); idx += 1
    assert not pinned.finished and int(pinned.position) == idx
    assert pinned.total_seconds() == follow.total_seconds()                  # one pace, pinned or followed


def test_inspecting_three_devices_repeatedly_follows_each_and_never_touches_capture(qt_app):
    w, sent = live_window()
    bus = Bus(); t_us = 1_000_000
    for _ in range(2):                                        # two rounds over three devices
        for addr in (0x68, 0x76, 0x3C):
            t_us += 2_000_000
            w._on_lines(bus.until(t_us))
            w.p_net.net.node_clicked.emit(addr)              # 3D I2C Network page click
            n_before = len(w.engine.packets)
            assert w.playback.txn.address == addr and not w.playback.pinned
            assert w.playback.txn is next(t for t in reversed(w.engine.packets) if t.address == addr)
            assert w.engine.capture_active and w.capturing and sent == []
            t_us += 500_000
            w._on_lines(bus.until(t_us))
            assert len(w.engine.packets) > n_before          # events keep arriving
    assert w.engine.lost_records == 0 and w.engine.transport_errors == 0
    w.close()


def test_pinned_packet_is_released_by_device_inspection_and_by_follow_latest(qt_app):
    w, _ = live_window()
    bus = Bus(); w._on_lines(bus.until(3_000_000))
    old = w.engine.packets[5]
    w.select_transaction(old)                                # e.g. a packet-table row click
    assert w.playback.pinned and w.playback.txn is old
    w._on_lines(bus.until(5_000_000)); w._refresh()
    assert w.playback.txn is old                             # deliberate pin is respected...
    assert "PINNED packet" in w.p_net.net.transaction_overlay.text()
    w.focus_device(0x76)                                     # ...until the user inspects a device
    assert not w.playback.pinned and w.playback.txn.address == 0x76 and w.playback.txn is not old
    w.select_transaction(old)
    w.follow_device(None)                                    # "Follow latest" button path
    assert not w.playback.pinned and w.playback.txn is w.engine.packets[-1]
    assert "FOLLOWING live traffic" in w.p_net.net.transaction_overlay.text()
    w.close()


def test_packet_arriving_mid_playback_is_not_skipped_when_playback_finishes(qt_app):
    w, _ = live_window()
    bus = Bus(); w._on_lines(bus.until(2_000_000)); w.focus_device(0x68)
    first = w.playback.txn
    w._on_lines(bus.until(3_100_000)); w._refresh()          # new 0x68 packet arrives in this tick
    newest = next(t for t in reversed(w.engine.packets) if t.address == 0x68)
    assert newest is not first
    for _ in range(3):                                       # later ticks bring no 0x68 traffic
        w.engine.take_activity(); w.playback.advance(w.playback.total_seconds() * 2); w._refresh()
    assert w.playback.txn is newest                          # was lost: only the latest tick was checked
    w.close()


def test_pause_display_does_not_stop_collection(qt_app):
    w, _ = live_window()
    bus = Bus(); w._on_lines(bus.until(2_000_000)); w._refresh()
    w._pause_display(True)
    shown = w.playback.txn; n = len(w.engine.packets)
    w._on_lines(bus.until(4_000_000)); w._refresh()
    assert len(w.engine.packets) > n and w.engine.capture_active and w.playback.txn is shown
    w._pause_display(False); w._refresh(); w.playback.advance(w.playback.total_seconds() * 2); w._refresh()
    assert w.playback.txn is not shown
    w.close()
