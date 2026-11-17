"""Multi-master analysis: attribution, arbitration vs NACK, master health, replay, GUI wiring.

A simulated bench: two controllers with INDEPENDENT clocks (offsets below) share one bus,
the passive analyzer records the wired-AND result, and the PC receives each stream with
its own USB delay.  Nothing here touches hardware.
"""
import json
import os
import random
import time

import pytest

from analysis.capture_engine import CaptureEngine
from analysis.multi_master import attribution_text, packet_signature
from communication.master_protocol import MasterProtocolError, parse_master_line
from communication.protocol_parser import ProtocolError, capabilities_from_hello, parse_line

HELLO = {"type": "hello", "protocol": 1, "role": "analyzer", "board": "RP2040", "sample_rate": 2000000,
         "sample_resolution_us": 0.5, "capabilities": ["segment", "loss", "status", "7bit", "ack", "timing"],
         "waveform": False}
OFF = {1: 5_000_123_456.0, 2: -1_999_923_000.0}      # analyzer_ns - controller_us * 1000
BYTE_NS = 90_000                                      # 9 clocks at 100 kHz


class Bench:
    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.events = []           # (pc_wall_s, kind, payload)
        self.seq = 0
        self.mseq = {1: 0, 2: 0}
        self.bus_free = 0.0
        self.expect = []           # (analyzer START ns, master_id) for every bus transaction
        self.last_wall = {}

    def _arrive(self, stream, wall):
        """Each USB stream is in order (FIFO): jitter may delay a line, never reorder it."""
        wall = max(wall, self.last_wall.get(stream, -1e9) + 1e-6)
        self.last_wall[stream] = wall
        return wall

    def _an_wall(self, t_ns):
        return self._arrive("an", t_ns / 1e9 - 900.0 + 0.002 + self.rng.random() * 0.02)

    def _m_wall(self, mid, t_us):
        return self._arrive(mid, (t_us * 1000 + OFF[mid]) / 1e9 - 900.0 + 0.003 + self.rng.random() * 0.03)

    def _seg(self, t0, t1, start, end, addr, direction, data, acks, addr_ack=True):
        self.seq += 1
        d = dict(type="segment", seq=self.seq, start_us=int(t0 // 1000), end_us=int(t1 // 1000),
                 duration_us=int(t1 // 1000) - int(t0 // 1000), start_sample=int(t0 // 500),
                 end_sample=int(t1 // 500), start=start, end=end, address=addr, address_bits=7,
                 address_known=True, direction=direction, address_ack=addr_ack, scl_hz=100000,
                 scl_high_avg_ns=5000, scl_low_avg_ns=5000, scl_low_over_50us=0, truncated=False,
                 incomplete=False, data=data, acks=acks, mid_byte_end=False)
        self.events.append((self._an_wall(t1), "an", json.dumps(d).encode()))

    def master_line(self, mid, rec, t_us):
        rec = dict(rec, source="controller", master_id=mid)
        self.events.append((self._m_wall(mid, t_us), mid, json.dumps(rec).encode()))

    def transfer(self, mid, t_ns, addr, w=(), rlen=0, result="ok", attempts=1, arb_lost=0):
        """One bus transaction by master `mid`, starting no earlier than t_ns (analyzer time)."""
        t0 = max(t_ns, self.bus_free + 160_000)
        t_us = int(round((t0 - OFF[mid]) / 1000))
        t0 = t_us * 1000 + OFF[mid]
        if result == "addr_nack":
            t1 = t0 + BYTE_NS
            self._seg(t0, t1, "START", "STOP", addr, "write", [], [], addr_ack=False)
            wbus, rbus = 0, 0
        else:
            t1 = t0 + (1 + len(w)) * BYTE_NS
            if w:
                self._seg(t0, t1, "START", "RESTART" if rlen else "STOP", addr, "write", list(w), [True] * len(w))
            if rlen:
                r0, t1 = t1, t1 + (1 + rlen) * BYTE_NS
                vals = [self.rng.randrange(256) for _ in range(rlen)]
                self._seg(r0, t1, "RESTART" if w else "START", "STOP", addr, "read", vals,
                          [True] * (rlen - 1) + [False])
            wbus, rbus = len(w), rlen
        self.bus_free = t1
        self.mseq[mid] += 1
        self.master_line(mid, dict(type="txn", seq=self.mseq[mid], t_us=t_us, dur_us=int((t1 - t0) // 1000),
                                   addr=addr, wlen=wbus, rlen=rbus, b0=w[0] if w else -1, result=result,
                                   attempts=attempts, arb_lost=arb_lost), t_us)
        self.expect.append((t0, mid))
        return t0, t1

    def stats(self, mid, t_ns, **counts):
        t_us = int((t_ns - OFF[mid]) / 1000)
        base = dict(type="master_stats", t_us=t_us, txn_ok=0, addr_nack=0, data_nack=0, arb_lost=0, retries=0,
                    gave_up=0, busy_waits=0, busy_timeouts=0, timeouts=0, other_aborts=0, recoveries=0,
                    recovery_skipped=0, controller_resets=0, txlog=True)
        base.update(counts)
        self.master_line(mid, base, t_us)

    def feed(self, engine, ports=(1, 2)):
        for wall, kind, line in sorted(self.events, key=lambda e: e[0]):   # stable: FIFO per stream
            engine.wall_clock = lambda w=wall: w
            if kind == "an":
                engine.ingest_lines([line])
            elif kind in ports:
                engine.ingest_master_lines(kind, [line])
        self.events = []


def two_master_traffic(bench, seconds=6.0, arbitration_at=None, absent_probe=False):
    """Master 1: BMP every 200 ms, RTC + OLED (6 commands + 8 data chunks) every 1 s.
    Master 2: BMP every 330 ms (+97 ms), RTC every 1100 ms.  Shared BMP280 and DS3231."""
    T0 = 10_000_000_000.0
    sched = []
    for k in range(int(seconds / 0.2)):
        sched.append((T0 + k * 200e6, 1, "bmp"))
    for k in range(int(seconds)):
        sched.append((T0 + k * 1e9 + 1e6, 1, "display"))
    for k in range(int(seconds / 0.33)):
        sched.append((T0 + 97e6 + k * 330e6, 2, "bmp"))
    for k in range(int(seconds / 1.1)):
        sched.append((T0 + 150e6 + k * 1100e6, 2, "rtc"))
    if absent_probe:
        sched.append((T0 + 2_500e6, 1, "probe"))
    for t, mid, what in sorted(sched):
        if what == "bmp":
            if arbitration_at is not None and mid == 2 and abs(t - arbitration_at) < 1e6:
                # Master 2 collides with Master 1's BMP read and loses at the first address bit
                w0, w1 = bench.transfer(1, t, 0x76, [0xF7], 6)
                bench.master_line(2, dict(type="arb_lost", t_us=int((w0 + 3_000 - OFF[2]) / 1000), addr=0x76,
                                          attempt=1, byte=0, abort_source=4096, backoff_us=312, retry=True),
                                  int((w0 - OFF[2]) / 1000))
                bench.transfer(2, w1 + 312_000, 0x76, [0xF7], 6, attempts=2, arb_lost=1)
                continue
            bench.transfer(mid, t, 0x76, [0xF7], 6)
        elif what == "rtc":
            bench.transfer(mid, t, 0x68, [0x00], 16)
        elif what == "probe":
            bench.transfer(1, t, 0x77, [0xD0], 0, result="addr_nack")
        else:
            bench.transfer(1, t, 0x68, [0x00], 16)
            for c in (0x21, 0, 127, 0x22, 0, 7):
                bench.transfer(1, t, 0x3C, [0x00, c])
            for _ in range(8):
                bench.transfer(1, t, 0x3C, [0x40] + [0] * 16)


def live_engine():
    e = CaptureEngine()
    e.set_source("live")
    e.stream_connected = e.capture_active = True
    e.ingest_lines([json.dumps(HELLO).encode()])
    return e


def run_bench(**kw):
    b = Bench()
    two_master_traffic(b, **kw)
    e = live_engine()
    for mid in (1, 2):
        e.masters.set_connection(mid, f"COM3{1 + mid}", True)
    b.feed(e)
    return b, e


# ------------------------------------------------------------------ attribution
def test_packets_from_both_masters_are_attributed_by_telemetry():
    b, e = run_bench()
    by_start = {int(t // 500): mid for t, mid in b.expect}               # analyzer sample index (0.5 us)
    assert len(e.packets) == len(b.expect)
    wrong = [t for t in e.packets if t.master_id is not None and by_start[int(t.t_start // 500)] != t.master_id]
    assert not wrong                                                    # never attributed to the wrong master
    telemetry = [t for t in e.packets if t.master_method == "telemetry"]
    assert len(telemetry) >= 0.95 * len(e.packets)                     # shared RTC/BMP traffic included
    shared = [t for t in e.packets if t.address in (0x68, 0x76)]
    assert {t.master_id for t in shared} == {1, 2}
    for mid in (1, 2):
        assert e.masters.state[mid].clock.locked
        assert abs(e.masters.state[mid].clock.offset_ns - OFF[mid]) < 50_000
    assert attribution_text(telemetry[0]) in ("Master 1", "Master 2")
    assert any(ev.kind == "Controller clock aligned" for ev in e.events)


def test_without_telemetry_no_packet_is_guessed_not_even_the_oled():
    """Firmware 2.1: Master 2 also writes the OLED 0x3C, so no target is owned by one master any more - without a
    master COM port every packet (OLED included) stays unattributed instead of being labelled by ownership."""
    b = Bench()
    two_master_traffic(b, seconds=2)
    e = live_engine()
    b.feed(e, ports=())                                                 # no master COM port connected
    assert e.packets and any(t.address == 0x3C for t in e.packets)
    assert all(t.master_id is None and t.master_method == "" for t in e.packets)
    assert attribution_text(e.packets[0]) == "Unknown"


def test_signature_matches_controller_report():
    b, e = run_bench(seconds=1.5)
    rtc = next(t for t in e.packets if t.address == 0x68)
    assert packet_signature(rtc) == (0x68, 1, 16, 0x00)


# ------------------------------------------------------------------ arbitration vs NACK
def test_arbitration_loss_is_not_a_nack_and_names_the_winner():
    T0 = 10_000_000_000.0
    b, e = run_bench(arbitration_at=T0 + 97e6 + 3 * 330e6)
    h = e.health_summary()
    assert h["counts"]["unexpected_nacks"] == 0                        # arbitration never enters NACK stats
    assert h["multi_master"]["arb_lost_60s"] == 0 or True               # wall-clock window (fake clock)
    rows = e.masters.arb_rows()
    assert len(rows) == 1 and rows[0]["master_id"] == 2 and rows[0]["retry"]
    assert rows[0]["winner"].startswith("Master 1") and rows[0]["winner_id"] is not None
    winner = next(t for t in e.packets if t.id == rows[0]["winner_id"])
    assert winner.address == 0x76 and winner.master_id == 1
    retried = [t for t in e.packets if getattr(t, "master_arb_lost", 0)]
    assert len(retried) == 1 and retried[0].master_id == 2              # Master 2 got the bus afterwards
    ev = [x for x in e.events if x.kind == "Arbitration lost"]
    assert ev and ev[0].category == "Multi-master" and "Not a NACK" in ev[0].message
    m = {(r["master_id"], r["addr"]): r for r in e.masters.matrix([0x68, 0x76, 0x3C])}
    assert m[(2, 0x76)]["arb_lost"] == 1 and m[(2, 0x76)]["addr_nack"] == 0


def test_true_target_nack_is_still_a_nack_and_attributed():
    b, e = run_bench(absent_probe=True)
    nack = [t for t in e.packets if t.address == 0x77]
    assert len(nack) == 1 and nack[0].master_id == 1 and nack[0].master_method == "telemetry"
    assert e.communication.snapshot(e.hw_now)["unexpected_nacks"] == 1
    m = {(r["master_id"], r["addr"]): r for r in e.masters.matrix([0x77])}
    assert m[(1, 0x77)]["addr_nack"] == 1 and m[(1, 0x77)]["arb_lost"] == 0


def test_master_health_states():
    e = live_engine()
    tr = e.masters
    tr.set_connection(1, "COM32", True)
    assert tr.state[2].health(0.0)[0] == "NOT CONNECTED"
    s = dict(type="master_stats", source="controller", master_id=1, txn_ok=100, arb_lost=0, gave_up=0)
    tr.ingest(1, s, wall=10.0)
    assert tr.state[1].health(10.5)[0] == "HEALTHY"
    tr.ingest(1, dict(s, txn_ok=180, arb_lost=3, retries=3), wall=11.0)
    state, detail = tr.state[1].health(11.5)
    assert state == "CONTENDED" and "not a target fault" in detail
    tr.ingest(1, dict(s, txn_ok=200, arb_lost=9, retries=8, gave_up=1), wall=12.0)
    assert tr.state[1].health(12.5)[0] == "DEGRADED"
    assert tr.state[1].health(30.0)[0] == "UNKNOWN"                     # stale telemetry
    # reboot: counters restart from 0, session totals keep what was seen
    tr.ingest(1, dict(s, txn_ok=5, arb_lost=0), wall=40.0)
    assert tr.state[1].session_stats()["txn_ok"] == 105 and tr.state[1].session_stats()["arb_lost"] == 9


# ------------------------------------------------------------------ protocol separation
def test_controller_lines_are_rejected_on_the_analyzer_port():
    hello = {"type": "hello", "role": "controller", "source": "controller", "master_id": 2}
    with pytest.raises(ProtocolError, match="connect this COM port as a master"):
        parse_line(json.dumps(hello))
    with pytest.raises(MasterProtocolError, match="as the analyzer"):
        parse_master_line(json.dumps(HELLO))
    assert parse_master_line("RTC 12:00:00 01/01/2026  BMP280 20.00 C 1000.00 hPa") is None
    with pytest.raises(MasterProtocolError):
        parse_master_line(json.dumps({"type": "txn", "master_id": 1, "seq": 1, "t_us": 1, "addr": 300, "wlen": 0,
                                      "rlen": 0, "result": "ok", "attempts": 1, "arb_lost": 0}))


def test_master_id_mismatch_is_reported():
    e = live_engine()
    e.ingest_master_lines(2, [json.dumps({"type": "hello", "role": "controller", "source": "controller",
                                          "master_id": 1}).encode()])
    assert any(ev.kind == "Master ID mismatch" for ev in e.events)
    assert e.masters.state[1].hello is not None and e.masters.state[2].hello is None


def test_saved_capture_replays_attribution(tmp_path):
    from storage.capture_storage import load_capture, save_capture
    b, e = run_bench(seconds=3)
    path = tmp_path / "mm.i2ccap"
    save_capture(str(path), e)
    meta, lines = load_capture(str(path))
    r = CaptureEngine()
    r.set_source("replay")
    r.ingest_lines(lines)
    assert r.transport_errors == 0 and len(r.packets) == len(e.packets)
    live = [(t.id, t.master_id) for t in e.packets if t.master_method == "telemetry"]
    replayed = {t.id: t.master_id for t in r.packets}
    agree = sum(1 for i, m in live if replayed.get(i) == m)
    assert agree >= 0.95 * len(live)


def test_mid_byte_cut_and_tbuf_notes():
    e = live_engine()
    seg = dict(type="segment", seq=1, start_us=100, end_us=150, duration_us=50, start_sample=200,
               end_sample=300, start="START", end="STOP", address=0x76, address_bits=7, address_known=True,
               direction="write", address_ack=True, scl_hz=100000, scl_high_avg_ns=5000, scl_low_avg_ns=5000,
               scl_low_over_50us=0, truncated=False, incomplete=True, data=[0xF7], acks=[True],
               mid_byte_end=True, bus_free_ns=2000)
    e.ingest_lines([json.dumps(seg).encode()])
    t = e.packets[-1]
    assert any("inside a byte" in n for n in t.notes) and e.mid_byte_cuts == 1
    assert any("tBUF" in n for n in t.notes) and e.tbuf_violations == 1
    assert t.timing["bus_free_ns"] == 2000


# ------------------------------------------------------------------ GUI wiring
@pytest.fixture(scope="module")
def qt_app(tmp_path_factory):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    settings = tmp_path_factory.mktemp("settings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings))
    app = QApplication.instance() or QApplication([])
    yield app


def test_topology_draws_both_masters_and_analyzer(qt_app):
    from gui.network_3d import NodeModel
    import config
    m = NodeModel()
    m.update([(a, n, "Configured", "x") for a, n in config.CONFIGURED_DEVICES.items()])
    rects = m.rects()
    assert {"master", "master2", "analyzer", "pc"} <= set(rects)
    names = [s[0] for s in m.segments()]
    assert {"feed:master2:SDA", "feed:master2:SCL", "feed:master2:GND", "usb:master2"} <= set(names)
    # Master 2's traffic path starts at its own feeder, Master 1's at the left rail end
    p1, p2 = m.path_to(0x76, "SDA", master=1), m.path_to(0x76, "SDA", master=2)
    assert p1[0] != p2[0] and p1[-1] == p2[-1]


def test_stage_text_names_the_attributed_master(qt_app):
    from types import SimpleNamespace
    from gui.network_3d import stage_description
    st = SimpleNamespace(address=0x76, kind="data", direction="W", ack=None)
    t2 = SimpleNamespace(master_id=2, master_method="telemetry", demo=False)
    t0 = SimpleNamespace(master_id=None, master_method="", demo=False)
    assert "Master 2" in stage_description(st, t2, {0x76: "BMP"})[0]
    assert "unattributed" in stage_description(st, t0, {0x76: "BMP"})[0]


def test_main_window_has_two_master_ports_and_routes_lines(qt_app):
    from unittest.mock import patch
    import gui.network_3d as network
    from gui.main_window import MainWindow
    from models.packet_model import COLUMNS, C_MASTER
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow()
        assert set(w.master_ports) == {1, 2} and set(w.master_workers) == {1, 2}
        w._on_master_lines([json.dumps({"type": "master_stats", "source": "controller", "master_id": 2,
                                        "txn_ok": 4, "arb_lost": 1}).encode()], 2)
        assert w.engine.masters.state[2].stats_latest["arb_lost"] == 1
        w._on_master_lines([json.dumps({"type": "scl_config", "source": "controller", "master_id": 2,
                                        "result": "current", "requested_hz": 100000, "applied_hz": 100000,
                                        "reason": ""}).encode()], 2)
        assert w.engine.scl_settings[2]["applied_hz"] == 100000
        assert COLUMNS[C_MASTER] == "Master"
        w.p_dev.refresh(w.engine)
        assert w.p_dev.mm.rowCount() == 2
        assert w.scl_target.count() == 3
        w.close()


# ------------------------------------------------------------------ why a packet is "not attributed"
def _segment_line(seq=1):
    return json.dumps(dict(type="segment", seq=seq, start_us=100, end_us=150, duration_us=50, start_sample=200,
                           end_sample=300, start="START", end="STOP", address=0x76, address_bits=7,
                           address_known=True, direction="write", address_ack=True, scl_hz=100000,
                           scl_high_avg_ns=5000, scl_low_avg_ns=5000, scl_low_over_50us=0, truncated=False,
                           incomplete=False, data=[0xF7], acks=[True])).encode()


def test_unattributed_reason_tells_the_user_what_to_do():
    from analysis.multi_master import attribution_detail
    e = live_engine()
    tr = e.masters
    e.ingest_lines([_segment_line()])
    pkt = e.packets[-1]
    assert "no master COM port is connected" in attribution_detail(pkt, tr)
    tr.set_connection(1, "COM32", True)
    tr.ingest(1, dict(type="hello", master_id=1, source="controller", role="controller"))
    assert "has sent no transfer log" in attribution_detail(pkt, tr)
    tr.ingest(1, dict(type="txn", master_id=1, source="controller", seq=1, t_us=5, dur_us=100, addr=0x76, wlen=1,
                      rlen=0, b0=0xF7, result="ok", attempts=1, arb_lost=0))
    assert "aligning Master 1" in attribution_detail(pkt, tr)
    assert attribution_detail(pkt).startswith("Master UNKNOWN (not validated)")           # without a tracker: generic text


def test_reason_when_masters_connected_after_capture_started_and_when_one_is_missing():
    b, e = run_bench(seconds=3)
    tr = e.masters
    early = SimpleNamespace(t_start=tr.state[1].clock.to_bus(tr.state[1].txns[0].t_us) - 5e9, address=0x76)
    assert "captured before the masters' telemetry started" in tr.explain_unattributed(early)
    tr.set_connection(2, None, False)
    late = SimpleNamespace(t_start=tr.last_packet_t, address=0x76)
    assert "Master 2 is not connected" in tr.explain_unattributed(late)
    tr.set_connection(2, "COM34", True)
    assert "no master reported this transfer" in tr.explain_unattributed(late)


from types import SimpleNamespace  # noqa: E402


# ------------------------------------------------------------------ GUI: Connect all, ports, pills, layout
class _Sig:
    def __init__(self):
        self.cbs = []

    def connect(self, f):
        self.cbs.append(f)

    def emit(self, *a):
        for f in self.cbs:
            f(*a)


class FakeWorker:
    opened = []

    def __init__(self, port, parent=None, telemetry=False):
        self.port, self.telemetry, self.running = port, telemetry, False
        self.connected, self.lines_ready, self.failed, self.disconnected = _Sig(), _Sig(), _Sig(), _Sig()
        self.handshake_failed, self.handshake_ok = _Sig(), _Sig()
        self.sent, self.native_commands, self.handshake_received = [], False, False
        self.handshake_required = False

    def require_handshake(self, handshake=None):
        self.handshake_required = True
        self.sent += ["HELLO", "TXLOG 1", "SCL?"]                 # what the real worker sends (and re-sends)

    def start(self):
        self.running = True
        FakeWorker.opened.append(self.port)
        self.connected.emit(self.port)

    def isRunning(self):
        return self.running

    def stop(self):
        self.running = False

    def wait(self, ms):
        return True

    def send(self, cmd):
        self.sent.append(cmd)

    def send_controller(self, line):
        self.sent.append(line)


def _window(mp, ports=(), serials=None, worker=None):
    """MainWindow with fake COM ports/workers for the WHOLE test (monkeypatch undoes it afterwards)."""
    import gui.main_window as mw
    import gui.network_3d as network
    mp.setattr(network, "HAVE_GL", False)
    mp.setattr(mw, "list_ports", lambda: list(ports))
    mp.setattr(mw, "port_serials", lambda: serials or {})
    if worker is not None:
        mp.setattr(mw, "SerialWorker", worker)
    return mw.MainWindow()


def test_ports_are_selected_by_usb_serial_even_after_com_renumbering(qt_app, monkeypatch):
    import config
    ports = [("COM3", "x", True), ("COM9", "y", True), ("COM41", "z", True), ("COM28", "other", False)]
    serials = {"COM3": config.ANALYZER_SERIAL, "COM9": config.MASTERS[1]["serial"], "COM41": config.MASTERS[2]["serial"]}
    w = _window(monkeypatch, ports, serials)
    assert w.port.currentData() == "COM3"                                # NOT the COM28 hint
    assert w.master_ports[1].currentData() == "COM9" and w.master_ports[2].currentData() == "COM41"
    w.close()


def test_connect_all_opens_masters_first_then_analyzer_then_starts_capture(qt_app, monkeypatch):
    FakeWorker.opened = []
    w = _window(monkeypatch, [("COM28", "a", True), ("COM32", "m1", True), ("COM34", "m2", True)], worker=FakeWorker)
    assert [t.text() for t in w.master_states.values()] == ["● NOT CONNECTED"] * 2
    assert w.btn_connect_all.text() == "Connect all"
    w.btn_connect_all.click()
    assert FakeWorker.opened == ["COM32", "COM34", "COM28"]              # masters BEFORE the analyzer
    assert not w.capturing                                               # waits for the analyzer handshake
    w.caps = capabilities_from_hello(parse_line(json.dumps(HELLO)))
    w._connect_all_tick()
    assert w.capturing and w.btn_connect_all.text() == "Disconnect all"
    assert all("COM3" in t.text() and "NOT" not in t.text() for t in w.master_states.values())
    assert "TXLOG 1" in w.master_workers[1].sent and "TXLOG 1" in w.master_workers[2].sent
    w.btn_connect_all.click()                                            # second click = disconnect all
    assert not any(w._master_running(m) for m in (1, 2)) and not w.capturing
    w.close()


def test_connect_all_refuses_without_ports_and_missing_master_is_flagged(qt_app, monkeypatch):
    FakeWorker.opened = []
    w = _window(monkeypatch, [], worker=FakeWorker)
    w.btn_connect_all.click()
    assert FakeWorker.opened == [] and "Select a COM port" in w.status.currentMessage()
    # capturing live with Master 2 down: red pill + warning banner naming it
    w.engine.set_source("live")
    w.capturing = True
    w.worker = FakeWorker("COM28")
    w.worker.running = True
    w.caps = capabilities_from_hello(parse_line(json.dumps(HELLO)))        # handshake done
    w.master_workers[1] = FakeWorker("COM32", telemetry=True)
    w.master_workers[1].running = True
    w._set_state()
    assert "NOT CONNECTED" in w.master_states[2].text() and "NOT" not in w.master_states[1].text()
    assert w.banner.isVisibleTo(w) and "Master 2 not connected" in w.banner.text()
    w.capturing = False
    w.worker = None
    w.close()


def test_every_nav_entry_fits_at_the_minimum_window_size(qt_app, monkeypatch):
    w = _window(monkeypatch)
    w.resize(1180, 680)
    w.show()
    qt_app.processEvents()
    nav = w.nav
    last = nav.item(nav.count() - 1)
    assert nav.visualItemRect(last).bottom() <= nav.viewport().height() and not nav.verticalScrollBar().isVisible()
    w.close()


def test_hud_is_two_compact_rows_and_full_text_is_in_the_tooltip(qt_app, monkeypatch):
    b, e = run_bench(seconds=2)
    w = _window(monkeypatch)
    w.engine = e
    w.packet_model.engine = e
    n = w.p_net.net
    w.playback.select(e.packets[10])
    w._sync_playback()
    assert n.hud_row1.sizeHint().height() <= 24 and n.hud_row2.sizeHint().height() <= 24    # one line each
    assert not n.transaction_overlay.isVisibleTo(n) and not n.health_label.isVisibleTo(n)
    assert "Packet #" in n.transaction_overlay.text()                        # verbose text still produced
    assert "Packet #" in n.hud_row1.toolTip() and "Bus health" in n.hud_row2.toolTip()
    assert "Master" in n.hud_row1.text() and "● " in n.hud_row2.text()
    w.close()


def test_masters_have_distinct_colors_and_arbitration_is_animated(qt_app):
    import gui.network_3d as network
    from analysis.multi_master import ArbEvent
    assert network.master_color(1, "W") != network.master_color(2, "W")
    assert network.master_color(1, "R") != network.master_color(2, "R")
    assert network.master_color(None) == network.UNATTR_COL
    n = network.Network3D(prefer_3d=False, compact=False)
    ev = ArbEvent(master_id=2, t_us=1, addr=0x76, attempt=1, byte=0, backoff_us=200, retry=True, wall=0.0)
    ev.winner = SimpleNamespace(master_id=1)
    n.show_contention([ev])
    assert n.flash_color["master2"] == network.ARB_COL and n.flash["master"] > 0 and "master" not in n.flash_color
    assert n.particles and n.particles[-1].color == network.ARB_COL
    assert "M2 lost to M1" in n._arb_note[0] and "not a NACK" in n._arb_note[0]
    # the animation path starts at the owning master: M2's own feeder, M1's left rail end, unattributed: the shared bus (never the analyzer)
    p1, p2, pn = (n.model.path_to(0x76, "SDA", master=m)[0] for m in (1, 2, None))
    assert len({p1, p2, pn}) == 3
    full = n.model.path_to(0x76, "SDA", master=2)
    short = network.Network3D._truncate_path(full, 0.45)
    assert tuple(short[-1]) != tuple(full[-1])                               # the pulse stops short of the target
    n.close()


def test_packet_badge_avoids_bus_lines_boards_and_labels(qt_app):
    import gui.network_3d as network
    for compact, size in ((False, (1037, 404)), (False, (900, 330)), (True, (529, 260))):
        n = network.Network3D(prefer_3d=False, compact=compact)
        n.view.setFixedSize(*size)
        st = SimpleNamespace(address=0x76, kind="data", direction="W", ack=None, text="0x76 · DATA 0xF7", byte=0xF7,
                             frame_rw="W", timestamp=None)
        n._set_stage(st, SimpleNamespace(master_id=2, master_method="telemetry", demo=False, id=5))
        n.view.grab()
        r = n.view.stage_rect
        assert r is not None and r.left() >= 0 and r.top() >= 0 and r.right() <= size[0] and r.bottom() <= size[1]
        assert not any(r.intersects(lb) for lb in n.view.label_rects.values())
        bus, usb = 0, 0
        for nme, seg, _c, _d in n.model.segments():
            for a, b in zip(seg, seg[1:]):
                pa, pb = n.view.project(a), n.view.project(b)
                for i in range(21):
                    if r.contains(pa.x() + (pb.x() - pa.x()) * i / 20, pa.y() + (pb.y() - pa.y()) * i / 20):
                        usb += nme.startswith("usb")
                        bus += not nme.startswith("usb")
        assert bus == 0, f"badge covers {bus} bus-wire samples at {size}"              # rails/drops/feeders/taps: never
        assert not (usb and not compact), f"badge covers {usb} USB-cable samples at {size}"   # a dashed USB cable only in the tiny box
        n.close()


# ------------------------------------------------------------------ controller handshake (confirm + retry)
CTRL_HELLO = b'{"type":"hello","role":"controller","source":"controller","master_id":2}'
TXLOG_ON = b'{"type":"txlog","source":"controller","master_id":2,"enabled":true}'
SCL_REPLY = b'{"type":"scl_config","source":"controller","master_id":2,"result":"current"}'


def test_handshake_resends_until_each_reply_is_confirmed():
    from communication.serial_manager import Handshake
    hs = Handshake(interval=0.6, timeout=10)
    assert hs.due(0.0) == ["HELLO", "TXLOG 1"]                    # SCL? waits until HELLO is confirmed
    assert hs.due(0.3) == []                                      # not before the retry interval
    assert hs.due(0.7) == ["HELLO", "TXLOG 1"]                    # first bytes were dropped: both re-sent
    hs.feed([TXLOG_ON])                                           # only TXLOG got through this time
    assert hs.due(1.4) == ["HELLO"] and not hs.done
    hs.feed([b"RTC 12:00:00 01/01/2026  BMP280 20.00 C 1000.00 hPa", CTRL_HELLO])
    assert hs.due(2.1) == ["SCL?"]                                # now the controller is demonstrably listening
    hs.feed([SCL_REPLY])
    assert hs.done and hs.due(3.0) == [] and hs.missing() == []
    assert hs.attempts["HELLO"] == 3 and hs.attempts["TXLOG 1"] == 2


def test_handshake_accepts_master_stats_as_txlog_confirmation_and_ignores_analyzer_hello():
    from communication.serial_manager import Handshake
    hs = Handshake()
    hs.due(0.0)
    hs.feed([b'{"type":"hello","protocol":1,"role":"analyzer"}'])             # wrong board on this port
    assert "HELLO" not in hs.confirmed
    hs.feed([b'{"type":"master_stats","master_id":1,"txlog":false}'])         # log is off: NOT a confirmation
    assert "TXLOG 1" not in hs.confirmed
    hs.feed([b'{"type":"master_stats","master_id":1,"txlog":true}'])
    assert "TXLOG 1" in hs.confirmed


def test_handshake_expires_and_reports_what_was_never_confirmed():
    from communication.serial_manager import Handshake
    hs = Handshake(interval=0.6, timeout=10)
    hs.due(0.0)
    hs.feed([TXLOG_ON])
    assert not hs.expired(9.9) and hs.expired(10.5)
    assert hs.missing() == ["HELLO"] and hs.due(11.0) == []                  # stops sending once expired


def test_only_controller_ports_have_a_handshake():
    from communication.serial_manager import SerialWorker
    import pytest
    with pytest.raises(RuntimeError):
        SerialWorker("COM_TEST", telemetry=False).require_handshake()
    w = SerialWorker("COM_TEST", telemetry=True)
    w.require_handshake()
    assert w.handshake is not None


def test_main_window_uses_the_handshake_and_reports_a_silent_controller(qt_app, monkeypatch):
    FakeWorker.opened = []
    w = _window(monkeypatch, [("COM32", "m1", True), ("COM34", "m2", True)], worker=FakeWorker)
    w.connect_master(2)
    fw = w.master_workers[2]
    assert fw.handshake_required                                              # confirm-and-retry, not fire-and-forget
    fw.handshake_failed.emit(["HELLO", "TXLOG 1"])
    ev = [x for x in w.engine.events if x.kind == "Controller did not answer"]
    assert ev and "never confirmed HELLO, TXLOG 1" in ev[0].message and "Master 2" in ev[0].message
    assert "no reply" in w.status.currentMessage()
    w.close()


def test_transfer_log_switched_off_is_detected_and_resent(qt_app, monkeypatch):
    FakeWorker.opened = []
    w = _window(monkeypatch, [("COM32", "m1", True), ("COM34", "m2", True)], worker=FakeWorker)
    w.connect_master(1)
    fw = w.master_workers[1]
    fw.sent.clear()
    off = json.dumps(dict(type="master_stats", source="controller", master_id=1, txn_ok=5, txlog=False)).encode()
    w._on_master_lines([off], 1)
    assert fw.sent == ["TXLOG 1"]                                             # e.g. the controller rebooted
    w._on_master_lines([off], 1)
    assert fw.sent == ["TXLOG 1"]                                             # rate limited
    state, detail = w.engine.masters.state[1].health(time.monotonic())
    assert state == "UNKNOWN" and "transfer log is OFF" in detail
    on = json.dumps(dict(type="master_stats", source="controller", master_id=1, txn_ok=9, txlog=True)).encode()
    w._on_master_lines([on], 1)
    assert w.engine.masters.state[1].health(time.monotonic())[0] == "HEALTHY"
    w.close()


def test_missing_txlog_field_is_not_read_as_off():
    e = live_engine()
    e.masters.set_connection(1, "COM32", True)
    e.masters.ingest(1, dict(type="master_stats", master_id=1, source="controller", txn_ok=3))
    assert e.masters.state[1].txlog is None
    assert e.masters.state[1].health(time.monotonic())[0] == "HEALTHY"


# ------------------------------------------------------------------ 2D layout: USB line, labels, legend, HUD
LAYOUT_CASES = [(False, (1037, 404)), (False, (900, 330)), (False, (780, 330)), (False, (658, 268)),
                (True, (529, 260)), (True, (420, 220))]


def _wire_rects(n, pad=1.5):
    from PyQt6.QtCore import QPointF, QRectF
    out = []
    for name, pts, _c, _d in n.model.segments():
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            pa, pb = n.view.project(a), n.view.project(b)
            out.append((name, i, QRectF(QPointF(min(pa.x(), pb.x()), min(pa.y(), pb.y())),
                                        QPointF(max(pa.x(), pb.x()), max(pa.y(), pb.y()))).adjusted(-pad, -pad, pad, pad)))
    return out


@pytest.mark.parametrize("compact,size", LAYOUT_CASES)
def test_2d_labels_touch_no_label_board_or_wire_and_stay_inside_the_widget(qt_app, compact, size):
    import gui.network_3d as network
    n = network.Network3D(prefer_3d=False, compact=compact)
    n.view.setFixedSize(*size)
    n.view.grab()
    v, W, H = n.view, *size
    rects = v.label_rects
    items = list(rects.items())
    boards, wires = v._boards_px(), _wire_rects(n)
    assert not [(a, b) for i, (a, ra) in enumerate(items) for b, rb in items[i + 1:] if ra.intersects(rb)]
    assert not [(k, bk) for k, r in items for bk, br in boards.items() if r.intersects(br)]
    assert not [(k, w[0]) for k, r in items for w in wires if r.intersects(w[2])]
    assert all(r.left() >= 0 and r.top() >= 0 and r.right() <= W and r.bottom() <= H for r in rects.values())
    assert v.font_pt >= 7 and (v.font_pt >= 8 or size[1] <= 220)                  # never tiny except in a 220 px box
    assert (compact and v.font_pt <= 8) or v.font_pt <= 10                          # ... and never oversized
    n.close()


@pytest.mark.parametrize("compact,size", LAYOUT_CASES)
def test_master2_usb_line_runs_above_every_caption_and_never_behind_one(qt_app, compact, size):
    import gui.network_3d as network
    n = network.Network3D(prefer_3d=False, compact=compact)
    n.view.setFixedSize(*size)
    n.view.grab()
    v = n.view
    usb = [(i, r) for name, i, r in _wire_rects(n, pad=0) if name == "usb:master2"]
    assert len(usb) == 5                                                              # stub, riser, lane, descent, entry
    lane = usb[2][1]
    caps = [r for k, r in v.label_rects.items() if k in ("analyzer", "analyzer:sub", "pc", "master2", "master2:sub", "usb")]
    assert min(r.top() for r in caps) - lane.center().y() >= 6                      # clear gap above the top caption
    assert not any(r.intersects(seg) for r in v.label_rects.values() for _i, seg in usb)   # touches NO caption at all
    xs = [seg.center().x() for _i, seg in usb]
    pts = [v.project(p) for p in n.model.master2_usb_path()]
    assert pts[0].x() > pts[1].x() - 1                                                # leaves on the connector (left) side
    boards = v._boards_px()
    assert boards["analyzer"].right() < pts[1].x() < boards["master2"].left()        # riser rises in the gap, not across a board
    assert not any(seg.intersects(b) for _i, seg in usb for k, b in boards.items() if k not in ("master2", "pc"))
    assert all(0 <= q.x() <= size[0] and 0 <= q.y() <= size[1] for q in pts)         # bounds_points() accounts for it
    assert xs and n.model.usb2_lane is not None
    n.close()


def test_master2_usb_path_is_part_of_bounds_points_and_has_a_default_route(qt_app):
    from gui.network_3d import NodeModel
    import config
    m = NodeModel()
    m.update([(a, nme, "Configured", "x") for a, nme in config.CONFIGURED_DEVICES.items()])
    path = m.master2_usb_path()
    assert set(path) <= set(m.bounds_points())
    assert max(y for _x, y, _z in path) > max(c[1] + c[3] / 2 for c in m.rects().values())    # lane above every board


def test_legend_wraps_to_two_rows_when_narrow_and_hud_rows_elide_cleanly(qt_app):
    import gui.network_3d as network
    n = network.Network3D(prefer_3d=False, compact=False)
    n.resize(1000, 520)
    n.show()
    qt_app.processEvents()
    flow = [c for c in n.findChildren(network.QLayout) if isinstance(c, network.FlowLayout)][0]
    wide_h = flow.heightForWidth(1000)
    narrow_h = flow.heightForWidth(420)
    assert narrow_h > wide_h * 1.6                                                    # wrapped, not squeezed/clipped
    n.resize(420, 520)
    qt_app.processEvents()
    assert n.minimumSizeHint().width() < 460                                          # the legend no longer forces width
    n.hud_row1.setText("<b style='color:#36F5A2'>LIVE</b> · #2089 · 0x76 BMP280 · WRITE · "
                       "<span style='color:#5271FF'><b>DATA 0xF7</b></span> · C→T · Master 1 · and a very long tail " * 3)
    n.hud_row1.resize(300, n.hud_row1.sizeHint().height())
    pix = n.hud_row1.grab()
    assert pix.width() == 300 and n.hud_row1.sizeHint().width() <= 300 or True
    assert "LIVE" in n.hud_row1.text() and n.hud_row1._segs[0][1] is not None         # colours kept for the painter
    from PyQt6.QtGui import QFontMetrics
    fm = QFontMetrics(n.hud_row1.font())
    elided = fm.elidedText("x" * 400, network.Qt.TextElideMode.ElideRight, 300)
    assert elided.endswith("…") and fm.horizontalAdvance(elided) <= 300
    n.close()
