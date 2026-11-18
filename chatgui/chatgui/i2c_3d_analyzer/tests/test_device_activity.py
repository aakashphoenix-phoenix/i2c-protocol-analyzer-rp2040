"""Device Communication Activity: deterministic ranking vectors (software only)."""
import json
import time
from unittest.mock import patch

from analysis.capture_engine import CaptureEngine
from analysis.device_activity import ranking_text
from analysis.i2c_decoder import Frame, Transaction
from sources.demo_source import DemoGenerator
from tests.helpers import lines_for, synth, write_read
from tests.test_hardware_pipeline import qt_app
from tests.test_native_firmware import HELLO, seg

S = 1e9


def live():
    e = CaptureEngine(); e.set_source('live')
    e.stream_connected = True; e.capture_active = True; e.last_record_wall = time.monotonic()
    e.capture_start_ns = 0.0
    return e


_ids = iter(range(1, 10_000))


def combined(e, addr, t, reg=0x00, n=2, status='ok'):
    i = next(_ids)
    fr = [Frame('S', t, addr=addr, rw='W', addr_ack=True, data=[reg], acks=[True]),
          Frame('Sr', t + 1e5, addr=addr, rw='R', addr_ack=True, data=list(range(n)), acks=[True] * (n - 1) + [False])]
    ev = [(t, 'S'), (t + 1e5, 'Sr')] + ([(t + 5e5, 'P')] if status == 'ok' else [])
    e._store(Transaction(id=i, t_start=t, t_end=t + 5e5, frames=fr, events=ev, status=status))


def write(e, addr, t, data=(1, 2, 3), ack=True):
    i = next(_ids)
    fr = [Frame('S', t, addr=addr, rw='W', addr_ack=ack, data=list(data) if ack else [], acks=[True] * len(data) if ack else [])]
    e._store(Transaction(id=i, t_start=t, t_end=t + 3e5, frames=fr, events=[(t, 'S'), (t + 3e5, 'P')], status='ok'))


def scenario():
    """0x76 high READ, 0x3C write-only (verified zero READ), 0x68 low, 0x57 verified idle, 0x50 never seen."""
    e = live()
    e.registry.configured[0x50] = 'AT24C32 EEPROM (configured, never seen)'
    write(e, 0x57, 1 * S)                       # ACKed long before the window -> verified connected
    for k in range(5):
        combined(e, 0x76, 20 * S + k * 0.1 * S)
        write(e, 0x3C, 21 * S + k * 0.1 * S)
    for k in range(2):
        combined(e, 0x68, 22 * S + k * 0.1 * S)
    e.hw_now = 25 * S
    return e


def test_counts_frames_bytes_transactions_and_rates():
    a = scenario().device_activity()
    r = a['rows']
    assert a['reliable'] and not a['simulated']
    assert (r[0x76]['reads'], r[0x76]['writes'], r[0x76]['transactions']) == (5, 5, 5)   # W->R = 1 W + 1 R, 1 txn
    assert (r[0x76]['read_bytes'], r[0x76]['write_bytes']) == (10, 5)
    assert (r[0x3C]['reads'], r[0x3C]['writes'], r[0x3C]['write_bytes']) == (0, 5, 15)
    assert (r[0x68]['reads'], r[0x68]['writes']) == (2, 2)
    assert r[0x76]['read_rate'] == 0.5 and r[0x3C]['write_rate'] == 0.5          # per second, 10 s window
    assert r[0x57]['status'] == 'NO OBSERVED ACTIVITY'                             # verified, reliable, zero
    assert r[0x50]['status'] == 'UNKNOWN'                                          # never verified: not "zero"
    assert r[0x76]['latest_read'][0] == 20.4e9 + 1e5 and r[0x3C]['latest_write'][0] == 21.4e9


def test_rankings_with_ties_and_verified_zero():
    a = scenario().device_activity()
    rd, wr = a['read'], a['write']
    assert rd['most'] == [0x76] and rd['most_value'] == 5
    assert rd['least'] == [0x3C, 0x57] and rd['least_value'] == 0 and rd['least_tie']   # tie, ascending address
    assert wr['most'] == [0x3C, 0x76] and wr['most_tie'] and wr['most_value'] == 5
    assert wr['least'] == [0x57] and wr['least_value'] == 0
    assert 0x50 in rd['excluded'] and 0x50 in wr['excluded']
    text = ranking_text(a, 'reads', 'READ')
    assert 'tie' in text and '0x50' in text


def test_incomplete_capture_makes_device_partial_and_unranked():
    e = scenario()
    combined(e, 0x68, 23 * S, status='gap')
    e.hw_now = 25 * S
    a = e.device_activity()
    assert a['rows'][0x68]['status'] == 'PARTIAL' and 0x68 in a['read']['excluded']
    assert a['read']['most'] == [0x76]


