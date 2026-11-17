"""Focused regressions for the topology diagrams, demo realism, RTC provenance
and NACK/health classification.  Deterministic software vectors only — these
are NOT physical hardware verification."""
import json
import time
from unittest.mock import patch

import pytest

from analysis.capture_engine import CaptureEngine
from analysis.communication_health import acknowledgment_counts
from analysis.playback import Playback, stages
from sources.demo_source import DemoGenerator, SIM_RTC_EPOCH, OLED_INIT
from tests.helpers import synth, write, write_read, lines_for
from tests.test_hardware_pipeline import qt_app
from tests.test_native_firmware import HELLO, seg


def demo_engine(seconds=3.0, **kw):
    g = DemoGenerator(seed=3, **kw)
    e = CaptureEngine(); e.set_source('demo')
    e.ingest_lines([g.hello()])
    k = 1
    while k * 0.05 <= seconds:
        e.ingest_lines(g.lines_until(k * 0.05)); k += 1
    return e


def live_engine():
    e = CaptureEngine(); e.set_source('live')
    e.stream_connected = True; e.capture_active = True; e.last_record_wall = time.monotonic()
    return e


# ------------------------------------------------------------------ demo realism
def test_demo_matches_controller_firmware_traffic():
    e = demo_engine(3.2)
    addrs = {t.address for t in e.packets}
    assert addrs == {0x68, 0x76, 0x3C}                    # no invented absent-address probe
    rtc = [t for t in e.packets if t.address == 0x68]
    assert rtc and all(t.operation == 'WRITE→READ' and t.frames[0].data == [0x00]
                       and len(t.frames[1].data) == 16 and t.event_seq() == "S Sr P" for t in rtc)   # regs 0x00..0x0F
    bmp = [t for t in e.packets if t.address == 0x76 and t.has_read]
    assert {t.frames[0].data[0] for t in bmp} >= {0xD0, 0x88, 0xF7}
    oled = [t for t in e.packets if t.address == 0x3C]
    cmds = [t.frames[0].data for t in oled if t.frames[0].data[0] == 0x00]
    assert [c[1] for c in cmds[:len(OLED_INIT)]] == OLED_INIT          # one command per transaction
    assert all(len(t.frames[0].data) == 17 for t in oled if t.frames[0].data[0] == 0x40)
    s = e.perf.compute()
    assert s.incomplete == 0 and s.malformed == 0
    # every raw NACK is the controller's normal end-of-read NACK, and it is still counted
    assert s.nacks == s.read_end_nacks == sum(1 for t in e.packets if t.has_read)
    assert s.unexpected_nacks == 0


def test_demo_rtc_is_simulated_epoch_not_pc_clock():
    e = demo_engine(2.2)
    r = e.rtc_status()
    assert r['state'] == 'VALID' and 'SIMULATED' in r['source']
    assert r['text'].startswith(SIM_RTC_EPOCH.strftime('%Y-%m-%d %H:%M'))


def test_demo_health_is_labelled_simulated_never_verified():
    e = demo_engine(2.0)
    h = e.health_summary()
    assert h['state'] == 'UNKNOWN' and h['simulated'] and h['simulated_state'] == 'HEALTHY'
    assert 'SIMULATED' in h['detail']


def test_opt_in_fault_injection_degrades_simulated_policy_only():
    e = demo_engine(8.5, inject_absent_probe=True)
    c = e.health_summary()['counts']
    assert c['unexpected_nacks'] >= 1 and c['address_nacks'] >= 1
    assert c['raw_nacks'] == c['expected_read_nacks'] + c['unexpected_nacks'] + c['unverified_read_end_nacks']
    assert e.health_summary()['simulated_state'] == 'DEGRADED'
    assert e.health_summary()['state'] == 'UNKNOWN'


# ------------------------------------------------------------------ NACK classification
def test_rates_use_known_slots_and_keep_end_of_read_in_raw():
    e = live_engine()
    e.ingest_lines(lines_for(synth(write_read(0x68, 0x00, [1, 2, 3]))))
    c = e.health_summary()['counts']
    # addr W ACK, reg ACK, addr R ACK, 2 data ACK, final NACK
    assert (c['acks'], c['raw_nacks'], c['expected_read_nacks'], c['unexpected_nacks']) == (5, 1, 1, 0)
    assert c['opportunities'] == 6
    assert c['raw_nack_pct'] == pytest.approx(100 / 6) and c['unexpected_nack_pct'] == 0
    assert e.health_summary()['state'] == 'HEALTHY'


