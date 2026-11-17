"""Software-only serial-to-Qt and native measured-waveform regressions.

No test here connects to a physical COM port or Pico.
"""
import json
import os
import time
from collections import deque
from unittest.mock import patch

import pytest

from analysis.capture_engine import CaptureEngine
from communication.protocol_parser import LineAssembler, MAX_LINE, ProtocolError, parse_line
from tests.test_native_firmware import HELLO, seg


def encoded(obj):
    return json.dumps(obj).encode()


def waveform(t0=200000, states=None):
    return dict(t='edges', snapshot=True, t0=t0,
                e=states or [[0, 3], [1, 2], [10, 0], [10, 2], [10, 3], [2000, 0], [10, 2], [10, 3], [1949, 3]])


@pytest.mark.parametrize('line', [
    {'t': []}, {'t': {}}, {'t':'evt','kind':[]}, {'type': 'hello', 'protocol': 1, 'sample_resolution_us': 'bad'},
    {'t':'hello', 'proto':1, 'sample_hz': 'bad'},
    {'t':'hello', 'proto':1, 'tick_ns': float('nan')},
    {'t':'hello', 'proto':1, 'tick_ns': -1},
    {'t':'hello', 'proto':1, 'tick_ns': []},
    {'t':'edges', 't0':0, 'e':[], 'snapshot':'yes'},
])
def test_malformed_serial_never_escapes_protocol_error(line):
    with pytest.raises(ProtocolError):
        parse_line(encoded(line))


def test_fragmented_snapshot_and_overlong_frame_resync():
    asm = LineAssembler()
    raw = encoded(waveform()) + b'\r\n'
    lines = []
    for i in range(0, len(raw), 7):
        lines.extend(asm.feed(raw[i:i+7]))
    assert parse_line(lines[0]).data['e'] == waveform()['e']
    assert asm.feed(b'x' * (MAX_LINE + 1)) == []
    assert asm.feed(b'{"t":"log","msg":"invalid tail"}\n' + raw) == [raw[:-1]]
    assert asm.overlong == 1
    assert asm.feed(b'x' * (MAX_LINE + 1) + b'\n' + raw) == [raw[:-1]]
    assert asm.overlong == 2


def test_native_snapshots_do_not_duplicate_transactions_or_bridge_windows():
    e = CaptureEngine()
    e.set_source('live')
    e.ingest_lines([encoded(dict(HELLO, capabilities=HELLO['capabilities'] + ['edges']))])
    e.ingest_lines([encoded(seg(1,100,400,'START','STOP',0x68,'write',[0],[True]))])
    before = e.perf.compute().total
    t = e.hw_now
    e.ingest_lines([encoded(waveform())])
    assert e.perf.compute().total == before == 1
    assert e.hw_now == t  # snapshots do not advance or double-count metrics
    assert e.edges[0] == (100000000.0, 3)
    e.ingest_lines([encoded(waveform(700000))])
    assert e.edges[0][0] == 350000000
    assert not e.edges_available(100000000, 352000000)
    assert e.edges_available(350000000, 352000000)
    assert e.waveform_snapshots == 2 and e.lost_records == 0


@pytest.fixture(scope='module')
def qt_app(tmp_path_factory):
    # Use actual Qt widgets, plots and event delivery, with the 2D network
    # fallback because the offscreen platform has no OpenGL context.
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PyQt6.QtWidgets')
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    settings = tmp_path_factory.mktemp('settings')
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings))
    QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, 'I2CAnalyzer', 'I2C3DAnalyzer').setValue(
        'captures_dir', str(settings/'captures'))        # the plain (org, app) form would write the real registry
    app = QApplication.instance() or QApplication([])
    yield app


