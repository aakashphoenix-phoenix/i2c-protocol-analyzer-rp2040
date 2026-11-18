"""Shared visual building blocks: cards, metric tiles, status pill, plot styling."""
from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, pyqtProperty
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QTextDocument
from PyQt6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLayout,
                             QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget)

from config import COLORS as C
from gui.theme import MONO_CSS, MONO_FAMILIES, mono_font, remap_qcolor, remap_text, set_mono  # noqa: F401  (re-exported)

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


def label(text="", obj=None, wrap=False, mono=False):
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    if mono:
        set_mono(lb)
    return lb


class ElidedLabel(QLabel):
    """One-line plain-text label that shrinks with its layout and elides ("…") instead of clipping mid-word.

    text() always returns the FULL text (callers and tests rely on it); while the shown text is elided and no explicit
    tooltip was set, the full text becomes the tooltip.  sizeHint() is the full text, minimumSizeHint() is tiny, so a
    layout gives the label what it needs when there is room and takes it back first when there is not."""

    def __init__(self, text="", obj=None, mode=Qt.TextElideMode.ElideRight, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self._explicit_tip = False
        self._auto_tip = False
        if obj:
            self.setObjectName(obj)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(text)

    # --- text / tooltip
    def text(self):
        return self._full

    def setText(self, text):
        self._full = text if isinstance(text, str) else str(text)
        self._apply()
        self.updateGeometry()

    def setToolTip(self, tip):
        self._explicit_tip = bool(tip)
        self._auto_tip = False
        super().setToolTip(tip)

    def setElideMode(self, mode):
        self._mode = mode
        self._apply()

    # --- geometry
    def _chrome(self):
        """Pixels around the text (padding + border + margins) as the style resolves them."""
        shown = QLabel.text(self)
        if not shown:
            return 0
        return max(0, QLabel.sizeHint(self).width() - self.fontMetrics().horizontalAdvance(shown))

    def sizeHint(self):
        sh = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + self._chrome() + 1, sh.height())

    def minimumSizeHint(self):
        return QSize(24, super().minimumSizeHint().height())

    def _apply(self):
        fm = self.fontMetrics()
        if self.width() > 40:
            shown = fm.elidedText(self._full, self._mode, max(0, self.width() - self._chrome()))
        else:
            shown = self._full                      # not laid out yet: the first resizeEvent elides
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)
        if not self._explicit_tip:
            if shown != self._full:
                QLabel.setToolTip(self, self._full)
                self._auto_tip = True
            elif self._auto_tip:
                QLabel.setToolTip(self, "")
                self._auto_tip = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (e.Type.FontChange, e.Type.StyleChange):
            self._apply()


class MonoDelegate(QStyledItemDelegate):
    """Item delegate drawing (and sizing) its cells in MONO_FONT: digits keep one width, columns never jitter.
    right=True also right-aligns the cells (numbers line up on the units digit)."""

    def __init__(self, parent=None, right=False):
        super().__init__(parent)
        self.right = right

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        f = QFont(option.font)
        f.setFamilies(list(MONO_FAMILIES))
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
        option.font = f
        if self.right:
            option.displayAlignment = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def mono_columns(view, columns, right=False):
    """Show the given columns of a QTableView / QTableWidget in MONO_FONT (QSS cannot target a column);
    right=True right-aligns them and their header labels (QTableWidget) too."""
    key = "_mono_delegate_r" if right else "_mono_delegate"
    d = getattr(view, key, None)
    if d is None:
        d = MonoDelegate(view, right)
        setattr(view, key, d)
    for c in columns:
        view.setItemDelegateForColumn(c, d)
        if right and hasattr(view, "horizontalHeaderItem"):
            it = view.horizontalHeaderItem(c)
            if it is not None:
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def fit_table(view, specs, stretch=None):
    """Column sizing that never crushes a column or cuts a header word.

    specs: {column: preferred width in px}.  Each listed column is Interactive and at least as wide as its header
    text (+ padding / sort arrow) and at least `preferred`.  `stretch` = the one column that takes the leftover
    width.  Safe to call again after the model / columns changed."""
    hh = view.horizontalHeader()
    hh.setMinimumSectionSize(40)
    hh.setHighlightSections(False)
    fm = hh.fontMetrics()
    model = view.model()
    for col, w in specs.items():
        if model is None or col >= model.columnCount():
            continue
        head = model.headerData(col, Qt.Orientation.Horizontal)
        need = fm.horizontalAdvance(str(head or "")) + 40          # section padding + sort indicator
        hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        view.setColumnWidth(col, max(w, need))
    if stretch is not None:
        hh.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)


SHORT_HINT = 64          # card hints up to this many characters sit beside the title; longer ones wrap below it