def test_state_follows_evidence_healthy_degraded_unknown():
    e = live_engine()
    first = lines_for(synth(write(0x3C, [0x00, 0xAF])))
    e.ingest_lines(first)
    assert e.health_summary()['state'] == 'HEALTHY'
    # contiguous sequence numbers: no record loss, so the capture stays trustworthy
    e.ingest_lines(lines_for(synth(write(0x77, [], nack_addr=True), t_start=10_000_000), seq0=len(first) - 1)[1:])
    assert e.health_summary()['state'] == 'DEGRADED'
    e.stream_connected = False
    assert e.health_summary()['state'] == 'UNKNOWN'


def test_read_nack_without_captured_stop_is_unverified_not_unexpected():
    def build(s):
        s.start(); s.byte(0x76 << 1); s.byte(0xF7)
        s.rstart(); s.byte(0x76 << 1 | 1); s.byte(0x11, ack=True); s.byte(0x22, ack=False)
    e = live_engine()
    e.ingest_lines(lines_for(synth(build)))
    txn = e.decoder.check_timeout(e.hw_now + 1e9)[0]
    c = acknowledgment_counts(txn)
    assert c['raw_nacks'] == 1 and c['unverified_read_end_nacks'] == 1
    assert c['unexpected_nacks'] == 0 and c['expected_read_nacks'] == 0


def test_native_capture_starting_at_repeated_start_keeps_end_of_read_expected():
    e = live_engine()
    e.ingest_lines([json.dumps(HELLO).encode()])
    e.ingest_lines([json.dumps(seg(1, 100, 400, 'RESTART', 'STOP', 0x68, 'read', [1, 2], [True, False])).encode()])
    t = e.packets[0]
    assert t.status == 'incomplete'
    c = acknowledgment_counts(t)
    assert c['expected_read_nacks'] == 1 and c['unexpected_nacks'] == 0 and c['raw_nacks'] == 1


def test_live_native_replay_records_classified_and_raw_timestamps_unchanged():
    e = live_engine()
    e.ingest_lines([json.dumps(HELLO).encode()])
    a = seg(1, 100, 250, 'START', 'RESTART', 0x68, 'write', [0], [True])
    b = seg(2, 250, 900, 'RESTART', 'STOP', 0x68, 'read', [0x05, 0x22, 0x08, 1, 0x18, 0x10, 0x26],
            [True] * 6 + [False])
    e.ingest_lines([json.dumps(a).encode(), json.dumps(b).encode()])
    t = e.packets[0]
    assert (t.t_start, t.t_end) == (100_000.0, 900_000.0)
    assert e.health_summary()['counts']['expected_read_nacks'] == 1
    r = e.rtc_status()
    assert r['state'] == 'VALID' and r['source'].startswith('bus-decoded') and r['text'] == '2026-10-18 08:22:05'


# ------------------------------------------------------------------ RTC provenance
def test_rtc_unavailable_stale_and_master_telemetry_labels():
    e = live_engine()
    assert e.rtc_status()['state'] == 'UNAVAILABLE'
    e.update_master_telemetry('2026-10-18T08:22:00', 21.0, 1000.0)
    r = e.rtc_status()
    assert r['state'] == 'VALID' and 'telemetry' in r['source'] and 'not bus-decoded' in r['source']
    e.master_telemetry['received_at'] -= 10
    assert e.rtc_status()['state'] == 'STALE'
    d = demo_engine(0.5)
    d.update_master_telemetry('2026-10-18T08:22:00', 21.0, 1000.0)
    assert 'telemetry' not in d.rtc_status()['source']


def test_bus_decoded_rtc_goes_stale_on_capture_clock():
    e = live_engine()
    e.ingest_lines(lines_for(synth(write_read(0x68, 0x00, [0x05, 0x22, 0x08, 1, 0x18, 0x10, 0x26]))))
    assert e.rtc_status()['state'] == 'VALID'
    e.hw_now += 5e9
    assert e.rtc_status()['state'] == 'STALE'