def pump(app, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate(): return
        time.sleep(.005)
    assert predicate(), 'Qt event condition timed out'


class FakeSerial:
    """Fragmenting USB CDC double; all I/O runs on the actual SerialWorker."""
    def __init__(self, *args, **kwargs):
        self.pending = deque()
        self.writes = []
        self.closed = False
        self.unplugged = False
        self.dtr = False
        self.kwargs = kwargs

    def enqueue(self, records):
        raw = b''.join(encoded(r) + b'\n' for r in records)
        self.pending.extend(raw[i:i+13] for i in range(0,len(raw),13))

    @property
    def in_waiting(self):
        if self.unplugged: raise OSError('synthetic USB disconnect')
        return len(self.pending[0]) if self.pending else 0

    def read(self, count):
        if self.unplugged: raise OSError('synthetic USB disconnect')
        if self.pending: return self.pending.popleft()
        time.sleep(.005)
        return b''

    def write(self, payload):
        self.writes.append(payload)
        if payload == b'HELLO\n':
            self.enqueue([dict(HELLO, capabilities=HELLO['capabilities']+['edges'], waveform=True)])
        if payload == b'START\n':
            records = [dict(type='status', capturing=True, dma_lost_buffers=0, dropped_records=0)]
            for i in range(16):
                records.append(seg(i+1,100000+i*250000,100400+i*250000,
                                   'START','STOP',0x68,'write',[0],[True]))
            records.append(waveform())
            self.enqueue(records)
        return len(payload)

    def close(self): self.closed = True


def test_serial_worker_to_gui_graph_disconnect_reconnect_and_demo(qt_app, tmp_path):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    from PyQt6.QtWidgets import QMessageBox
    links = []
    def factory(*args, **kwargs):
        link = FakeSerial(*args, **kwargs)
        links.append(link)
        return link

    with patch.object(network, 'HAVE_GL', False), \
         patch('gui.main_window.list_ports', return_value=[('COM_TEST', 'Synthetic analyzer', True)]), \
         patch('communication.serial_manager.serial.Serial', side_effect=factory), \
         patch.object(QMessageBox, 'warning'):
        w = MainWindow()
        try:
            w.connect_analyzer()
            pump(qt_app, lambda: w.caps is not None)
            assert w.worker.native_commands and w.worker.handshake_received
            assert w.btn_start.isEnabled()
            w.start_capture()
            pump(qt_app, lambda: len(w.engine.packets) == 16 and len(w.engine.edges) > 0)
            assert links[0].dtr and links[0].kwargs['write_timeout'] == .25
            assert b'START\n' in links[0].writes
            w.nav.setCurrentRow(0)
            w._refresh()
            x, y = w.p_dash.p_freq.listDataItems()[-1].getData()
            assert len(x) >= 2 and all(value == 100 for value in y)
            w.nav.setCurrentRow(3)
            w.p_wave._redraw(force=True)
            x, y = w.p_wave.c_scl.getData()
            assert len(x) > 2 and len(set(y)) == 2
            assert x[-1] == 1000  # selected default window is 1 ms
            assert 'snapshot' in w.p_wave.title.text()
            w.p_wave.plot.grab().save(str(tmp_path/'synthetic-waveform.png'))
            # The current master prints RTC and BMP280 on ONE line.
            w._on_master_lines([b'RTC 12:34:56 07/11/2026  BMP280 25.50 C 1005.20 hPa'])
            assert w.engine.master_telemetry['temperature_c'] == 25.5
            worker = w.worker
            links[0].unplugged = True
            pump(qt_app, lambda: w.worker is None)
            assert worker.wait(1000) and links[0].closed
            assert not w.capturing
            w.connect_analyzer()
            pump(qt_app, lambda: w.caps is not None)
            w.start_capture()
            pump(qt_app, lambda: len(w.engine.packets) == 16)
            assert len(links) == 2
            worker = w.worker
            w.disconnect_analyzer()
            pump(qt_app, lambda: w.worker is None)
            assert worker.wait(1000) and b'STOP\n' in links[1].writes
            # Existing demo producer and existing edge decoder are unchanged.
            w.btn_demo.setChecked(True)
            pump(qt_app, lambda: len(w.engine.packets) > 3)
            assert w.engine.source == 'demo' and len(w.engine.edges) > 10
            w.p_wave._redraw(force=True)
            assert len(w.p_wave.c_scl.getData()[0]) > 2
            w.btn_demo.setChecked(False)
        finally:
            w.close()
            qt_app.processEvents()


def test_serial_open_error_and_handshake_retry(qt_app):
    from communication.serial_manager import SerialWorker
    errors = []
    with patch('communication.serial_manager.serial.Serial', side_effect=OSError('synthetic port busy')):
        worker = SerialWorker('COM_BUSY')
        worker.failed.connect(errors.append)
        worker.start()
        pump(qt_app, lambda: bool(errors))
        assert worker.wait(1000)
        assert 'Cannot open COM_BUSY' in errors[0] and 'port busy' in errors[0]
    link = FakeSerial()
    link.write = lambda data: link.writes.append(data) or len(data)  # no handshake
    with patch('communication.serial_manager.serial.Serial', return_value=link):
        worker = SerialWorker('COM_DELAYED')
        worker.start()
        try:
            pump(qt_app, lambda: b'{"cmd": "hello"}\n' in link.writes)
        finally:
            worker.stop()
            assert worker.wait(1000)
        assert link.closed
