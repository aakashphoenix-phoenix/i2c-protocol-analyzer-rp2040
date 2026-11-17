"""Interactive I²C network view.

3D (pyqtgraph.opengl on QOpenGLWidget) with a 2D QPainter fallback. Pulses are
spawned ONLY from decoded transactions handed in by the capture engine; nothing
animates on its own in live mode.
"""
from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QVector3D
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QToolTip, QVBoxLayout, QWidget

from config import ANALYZER_LABEL, COLORS as C, MASTER_LABEL
from gui.widgets import MOTION

try:
    import pyqtgraph.opengl as gl
    from pyqtgraph.opengl import MeshData
    HAVE_GL = True
except Exception:          # PyOpenGL missing or broken driver
    HAVE_GL = False

SLAVE_Y = -4.2
ANALYZER_Y = 4.6
RAIL_Z = 0.25
SDA_OFF, SCL_OFF = -0.18, 0.18


def rgba(hex_color, a=1.0, k=1.0):
    q = QColor(hex_color)
    return (min(1, q.redF() * k), min(1, q.greenF() * k), min(1, q.blueF() * k), a)


def kind_for(addr, name):
    n = (name or "").lower()
    if "oled" in n or "ssd1306" in n:
        return "oled"
    if "ds3231" in n or "rtc" in n:
        return "rtc"
    if "bmp" in n or "bme" in n:
        return "bmp"
    return "unknown"


KIND_STYLE = {   # board size (x, y), pcb colour
    "pico": ((2.1, 5.1), "#1E7A46"),
    "rtc": ((3.8, 2.2), "#1F4FA3"),
    "bmp": ((1.6, 1.2), "#5E2C8C"),
    "oled": ((2.7, 2.8), "#123C70"),
    "unknown": ((2.0, 1.6), "#4A566B"),
}


class Particle:
    __slots__ = ("path", "seg_len", "total", "s", "speed", "color")

    def __init__(self, path, color, speed):
        self.path = np.asarray(path, dtype=float)
        d = np.diff(self.path, axis=0)
        self.seg_len = np.sqrt((d ** 2).sum(axis=1))
        self.total = float(self.seg_len.sum()) or 1.0
        self.s = 0.0
        self.speed = speed
        self.color = color

    def pos(self):
        s = self.s
        for i, L in enumerate(self.seg_len):
            if s <= L or i == len(self.seg_len) - 1:
                f = 0 if L == 0 else min(1.0, s / L)
                return self.path[i] + f * (self.path[i + 1] - self.path[i])
            s -= L
        return self.path[-1]


class NodeModel:
    """Shared layout logic for both renderers."""

    def __init__(self):
        self.slaves: dict[int, dict] = {}     # addr -> {name, conf, status, x, kind}
        self.order: list[int] = []

    def update(self, devices):
        """devices: list of (addr, name, confidence, status). Returns True if layout changed."""
        changed = False
        for addr, name, conf, status in devices:
            if addr not in self.slaves:
                self.slaves[addr] = {"name": name, "conf": conf, "status": status, "kind": kind_for(addr, name)}
                self.order.append(addr)
                changed = True
            else:
                s = self.slaves[addr]
                if s["name"] != name:
                    s["kind"] = kind_for(addr, name)
                    changed = True
                if s["status"] != status or s["conf"] != conf:
                    changed = True
                s.update(name=name, conf=conf, status=status)
        if changed:
            # configured devices first in a fixed order, then discovered ones by address
            fixed = [a for a in (0x68, 0x76, 0x77, 0x3C, 0x3D) if a in self.slaves]
            rest = sorted(a for a in self.slaves if a not in fixed)
            self.order = fixed + rest
            for i, a in enumerate(self.order):
                self.slaves[a]["x"] = -3.0 + i * 4.6
        return changed

    @property
    def rail_end(self):
        return max([-3.0 + 4.6 * max(len(self.order) - 1, 2)]) + 2.5

    def analyzer_x(self):
        return (-9.0 + self.rail_end) / 2 + 1.0

    def path_to(self, addr, line_off):
        x = self.slaves[addr]["x"] + line_off
        return [(-8.0, line_off, RAIL_Z), (x, line_off, RAIL_Z), (x, SLAVE_Y + 1.3, RAIL_Z)]


