"""Shared visual building blocks: cards, metric tiles, status pill, plot styling."""
from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
                             QVBoxLayout, QWidget)

from config import COLORS as C

pg.setConfigOptions(antialias=True, background=C["card"], foreground=C["text2"])

MOTION = {"animations": True, "reduced": False}   # set from Settings


def fmt_si(v, unit="", digits=3):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{digits}g}{unit}"


def fmt_freq(hz):
    if hz is None:
        return "—", "kHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.3f}", "MHz"
    return f"{hz / 1e3:.2f}", "kHz"


def fmt_time_ns(ns):
    if ns is None:
        return "—"
    if ns >= 1e9:
        return f"{ns / 1e9:.3f} s"
    if ns >= 1e6:
        return f"{ns / 1e6:.3f} ms"
    if ns >= 1e3:
        return f"{ns / 1e3:.2f} µs"
    return f"{ns:.0f} ns"


def label(text="", obj=None, wrap=False):
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    return lb


class Card(QFrame):
    """Rounded, bordered panel with a soft drop shadow for depth."""

    def __init__(self, title: str = "", hint: str = "", parent=None, shadow: bool = True):
        super().__init__(parent)
        self.setObjectName("card")
        if shadow:   # never on cards holding an OpenGL view: effects break GL text and cost GPU time
            sh = QGraphicsDropShadowEffect(self)
            sh.setBlurRadius(28)
            sh.setOffset(0, 6)
            sh.setColor(QColor(0, 0, 0, 150))
            self.setGraphicsEffect(sh)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 14, 16, 14)
        self.lay.setSpacing(10)
        if title:
            row = QHBoxLayout()
            row.setSpacing(10)
            self.title = label(title, "cardTitle")
            row.addWidget(self.title)
            if hint:
                self.hint = label(hint, "cardHint")
                row.addWidget(self.hint)
            row.addStretch(1)
            self.header = row
            self.lay.addLayout(row)

    def add(self, w, stretch=0):
        if isinstance(w, QWidget):
            self.lay.addWidget(w, stretch)
        else:
            self.lay.addLayout(w, stretch)
        return w


class Sparkline(QWidget):
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.values: list = []
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, v):
        self.values = [x for x in v if x is not None and not (isinstance(x, float) and math.isnan(x))][-60:]
        self.update()

    def paintEvent(self, e):
        if len(self.values) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        lo, hi = min(self.values), max(self.values)
        rng = (hi - lo) or 1
        w, h = self.width(), self.height() - 4
        n = len(self.values)
        path = QPainterPath()
        for i, v in enumerate(self.values):
            x = i * w / (n - 1)
            y = 2 + h - (v - lo) / rng * h
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        fill = QPainterPath(path)
        fill.lineTo(w, h + 4)
        fill.lineTo(0, h + 4)
        c = QColor(self.color)
        c.setAlpha(40)
        p.fillPath(fill, c)
        p.setPen(QPen(self.color, 1.6))
        p.drawPath(path)


