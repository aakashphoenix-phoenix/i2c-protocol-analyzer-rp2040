"""Vector line icons drawn with QPainter (no image files, works offline)."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from config import COLORS


def _draw(name: str, p: QPainter, s: float):
    def L(x1, y1, x2, y2):
        p.drawLine(QPointF(x1 * s, y1 * s), QPointF(x2 * s, y2 * s))

    def R(x, y, w, h, r=2):
        p.drawRoundedRect(QRectF(x * s, y * s, w * s, h * s), r * s / 24 * 2, r * s / 24 * 2)

    def C(x, y, r):
        p.drawEllipse(QPointF(x * s, y * s), r * s, r * s)

    if name == "dashboard":
        R(3, 3, 8, 8); R(13, 3, 8, 5); R(13, 10, 8, 11); R(3, 13, 8, 8)
    elif name == "network":
        C(12, 5, 2.3); C(5, 18, 2.3); C(12, 18, 2.3); C(19, 18, 2.3)
        L(12, 7.3, 12, 11); L(5, 11, 19, 11); L(5, 11, 5, 15.7); L(12, 11, 12, 15.7); L(19, 11, 19, 15.7)
    elif name == "packets":
        R(3, 4, 18, 16); L(3, 9, 21, 9); L(3, 14, 21, 14); L(9, 4, 9, 20)
    elif name == "waveform":
        path = QPainterPath(QPointF(2 * s, 10 * s))
        for x, y in [(5, 10), (5, 4), (9, 4), (9, 10), (13, 10), (13, 4), (17, 4), (17, 10), (22, 10)]:
            path.lineTo(x * s, y * s)
        p.drawPath(path)
        path = QPainterPath(QPointF(2 * s, 20 * s))
        for x, y in [(7, 20), (7, 14), (15, 14), (15, 20), (22, 20)]:
            path.lineTo(x * s, y * s)
        p.drawPath(path)
    elif name == "performance":
        L(3, 21, 21, 21); L(3, 21, 3, 3)
        path = QPainterPath(QPointF(5 * s, 17 * s))
        for x, y in [(9, 12), (13, 15), (20, 6)]:
            path.lineTo(x * s, y * s)
        p.drawPath(path)
    elif name == "sensor":
        R(9.5, 2.5, 5, 13, 5); C(12, 18, 3.5); L(12, 8, 12, 15)
    elif name == "chip":
        R(6, 6, 12, 12)
        for k in (9, 12, 15):
            L(k, 2, k, 6); L(k, 18, k, 22); L(2, k, 6, k); L(18, k, 22, k)
    elif name == "error":
        path = QPainterPath(QPointF(12 * s, 3 * s))
        path.lineTo(22 * s, 20 * s); path.lineTo(2 * s, 20 * s); path.closeSubpath()
        p.drawPath(path); L(12, 9, 12, 14); C(12, 17, 0.4)
    elif name == "history":
        C(12, 12, 9); L(12, 7, 12, 12); L(12, 12, 16, 14)
    elif name == "settings":
        C(12, 12, 3)
        for a in range(8):
            import math
            ang = a * math.pi / 4
            L(12 + 6 * math.cos(ang), 12 + 6 * math.sin(ang), 12 + 9 * math.cos(ang), 12 + 9 * math.sin(ang))
        C(12, 12, 6)
    elif name == "logo":
        path = QPainterPath(QPointF(2 * s, 16 * s))
        for x, y in [(5, 16), (5, 8), (8, 8), (8, 16), (11, 16), (11, 8), (14, 8), (14, 16), (17, 16), (17, 8), (20, 8), (20, 16), (22, 16)]:
            path.lineTo(x * s, y * s)
        p.drawPath(path)


def icon(name: str, color: str = COLORS["text2"], active: str = COLORS["cyan"], size: int = 24) -> QIcon:
    ic = QIcon()
    for mode, col in ((QIcon.Mode.Normal, color), (QIcon.Mode.Selected, active), (QIcon.Mode.Active, active)):
        pm = QPixmap(size * 2, size * 2)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(col), 1.7 * 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        _draw(name, p, size * 2 / 24)
        p.end()
        pm.setDevicePixelRatio(2)
        ic.addPixmap(pm, mode)
    return ic


def pixmap(name: str, color: str, size: int = 24) -> QPixmap:
    return icon(name, color, color, size).pixmap(size, size)