# ============================================================================ 3D
if HAVE_GL:
    class _GLView(gl.GLViewWidget):
        hovered = pyqtSignal(object, QPointF)
        clicked = pyqtSignal(object)

        def __init__(self, owner):
            super().__init__()
            self.owner = owner
            self._press = None
            self.setMouseTracking(True)

        def project(self, p):
            viewport = self.getViewport()
            m = self.projectionMatrix(viewport, viewport) * self.viewMatrix()
            v = m.map(QVector3D(*p))
            return QPointF(
                (v.x() + 1) / 2 * self.width(),
                (1 - v.y()) / 2 * self.height()
            )


        def pick(self, pos):
            best, bd = None, 46.0
            for key, p in self.owner.anchor_points().items():
                sp = self.project(p)
                d = math.hypot(sp.x() - pos.x(), sp.y() - pos.y())
                if d < bd:
                    best, bd = key, d
            return best

        def mousePressEvent(self, ev):
            self._press = ev.position()
            super().mousePressEvent(ev)

        def mouseReleaseEvent(self, ev):
            super().mouseReleaseEvent(ev)
            if self._press is not None and (ev.position() - self._press).manhattanLength() < 5:
                self.clicked.emit(self.pick(ev.position()))
            self._press = None

        def mouseMoveEvent(self, ev):
            super().mouseMoveEvent(ev)
            if not ev.buttons():
                self.hovered.emit(self.pick(ev.position()), ev.globalPosition())


