"""Verify the Digital Waveform page in the real GUI (same start-up as main.py, real Qt event loop).

Part 1 - Presentation mode (bench timing, SIMULATED):
  * cursors A / B DRAGGED with the mouse to 250 / 420 µs, then 780 / 120 µs, while new packets keep arriving:
    they must never snap back to the 300 / 310 µs defaults
  * the A / B / delta readout recomputed independently (capture time, SCL cycle at each cursor, HIGH / LOW / duty,
    Δt, 1/Δt) at every check
  * no clipped readout line at three window sizes; the header banner names cursor A's packet
  * bench clock (~94.6 kHz, HIGH ~4.44 µs / LOW ~6.13 µs) and real register traffic (no OLED 0x3C reads)
Part 2 (--live): the same cursor checks on the attached hardware (Connect all), ~40 s of live sweeps.
Writes work/verification/wave_*.png and wave_report.json.   Usage:  python -X utf8 tools/verify_waveform.py [--live]
"""
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
OUT = ROOT / "work" / "verification"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS = []
sys.excepthook = lambda t, v, tb: (ERRORS.append("".join(traceback.format_exception(t, v, tb))), sys.__excepthook__(t, v, tb))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _ErrorLog(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            ERRORS.append(f"log {record.name}: {record.getMessage()}")


logging.getLogger().addHandler(_ErrorLog())
from PyQt6.QtCore import QEvent, QEventLoop, QObject, QPoint, QPointF, Qt, QTimer, QtMsgType, qInstallMessageHandler  # noqa
from PyQt6.QtGui import QFont, QGuiApplication, QMouseEvent, QSurfaceFormat  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _qt_messages(kind, _ctx, msg):
    if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        ERRORS.append(f"Qt: {msg}")


qInstallMessageHandler(_qt_messages)
fmt = QSurfaceFormat(); fmt.setSamples(4); fmt.setDepthBufferSize(24)
QSurfaceFormat.setDefaultFormat(fmt)
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
from gui.theme import STYLESHEET  # noqa: E402
app.setStyleSheet(STYLESHEET)
from analysis import scl_timing  # noqa: E402
from analysis.playback import format_capture_ns  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

HUMAN = []


class _InputSpy(QObject):
    def eventFilter(self, obj, ev):
        if ev.spontaneous() and ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel):
            HUMAN.append((round(time.monotonic(), 1), ev.type().name))
        return False


_spy = _InputSpy()
app.installEventFilter(_spy)
w = MainWindow()
w.resize(1680, 980)
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)      # an automated run: desktop clicks pass through
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED VERIFICATION RUN (input disabled)")
w.show()
wave = w.p_wave
report = dict(failures=[], checks={}, shots=[], readouts=[])
fail = report["failures"]
loop = QEventLoop()


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(15, loop.quit)
        loop.exec()
        if until is not None and until():
            return True
    return until() if until is not None else True


def check(name, ok, detail=""):
    report["checks"][name] = dict(ok=bool(ok), detail=str(detail)[:1500])
    if not ok:
        fail.append(f"{name}: {str(detail)[:400]}")


def shot(tag):
    p = OUT / f"wave_{tag}.png"
    w.grab().save(str(p))
    report["shots"].append(p.name)


