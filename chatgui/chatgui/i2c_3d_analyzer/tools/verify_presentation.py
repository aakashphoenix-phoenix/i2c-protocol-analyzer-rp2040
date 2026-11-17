"""Measure the review PRESENTATION in the real GUI (same start-up as main.py, real Qt event loop, OpenGL 3D view).

One complete, unattended cycle at the default 0.25x: Master 1 (Pico 1) and Master 2 (Pico 3), each with the DS3231 RTC,
the BMP280 and the SSD1306 OLED 0x3C, WRITE then READ (12 transfers).  For every transfer it measures, from the screen:
  * the wall-clock duration of the transfer and of each of its phases (START, ADDRESS + R/W, ADDRESS ACK, DATA, DATA ACK,
    DATA, DATA ACK/NACK, STOP) and the order the phases appear in
  * every dot: which board sends it and which receives it (Master -> Slave / Slave -> Master), never two at once
  * the phase banner of the 3D view == the Packet Analyzer's live banner, every frame; the Packet Analyzer row at the end
Then Pause / Step / NACK / Play / Stop.  Writes work/verification/pres_*.png and pres_report.json.
Usage:  python -X utf8 tools/verify_presentation.py
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
from PyQt6.QtCore import QEventLoop, Qt, QTimer, QtMsgType, qInstallMessageHandler  # noqa: E402
from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat  # noqa: E402
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
import config  # noqa: E402
from analysis.playback import phase_of  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.network_3d import M_KEY  # noqa: E402
from gui.phase_panel import target_name  # noqa: E402
from models.packet_model import C_ACK, C_ADDR, C_DATA, C_MASTER, C_RW, C_STAT  # noqa: E402
from sources.presentation_source import SCRIPT as FULL_SCRIPT  # noqa: E402

# --from N: measure only the scripted transfers N.. (1-based), e.g. --from 11 = Master 2 <-> OLED WRITE + READ
FROM = int(sys.argv[sys.argv.index("--from") + 1]) - 1 if "--from" in sys.argv else 0
SCRIPT = FULL_SCRIPT[FROM:]

from PyQt6.QtCore import QEvent, QObject  # noqa: E402

HUMAN = []                                        # real (OS-originated) mouse / keyboard input during the run
NAVLOG = []


class _InputSpy(QObject):
    def eventFilter(self, obj, ev):
        if ev.spontaneous() and ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel):
            HUMAN.append((round(time.monotonic(), 1), ev.type().name, type(obj).__name__))
        return False


_spy_input = _InputSpy()
app.installEventFilter(_spy_input)
w = MainWindow()
w.resize(1680, 980)
# an automated measurement: the window stays visible but ignores mouse / keyboard (clicks pass through to the desktop),
# otherwise a click on it (seen twice: a page change to Digital Waveform hid the 3D view mid-transfer) spoils the timing
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED VERIFICATION RUN (input disabled)")
w.show()
w.nav.currentRowChanged.connect(lambda r: NAVLOG.append((round(time.monotonic(), 1), r)))
net = w.p_net.net
report = dict(renderer="3D (OpenGL)" if net.is_3d else "2D fallback", failures=[], checks={}, shots=[])
fail = report["failures"]
loop = QEventLoop()


def pump(seconds, until=None, each=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(15, loop.quit)
        loop.exec()
        if each is not None:
            each()
        if until is not None and until():
            return True
    return until() if until is not None else True


def shot(tag, page=None):
    if page is not None:
        w.nav.setCurrentRow(page)
        pump(0.5)
    p = OUT / f"pres_{tag}.png"
    w.grab().save(str(p))
    report["shots"].append(p.name)


def check(name, ok, detail=""):
    report["checks"][name] = dict(ok=bool(ok), detail=str(detail)[:1500])
    if not ok:
        fail.append(f"{name}: {str(detail)[:400]}")


# ------------------------------------------------------------------ 1. speeds
speeds = [w.visual_speed.itemData(i) for i in range(w.visual_speed.count())]
check("speed options 0.1 / 0.25 / 0.4 / 0.5 (no 2x)", speeds == [0.1, 0.25, 0.4, 0.5], speeds)
check("default speed 0.25x (< 0.5x)", w.visual_speed.currentData() == 0.25 and w.playback.speed == 0.25,
      w.visual_speed.currentData())

# ------------------------------------------------------------------ 2. one full unattended cycle, measured
DOTS = []
_orig_spawn = net._spawn_stage_particle


def _spy(stage, txn):
    before = list(net.particles)
    _orig_spawn(stage, txn)
    for q in net.particles:
        if not any(q is b for b in before):
            DOTS.append(dict(tid=txn.id, idx=w.playback.stage_index, stage=stage, src=q.src, dst=q.dst,
                             partial=q.partial, color=q.color, t=time.monotonic()))


net._spawn_stage_particle = _spy
w.nav.setCurrentRow(1)
pump(1.0)
w.btn_present.click()
w.presentation.index = FROM                       # (before the first refresh releases a transfer)
check("presentation mode on (SIMULATED)", w.presentation is not None and w.engine.source == "demo")

state = dict(stage=None, st0=None, phase=None, max_dots=0, lockstep_bad=[], frames=0)
TRANSFERS = {}
order = []


def watch():
    pb = w.playback
    now = time.monotonic()
    state["frames"] += 1
    state["max_dots"] = max(state["max_dots"], sum(1 for q in net.particles if not q.done and not q.partial))
    if pb.txn is None:
        return
    tid = pb.txn.id
    if tid not in TRANSFERS:
        if order:
            prev = TRANSFERS[order[-1]]
            prev["end"] = now
            if state["stage"] is not None:
                prev["phases"].append((state["stage"], state["phase"], now - state["st0"]))
        TRANSFERS[tid] = dict(start=now, end=None, phases=[], banners=[], txn=pb.txn)
        order.append(tid)
        state["stage"], state["st0"], state["phase"] = pb.stage_index, now, phase_of(pb.events, pb.stage_index)
    elif pb.stage_index != state["stage"]:
        TRANSFERS[tid]["phases"].append((state["stage"], state["phase"], now - state["st0"]))
        state["stage"], state["st0"], state["phase"] = pb.stage_index, now, phase_of(pb.events, pb.stage_index)
    b3d, bpa = net.phase_panel.text, w.p_pk.phase.text
    if b3d != bpa and len(state["lockstep_bad"]) < 5:
        state["lockstep_bad"].append((b3d, bpa))
    bt = TRANSFERS[tid]["banners"]
    if not bt or bt[-1] != b3d:
        bt.append(b3d)


# the measured cycle is left alone: no screenshot, no tab switch (a window grab stalls the GUI thread for a moment);
# the screenshots of every pair are taken afterwards, in a Step pass over the next cycle
cycle_t0 = time.monotonic()
while len(order) <= len(SCRIPT) and time.monotonic() - cycle_t0 < 600:
    pump(0.25, each=watch)
cycle_s = time.monotonic() - cycle_t0
cycle_t1 = time.monotonic()
outside = [h for h in HUMAN if cycle_t0 <= h[0] <= cycle_t1]
report["outside_input_during_cycle"] = outside[:20]
report["page_changes_during_cycle"] = [n for n in NAVLOG if cycle_t0 <= n[0] <= cycle_t1]
check("measured cycle undisturbed (no outside mouse / key input, no page change)",
      not outside and not report["page_changes_during_cycle"], (outside[:5], report["page_changes_during_cycle"][:5]))
check("full cycle completed without stopping", len(order) > len(SCRIPT), f"{len(order)} transfers in {cycle_s:.0f} s")

pairs, durations = [], []
for n_t, (mid, addr, op, data, what) in enumerate(SCRIPT):
    if n_t >= len(order) or TRANSFERS[order[n_t]]["end"] is None:
        check(f"transfer {n_t + 1} measured", False, "not reached")
        continue
    tr = TRANSFERS[order[n_t]]
    t = tr["txn"]
    opname = "READ" if op == "R" else "WRITE"
    mlabel = f"{config.MASTERS[mid]['label']} ({config.MASTERS[mid]['pico']})"
    name = f"{mlabel} ↔ {target_name(addr)} {opname}"
    dur = tr["end"] - tr["start"]
    durations.append(dur)
    seq = [p for _s, p, _d in tr["phases"]]
    phase_runs = [p for i, p in enumerate(seq) if i == 0 or p != seq[i - 1]]
    problems = []
    if (t.master_id, t.address, t.operation) != (mid, addr, opname):
        problems.append(f"played {(t.master_id, t.address, t.operation)}")
    if phase_runs != [1, 2, 3, 4, 5, 4, 5, 6]:
        problems.append(f"phase order {phase_runs}")
    stage_idx = [s for s, _p, _d in tr["phases"]]
    if stage_idx != list(range(9)):
        problems.append(f"stages shown {stage_idx} (want all 9, in order)")
    want_prefix = f"[{mlabel} → {target_name(addr)} · {opname}]"
    if not all(b.startswith(want_prefix) for b in tr["banners"]):
        problems.append("banner " + str([b for b in tr["banners"] if not b.startswith(want_prefix)][:2]))
    key = M_KEY[mid]
    dots = [d for d in DOTS if d["tid"] == t.id and not d["partial"]]
    m2s = s2m = 0
    for d in dots:
        st = d["stage"]
        master_sends = st.kind == "cond" or st.direction == "W"
        want = (key, addr) if master_sends else (addr, key)
        if (d["src"], d["dst"]) != want:
            problems.append(f"dot {st.text}: {d['src']}->{d['dst']} (want {want})")
        m2s += master_sends
        s2m += not master_sends
    if m2s == 0 or s2m == 0:
        problems.append(f"directions m2s={m2s} s2m={s2m}")
    dot_stages = sorted({d["idx"] for d in dots})
    if dot_stages != [0, 1, 3, 4, 5, 6, 7, 8]:                     # every stage but the R/W bit (part of the address dot)
        problems.append(f"dot stages {dot_stages}")
    m = w.packet_model
    r = next(i for i in range(len(m.rows) - 1, -1, -1) if m.rows[i] is t)
    cell = lambda c: str(m.data(m.index(r, c)))
    row = dict(master=cell(C_MASTER), addr=cell(C_ADDR), rw=cell(C_RW), data=cell(C_DATA), ack=cell(C_ACK),
               status=cell(C_STAT))
    if not row["master"].startswith(config.MASTERS[mid]["label"]) or row["addr"] != f"0x{addr:02X}" or row["rw"] != opname:
        problems.append(f"analyzer row {row}")
    if data is not None and row["data"] != f"{op}: " + " ".join(f"{b:02X}" for b in data):
        problems.append(f"analyzer data {row['data']}")
    phase_d = {}
    for _s, p, d in tr["phases"]:
        phase_d.setdefault(p, []).append(round(d, 2))
    pairs.append(dict(index=FROM + n_t + 1, pair=name, packet=t.id, duration_s=round(dur, 2), phases_in_order=phase_runs,
                      phase_seconds=phase_d, dots=len(dots), master_to_slave_dots=m2s, slave_to_master_dots=s2m,
                      analyzer_row=row, ok=not problems, problems=problems))
    check(f"pair {FROM + n_t + 1:02d} {name}", not problems, problems)
report["pairs"] = pairs
if durations:
    med = statistics.median(durations)
    spread = (max(durations) - min(durations)) / med
    report["duration_median_s"] = round(med, 2)
    report["duration_spread_pct"] = round(100 * spread, 1)
    check("uniform: every pair within 5 % of the median duration (no fast-forward)", spread <= 0.05,
          [round(d, 2) for d in durations])
    rtc = [p["duration_s"] for p in pairs if "RTC" in p["pair"]]
    others = [p["duration_s"] for p in pairs if "RTC" not in p["pair"]]
    if rtc and others:                            # (a --from subset may hold no RTC transfer)
        check("BMP280 / OLED never faster than RTC", min(others) >= 0.95 * statistics.mean(rtc), (rtc, others))
all_phase = [d for p in pairs for k, ds in p["phase_seconds"].items() for d in ds]
check("every stage on screen >= 1.5 s at 0.25x", all_phase and min(all_phase) >= 1.5, min(all_phase) if all_phase else None)
check("zero overlap: never more than one stage dot in flight", state["max_dots"] <= 1, state["max_dots"])
check("3D banner == Packet Analyzer banner on every frame", not state["lockstep_bad"],
      state["lockstep_bad"][:2] + [f"{state['frames']} frames"])
check("no dot cut while playing", net.particles_cut == 0, net.particles_cut)
# ------------------------------------------------------------------ 3. Pause, then a Step pass with a screenshot per pair
w.btn_play.click()
check("Pause holds", w.stage_hold and w.btn_play.text() == "▶ Play")
pump(4.0)
for n_t, (mid, addr, op, _d, _w) in enumerate(SCRIPT if FROM == 0 else ()):   # screenshots: full runs only
    t = w.playback.txn
    guard = 0
    while w.playback.current.kind != "data" and guard < 12:
        w.btn_step.click()
        pump(0.3)
        guard += 1
    pump(2.4)                                                     # held on the first DATA phase: its dot crosses
    tag = f"pair_{n_t + 1:02d}_M{mid}_{target_name(addr).split()[0]}_{'READ' if op == 'R' else 'WRITE'}"
    shot(tag)
    if n_t in (10, 11):                                           # Master 2 <-> OLED: the Packet Analyzer at that moment
        img = w.p_pk.grab()                                       # rendered off-screen: no tab switch
        img.save(str(OUT / f"pres_{tag}_packet_analyzer.png"))
        report["shots"].append(f"pres_{tag}_packet_analyzer.png")
    guard = 0
    while w.playback.txn is t and guard < 12:                     # STOP, then phase 1 of the next transfer
        w.btn_step.click()
        pump(0.3)
        guard += 1
shot("cycle_packet_analyzer_all_transfers", page=2)
w.nav.setCurrentRow(1)
pump(0.6)

# ------------------------------------------------------------------ 4. Step / NACK / Play / Stop
held_at = (w.playback.txn.id, w.playback.stage_index)
pump(4.0)
check("held: nothing advances", (w.playback.txn.id, w.playback.stage_index) == held_at, held_at)
w.btn_step.click()
pump(0.8)
check("Step advances exactly one phase", w.playback.stage_index == held_at[1] + 1 or
      (w.playback.txn.id != held_at[0] and w.playback.stage_index == 0), (held_at, w.playback.stage_index))
shot("step_paused_phase")
w.btn_nack.setChecked(True)
guard = 0
while not (w.playback.txn.addr_nack and w.playback.current.kind == "ack") and guard < 40:
    w.btn_step.click()
    pump(0.5)
    guard += 1
pump(2.0)
shot("nack_address")
check("NACK toggle: the next target NACKs its address", "Address NACK" in net.phase_panel.text, net.phase_panel.text)
w.btn_play.click()
check("Play resumes", not w.stage_hold and w.btn_play.text() == "⏸ Pause")
pump(8.0)
w.btn_stop.click()
check("Stop: draining", w.net_run_state in ("draining", "stopped"), w.net_run_state)
pump(120, lambda: w.net_run_state == "stopped")
shot("stopped")
check("Stop: the transfer on display finished, then stopped", w.net_run_state == "stopped" and w.playback.finished)

report["engine_errors"] = [f"{e.category} {e.kind}: {e.message}" for e in list(w.engine.events) if e.severity == "error"]
check("no engine errors", not report["engine_errors"], report["engine_errors"])
report["python_or_qt_errors"] = ERRORS
check("no Python / Qt errors", not ERRORS, ERRORS[:2])
report["max_stage_dots_in_flight"] = state["max_dots"]
report["cycle_seconds"] = round(cycle_s, 1)
report["result"] = "PASS" if not fail else "FAIL"
w.close()
pump(0.3)
(OUT / "pres_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: report[k] for k in ("result", "failures", "renderer", "max_stage_dots_in_flight",
                                          "cycle_seconds")}, indent=2, ensure_ascii=False))
os._exit(0 if not fail else 1)