def box_mesh(sx, sy, sz):
    x, y, z = sx / 2, sy / 2, sz
    v = np.array([[-x, -y, 0], [x, -y, 0], [x, y, 0], [-x, y, 0],
                  [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]], dtype=float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return MeshData(vertexes=v, faces=f)


class Network3D(QWidget):
    node_clicked = pyqtSignal(object)      # address int, "master", "analyzer" or None

    def __init__(self, prefer_3d=True, compact=False, parent=None):
        super().__init__(parent)
        self.model = NodeModel()
        self.particles: list = []
        self.flash: dict = {}
        self.auto_rotate = False
        self.active = True
        self.selected = None
        self._last_spawn = {}
        self.use_gl = HAVE_GL and prefer_3d
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        if self.use_gl:
            self.view = _GLView(self)
            self.view.setBackgroundColor(C["bg"])
            self.view.setCameraPosition(distance=24 if not compact else 23, elevation=34, azimuth=-64)
            self.view.opts["center"] = QVector3D(1.5, 0, 0)
            self.view.hovered.connect(self._hover)
            self.view.clicked.connect(self.node_clicked.emit)
            self.view.setMinimumHeight(240)
            lay.addWidget(self.view, 1)
            self._items: dict = {}
            self._dynamic: list = []
            self._build_static()
        else:
            self.view = Topology2D(self)
            self.view.clicked.connect(self.node_clicked.emit)
            lay.addWidget(self.view, 1)
        legend = QHBoxLayout()
        legend.setSpacing(14)
        for txt, col in (("SDA", C["sda"]), ("SCL", C["scl"]), ("READ pulse", C["cyan"]),
                         ("WRITE pulse", C["blue"]), ("NACK", C["amber"])):
            lb = QLabel(f"<span style='color:{col}'>━━</span> {txt}")
            lb.setObjectName("cardHint")
            legend.addWidget(lb)
        legend.addStretch(1)
        note = QLabel("Light pulses mark decoded packet events — they are not the electrical waveform.")
        note.setObjectName("cardHint")
        note.setWordWrap(True)
        lay.addLayout(legend)
        if not compact:
            lay.addWidget(note)
        else:
            self.setToolTip(note.text())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    # ------------------------------------------------------------------ API
    @property
    def is_3d(self):
        return self.use_gl

    def set_active(self, on: bool):
        self.active = on

    def set_auto_rotate(self, on: bool):
        self.auto_rotate = on

    def set_selected(self, key):
        self.selected = key
        if self.use_gl:
            self._rebuild_dynamic()
        else:
            self.view.update()

    def set_devices(self, devices):
        if self.model.update(devices):
            if self.use_gl:
                self._rebuild_dynamic()
        if not self.use_gl:
            self.view.update()

    def pulse(self, addr, rw: str, nack: bool):
        import time as _t
        now = _t.monotonic()
        last = self._last_spawn.get(addr, 0)
        if addr not in self.model.slaves or not MOTION["animations"] or (now - last < 0.35 and not nack):
            self.flash[addr] = 1.0          # still flash the node for every observed packet
            return
        self._last_spawn[addr] = now
        col = C["amber"] if nack else (C["cyan"] if rw == "R" else C["blue"])
        speed = 0.9 if MOTION["reduced"] else 0.55
        if len(self.particles) < 120:
            for off in (SDA_OFF, SCL_OFF):
                path = self.model.path_to(addr, off)
                if rw == "R" and not nack:
                    path = path[::-1]
                self.particles.append(Particle(path, col, speed))
        self.flash[addr] = 1.0
        self.flash["analyzer"] = 0.6

    def anchor_points(self):
        pts = {"master": (-9.0, 0, 0.6), "analyzer": (self.model.analyzer_x(), ANALYZER_Y, 0.6)}
        for a, s in self.model.slaves.items():
            pts[a] = (s["x"], SLAVE_Y, 0.6)
        return pts

    def describe(self, key):
        if key == "master":
            return f"<b>{MASTER_LABEL}</b><br>Bus controller · I²C has no master address<br>SDA GP4 · SCL GP5"
        if key == "analyzer":
            return (f"<b>{ANALYZER_LABEL}</b><br>Passive tap on GP2 (SDA) / GP3 (SCL)<br>"
                    "Observes only — never drives the bus")
        s = self.model.slaves.get(key)
        if not s:
            return ""
        return (f"<b>{s['name']}</b><br>Address 0x{key:02X} · name {s['conf'].lower()}<br>"
                f"Status: {s['status']}<br><i>click to filter packets</i>")

    def _hover(self, key, gpos):
        if key is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(gpos.toPoint(), self.describe(key), self)

    # ------------------------------------------------------------------ 3D scene
    def _add(self, item, dynamic=False):
        self.view.addItem(item)
        if dynamic:
            self._dynamic.append(item)
        return item

    def _board(self, kind, x, y, highlight=False, dynamic=True, rot90=False):
        (sx, sy), pcb = KIND_STYLE[kind]
        if rot90:
            sx, sy = sy, sx
        items = []
        shadow = gl.GLMeshItem(meshdata=box_mesh(sx + 0.5, sy + 0.5, 0.01), color=(0, 0, 0, 0.45),
                               smooth=False, shader=None, glOptions="translucent")
        shadow.translate(x + 0.25, y - 0.25, 0.005)
        items.append(shadow)
        k = 1.5 if highlight else 1.0
        board = gl.GLMeshItem(meshdata=box_mesh(sx, sy, 0.16), color=rgba(pcb, 1, k), smooth=False, shader="shaded")
        board.translate(x, y, 0.05)
        items.append(board)
        hx, hy, zt = sx / 2, sy / 2, 0.215          # crisp outline of the top face
        outline = gl.GLLinePlotItem(pos=np.array([[x - hx, y - hy, zt], [x + hx, y - hy, zt], [x + hx, y + hy, zt],
                                                  [x - hx, y + hy, zt], [x - hx, y - hy, zt]]),
                                    color=rgba(C["cyan"] if highlight else pcb, 1, 1.8), width=2 if highlight else 1.2,
                                    antialias=True)
        items.append(outline)
        if kind == "pico":
            chip = gl.GLMeshItem(meshdata=box_mesh(0.7, 0.7, 0.12), color=(0.08, 0.08, 0.1, 1), shader="shaded")
            chip.translate(x, y, 0.21)
            usb = gl.GLMeshItem(meshdata=box_mesh(0.8 if not rot90 else 0.55, 0.55 if not rot90 else 0.8, 0.28),
                                color=(0.75, 0.78, 0.82, 1), shader="shaded")
            usb.translate(x, y + (sy / 2 - 0.25) if not rot90 else y, 0.21)
            if rot90:
                usb.resetTransform()
                usb.translate(x - (sx / 2 - 0.25), y, 0.21)
            items += [chip, usb]
            for i in range(10):
                for side in (-1, 1):
                    pin = gl.GLMeshItem(meshdata=box_mesh(0.12, 0.12, 0.05), color=(0.85, 0.7, 0.3, 1), shader="shaded")
                    if rot90:
                        pin.translate(x - sx / 2 + 0.3 + i * (sx - 0.6) / 9, y + side * (sy / 2 - 0.12), 0.21)
                    else:
                        pin.translate(x + side * (sx / 2 - 0.12), y - sy / 2 + 0.3 + i * (sy - 0.6) / 9, 0.21)
                    items.append(pin)
        elif kind == "rtc":
            bat = gl.GLMeshItem(meshdata=MeshData.cylinder(rows=1, cols=28, radius=[0.85, 0.85], length=0.3),
                                color=(0.78, 0.8, 0.84, 1), shader="shaded", smooth=True)
            bat.translate(x + 0.8, y, 0.21)
            chip = gl.GLMeshItem(meshdata=box_mesh(0.9, 0.6, 0.12), color=(0.07, 0.07, 0.09, 1), shader="shaded")
            chip.translate(x - 1.0, y, 0.21)
            items += [bat, chip]
        elif kind == "bmp":
            chip = gl.GLMeshItem(meshdata=box_mesh(0.3, 0.25, 0.1), color=(0.8, 0.82, 0.86, 1), shader="shaded")
            chip.translate(x, y, 0.21)
            items.append(chip)
        elif kind == "oled":
            glass = gl.GLMeshItem(meshdata=box_mesh(2.5, 1.7, 0.06), color=(0.02, 0.04, 0.08, 1), shader="shaded")
            glass.translate(x, y - 0.25, 0.21)
            items.append(glass)
            for r in range(3):
                ln = gl.GLLinePlotItem(pos=np.array([[x - 0.9, y - 0.75 + r * 0.42, 0.29],
                                                     [x + 0.4 - r * 0.25, y - 0.75 + r * 0.42, 0.29]]),
                                       color=rgba(C["cyan"], 0.85), width=2, antialias=True)
                items.append(ln)
        else:
            chip = gl.GLMeshItem(meshdata=box_mesh(0.5, 0.5, 0.1), color=(0.1, 0.1, 0.12, 1), shader="shaded")
            chip.translate(x, y, 0.21)
            items.append(chip)
        for it in items:
            self._add(it, dynamic)
        return board

    def _label(self, x, y, z, text, color, size=11, dynamic=True, bold=False):
        f = QFont("Segoe UI", size)
        f.setBold(bold)
        t = gl.GLTextItem(pos=(x, y, z), text=text, color=QColor(color), font=f)
        t.setDepthValue(10)          # paint after the meshes so labels stay on top
        return self._add(t, dynamic)

    def _line(self, pts, color, width=2.0, alpha=1.0, dynamic=True, mode="line_strip"):
        it = gl.GLLinePlotItem(pos=np.asarray(pts, dtype=float), color=rgba(color, alpha), width=width,
                               antialias=True, mode=mode)
        return self._add(it, dynamic)

    def _build_static(self):
        g = gl.GLGridItem()
        g.setSize(60, 40)
        g.setSpacing(1, 1)
        g.setColor((50, 80, 130, 70))
        self._add(g)
        self.scatter = gl.GLScatterPlotItem(pos=np.zeros((1, 3)), size=0.0, pxMode=True)
        self.scatter.setGLOptions("additive")
        self._add(self.scatter)
        self._rebuild_dynamic()

    def _rebuild_dynamic(self):
        for it in self._dynamic:
            self.view.removeItem(it)
        self._dynamic.clear()
        self._boards = {}
        m = self.model
        end = m.rail_end
        # ---- rails (with halo)
        for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
            self._line([(-8.0, off, RAIL_Z), (end, off, RAIL_Z)], col, 7, 0.16)
            self._line([(-8.0, off, RAIL_Z), (end, off, RAIL_Z)], col, 2.4, 0.95)
        self._label(end + 0.3, SDA_OFF - 0.4, RAIL_Z, "SDA", C["sda"], 10)
        self._label(end + 0.3, SCL_OFF + 0.2, RAIL_Z, "SCL", C["scl"], 10)
        # ---- master
        self._boards["master"] = self._board("pico", -9.0, 0, self.selected == "master", rot90=False)
        for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
            self._line([(-7.95, off, RAIL_Z), (-8.0, off, RAIL_Z)], col, 2.4)
        self._label(-10.6, -3.2, 1.5, "Pico 1 · MASTER", C["text"], 11, bold=True)
        self._label(-10.6, -3.2, 0.7, "GP4 SDA · GP5 SCL", C["text2"], 9)
        # ---- slaves
        for a in m.order:
            s = m.slaves[a]
            x = s["x"]
            col_dim = 0.35 if s["status"] in ("Not responding", "No device (NACK)") else 0.9
            for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
                self._line([(x + off, off, RAIL_Z), (x + off, SLAVE_Y + 1.3, RAIL_Z)], col, 2.0, col_dim)
            for off in (SDA_OFF, SCL_OFF):          # junction dots
                dot = gl.GLScatterPlotItem(pos=np.array([[x + off, off, RAIL_Z]]), size=8, color=(1, 1, 1, 0.9), pxMode=True)
                self._add(dot, True)
            self._boards[a] = self._board(s["kind"], x, SLAVE_Y, self.selected == a)
            self._label(x - 1.7, SLAVE_Y - 2.0, 1.1, s["name"][:22], C["text"], 11, bold=True)
            conf_col = {"Confirmed": C["green"], "Configured": C["cyan"], "Likely": C["amber"]}.get(s["conf"], C["red"])
            self._label(x - 1.7, SLAVE_Y - 2.0, 0.25, f"0x{a:02X} · {s['conf']} · {s['status']}", conf_col, 9)
        # ---- analyzer in parallel (dashed taps)
        ax = m.analyzer_x()
        self._boards["analyzer"] = self._board("pico", ax, ANALYZER_Y, self.selected == "analyzer", rot90=True)
        for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
            pts = []
            y0, y1 = off, ANALYZER_Y - 1.1
            n = 14
            for i in range(n):
                if i % 2 == 0:
                    ya = y0 + (y1 - y0) * i / n
                    yb = y0 + (y1 - y0) * (i + 1) / n
                    pts += [(ax + off * 3, ya, RAIL_Z), (ax + off * 3, yb, RAIL_Z)]
            self._line(pts, col, 1.8, 0.85, mode="lines")
        self._label(ax - 2.4, ANALYZER_Y + 1.6, 1.9, "Pico 2 · PASSIVE ANALYZER", C["text"], 11, bold=True)
        self._label(ax - 2.4, ANALYZER_Y + 1.6, 1.1, "GP2 SDA · GP3 SCL · USB → PC", C["text2"], 9)

    def _tick(self):
        if not self.active or not self.isVisible():
            return
        dt = 0.033
        moving = False
        if self.particles:
            moving = True
            alive = []
            for p in self.particles:
                p.s += p.speed * dt * p.total * 2.2
                if p.s < p.total:
                    alive.append(p)
            self.particles = alive
        for k in list(self.flash):
            self.flash[k] -= dt * 1.8
            if self.flash[k] <= 0:
                del self.flash[k]
        if self.use_gl:
            if self.particles:
                pos = np.array([p.pos() for p in self.particles])
                cols = np.array([rgba(p.color) for p in self.particles])
                self.scatter.setData(pos=pos, color=cols, size=11)
            elif moving:
                self.scatter.setData(pos=np.zeros((1, 3)), size=0.0)
            for key, board in getattr(self, "_boards", {}).items():
                f = self.flash.get(key, 0)
                kind = "pico" if key in ("master", "analyzer") else self.model.slaves[key]["kind"]
                base = KIND_STYLE[kind][1]
                if f > 0 or getattr(board, "_lit", False):
                    if f > 0:
                        board.setColor(rgba(base, 1, 1 + 1.6 * f))
                        board._lit = True
                    else:
                        board.setColor(rgba(base, 1, 1.5 if self.selected == key else 1.0))
                        board._lit = False
            if self.auto_rotate and MOTION["animations"] and not MOTION["reduced"]:
                self.view.orbit(0.25, 0)
        else:
            if moving or self.flash:
                self.view.update()


# ============================================================================ 2D fallback
class Topology2D(QWidget):
    clicked = pyqtSignal(object)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMouseTracking(True)
        self.setMinimumHeight(240)
        self._rects = {}

    def _map(self, x, y):
        m = self.owner.model
        x0, x1 = -11.5, m.rail_end + 2.5
        y0, y1 = ANALYZER_Y + 2.2, SLAVE_Y - 3.4
        w, h = self.width(), self.height()
        return QPointF((x - x0) / (x1 - x0) * w, (y - y0) / (y1 - y0) * h)

    def paintEvent(self, e):
        o = self.owner
        m = o.model
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["bg"]))
        p.setPen(QPen(QColor(30, 45, 75), 1))
        for gx in range(0, self.width(), 28):
            p.drawLine(gx, 0, gx, self.height())
        for gy in range(0, self.height(), 28):
            p.drawLine(0, gy, self.width(), gy)
        end = m.rail_end
        for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
            p.setPen(QPen(QColor(col), 2.4))
            p.drawLine(self._map(-8, off * 2.5), self._map(end, off * 2.5))
        self._rects = {}

        def node(key, x, y, w, h, title, sub, color, sel):
            a, b = self._map(x - w / 2, y + h / 2), self._map(x + w / 2, y - h / 2)
            r = QRectF(a, b).normalized()
            f = o.flash.get(key, 0)
            fill = QColor(color)
            fill = fill.lighter(int(100 + 80 * f))
            p.setPen(QPen(QColor(C["cyan"] if sel else C["border"]), 2 if sel else 1))
            p.setBrush(fill)
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QColor(C["text"]))
            p.drawText(r.adjusted(4, 4, -4, -r.height() / 2), Qt.AlignmentFlag.AlignCenter, title)
            p.setPen(QColor(C["text2"]))
            p.drawText(r.adjusted(4, r.height() / 2, -4, -4), Qt.AlignmentFlag.AlignCenter, sub)
            self._rects[key] = r

        node("master", -9.0, 0, 3.4, 2.8, "Pico 1 · Master", "GP4 / GP5", "#17402E", o.selected == "master")
        for a in m.order:
            s = m.slaves[a]
            for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
                p.setPen(QPen(QColor(col), 1.8))
                p.drawLine(self._map(s["x"] + off * 2.5, off * 2.5), self._map(s["x"] + off * 2.5, SLAVE_Y + 1.4))
            node(a, s["x"], SLAVE_Y, 3.6, 2.6, s["name"][:20], f"0x{a:02X} · {s['conf']}",
                 KIND_STYLE[s["kind"]][1], o.selected == a)
        ax = m.analyzer_x()
        for off, col in ((SDA_OFF, C["sda"]), (SCL_OFF, C["scl"])):
            pen = QPen(QColor(col), 1.6)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(self._map(ax + off * 3, off * 2.5), self._map(ax + off * 3, ANALYZER_Y - 1.3))
        node("analyzer", ax, ANALYZER_Y, 5.0, 2.4, "Pico 2 · Passive analyzer", "GP2 / GP3 · USB", "#17402E",
             o.selected == "analyzer")
        for pt in o.particles:
            x, y, _ = pt.pos()
            q = self._map(x, y * 2.5 if abs(y) < 1 else y)
            c = QColor(pt.color)
            p.setPen(Qt.PenStyle.NoPen)
            halo = QColor(c)
            halo.setAlpha(70)
            p.setBrush(halo)
            p.drawEllipse(q, 9, 9)
            p.setBrush(c)
            p.drawEllipse(q, 4, 4)

    def _at(self, pos):
        for k, r in self._rects.items():
            if r.contains(pos):
                return k
        return None

    def mouseMoveEvent(self, e):
        k = self._at(e.position())
        if k is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(e.globalPosition().toPoint(), self.owner.describe(k), self)

    def mouseReleaseEvent(self, e):
        self.clicked.emit(self._at(e.position()))