def drag_cursor(line, to_us):
    """A real mouse drag on the plot: press on the cursor line, move in steps, release (pyqtgraph's drag path)."""
    vb = wave.plot.getPlotItem().getViewBox()
    vp = wave.plot.viewport()
    y = sum(vb.viewRange()[1]) / 2

    def vp_pos(x_us):
        sp = vb.mapViewToScene(QPointF(x_us, y))
        return QPointF(wave.plot.mapFromScene(sp))

    a, b = vp_pos(line.value()), vp_pos(to_us)

    def send(kind, pos, button, buttons):
        ev = QMouseEvent(kind, pos, QPointF(vp.mapToGlobal(pos.toPoint())), button, buttons,
                         Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, ev)
        app.processEvents()

    send(QEvent.Type.MouseMove, a, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
    send(QEvent.Type.MouseButtonPress, a, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    for k in range(1, 21):
        send(QEvent.Type.MouseMove, a + (b - a) * (k / 20), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseButtonRelease, b, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    px_per_us = abs((vp_pos(1.0) - vp_pos(0.0)).x()) or 1.0
    return abs(line.value() - to_us) <= 1.5 / px_per_us + 0.05          # within ~1 px of the target


def independent_readout():
    """Recompute everything the readout shows, without the page's helpers, and compare with its text."""
    eng = w.engine
    xa, xb = wave.ca.value(), wave.cb.value()
    ta, tb = wave.origin + xa * 1e3, wave.origin + xb * 1e3
    txt = wave.cursor_lbl.text()
    lines = txt.split("\n")
    problems = []
    if len(lines) != 3:
        problems.append(f"{len(lines)} lines")
    for name, x, t_ns, line in (("A", xa, ta, lines[0] if lines else ""), ("B", xb, tb, lines[1] if len(lines) > 1 else "")):
        if f"Cursor {name}: x_{name} = {x:.2f} µs" not in line:
            problems.append(f"x_{name}")
        if format_capture_ns(t_ns) not in line or f"({t_ns / 1e3:,.3f} µs)" not in line:
            problems.append(f"capture time {name}")
        near = [p for p in eng.packets[-400:] if p.t_start <= t_ns <= (p.t_end or p.t_start)]
        cyc = scl_timing.cycle_at(near, t_ns, eng.edges_between)
        if cyc is None:
            if f"SCL cycle at {name}: UNKNOWN" not in line:
                problems.append(f"cycle {name} should be UNKNOWN")
        else:
            want = [f"(packet #{cyc['txn_id']})", f"T_{name} = rise→next rise {cyc['period_ns'] / 1e3:.2f} µs",
                    f"f_{name} = 1/T_{name} = {1e9 / cyc['period_ns'] / 1e3:.3f} kHz"]
            if cyc["high_ns"] is not None:
                want += [f"HIGH {cyc['high_ns'] / 1e3:.2f} µs", f"LOW {cyc['low_ns'] / 1e3:.2f} µs",
                         f"duty {100 * cyc['high_ns'] / cyc['period_ns']:.1f} %"]
            problems += [f"{name}: missing '{s}'" for s in want if s not in line]
    dt = abs(tb - ta) / 1e3
    want_d = f"Δt = |t_B − t_A| = |{tb / 1e3:,.3f} − {ta / 1e3:,.3f}| µs = {dt:,.3f} µs"
    want_f = f"Capture Frequency (1/Δt) = 1 / {dt:,.3f} µs = {1e3 / dt:.3f} kHz" if dt > 0 else "undefined"
    if len(lines) < 3 or want_d not in lines[2] or want_f not in lines[2]:
        problems.append("delta line")
    report["readouts"].append(dict(A=round(xa, 2), B=round(xb, 2), text=lines))
    return problems


def not_clipped():
    bad = []
    for lb in (wave.info, wave.cursor_lbl):
        need = lb.heightForWidth(lb.width())
        if lb.height() < need:
            bad.append((lb.objectName() or "label", lb.height(), need))
    return bad


def banner_matches_a():
    eng = w.engine
    a_ns = wave.origin + wave.ca.value() * 1e3
    if wave._shown is None:
        return True, "no window"
    t0, t1 = wave._shown["t0"], wave._shown["t1"]
    under = [p for p in eng.packets[-400:] if p.t_start <= a_ns <= (p.t_end or p.t_start) and p.t_start <= t1
             and (p.t_end or p.t_start) >= t0]
    if not under:
        return True, "cursor A between packets"
    p = under[-1]
    return (wave._banner_txn(eng) is p and f"#{p.id}" in (wave.banner._last or "")), f"packet #{p.id}"


def advance(n):
    """Release n more transfers (Step through the presentation) and let the waveform redraw."""
    start = len(w.engine.packets)
    guard = 0
    while len(w.engine.packets) < start + n and guard < 200:
        w.btn_step.click()
        pump(0.05)
        guard += 1
    pump(1.2)                                                       # the waveform's refresh picks the new sweep up


# ================================================================== part 1: presentation (bench timing)
w.btn_present.click()
check("presentation on", w.presentation is not None)
w.set_stage_hold(True)
advance(3)
w.nav.setCurrentRow(3)                                              # Digital Waveform
pump(2.0)
check("waveform shows a window", wave._shown is not None)
defaults = (round(wave.ca.value(), 3), round(wave.cb.value(), 3))
report["default_cursors"] = defaults
shot("01_initial")
positions = [(250.0, 420.0), (780.0, 120.0), (420.0, 780.0)]
for k, (xa, xb) in enumerate(positions, 1):
    ok_a, ok_b = drag_cursor(wave.ca, xa), drag_cursor(wave.cb, xb)
    pump(0.4)
    check(f"drag {k}: A -> {xa} µs, B -> {xb} µs by mouse", ok_a and ok_b, (wave.ca.value(), wave.cb.value()))
    placed = (wave.ca.value(), wave.cb.value())
    for r in range(3):                                              # packets keep advancing / redrawing
        advance(1)
        now = (wave.ca.value(), wave.cb.value())
        check(f"drag {k} round {r + 1}: cursors not reset by new packets", now == placed, (placed, now))
        probs = independent_readout()
        check(f"drag {k} round {r + 1}: readout matches independent recomputation", not probs, probs)
        ok, why = banner_matches_a()
        check(f"drag {k} round {r + 1}: banner = cursor A's packet", ok, why)
    shot(f"02_cursors_{int(xa)}_{int(xb)}")
wave._redraw(force=True)
check("forced redraw keeps cursors", (wave.ca.value(), wave.cb.value()) == placed)
wave.fit.click(); pump(0.5)
check("Fit packet keeps on-screen cursors", (wave.ca.value(), wave.cb.value()) == placed)
check("never snapped back to the defaults", all(r["A"] != round(defaults[0], 2) for r in report["readouts"]))
# A / B on two different packets: the banner follows A
for size, tag in (((1680, 980), "normal"), ((1280, 760), "medium"), ((1024, 700), "small")):
    w.resize(*size)
    pump(1.0)
    check(f"readout not clipped at {size[0]}x{size[1]}", not not_clipped(), not_clipped())
    from PyQt6.QtWidgets import QScrollArea
    sa = wave.parentWidget()
    while sa is not None and not isinstance(sa, QScrollArea):
        sa = sa.parentWidget()
    if sa is not None:                                              # a small window scrolls the page: show the bottom
        sa.verticalScrollBar().setValue(sa.verticalScrollBar().maximum())
        pump(0.5)
        lb = wave.cursor_lbl
        top = lb.mapTo(sa.viewport(), QPoint(1, 0))
        bottom = lb.mapTo(sa.viewport(), QPoint(1, lb.height() - 1))
        vis = sa.viewport().rect().contains(top) and sa.viewport().rect().contains(bottom)
        check(f"readout fully reachable at {size[0]}x{size[1]} (scrolled)", vis,
              (top.y(), bottom.y(), sa.viewport().height()))
    shot(f"03_size_{tag}")
    if sa is not None:
        sa.verticalScrollBar().setValue(0)
w.resize(1680, 980)
pump(1.0)
# bench timing + real traffic
pk = list(w.engine.packets)
f = [p.timing.get("frequency_hz") for p in pk if p.timing.get("frequency_hz")]
hi = [p.timing.get("high_ns") for p in pk if p.timing.get("high_ns")]
lo = [p.timing.get("low_ns") for p in pk if p.timing.get("low_ns")]
report["bench_timing"] = dict(packets=len(pk), f_khz=[round(min(f) / 1e3, 2), round(statistics.median(f) / 1e3, 2),
                                                     round(max(f) / 1e3, 2)],
                              high_us=round(statistics.median(hi) / 1e3, 2), low_us=round(statistics.median(lo) / 1e3, 2))
check("bench clock ~94.6 kHz (not an ideal 100 kHz)", 93.5e3 <= statistics.median(f) <= 95.8e3, report["bench_timing"])
check("HIGH ~4.44 µs / LOW ~6.13 µs", abs(statistics.median(hi) - 4440) < 200 and abs(statistics.median(lo) - 6130) < 200,
      report["bench_timing"])
oled_reads = [p.id for p in pk if p.address == 0x3C and any(fr.rw == "R" for fr in p.frames)]
check("no OLED 0x3C reads", not oled_reads, oled_reads)
report["traffic"] = [f"#{p.id} 0x{p.address:02X} {p.operation} {p.data_hex()}" for p in pk]
w.set_stage_hold(False)
w.btn_present.click()                                               # presentation off (drains)
pump(2.0)

# ================================================================== parts 2 / 3: Demo mode, hardware connected
def cursor_session(tag, rounds=8, every=4.0):
    """Drag A / B with the mouse, then let live sweeps run: the cursors must stay, the readout must match an
    independent recomputation every time, the banner must name cursor A's packet, nothing may be clipped."""
    w.nav.setCurrentRow(3)
    pump(4.0)
    ok_a, ok_b = drag_cursor(wave.ca, 250.0), drag_cursor(wave.cb, 420.0)
    check(f"{tag}: cursors dragged by mouse to 250 / 420 µs", ok_a and ok_b, (wave.ca.value(), wave.cb.value()))
    placed = (wave.ca.value(), wave.cb.value())
    reads, origins = [], set()
    for r in range(rounds):
        pump(every)
        origins.add(round(wave.origin))
        now = (wave.ca.value(), wave.cb.value())
        check(f"{tag} {r + 1}: cursors not reset by new sweeps", now == placed, (placed, now))
        probs = independent_readout()
        check(f"{tag} {r + 1}: readout matches independent recomputation", not probs, probs)
        ok, why = banner_matches_a()
        check(f"{tag} {r + 1}: banner = cursor A's packet", ok, why)
        reads.append(wave.cursor_lbl.text().split("\n"))
    check(f"{tag}: new packets / sweeps arrived meanwhile", len(origins) > 1, len(origins))
    check(f"{tag}: readout not clipped", not not_clipped(), not_clipped())
    report[f"{tag}_readouts"] = reads[-2:]
    shot(f"04_{tag}")


pump(40, lambda: w.net_run_state == "stopped")
if "--demo" in sys.argv:
    w.btn_demo.click()                                              # Demo mode (simulated two-master bench)
    check("demo: running", w.demo is not None and w.demo.isRunning())
    cursor_session("demo")
    w.btn_demo.click()                                              # demo off
    pump(2.0)

if "--live" in sys.argv:
    w.btn_connect_all.click()
    pump(25, lambda: w.capturing and w.engine.masters.can_attribute())
    check("live: capturing", w.capturing)
    cursor_session("live")
    w.btn_connect_all.click()                                       # disconnect all
    pump(2.0)

report["outside_input"] = HUMAN[:20]
check("no outside mouse / key input during the run", not HUMAN, HUMAN[:5])
report["python_or_qt_errors"] = ERRORS
check("no Python / Qt errors", not ERRORS, ERRORS[:2])
report["result"] = "PASS" if not fail else "FAIL"
w.close()
pump(0.3)
(OUT / "wave_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: report[k] for k in ("result", "failures")}, indent=2, ensure_ascii=False))
os._exit(0 if not fail else 1)