class MetricCard(QFrame):
    """Large live metric with unit, sub-line, sparkline, tooltip and 'unavailable' state."""

    def __init__(self, name, unit, tooltip, color=C["cyan"], parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setToolTip(tooltip)
        self.color = color
        self._glow = 0.0
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(24)
        self.shadow.setOffset(0, 5)
        self.shadow.setColor(QColor(0, 0, 0, 150))
        self.setGraphicsEffect(self.shadow)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(2)
        top = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{color}; font-size:10px;")
        top.addWidget(dot)
        top.addWidget(label(name.upper(), "metricName"))
        top.addStretch(1)
        lay.addLayout(top)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.value = label("—", "metricValue")
        self.unit = label(unit, "metricUnit")
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        self.sub = label("No measurement", "cardHint")
        lay.addWidget(self.sub)
        self.spark = Sparkline(color)
        lay.addWidget(self.spark)
        self.setMinimumWidth(190)
        self._last = None
        self.anim = QPropertyAnimation(self, b"glow", self)
        self.anim.setDuration(700)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_glow(self):
        return self._glow

    def set_glow(self, g):
        self._glow = g
        col = QColor(self.color)
        base = QColor(0, 0, 0, 150)
        a = g
        mix = QColor(int(base.red() * (1 - a) + col.red() * a), int(base.green() * (1 - a) + col.green() * a),
                     int(base.blue() * (1 - a) + col.blue() * a), int(150 * (1 - a) + 200 * a))
        self.shadow.setColor(mix)
        self.shadow.setBlurRadius(24 + 14 * g)

    glow = pyqtProperty(float, get_glow, set_glow)

    def set(self, value: str | None, sub: str = "", unit: str | None = None, color: str | None = None):
        if value is None:
            self.value.setText("—")
            self.value.setStyleSheet(f"color:{C['gray']};")
            self.sub.setText(sub or "Unavailable — no measurement data")
            return
        self.value.setStyleSheet(f"color:{color or C['text']};")
        if unit is not None:
            self.unit.setText(unit)
        if value != self._last and self._last not in (None, "—") and MOTION["animations"] and not MOTION["reduced"]:
            self.anim.stop()
            self.anim.setStartValue(0.9)
            self.anim.setEndValue(0.0)
            self.anim.start()
        self._last = value
        self.value.setText(value)
        self.sub.setText(sub)


class StatusPill(QWidget):
    """Animated connection indicator."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = "Disconnected"
        self.color = QColor(C["gray"])
        self.pulse = False
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)
        self.setFixedHeight(30)
        self.setMinimumWidth(170)

    def set_state(self, text, color, pulse=False):
        self.text, self.color, self.pulse = text, QColor(color), pulse
        fm = self.fontMetrics()
        self.setMinimumWidth(fm.horizontalAdvance(text) + 46)
        self.update()

    def _tick(self):
        if self.pulse and MOTION["animations"] and not MOTION["reduced"]:
            self.phase = (self.phase + 0.06) % 1.0
            self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        bg = QColor(self.color)
        bg.setAlpha(28)
        p.setBrush(bg)
        bd = QColor(self.color)
        bd.setAlpha(140)
        p.setPen(QPen(bd, 1))
        p.drawRoundedRect(r, 15, 15)
        cx, cy = 16, self.height() / 2
        if self.pulse:
            ring = QColor(self.color)
            ring.setAlpha(int(160 * (1 - self.phase)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ring)
            rr = 4 + 7 * self.phase
            p.drawEllipse(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr))
        p.setBrush(self.color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))
        p.setPen(QColor(C["text"]))
        p.drawText(QRectF(30, 0, self.width() - 34, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text)


class ResponsiveGrid(QWidget):
    """Lays child widgets out in as many columns as fit (8 cards: 1 row at 1920 px, 2 rows at 1366 px)."""

    def __init__(self, min_w=210, parent=None):
        super().__init__(parent)
        self.min_w = min_w
        self.items: list = []
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self.cols = 0

    def add(self, w):
        self.items.append(w)
        self._relayout(force=True)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self, force=False):
        n = len(self.items)
        if not n:
            return
        cols = max(1, min(n, self.width() // self.min_w))
        if cols > 4 and cols < n:
            cols = 4 if n == 8 else cols          # keep 8 cards balanced: 8x1 or 4x2
        if cols == self.cols and not force:
            return
        self.cols = cols
        for w in self.items:
            self.grid.removeWidget(w)
        for i, w in enumerate(self.items):
            self.grid.addWidget(w, i // cols, i % cols)
        for c in range(8):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)


def styled_plot(title="", y_label="", x_label="", height=None) -> pg.PlotWidget:
    pw = pg.PlotWidget()
    pw.setBackground(C["card"])
    pi = pw.getPlotItem()
    pi.showGrid(x=True, y=True, alpha=0.12)
    if title:
        pi.setTitle(f"<span style='color:{C['text']};font-size:11pt'>{title}</span>")
    for ax_name, txt in (("left", y_label), ("bottom", x_label)):
        ax = pi.getAxis(ax_name)
        ax.setPen(pg.mkPen(C["border"]))
        ax.setTextPen(pg.mkPen(C["text2"]))
        if txt:
            ax.setLabel(txt, color=C["text2"])
    pi.getViewBox().setMouseEnabled(x=True, y=False)
    pw.setMenuEnabled(False)
    if height:
        pw.setMinimumHeight(height)
    return pw


def glow_curve(plot, color, width=2.0):
    """A line with a faint wide halo underneath (looks 'lit' without hurting readability)."""
    halo = QColor(color)
    halo.setAlpha(55)
    h = plot.plot([], [], pen=pg.mkPen(halo, width=width * 3.5))
    c = plot.plot([], [], pen=pg.mkPen(color, width=width))

    class Pair:
        def setData(self, x, y):
            h.setData(x, y)
            c.setData(x, y)
    return Pair()


class Divider(QFrame):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(1)
        self.setStyleSheet(f"background:{C['border']};")
