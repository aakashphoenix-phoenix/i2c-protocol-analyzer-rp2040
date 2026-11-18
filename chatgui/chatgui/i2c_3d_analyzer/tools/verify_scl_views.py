"""Render the waveform/packet pages with SIMULATED demo records before and after a
simulated controller SCL change (100 kHz -> 400 kHz).  No serial port is opened.
Screenshots: work/verification/scl_*.png.  Usage: python -X utf8 tools/verify_scl_views.py
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

app = QApplication([])
FORCE_2D = "--2d" in sys.argv
if FORCE_2D:
    import gui.network_3d as _n
    _n.HAVE_GL = False
from gui.theme import STYLESHEET
app.setStyleSheet(STYLESHEET)
from gui.main_window import MainWindow
from sources.demo_source import DemoGenerator

w = MainWindow()
e = w.engine
e.set_source("demo")
g = DemoGenerator(seed=11)
e.ingest_lines([g.hello()])
e.record_scl_setting(100_000, 100_000, "boot", "SIMULATED demo controller", "simulated edge timing only")
for k in range(1, 41):
    e.ingest_lines(g.lines_until(k * 0.05))
g.request_scl(400_000)
e.record_scl_setting(400_000, 400_000, "applied", "SIMULATED demo controller", "simulated edge timing only")
for k in range(41, 81):
    e.ingest_lines(g.lines_until(k * 0.05))
w.packet_model.sync()
w.scl_choice.setCurrentIndex(1)
txn = next(t for t in reversed(e.packets) if t.address == 0x68)
w._set_state(); w.select_transaction(txn); w.timer.stop(); w._update_scl_status()
out = ROOT / "work" / "verification"; out.mkdir(parents=True, exist_ok=True)
steps = [("normal", 1680, 980), ("small", 1280, 760)]


def shot():
    if not steps:
        print("status:", w.scl_status.text(), flush=True)
        print("cursor:", w.p_wave.cursor_lbl.text(), flush=True)
        print("packet:", txn.id, txn.timing.get("frequency_hz"), txn.timing.get("frequency_scope"), flush=True)
        w.close(); app.quit(); return
    name, W, H = steps.pop(0)
    w.showNormal(); w.resize(W, H)
    w.nav.setCurrentRow(3)
    w.p_wave.show_packet(txn, synchronize=False)
    addr = txn.bytes[0]
    x0 = (addr.bit_times[0] - w.p_wave.origin) / 1e3 - 2
    x1 = (txn.bytes[1].t_ack - w.p_wave.origin) / 1e3 + 2
    w.p_wave.plot.getPlotItem().setXRange(x0, x1, padding=0)       # zoom: binary bits visible

    def grab():
        w.p_wave._redraw(force=True)
        w.p_wave.plot.getPlotItem().setXRange(x0, x1, padding=0)
        w.p_wave._annotate(e, w.p_wave.origin, w.p_wave._window_end)
        r0, r1 = addr.bit_times[2], addr.bit_times[3]
        w.p_wave.ca.setValue((r0 + 300 - w.p_wave.origin) / 1e3)
        w.p_wave.cb.setValue((r1 - w.p_wave.origin) / 1e3)
        w.p_wave._cursor_text()
        w.grab().save(str(out / f"scl_waveform_{name}.png"))
        w.nav.setCurrentRow(2)
        w.p_pk.refresh(e)
        QTimer.singleShot(500, lambda: (w.grab().save(str(out / f"scl_packets_{name}.png")), shot()))
    QTimer.singleShot(900, grab)


w.show()
QTimer.singleShot(700, shot)
app.exec()
