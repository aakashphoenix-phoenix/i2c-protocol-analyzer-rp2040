"""Data provenance regressions: synthetic inputs are confined to these tests."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from analysis.capture_engine import CaptureEngine
from communication.protocol_parser import capabilities_from_hello, parse_line
from tests.helpers import lines_for, synth, write_read
from tests.test_hardware_pipeline import qt_app
from tests.test_native_firmware import HELLO


def demo_engine():
    engine = CaptureEngine()
    engine.set_source('demo')
    engine.ingest_lines(lines_for(synth(write_read(0x76,0xF7,[1,2,3]))))
    return engine


def test_no_edges_clears_demo_annotations_and_metadata(qt_app):
    from gui.waveform_viewer import WaveformPage
    engine = demo_engine()
    page = WaveformPage(SimpleNamespace(engine=engine))
    page.win.setCurrentText('5 ms')
    page.refresh(engine)
    assert page.ann_items and 'SIMULATED' in page.info.text()
    engine.clear(); engine.set_source('live')
    engine.set_caps(capabilities_from_hello(parse_line(json.dumps(HELLO))))
    page.refresh(engine)
    assert not page.ann_items
    assert not page.c_scl.getData()[0].size if page.c_scl.getData()[0] is not None else True
    assert 'SIMULATED' not in page.info.text() and 'raw capture data required' in page.info.text()
    assert page.cursor_lbl.text() == ''
    page.close()


def test_frozen_packet_view_cannot_survive_new_capture(qt_app):
    from gui.waveform_viewer import WaveformPage
    engine = demo_engine()
    page = WaveformPage(SimpleNamespace(engine=engine))
    page.refresh(engine)
    page.show_packet(engine.packets[0])
    assert page.txn is not None and not page.follow.isChecked()
    engine.clear(); engine.set_source('live')
    engine.set_caps(capabilities_from_hello(parse_line(json.dumps(HELLO))))
    page.refresh(engine)
    assert page.txn is None and not page.ann_items
    assert 'SIMULATED' not in page.info.text()
    page.close()


def test_absent_samples_are_not_reported_as_measured_idle(qt_app):
    from gui.waveform_viewer import WaveformPage
    engine = CaptureEngine(); engine.set_source('live')
    engine.hw_now = 1000000
    page = WaveformPage(SimpleNamespace(engine=engine))
    page.refresh(engine)
    assert 'bus state is unknown' in page.info.text()
    # A genuinely supplied constant sampled state is allowed to remain flat.
    engine.edges.extend([(0,3),(1000000,3)])
    page._redraw(force=True)
    x,y = page.c_scl.getData()
    assert len(x)>1 and len(set(y))==1
    page.close()


def test_demo_header_uses_demo_capabilities(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network,'HAVE_GL',False), patch('gui.main_window.list_ports',return_value=[]):
        window = MainWindow()
        window.caps = capabilities_from_hello(parse_line(json.dumps(HELLO)))
        window.engine.set_source('demo')
        from sources.demo_source import DemoGenerator
        window.engine.ingest_lines([DemoGenerator(seed=1).hello()])
        window._set_state()
        assert '10 MS/s' in window.hwinfo.text()
        assert 'edges' in window.side_mode.text() and 'segment' not in window.side_mode.text()
        window.close()


def test_close_sends_stop_before_worker_shutdown(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    calls=[]
    with patch.object(network,'HAVE_GL',False), patch('gui.main_window.list_ports',return_value=[]):
        window=MainWindow()
        window.worker=SimpleNamespace(send=lambda command:calls.append(command),
                                      stop=lambda:calls.append('worker-stop'),
                                      wait=lambda timeout:True)
        window.capturing=True
        window.close()
        assert calls==['stop','worker-stop']
