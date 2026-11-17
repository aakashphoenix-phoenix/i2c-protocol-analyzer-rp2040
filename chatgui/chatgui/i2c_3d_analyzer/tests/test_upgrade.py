"""Deterministic software vectors, never physical hardware certification."""
import csv
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from analysis.capture_engine import CaptureEngine
from analysis.communication_health import acknowledgment_counts
from analysis.i2c_decoder import I2CDecoder
from analysis.playback import Playback, stages
from tests.helpers import synth, write, write_read, lines_for, seq_of
from tests.test_hardware_pipeline import qt_app
from tests.test_native_firmware import HELLO


def capture(build=write_read(0x76, 0xF7, [0x65, 0x4A]), source='demo'):
    e = CaptureEngine(); e.set_source(source)
    e.ingest_lines(lines_for(synth(build)))
    return e


def test_final_read_nack_raw_and_expected():
    e = capture(); t = e.packets[0]; c = acknowledgment_counts(t)
    assert c['raw_nacks'] == 1 and c['expected_read_nacks'] == 1 and c['unexpected_nacks'] == 0
    assert e.perf.compute().nacks == 1
    assert e.communication_health()[0] == 'UNKNOWN'
    assert e.communication.snapshot(e.hw_now)['status'] == 'HEALTHY'


def test_address_nack_not_assumed_scan():
    e = capture(write(0x25, [], nack_addr=True))
    assert acknowledgment_counts(e.packets[0])['unexpected_nacks'] == 1


def test_rolling_policy_expires_failures():
    e = capture(write(0x25, [], nack_addr=True))
    assert e.communication.snapshot(e.hw_now)['status'] == 'DEGRADED'
    assert e.communication.snapshot(e.hw_now + 61e9)['status'] == 'UNKNOWN'


def test_truncated_read_nack_never_expected():
    e = capture(); t = e.packets[0]; t.status = 'incomplete'
    assert acknowledgment_counts(t)['expected_read_nacks'] == 0


def test_invalid_time_and_ambiguous_edge_preserve_input():
    for tail in [[(10, 0)], [(40, 3)]]:
        raw = [(0,3),(10,2),(20,0)] + tail
        original = list(raw)
        d = I2CDecoder(); tx = d.feed(raw)
        assert raw == original and tx[0].status == 'gap'


def test_bit_times_match_exact_source_levels():
    e = capture()
    lookup = dict(e.edges)
    for b in e.packets[0].bytes:
        assert len(b.bit_times) == 8
        value = 0
        for t in b.bit_times:
            assert lookup[t] & 2
            value = value*2 + (lookup[t] & 1)
        assert value == b.value
        assert b.ack == (lookup[b.t_ack] & 1 == 0)
        assert b.t_end > b.t_ack


def test_playback_read_address_and_ack_roles():
    t = capture().packets[0]
    ss = stages(t)
    assert [s.direction for s in ss if s.text.startswith('ADDR')] == ['W','W']
    assert [s.direction for s in ss if s.text.startswith('DATA')] == ['W','R','R']
    assert ss[-2].text == 'NACK' and ss[-2].direction == 'W'
    assert ss[-1].text == 'STOP'


@pytest.mark.parametrize('speed', [0.25,0.5,1,2])
def test_playback_speed_preserves_capture(speed):
    e = capture(); t = e.packets[0]; original = repr(t), tuple(e.edges), tuple(e.raw_lines)
    p = Playback(); p.select(t); p.speed = speed; p.advance(0.2)
    ref = Playback(); ref.select(t); ref.advance(0.2 * speed)            # speed scales the TIME base, never the data
    assert p.position == pytest.approx(ref.position) and p.position > 0
    assert original == (repr(t), tuple(e.edges), tuple(e.raw_lines))


def test_csv_has_absolute_times_and_classification(tmp_path):
    from storage.capture_storage import export_packets_csv
    e = capture(); path = tmp_path/'result.csv'; export_packets_csv(path,e)
    row = next(csv.DictReader(path.open(encoding='utf8')))
    assert row['source'] == 'DEMO' and float(row['start_ns']) == e.packets[0].t_start
    assert row['raw_nacks'] == '1' and row['expected_read_end_nacks'] == '1' and row['unexpected_nacks'] == '0'


def test_waveform_has_no_segment_bars_and_nack_red(qt_app):
    import pyqtgraph as pg
    from gui.waveform_viewer import WaveformPage
    from config import COLORS
    e = capture(); page = WaveformPage(SimpleNamespace(engine=e)); page.track.setChecked(False);       # classic text-label mode
    page.resize(2800, 800); page.show(); qt_app.processEvents()       # a real window: labels are size-aware
    page.refresh(e); page.show_packet(e.packets[0])
    assert not any(isinstance(x,pg.PlotCurveItem) for x in page.ann_items)
    labels = [x for x in page.ann_items if isinstance(x,pg.TextItem)]
    assert any(x.toPlainText() == 'ADDR 0x76 (7-bit)' for x in labels)
    nack = next(x for x in labels if x.toPlainText() == 'NACK (end of read)')   # still a red NACK
    assert nack.color.name().lower() == COLORS['red'].lower()
    page.ca.setValue(0);page.cb.setValue(10);page._cursor_text()
    assert '100.000 kHz' in page.cursor_lbl.text()
    page.close()