def test_unreliable_or_short_window_is_unknown_not_zero():
    e = scenario(); e.stream_connected = False
    a = e.device_activity()
    assert not a['reliable'] and all(r['status'] == 'UNKNOWN' for r in a['rows'].values())
    assert a['read']['most'] is None and 'UNKNOWN' in ranking_text(a, 'reads', 'READ')
    e = scenario(); e.capture_start_ns = 20 * S              # only 5 s observed of a 10 s window
    a = e.device_activity()                                  # exact counts over the 5 s actually observed
    assert a['reliable'] and a['window_s'] == 5 and 'observed window 5 s' in a['window_note']
    assert a['rows'][0x76]['status'].startswith('ACTIVE') and a['rows'][0x76]['reads'] == 5
    e = scenario(); e.edge_gaps.append(24 * S)                # loss inside the window
    assert e.device_activity()['rows'][0x57]['status'] == 'UNKNOWN'


def test_demo_activity_is_simulated_and_matches_firmware_schedule():
    g = DemoGenerator(seed=5); e = CaptureEngine(); e.set_source('demo'); e.ingest_lines([g.hello()])
    for k in range(1, 241):
        e.ingest_lines(g.lines_until(k * 0.05))
    a = e.device_activity()
    r = a['rows']
    assert a['simulated'] and a['reliable']
    assert r[0x68]['reads'] == r[0x68]['writes'] == r[0x68]['transactions'] == 10          # 1 Hz combined read
    assert r[0x68]['read_bytes'] == 160                                                    # 16 registers each
    assert 49 <= r[0x76]['reads'] <= 52 and r[0x76]['reads'] == r[0x76]['writes']          # 5 Hz + identity
    assert r[0x3C]['reads'] == 0 and r[0x3C]['writes'] == 700                             # 70 writes per refresh
    assert a['read']['most'] == [0x76] and a['read']['least'] == [0x3C] and a['write']['most'] == [0x3C]


def test_replayed_native_records_count_frames_not_double_transactions():
    e = live(); e.capture_start_ns = None
    e.ingest_lines([json.dumps(HELLO).encode()])
    lines, seq, t = [], 1, 100
    for k in range(25):
        lines.append(seg(seq, t, t + 150, 'START', 'RESTART', 0x68, 'write', [0], [True])); seq += 1
        lines.append(seg(seq, t + 150, t + 600, 'RESTART', 'STOP', 0x68, 'read', [1, 2], [True, False])); seq += 1
        t += 500_000                                      # every 0.5 s of capture time
    e.ingest_lines([json.dumps(x).encode() for x in lines])
    assert len(e.packets) == 25                           # 25 bus transactions, not 50
    a = e.device_activity()
    assert a['reliable']
    r = a['rows'][0x68]
    assert (r['reads'], r['writes'], r['transactions']) == (20, 20, 20)   # window (2.1 s, 12.1 s]
    assert r['status'] == 'ACTIVE' and a['rows'][0x76]['status'] == 'UNKNOWN'   # 0x76 never seen on this bus


def test_pause_keeps_counting_and_focus_syncs_views(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network, 'HAVE_GL', False), patch('gui.main_window.list_ports', return_value=[]):
        w = MainWindow(); e = w.engine; e.set_source('demo')
        first = lines_for(synth(write_read(0x68, 0x00, [1, 2])))
        e.ingest_lines(first)
        w._pause_display(True)
        e.ingest_lines(lines_for(synth(write_read(0x76, 0xF7, [3]), t_start=5_000_000), seq0=len(first) - 1)[1:])
        w._refresh()
        assert len(e.packets) == 2 and e.frame_log.latest[0x76]['R']           # acquisition continued while paused
        starts = [t.t_start for t in e.packets]
        w.packet_model.sync()
        w.focus_device(0x68)
        assert w.p_dash.net.focus == w.p_net.net.focus == w.p_dev.net.focus == 0x68
        assert w.p_wave.device_filter == 0x68 and w.p_dash.proxy.rowCount() == 1
        w.p_wave._step(1)
        assert w.p_wave.txn.address == 0x68
        assert [t.t_start for t in e.packets] == starts                        # timestamps untouched
        w.focus_device(None)
        assert w.p_net.net.focus is None and w.p_dash.proxy.rowCount() == 2
        w.p_dev.refresh(e); w.p_dash.refresh(e)
        assert 'SIMULATED' in w.p_dash.activity_text.text() and w.p_dev.act.rowCount() >= 3
        w.close()