class Card(QFrame):
    """Rounded, bordered panel with a soft drop shadow for depth."""

    def __init__(self, title: str = "", hint: str = "", parent=None, shadow: bool = True):
        super().__init__(parent)
        self.setObjectName("card")
        if shadow:   # never on cards holding an OpenGL view: effects break GL text and cost GPU time
            sh = QGraphicsDropShadowEffect(self)
            sh.setBlurRadius(28)
            sh.setOffset(0, 6)
            sh.setColor(QColor(0, 0, 0, C["shadow_a"]))
            self.setGraphicsEffect(sh)
            self._shadow = sh
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 14, 16, 14)
        self.lay.setSpacing(10)
        if title:
            row = QHBoxLayout()
            row.setSpacing(10)
            self.title = ElidedLabel(title, "cardTitle")
            self.title.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
            row.addWidget(self.title)
            self.header = row
            if hint and len(hint) <= SHORT_HINT:
                # one short line beside the title; elided (full text as tooltip) when the card is narrow
                self.hint = ElidedLabel(hint, "cardHint")
                row.addWidget(self.hint, 1)
                self.lay.addLayout(row)
            else:
                row.addStretch(1)
                self.lay.addLayout(row)
                if hint:             # long explanations get their own wrapped line under the title
                    self.hint = label(hint, "cardHint", wrap=True)
                    self.lay.addWidget(self.hint)

    def retheme(self):
        """Theme switch: the soft shadow is lighter on the light preset."""
        sh = getattr(self, "_shadow", None)
        if sh is not None:
            sh.setColor(QColor(0, 0, 0, C["shadow_a"]))

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

    def retheme(self):
        self.color = remap_qcolor(self.color)
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
        self.shadow.setColor(QColor(0, 0, 0, C["shadow_a"]))
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
        self.value = label("—", "metricValue")          # MONO_FONT (QSS #metricValue): digits never change width
        self.value.setMinimumWidth(0)
        self.unit = label(unit, "metricUnit")
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        self.sub = label("No measurement", "cardHint", wrap=True)   # wraps inside the card instead of widening it
        self.sub.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.sub)
        lay.addStretch(1)                      # spare height (taller neighbours in the grid row) goes above the sparkline
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
        base = QColor(0, 0, 0, C["shadow_a"])
        a = g
        mix = QColor(int(base.red() * (1 - a) + col.red() * a), int(base.green() * (1 - a) + col.green() * a),
                     int(base.blue() * (1 - a) + col.blue() * a),
                     int(C["shadow_a"] * (1 - a) + (C["shadow_a"] + 50) * a))
        self.shadow.setColor(mix)
        self.shadow.setBlurRadius(24 + 14 * g)

    glow = pyqtProperty(float, get_glow, set_glow)

    def retheme(self):
        """Theme switch: translate the accent colour and the cached value colour into the new palette."""
        self.color = remap_text(self.color)
        if getattr(self, "_color", None):
            self._color = remap_text(self._color)
            self.value.setStyleSheet("")                 # force _style_value to re-apply
            self._style_value(self._color)
        self.set_glow(self._glow)

    VALUE_PX = (28, 24, 20, 17)          # the value steps down when the card is too narrow for it (never clipped)

    def _style_value(self, color):
        self._color = color
        text = self.value.text()
        unit_w = self.unit.fontMetrics().horizontalAdvance(self.unit.text()) + 6 if self.unit.text() else 0
        avail = self.width() - 28 - unit_w if self.width() > 60 else 1 << 20
        px = self.VALUE_PX[-1]
        for size in self.VALUE_PX:
            f = mono_font(size, bold=True)
            if QFontMetrics(f).horizontalAdvance(text) <= avail:
                px = size
                break
        style = f"color:{color}; font-size:{px}px;"
        if style != self.value.styleSheet():
            self.value.setStyleSheet(style)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_color", None):
            self._style_value(self._color)

    def set(self, value: str | None, sub: str = "", unit: str | None = None, color: str | None = None):
        if value is None:
            self.value.setText("—")
            self._style_value(C["gray"])
            self.sub.setText(sub or "Unavailable — no measurement data")
            return
        if unit is not None:
            self.unit.setText(unit)
        if value != self._last and self._last not in (None, "—") and MOTION["animations"] and not MOTION["reduced"]:
            self.anim.stop()
            self.anim.setStartValue(0.9)
            self.anim.setEndValue(0.0)
            self.anim.start()
        self._last = value
        self.value.setText(value)
        self._style_value(color or C["text"])
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

    def retheme(self):
        self.color = remap_qcolor(self.color)
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


TICK_PX = 11             # axis tick numbers (fixed-pitch so they never jitter / shift the plot area while updating)