def test_both_views_same_transaction_and_pause_keeps_capture(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network,'HAVE_GL',False), patch('gui.main_window.list_ports',return_value=[]):
        w = MainWindow(); e=w.engine; e.set_source('demo')
        e.ingest_lines(lines_for(synth(write_read(0x76,0xF7,[0x65]))))
        w.packet_model.sync(); w.select_transaction(e.packets[0])
        w.playback.position=7; w._sync_playback()
        assert w.p_dash.net.transaction_overlay.text() == w.p_net.net.transaction_overlay.text()
        assert w.p_wave.txn is w.playback.txn
        w._pause_display(True); before=w.playback.position
        w._refresh(); assert w.playback.position == before
        assert 'SIMULATED' in w.p_dash.net.transaction_overlay.text()
        w.close()


def test_native_window_annotations_decode_without_duplicate_packets(qt_app):
    e = CaptureEngine(); e.set_source('live')
    hello=dict(HELLO);hello['capabilities']=hello['capabilities']+['edges'];hello['waveform']=True
    e.ingest_lines([json.dumps(hello)])
    raw=synth(write(0x76,[0x65]));prev=raw[0][0];entries=[]
    for t,state in raw:
        entries.append([t-prev,state]);prev=t
    e.ingest_lines([json.dumps(dict(t='edges',snapshot=True,t0=raw[0][0],e=entries))])
    assert len(e.packets)==0 and len(e.waveform_transactions)==1
    assert e.waveform_transactions[0].frames[0].data==[0x65]
    from gui.waveform_viewer import WaveformPage
    page=WaveformPage(SimpleNamespace(engine=e));page.track.setChecked(False);   # classic text-label mode
    page.refresh(e)
    assert any(getattr(x,'toPlainText',lambda:'' )()=='DATA 0x65' for x in page.ann_items)
    page.close()


def test_rendered_step_transitions_are_at_recorded_times(qt_app):
    import numpy as np
    import pyqtgraph as pg
    from gui.waveform_viewer import WaveformPage
    e=capture(); page=WaveformPage(SimpleNamespace(engine=e));page.show_packet(e.packets[0]);page.refresh(e)
    assert page.txn is e.packets[0]  # selection survives first page refresh
    x,y=page.c_sda.getData()
    xx,yy=pg.PlotCurveItem._generateStepModeData(page.c_sda.opts['stepMode'], x,y,None)
    transitions=xx[1:][np.diff(yy)!=0]
    wanted=[(t-page.origin)/1e3 for (previous,old),(t,state) in zip(e.edges,list(e.edges)[1:]) if old&1 != state&1 and page.origin <= t <= page._window_end]
    assert np.allclose(transitions,wanted)
    page.close()


def test_long_low_flagged_without_asserting_stretch_cause():
    def build(s):
        s.start();s.byte(0x76<<1);s.wait(3000);s.byte(0x65);s.stop()
    e=capture(build); t=e.packets[0]
    assert t.timing['long_low_ns'] and max(t.timing['long_low_ns']) > 300000
    assert 'UNDETERMINED' in t.timing['stretch_cause']
    assert 99000 < t.timing['frequency_hz'] < 101000


def test_live_records_drive_both_views_without_demo(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    import time
    with patch.object(network,'HAVE_GL',False), patch('gui.main_window.list_ports',return_value=[]):
        w=MainWindow();w.engine.set_source('live')
        w.engine.stream_connected=True;w.engine.capture_active=True
        w.engine.ingest_lines(lines_for(synth(write_read(0x76,0xF7,[0x65]))))
        w.select_transaction(w.engine.packets[0]);w._sync_playback()
        assert w.p_net.net.transaction_overlay.text()==w.p_dash.net.transaction_overlay.text()
        assert 'LIVE — HARDWARE CAPTURE' in w.p_net.net.transaction_overlay.text()
        w.engine.stream_connected=False;w._sync_playback()
        assert w.engine.source=='live' and w.demo is None
        assert 'disconnected/unverified' in w.p_net.net.transaction_overlay.text()
        w.close()


def test_incomplete_playback_has_no_invented_stop():
    def build(s):
        s.start();s.byte(0x76<<1);s.bit(1);s.bit(0)
    e=capture(build)
    done=e.decoder.check_timeout((e.hw_now or 0)+30e6)
    assert done and done[0].status=='incomplete'
    assert not any(x.text=='STOP' for x in stages(done[0]))


def test_missing_record_is_not_successful_transaction():
    raw=lines_for(synth(write(0x76,[0x65,0x44])),chunk=10)
    del raw[3]
    e=CaptureEngine();e.ingest_lines(raw)
    assert e.lost_records==1 and all(t.status!='ok' for t in e.packets)


def test_incomplete_packets_do_not_inflate_completed_transaction_rate():
    e=capture();t=e.packets[0];t.status='gap'
    from analysis.performance_calculator import PerformanceCalculator
    p=PerformanceCalculator();p.advance(t.t_start);p.add_transaction(t)
    assert not p.roll_txn and p.bucket.txns==0 and p.snap.complete==0