# ------------------------------------------------------------------ protocol stages
def test_combined_read_stages_are_chronological_with_roles():
    e = live_engine()
    e.ingest_lines(lines_for(synth(write_read(0x76, 0xF7, [0x65, 0x4A]))))
    st = stages(e.packets[0])
    assert [s.text for s in st] == ['START', 'ADDR 0x76', 'WRITE W=0', 'ACK', 'DATA 0xF7', 'ACK',
                                    'Repeated START', 'ADDR 0x76', 'READ R=1', 'ACK', 'DATA 0x65', 'ACK',
                                    'DATA 0x4A', 'NACK', 'STOP']
    ts = [s.timestamp for s in st]
    assert all(t is not None for t in ts) and ts == sorted(ts)
    assert [s.direction for s in st if s.kind == 'data'] == ['W', 'R', 'R']    # who transmits
    assert st[11].direction == 'W'                 # controller ACKs read data
    assert 'end-of-read' in st[13].note


# ------------------------------------------------------------------ both views
# Sizes measured in the running app (dashboard box: 529x260 at 1680x980 and at the
# smallest window; network page: 1037x404 normal, 658x268 small) plus a smaller
# compact box for margin.
@pytest.mark.parametrize('compact,size', [(True, (529, 260)), (True, (420, 220)),
                                          (False, (1037, 404)), (False, (658, 268))])
def test_topology_fits_without_clipping_or_overlap(qt_app, compact, size):
    import gui.network_3d as network
    if True:
        n = network.Network3D(prefer_3d=False, compact=compact)
        n.view.setFixedSize(*size)
        n.view.grab()
        rects = n.view.label_rects
        keys = {s[0] for s in n.model.label_specs(compact, n.label_level)}
        assert keys == set(rects) and {'master', 'analyzer', 'pc', 'rail:SDA', 'rail:SCL', 'rail:GND',
                                       0x68, 0x76, 0x3C} <= keys
        W, H = size
        assert all(r.left() >= 0 and r.top() >= 0 and r.right() <= W and r.bottom() <= H for r in rects.values())
        items = list(rects.values())
        assert not any(a.intersects(b) for i, a in enumerate(items) for b in items[i + 1:])
        for p in n.model.bounds_points():
            q = n.view.project(p)
            assert 0 <= q.x() <= W and 0 <= q.y() <= H
        n.close()


def test_topology_has_controller_targets_passive_analyzer_gnd_and_usb(qt_app):
    from gui.network_3d import NodeModel, RAIL_Y
    import config
    m = NodeModel(); m.update([(a, n, 'Configured', 'x') for a, n in config.CONFIGURED_DEVICES.items()])
    names = [s[0] for s in m.segments()]
    assert {'rail:SDA', 'rail:SCL', 'rail:GND', 'tap:SDA', 'tap:SCL', 'tap:GND', 'usb:analyzer'} <= set(names)
    taps = [s for s in m.segments() if s[0].startswith('tap')]
    assert all(dashed for *_, dashed in taps)                       # passive observation, drawn dashed
    assert all(f'drop:{a}:{l}' in names for a in config.CONFIGURED_DEVICES for l in RAIL_Y)
    assert 'analyzer' not in m.slaves                                 # analyzer is not an addressed target


def test_packet_selection_drives_both_views_and_waveform(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network, 'HAVE_GL', False), patch('gui.main_window.list_ports', return_value=[]):
        w = MainWindow(); e = w.engine; e.set_source('demo')
        e.ingest_lines(lines_for(synth(write_read(0x68, 0x00, [0x05, 0x22]))))
        w.packet_model.sync(); w.select_transaction(e.packets[0])
        for pos, text, who in ((4, 'DATA 0x00', 'Pico 1 (controller) →'), (10, 'DATA 0x05', '→ Pico 1 (controller)'),
                               (13, 'NACK', 'end-of-read')):
            w.playback.position = pos; w._sync_playback()
            a, b = w.p_dash.net.transaction_overlay.text(), w.p_net.net.transaction_overlay.text()
            assert a == b and text in a and who in a and 'SIMULATED' in a
            assert w.p_dash.net.stage_text == w.p_net.net.stage_text and w.p_dash.net.stage_text.startswith(f"0x68 · {text}")
            assert "Packet #" in a and "0x68 (7-bit)" in a and "target DS3231" in a
        assert w.p_wave.txn is w.playback.txn
        assert 'Simulation time' in w.p_dash.net.capture_clock.text()
        assert 'SIMULATED' in w.p_net.net.rtc_label.text() or 'UNAVAILABLE' in w.p_net.net.rtc_label.text()
        assert 'SIMULATED policy' in w.p_net.net.health_label.text()
        w.close()
