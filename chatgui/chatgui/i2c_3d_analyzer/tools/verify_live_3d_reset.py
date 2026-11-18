"""LIVE HARDWARE check of the 3D I2C Network page: master names, Reset view position and exact Reset restoration.

Real Hardware Mode only (no demo, no presentation, no simulated records).  Starts the GUI like main.py, presses
Connect all (analyzer + Master 1 + Master 2), then:
  1. Reset view sits on the LEFT of the 3D card header (before the title)
  2. the startup view (camera, label level, label positions, splitter split) is recorded; the view is then disturbed
     (plain wheel = must be ignored, Ctrl+wheel zoom, orbit, pan, fov, splitter drag, auto-rotate) and Reset view must
     bring back exactly the startup state
  3. live traffic is watched for --observe seconds: every animated transfer must name Master 1 (Pico 1) or
     Master 2 (Pico 3) - the banner never shows '?' / 'Unattributed'; Master 2 <-> OLED 0x3C transfers are counted
The window ignores mouse / keyboard (WindowTransparentForInput) so a stray click cannot spoil the run; QSettings go to a
temporary INI file (the user's registry settings are snapshotted before and compared after).
Usage:  python -X utf8 tools/verify_live_3d_reset.py [--observe 300] [--speed 0.5]
Writes work/verification/live3d_*.png and live3d_report.json.
"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
OUT = ROOT / "work" / "verification"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS = []
sys.excepthook = lambda t, v, tb: (ERRORS.append("".join(traceback.format_exception(t, v, tb))),
                                   sys.__excepthook__(t, v, tb))

from PyQt6.QtCore import QEvent, QEventLoop, QObject, QPoint, QPointF, QSettings, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat, QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def native_settings():
    s = QSettings(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, "I2CAnalyzer", "I2C3DAnalyzer")
    return {k: str(s.value(k)) for k in s.allKeys()}


NATIVE_BEFORE = native_settings()
TMP = tempfile.mkdtemp(prefix="live3d_settings_")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, TMP)

fmt = QSurfaceFormat(); fmt.setSamples(4); fmt.setDepthBufferSize(24)            # same as main.py
QSurfaceFormat.setDefaultFormat(fmt)
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
app.setApplicationName("I2C Packet Analyzer")
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
from gui.theme import STYLESHEET  # noqa: E402
app.setStyleSheet(STYLESHEET)
import config  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

OBSERVE_S = float(sys.argv[sys.argv.index("--observe") + 1]) if "--observe" in sys.argv else 300.0
SPEED = float(sys.argv[sys.argv.index("--speed") + 1]) if "--speed" in sys.argv else 0.5
HUMAN, NAVLOG = [], []


class _InputSpy(QObject):
    def eventFilter(self, obj, ev):
        if ev.spontaneous() and ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel):
            HUMAN.append((round(time.monotonic(), 1), ev.type().name, type(obj).__name__))
        return False


_spy = _InputSpy()
app.installEventFilter(_spy)
w = MainWindow()
w.resize(1680, 980)
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED LIVE VERIFICATION (input disabled)")
w.show()
assert w.demo is None and not w.btn_demo.isChecked() and w.presentation is None, "Real Hardware Mode only"
w.nav.currentRowChanged.connect(lambda r: NAVLOG.append((round(time.monotonic(), 1), r)))
w.nav.setCurrentRow(1)
w.visual_speed.setCurrentIndex(w.visual_speed.findData(SPEED))
page = w.p_net
net = page.net
view = net.view
report = dict(mode="LIVE HARDWARE", renderer="3D (OpenGL)" if net.is_3d else "2D fallback", checks={}, failures=[],
              shots=[])
fail = report["failures"]
loop = QEventLoop()


def pump(seconds, until=None, each=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        if each is not None:
            each()
        if until is not None and until():
            return True
    return until() if until is not None else True


import threading  # noqa: E402
STALLS, EVENTS = [], []                     # (monotonic, seconds the monitor thread woke late) / (monotonic, what)


def _monitor():
    while True:
        t = time.perf_counter()
        time.sleep(0.005)
        late = time.perf_counter() - t - 0.005
        if late > 0.1:
            STALLS.append((round(time.monotonic(), 2), round(late, 3)))


threading.Thread(target=_monitor, daemon=True).start()
import gc  # noqa: E402
_gc_t = {}
gc.callbacks.append(lambda ph, info: _gc_t.__setitem__("t", time.perf_counter()) if ph == "start" else
                    (info["generation"] == 2 and time.perf_counter() - _gc_t["t"] > 0.1 and
                     EVENTS.append((round(time.monotonic(), 2), f"gen2 GC {time.perf_counter() - _gc_t['t']:.3f}s"))))


def shot(tag):
    EVENTS.append((round(time.monotonic(), 2), f"shot {tag}"))
    p = OUT / f"live3d_{tag}.png"
    w.grab().save(str(p))
    report["shots"].append(p.name)


def check(name, ok, detail=""):
    report["checks"][name] = dict(ok=bool(ok), detail=str(detail)[:1500])
    if not ok:
        fail.append(f"{name}: {str(detail)[:300]}")


def camera():
    o = view.opts
    c = o["center"]
    return dict(center=(round(c.x(), 6), round(c.y(), 6), round(c.z(), 6)), distance=round(o["distance"], 6),
                elevation=round(o["elevation"], 6), azimuth=round(o["azimuth"], 6), fov=round(o["fov"], 6))


def labels():
    """Label CENTRES: a caption's text may legitimately change (e.g. 'Configured' -> 'Confirmed' once the chip ID is
    read), which changes its width but not where the layout anchors it."""
    return {str(k): (round(r.center().x()), round(r.center().y())) for k, r in net._label_rects.items()}


def wheel(delta, ctrl):
    pos = QPointF(view.width() / 2, view.height() / 2)
    ev = QWheelEvent(pos, QPointF(view.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, delta),
                     Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view, ev)


DOTS = []
_orig_spawn = net._spawn_stage_particle


def _spy_spawn(stage, txn):
    before = list(net.particles)
    _orig_spawn(stage, txn)
    for q in net.particles:
        if not any(q is b for b in before):
            DOTS.append(dict(p=q, stage=stage, txn=txn, mid=getattr(txn, "master_id", None)))


net._spawn_stage_particle = _spy_spawn
M2_OLED_SHOT = []

# ------------------------------------------------------------------ 1. Connect all (real hardware)
import serial as _serial  # noqa: E402
BACKLOG = []                                # (monotonic, bytes queued in the driver when the reader read)
_orig_read = _serial.Serial.read


def _read(self, size=1):
    if self.port and self.port.upper() == str(w.port.currentData() or "").upper():
        q = self.in_waiting
        if q > 6000:
            BACKLOG.append((round(time.monotonic(), 2), q))
    return _orig_read(self, size)


_serial.Serial.read = _read
_orig_te = w.engine.transport_error


def _te(msg):
    EVENTS.append((round(time.monotonic(), 2), "FRAMING " + msg[:60]))
    _orig_te(msg)


w.engine.transport_error = _te
report["ports"] = {"analyzer": w.port.currentData(), **{f"master{m}": c.currentData() for m, c in w.master_ports.items()}}
w.btn_connect_all.click()
locked = pump(30, lambda: w.capturing and all(s.clock.locked for s in w.engine.masters.state.values()))
report["connect_all"] = dict(capturing=w.capturing, masters=[(r["master_id"], r["state"], r["clock"])
                                                             for r in w.engine.masters.rows()])
check("Connect all: capturing, both masters clock-aligned", w.capturing and locked, report["connect_all"])
pump(3)

# ------------------------------------------------------------------ 2. Reset view on the left
btn = page.btn_reset
card = btn.parentWidget()
bx = btn.mapTo(card, QPoint(0, 0)).x()
title_x = card.title.mapTo(card, QPoint(0, 0)).x()
report["reset_button"] = dict(x_in_card=bx, title_x=title_x, card_w=card.width(), text=btn.text())
check("Reset view is on the LEFT of the 3D header (before the title)", bx < title_x and bx < 0.1 * card.width(),
      report["reset_button"])

# ------------------------------------------------------------------ 3. startup state, disturb, Reset view
base = dict(camera=camera(), level=net.label_level, split=page.split.sizes(), labels=labels(),
            initial=dict((k, (str(v) if k == "center" else v)) for k, v in (net._initial_camera or {}).items()))
report["startup"] = base
check("startup camera saved on first fit (elevation 62, azimuth -90, fov 60)",
      net._initial_camera is not None and base["camera"]["elevation"] == 62 and base["camera"]["azimuth"] == -90
      and base["camera"]["fov"] == 60, base)
shot("1_startup")
d0 = view.opts["distance"]
wheel(-360, ctrl=False)                                       # an accidental scroll while hovering
pump(0.3)
check("plain wheel over the view is ignored (no accidental zoom)", view.opts["distance"] == d0 and not view.user_view,
      (d0, view.opts["distance"]))
wheel(-480, ctrl=True)                                        # deliberate Ctrl + wheel zoom
pump(0.3)
check("Ctrl + wheel zooms", view.opts["distance"] != d0, (d0, view.opts["distance"]))
view.orbit(35, 12)
view.pan(4, -3, 0)
view.opts["fov"] = 45
view.user_view = True
page.split.setSizes([700, 740])
page.rot.setChecked(True)
pump(2.5)
disturbed = dict(camera=camera(), split=page.split.sizes(), labels=labels())
report["disturbed"] = disturbed
shot("2_disturbed")
check("view really disturbed before Reset", disturbed["camera"] != base["camera"] and disturbed["split"] != base["split"],
      disturbed)
btn.click()
pump(1.5)
after = dict(camera=camera(), level=net.label_level, split=page.split.sizes(), labels=labels(),
             rot=page.rot.isChecked(), auto_rotate=net.auto_rotate, user_view=view.user_view)
report["after_reset"] = after
shot("3_after_reset")
check("Reset: camera == startup camera (centre, distance, elevation, azimuth, fov)", after["camera"] == base["camera"],
      (base["camera"], after["camera"]))
check("Reset: splitter == startup split", after["split"] == base["split"], (base["split"], after["split"]))
check("Reset: label level and every board / label position == startup",
      after["level"] == base["level"] and after["labels"] == base["labels"],
      {k: (base["labels"].get(k), v) for k, v in after["labels"].items() if base["labels"].get(k) != v})
check("Reset: auto-rotate off, user view cleared", not after["rot"] and not after["auto_rotate"] and not after["user_view"],
      after)
pump(3)
check("Reset view stays put (no drift 3 s later)", camera() == base["camera"] and labels() == base["labels"],
      (camera(), base["camera"]))

# ------------------------------------------------------------------ 4. live traffic: every animated transfer named
played, banners, bad = [], set(), []


def watch():
    t = w.playback.txn
    st = w.playback.current
    if (t is not None and t.master_id == 2 and t.address == 0x3C and st is not None and st.kind == "data"
            and w.playback.stage_fraction > 0.5 and not M2_OLED_SHOT):
        M2_OLED_SHOT.append(t.id)
        shot("5_m2_oled_write")
    if t is not None and (not played or played[-1] is not t):
        played.append(t)
    b = net.phase_panel.text
    if b and not b.startswith("IDLE"):
        head = b.split("]")[0] + "]"
        banners.add(head)
        if "?" in head or "Unattributed" in head or "unattributed" in head:
            bad.append(b)


t_obs = time.monotonic()
k = 0
m2_oled_done = lambda: any(t.master_id == 2 and t.address == 0x3C for t in played[:-1])   # fully played
while time.monotonic() - t_obs < OBSERVE_S and not (m2_oled_done() and time.monotonic() - t_obs > 330):
    pump(1.0, each=watch)
    if time.monotonic() - t_obs > 60 * (k + 1):
        k += 1
        shot(f"4_live_{k}")
outside = [h for h in HUMAN if h[0] >= t_obs]
pairs = {}
for t in played:
    key = f"{config.MASTERS.get(t.master_id, {}).get('label', 'unattributed')} 0x{(t.address or 0):02X} {t.operation}"
    pairs[key] = pairs.get(key, 0) + 1
pk = w.engine.packets
report["live"] = dict(observe_s=OBSERVE_S, speed=SPEED, animated_transfers=len(played), banner_heads=sorted(banners),
                      animated_pairs=pairs, engine_packets=len(pk),
                      engine_unattributed=sum(1 for t in pk if t.master_id is None),
                      engine_by_master={str(m): sum(1 for t in pk if t.master_id == m) for m in (1, 2, None)},
                      m2_oled_packets=sum(1 for t in pk if t.master_id == 2 and t.address == 0x3C),
                      m1_oled_packets=sum(1 for t in pk if t.master_id == 1 and t.address == 0x3C),
                      outside_input=outside[:10], page_changes=[n for n in NAVLOG if n[0] >= t_obs])
check("every animated transfer names Master 1 (Pico 1) or Master 2 (Pico 3) - never '?' / unattributed",
      played and not bad and all(t.master_id in (1, 2) for t in played), bad[:5])
check("both masters animated live", any(t.master_id == 1 for t in played) and any(t.master_id == 2 for t in played),
      pairs)
check("observation undisturbed (no outside input, no page change)", not outside and not report["live"]["page_changes"],
      (outside[:5], report["live"]["page_changes"][:5]))
check("no dot cut", net.particles_cut == 0, net.particles_cut)
h = w.engine.health_summary()
T_START = t_obs
report["timeline"] = dict(observe_start=round(t_obs, 2), stalls_over_0_1s=STALLS[-40:], events=EVENTS[-80:],
                          backlog_over_6kB=BACKLOG[-200:], backlog_max=max((q for _t, q in BACKLOG), default=0))
report["health"] = dict(state=h["state"], reason=h.get("reason"), transport_errors=w.engine.transport_errors,
                        lost_records=w.engine.lost_records, fw=w.engine.fw_stats, hud=net.hud_row2.text()[:200],
                        capture_s=round(time.monotonic() - t_obs, 1))
check("long live capture stays trustworthy: Health HEALTHY, 0 framing errors, 0 lost records",
      h["state"] == "HEALTHY" and not w.engine.transport_errors and not w.engine.lost_records, report["health"])
# Master 2 (Pico 3) -> OLED 0x3C (firmware 2.1): a WRITE, animated on Master 2's own wire both ways
m2o = [t for t in played if t.master_id == 2 and t.address == 0x3C]
dots = [d for d in DOTS if d["mid"] == 2 and d["txn"].address == 0x3C and not d["p"].partial]
wrong = []
for d in dots:
    st = d["stage"]
    sends = st.kind == "cond" or st.direction == "W"
    want = ("master2", 0x3C) if sends else (0x3C, "master2")
    if (d["p"].src, d["p"].dst) != want:
        wrong.append(f"{st.text}: {d['p'].src}->{d['p'].dst}")
report["m2_oled"] = dict(animated=len(m2o), operations=sorted({t.operation for t in m2o}), dots=len(dots),
                         m2_to_oled=sum(1 for d in dots if d["stage"].kind == "cond" or d["stage"].direction == "W"),
                         oled_to_m2=sum(1 for d in dots if not (d["stage"].kind == "cond" or d["stage"].direction == "W")),
                         wrong=wrong[:10], arrived=all(d["p"].done for d in dots))
check("Master 2 (Pico 3) -> OLED 0x3C animated live as a WRITE", m2o and all(t.operation == "WRITE" for t in m2o),
      report["m2_oled"])
check("Master 2 <-> OLED dots run between Master 2 and the OLED (data M2->OLED, ACKs OLED->M2), all arrived",
      dots and not wrong and report["m2_oled"]["m2_to_oled"] and report["m2_oled"]["oled_to_m2"]
      and report["m2_oled"]["arrived"], report["m2_oled"])

# ------------------------------------------------------------------ 5. shutdown
w.btn_connect_all.click() if w.btn_connect_all.text() == "Disconnect all" else None
pump(2)
w.close()
pump(0.5)
report["python_errors"] = ERRORS
check("no Python errors", not ERRORS, ERRORS[:2])
report["engine_errors"] = [f"{e.category} {e.kind}: {e.message}" for e in list(w.engine.events)
                           if e.severity == "error"][-10:]
native_after = native_settings()
check("user's native QSettings untouched", native_after == NATIVE_BEFORE,
      {k: (NATIVE_BEFORE.get(k), native_after.get(k)) for k in set(NATIVE_BEFORE) | set(native_after)
       if NATIVE_BEFORE.get(k) != native_after.get(k)})
report["result"] = "PASS" if not fail else "FAIL"
(OUT / "live3d_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: report[k] for k in ("result", "failures", "renderer", "live")}, indent=2, default=str,
                 ensure_ascii=False))
sys.stdout.flush()
os._exit(0 if not fail else 1)
