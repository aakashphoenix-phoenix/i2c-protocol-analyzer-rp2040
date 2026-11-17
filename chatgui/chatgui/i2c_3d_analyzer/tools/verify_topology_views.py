"""Render the real GUI (demo records, no serial port) at two window sizes and
check that both topology diagrams fit: every board, line end and label lies
inside its viewport and no two labels overlap.  Screenshots are written to
work/verification/topology_*.png.  Usage:  python -X utf8 tools/verify_topology_views.py [--2d]
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from PyQt6.QtGui import QSurfaceFormat
_fmt = QSurfaceFormat(); _fmt.setSamples(4); _fmt.setDepthBufferSize(24)   # same as main.py
QSurfaceFormat.setDefaultFormat(_fmt)
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
w.engine.set_source('demo')
g = DemoGenerator(seed=17)
w.engine.ingest_lines([g.hello()])
for k in range(1, 241):            # 12 s of simulated capture (10 s activity window)
    w.engine.ingest_lines(g.lines_until(k * 0.05))
w.packet_model.sync()
txn = next(t for t in reversed(w.engine.packets) if t.address == 0x68)
w._set_state()
w.select_transaction(txn)
w.focus_device(0x76)               # device filter + amber focus ring in every 3D view
w.timer.stop()
out = ROOT / "work" / "verification"
out.mkdir(parents=True, exist_ok=True)
SIZES = [("normal", 1680, 980), ("small", 1280, 760)]
PAGES = [(0, "dashboard", lambda: w.p_dash.net), (1, "network", lambda: w.p_net.net),
         (6, "device_activity", lambda: w.p_dev.net)]
failures = []


def check(net, tag):
    from PyQt6.QtWidgets import QScrollArea
    v = net.view
    sa = v.parent()
    while sa is not None and not isinstance(sa, QScrollArea):
        sa = sa.parent()
    if sa is not None:
        sa.ensureWidgetVisible(v, 0, 0)
    net.reset_view() if net.is_3d else None
    app.processEvents()
    # The view must not be clipped by its card/splitter (legend or HUD painted over it).
    for sib in [c for c in net.findChildren(type(net.capture_clock)) if c.parent() is net and c.isVisible()]:
        if sib.geometry().intersects(v.geometry()):
            failures.append(tag + ":overlaps-" + sib.text()[:12])
    vis = v.visibleRegion().boundingRect()
    if vis.height() < v.height() - 2 or vis.width() < v.width() - 2:
        failures.append(tag + f":view-clipped-{vis.width()}x{vis.height()}")
    if net.is_3d:
        net._position_labels()
        rects = dict(net._label_rects)
        ok_scene = net._scene_fits()
    else:
        v.grab()                       # forces a real paint of the current size
        rects = dict(v.label_rects)
        if not rects:
            failures.append(tag + ":no-labels")
        ok_scene = True
    W, H = v.width(), v.height()
    clipped = [k for k, r in rects.items() if r.left() < 0 or r.top() < 0 or r.right() > W or r.bottom() > H]
    items = list(rects.items())
    overlap = [(a, b) for i, (a, ra) in enumerate(items) for b, rb in items[i + 1:] if ra.intersects(rb)]
    print(f"{tag}: view {W}x{H} renderer={'OpenGL' if net.is_3d else '2D'} scene_fits={ok_scene} "
          f"labels={len(rects)} clipped={clipped} overlaps={overlap}", flush=True)
    print(f"   visible region {vis.width()}x{vis.height()}", flush=True)
    if not ok_scene or clipped or overlap:
        failures.append(tag)


steps = [(s, p) for s in SIZES for p in PAGES]


def cam(net):
    o = net.view.opts
    return (round(o["distance"], 3), round(o["elevation"], 2), round(o["azimuth"], 2),
            round(o["center"].x(), 3), round(o["center"].y(), 3))


def reset_check():
    """Rotate/zoom the large view, then Reset: it must return to its own fitted
    camera while the dashboard camera stays untouched (and vice versa)."""
    big, small = w.p_net.net, w.p_dash.net
    if not (big.is_3d and small.is_3d):
        print("reset check: 2D renderer (always fitted)", flush=True)
        return
    w.nav.setCurrentRow(1); app.processEvents()
    big.reset_view(); fitted_big = cam(big)
    w.nav.setCurrentRow(0); app.processEvents()
    small.reset_view(); fitted_small = cam(small)
    big.view.orbit(40, 15); big.view.opts["distance"] *= 2.5; big.view.user_view = True
    w.p_net._reset()
    ok1 = cam(big) == fitted_big and cam(small) == fitted_small
    small.view.orbit(-30, 10); small.view.opts["distance"] *= 0.4; small.view.user_view = True
    moved = cam(small)
    small.btn_reset.click()
    ok2 = cam(small) == fitted_small and cam(big) == fitted_big and moved != fitted_small
    print(f"reset check: large restored={ok1} compact restored+independent={ok2} "
          f"large={fitted_big} compact={fitted_small}", flush=True)
    if not (ok1 and ok2):
        failures.append("reset_view")


def run():
    if not steps:
        reset_check()
        print("RESULT:", "PASS" if not failures else f"FAIL {failures}", flush=True)
        w.close(); app.quit(); return
    (size, W, H), (idx, name, net) = steps.pop(0)
    w.showNormal(); w.resize(W, H)
    w.nav.setCurrentRow(idx)
    w.pages[idx].refresh(w.engine)
    w.playback.position = min(9, len(w.playback.events) - 1)
    w._sync_playback()

    def snap():
        check(net(), f"{name}@{size}")
        w.grab().save(str(out / f"topology_{name}_{size}{'_2d' if FORCE_2D else ''}.png"))
        net().grab().save(str(out / f"topology_{name}_{size}{'_2d' if FORCE_2D else ''}_box.png"))
        run()
    QTimer.singleShot(900, snap)


w.show()
QTimer.singleShot(800, run)
app.exec()
sys.exit(1 if failures else 0)