def style_axes(plot, axes=("left", "bottom")):
    """Fixed-pitch tick numbers + a little text offset on the given axes of a PlotWidget / PlotItem."""
    pi = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
    for name in axes:
        ax = pi.getAxis(name)
        ax.setTickFont(mono_font(TICK_PX))
        ax.setStyle(tickTextOffset=6, autoExpandTextSpace=True)


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
    style_axes(pw)
    pi.layout.setContentsMargins(4, 6, 12, 4)           # room for the last tick label / title at the plot edges
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


class HudRow(QLabel):
    """One-line rich-text row that ELIDES cleanly ("…") instead of clipping mid-glyph or widening the layout.

    setText() takes the usual HTML (colours / bold are kept); text() still returns it."""

    def __init__(self, parent=None, mono=False):
        super().__init__(parent)
        self._segs = []
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        if mono:                       # status strips with counters / clocks: fixed-pitch digits
            set_mono(self)

    def setText(self, html):
        super().setText(html)
        doc = QTextDocument()
        doc.setHtml(html)
        segs = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fmt = frag.charFormat()
                    brush = fmt.foreground()
                    col = brush.color() if brush.style() != Qt.BrushStyle.NoBrush else None
                    segs.append((frag.text(), col, fmt.fontWeight() >= 600))
                it += 1
            block = block.next()
        self._segs = segs
        self.update()

    def sizeHint(self):
        return QSize(200, QFontMetrics(self.font()).height() + 3)

    def minimumSizeHint(self):
        return QSize(0, QFontMetrics(self.font()).height() + 3)

    def paintEvent(self, e):
        p = QPainter(self)
        base = self.font()
        x, width = 0, self.width()
        fm0 = QFontMetrics(base)
        y = (self.height() + fm0.ascent() - fm0.descent()) // 2
        for text, col, bold in self._segs:
            f = QFont(base)
            f.setBold(bold)
            fm = QFontMetrics(f)
            avail = width - x
            if avail <= 0:
                break
            shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail)
            p.setFont(f)
            p.setPen(col if col is not None else QColor(C["text"]))
            p.drawText(x, y, shown)
            x += fm.horizontalAdvance(shown)
            if shown != text:
                break


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto further rows when the width runs out (legend)."""

    def __init__(self, parent=None, hspace=12, vspace=2):
        super().__init__(parent)
        self._items, self._h, self._v = [], hspace, vspace

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._flow(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._flow(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _flow(self, rect, measure_only):
        rows, cur, x = [], [], rect.x()
        for it in self._items:
            if it.isEmpty():                        # hidden widget: takes no room
                continue
            sz = it.sizeHint()
            if cur and x + sz.width() > rect.right() + 1:
                rows.append(cur)
                cur, x = [], rect.x()
            cur.append((it, sz))
            x += sz.width() + self._h
        if cur:
            rows.append(cur)
        y = rect.y()
        for row in rows:
            row_h = max(sz.height() for _, sz in row)
            if not measure_only:
                xx = rect.x()
                for it, sz in row:                  # items are centred on the row (labels line up with their combos)
                    it.setGeometry(QRect(QPoint(xx, y + (row_h - sz.height()) // 2), sz))
                    xx += sz.width() + self._h
            y += row_h + self._v
        return (y - self._v - rect.y()) if rows else 0



class CommBanner(QFrame):
    """'Active communication' header above the graphs: which master talks to which slave right now.

    One elided line (full text in the tooltip) so it can never push the graph or overlap buttons/tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("commBanner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 3, 12, 3)
        lay.setSpacing(10)
        cap = QLabel("ACTIVE COMMUNICATION")
        cap.setObjectName("metricName")
        cap.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        lay.addWidget(cap)
        self.row = HudRow()
        lay.addWidget(self.row, 1)
        self.setFixedHeight(30)
        self._last = None
        self._style_for(C["text2"])

    def _style_for(self, color):
        self.setStyleSheet(f"QFrame#commBanner {{ background:{C['card_hi']}; border:1px solid {C['border']};"
                           f" border-left:3px solid {color}; border-radius:8px; }}")

    def update_from(self, engine, txn=None):
        from analysis.multi_master import active_communication
        info = active_communication(engine, txn)
        if info["html"] == self._last:                   # no change: no repaint, no flicker
            return info
        self._last = info["html"]
        self.row.setText(info["html"])
        self.row.setToolTip(info["plain"])
        self._style_for(info["color"])
        return info


def set_time_window(plot, span_s):
    """Fixed x window [-span, 0] for a rolling graph (set only when the span changes: no per-tick rescale)."""
    vb = plot.getPlotItem().getViewBox()
    if getattr(plot, "_span", None) != span_s:
        plot._span = span_s
        vb.setXRange(-span_s, 0.5, padding=0)
        vb.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
