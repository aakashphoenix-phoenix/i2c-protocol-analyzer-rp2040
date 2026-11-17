"""Application-wide Qt stylesheet, fonts and the live theme switch (dark default + light "presentation" preset).

Qt style sheets ignore CSS ``font-variant-numeric: tabular-nums``, so every live number (counters, clocks,
timestamps, kHz read-outs, numeric table columns) uses ``MONO_FONT``: a fixed-pitch face keeps every digit the
same width, so values never jitter horizontally when they refresh.  Three ways to get it:

* QSS:      ``objectName="mono"`` on a QLabel, or the dynamic property ``mono="true"`` on any widget (see ``set_mono``)
* QFont:    ``mono_font()`` for models (``FontRole``), delegates, pyqtgraph axes and painted text
* HTML:     ``MONO_CSS`` inside rich text (``<span style='font-family:...'>``)

Themes
------
``config.COLORS`` is the single live palette (``from config import COLORS as C`` everywhere).  ``apply_theme(app, name)``
mutates it IN PLACE from ``config.THEMES[name]`` and then re-themes what is already on screen:

1. the application style sheet (``build_stylesheet``) and the pyqtgraph defaults,
2. every widget's inline style sheet and rich-text label (old-palette colours are translated to the new palette,
   which is unambiguous because a preset never gives two different tokens the same hex),
3. every pyqtgraph plot (background, axes, grid, titles, curve / bar / line / text colours),
4. widgets with their own ``retheme()`` method (painted / cached colours) and ``on_change`` callbacks (module palettes).
"""
from __future__ import annotations

import re
import weakref

from PyQt6.QtGui import QColor, QFont, QPen

import config
from config import COLORS as C
from config import THEME_DEFAULT, THEMES

UI_FONT = '"Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'
MONO_FAMILIES = ("Cascadia Mono", "JetBrains Mono", "Consolas", "DejaVu Sans Mono", "monospace")
MONO_FONT = ", ".join(f'"{f}"' if " " in f else f for f in MONO_FAMILIES)
MONO_CSS = f"font-family:{MONO_FONT.replace(chr(34), chr(39))};"          # for HTML style='' attributes


def mono_font(pixel_size: int | None = None, bold: bool = False) -> QFont:
    """Fixed-pitch QFont (first installed family of MONO_FAMILIES); size is inherited unless given."""
    f = QFont()
    f.setFamilies(list(MONO_FAMILIES))
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    if pixel_size:
        f.setPixelSize(pixel_size)
    if bold:
        f.setBold(True)
    return f


def set_mono(widget, on: bool = True):
    """Switch a widget to MONO_FONT through the stylesheet (the explicit QFont would lose against QSS)."""
    widget.setProperty("mono", "true" if on else "false")
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
    return widget


# ====================================================================== stylesheet
def build_stylesheet(c=None) -> str:
    """The application style sheet for palette `c` (default: the live config.COLORS)."""
    c = C if c is None else c
    return f"""
* {{ font-family: {UI_FONT}; color: {c['text']}; }}
QMainWindow, QWidget#root {{ background: {c['bg']}; }}
QWidget {{ font-size: 13px; }}
QToolTip {{ background: {c['card_hi']}; color: {c['text']}; border: 1px solid {c['cyan']}; padding: 6px 8px; border-radius: 6px; }}

/* ---------- sidebar */
QFrame#sidebar {{ background: {c['sidebar']}; border-right: 1px solid {c['border']}; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ color: {c['text2']}; padding: 6px 12px; margin: 1px 8px; border-radius: 9px; }}
QListWidget#nav::item:hover {{ background: {c['hover']}; color: {c['text']}; }}
QListWidget#nav::item:selected {{ background: {c['sel']}; color: {c['cyan']}; border-left: 3px solid {c['cyan']}; }}

/* ---------- header / toolbar */
QFrame#header {{ background: {c['header_bg']}; border-bottom: 1px solid {c['border']}; }}
QLabel#appTitle {{ font-size: 18px; font-weight: 600; letter-spacing: 0.5px; }}
QLabel#appSub {{ color: {c['text2']}; font-size: 12px; }}
QLabel#muted, QLabel.muted {{ color: {c['text2']}; }}
QLabel#mono, *[mono="true"] {{ font-family: {MONO_FONT}; }}
QSpinBox, QDoubleSpinBox {{ font-family: {MONO_FONT}; }}
QFrame#header QPushButton {{ padding: 5px 11px; min-height: 18px; }}
QFrame#header QComboBox {{ padding: 4px 8px; min-height: 18px; }}
QPushButton#pill {{ background: {c['input_bg']}; border: 1px solid {c['border']}; border-radius: 14px; padding: 3px 12px;
                    min-height: 20px; color: {c['text2']}; }}
QPushButton#pill:hover {{ border-color: {c['border_hi']}; color: {c['text']}; }}
QPushButton#pill:checked {{ background: {c['sel']}; border-color: {c['cyan']}; color: {c['cyan']}; }}
QPushButton#stageChip {{ border-radius: 12px; padding: 2px 10px; min-height: 18px; background: {c['input_bg']}; color: {c['text2']}; }}
QPushButton#stageChip:hover {{ border-color: {c['border_hi']}; color: {c['text']}; }}
QFrame#portPanel {{ background: {c['panel_bg']}; border: 1px solid {c['border']}; border-radius: 10px; }}
QPushButton#portToggle {{ padding: 4px 10px; min-height: 18px; border-radius: 14px; font-weight: 600; }}
QFrame#portStrip {{ background: {c['input_bg']}; border: 1px solid {c['border']}; border-radius: 14px; }}
QFrame#portStrip:hover {{ border-color: {c['border_hi']}; }}

QPushButton {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 7px 12px; min-height: 20px; }}
QPushButton:hover {{ border-color: {c['cyan']}; background: {c['card_hi']}; }}
QPushButton:pressed {{ background: {c['pressed']}; }}
QPushButton:disabled {{ color: {c['dis_text']}; border-color: {c['dis_bd']}; background: {c['dis_bg']}; }}
QPushButton#primary {{ background: {c['primary_bg']}; border-color: {c['cyan']}; color: {c['primary_text']}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {c['primary_hi']}; }}
QPushButton#primary:disabled {{ background: {c['dis_bg']}; border-color: {c['dis_bd']}; color: {c['dis_text']}; font-weight: 600; }}
QPushButton#danger:disabled {{ background: {c['dis_bg']}; border-color: {c['dis_bd']}; color: {c['dis_text']}; }}
QPushButton#danger {{ background: {c['danger_bg']}; border-color: {c['danger_bd']}; color: {c['danger_text']}; }}
QPushButton#demo:checked {{ background: {c['demo_btn_bg']}; border-color: {c['demo']}; color: {c['demo_btn_text']}; font-weight: 600; }}
QPushButton#theme:checked {{ background: {c['sel']}; border-color: {c['cyan']}; color: {c['cyan']}; }}
QPushButton:checked {{ border-color: {c['cyan']}; background: {c['sel']}; }}

QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {c['input_bg']}; border: 1px solid {c['border']}; border-radius: 7px; padding: 6px 8px; min-height: 20px; }}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: {c['border_hi']}; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {c['cyan']}; }}
QComboBox QAbstractItemView {{ background: {c['card']}; border: 1px solid {c['border']}; selection-background-color: {c['sel']}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {c['check_bd']}; background: {c['input_bg']}; }}
QCheckBox::indicator:checked {{ background: {c['cyan']}; border-color: {c['cyan']}; }}

/* ---------- cards */
QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 14px; }}
QFrame#card:hover {{ border-color: {c['card_hover']}; }}
QLabel#cardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#cardHint {{ color: {c['text2']}; font-size: 12px; }}
QLabel#metricValue {{ font-family: {MONO_FONT}; font-size: 28px; font-weight: 600; }}
QLabel#metricUnit {{ color: {c['text2']}; font-size: 13px; }}
QLabel#metricName {{ color: {c['text2']}; font-size: 12px; letter-spacing: 0.6px; }}
QLabel#bigValue {{ font-family: {MONO_FONT}; font-size: 40px; font-weight: 600; }}
QLabel#clock {{ font-family: {MONO_FONT}; font-size: 54px; font-weight: 600; color: {c['cyan']}; }}

/* ---------- tables */
QTableView {{ background: {c['table_bg']}; alternate-background-color: {c['table_alt']}; border: 1px solid {c['border']}; border-radius: 10px;
              gridline-color: transparent; selection-background-color: {c['table_sel']}; selection-color: {c['text']}; }}
QTableView::item, QTableWidget::item {{ padding: 0 8px; }}
QHeaderView::section {{ background: {c['head_sec']}; color: {c['text2']}; border: none; border-bottom: 1px solid {c['border']};
                        padding: 6px 20px 6px 8px; font-weight: 600; font-size: 12px; }}
QHeaderView {{ background: {c['head_sec']}; border: none; }}
QTableCornerButton::section {{ background: {c['head_sec']}; border: none; }}

/* ---------- misc */
QScrollArea {{ background: transparent; border: none; }}
QWidget#scrollBody {{ background: {c['bg']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['scroll']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {c['scroll_hi']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['scroll']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {c['split_hover']}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {c['text2']}; padding: 8px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {c['cyan']}; border-bottom-color: {c['cyan']}; }}
QPlainTextEdit, QTextEdit {{ background: {c['input_bg']}; border: 1px solid {c['border']}; border-radius: 8px; font-family: {MONO_FONT}; }}
QLabel#bannerDemo {{ background: {c['demo_bg']}; color: {c['demo_text']}; border-bottom: 1px solid {c['demo']}; font-weight: 700;
                     letter-spacing: 1.5px; padding: 6px; }}
QLabel#bannerReplay {{ background: {c['replay_bg']}; color: {c['replay_text']}; border-bottom: 1px solid {c['replay_bd']}; font-weight: 700;
                       letter-spacing: 1.5px; padding: 6px; }}
QLabel#bannerWarn {{ background: {c['warn_bg']}; color: {c['warn_text']}; border-bottom: 1px solid {c['amber']}; padding: 6px; }}
"""


STYLESHEET = build_stylesheet()            # default (dark) sheet; apply_theme() refreshes this name on every switch


# ====================================================================== theme switching
_state = {"name": THEME_DEFAULT, "serial": 0, "remap": {}, "rgb": {}, "grid_old": C["grid_a"], "grid_new": C["grid_a"]}
_callbacks: list = []


def current_theme() -> str:
    return _state["name"]


def theme_serial() -> int:
    """Bumps on every real switch: use it in cache keys of painted layers."""
    return _state["serial"]


def theme_names():
    return list(THEMES)


def on_change(fn):
    """Register fn() to run after a theme switch (module-level palettes, caches).  Bound methods are held weakly."""
    _callbacks.append(weakref.WeakMethod(fn) if hasattr(fn, "__self__") else (lambda f=fn: f))
    return fn


def token_for(hex_color):
    """Name of the palette token whose value is hex_color (current palette), or None."""
    h = str(hex_color).lower()
    for k, v in C.items():
        if isinstance(v, str) and v.lower() == h:
            return k
    return None


_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
_RGB = re.compile(r"(rgba?\(\s*)(\d+)(\s*,\s*)(\d+)(\s*,\s*)(\d+)")


def remap_text(text: str) -> str:
    """Translate every old-palette colour in a style sheet / HTML string ('#rrggbb' and 'rgb[a](r,g,b' forms)."""
    if not text or ("#" not in text and "rgb" not in text):
        return text
    hexmap, rgbmap = _state["remap"], _state["rgb"]
    out = _HEX.sub(lambda m: hexmap.get(m.group(0).lower(), m.group(0)), text)

    def rgb(m):
        new = rgbmap.get((int(m.group(2)), int(m.group(4)), int(m.group(6))))
        return m.group(0) if new is None else f"{m.group(1)}{new[0]}{m.group(3)}{new[1]}{m.group(5)}{new[2]}"
    return _RGB.sub(rgb, out)


def tint(hex_color: str, alpha: float) -> str:
    """'rgba(r,g,b,a)' of a palette colour: the soft tinted background of pills, badges and banners."""
    q = QColor(hex_color)
    return f"rgba({q.red()},{q.green()},{q.blue()},{alpha})"


def remap_qcolor(color) -> QColor:
    """Old-palette QColor -> new-palette QColor (alpha kept); colours outside the palette are returned unchanged."""
    q = QColor(color)
    new = _state["remap"].get(q.name().lower())
    if new is None:
        return q
    out = QColor(new)
    out.setAlpha(q.alpha())
    return out


def remap_pen(pen) -> QPen:
    q = QPen(pen)
    q.setColor(remap_qcolor(q.color()))
    return q


def _build_remap(old: dict, new: dict):
    hexmap, rgbmap = {}, {}
    for k, ov in old.items():
        nv = new.get(k)
        if isinstance(ov, str) and ov.startswith("#") and isinstance(nv, str) and ov.lower() != nv.lower():
            hexmap.setdefault(ov.lower(), nv)
    for ov, nv in hexmap.items():
        a, b = QColor(ov), QColor(nv)
        rgbmap[(a.red(), a.green(), a.blue())] = (b.red(), b.green(), b.blue())
    return hexmap, rgbmap


def apply_theme(app, name: str, force: bool = False) -> bool:
    """Switch the whole application to theme `name` ('dark' | 'presentation') immediately.

    COLORS is updated in place (every `C[...]` reader sees the new palette), config.MASTERS gets the theme's wire colours,
    then the application style sheet, pyqtgraph defaults and everything already on screen are re-themed.  The
    application style sheet is only (re)installed when the app already has one (main.py installs it) or `force` is set,
    so headless callers keep their bare Qt style.  Returns True when the theme actually changed."""
    if name not in THEMES:
        raise ValueError(f"unknown theme {name!r}; available: {', '.join(THEMES)}")
    if name == _state["name"] and not force:
        return False
    old = dict(C)
    new = THEMES[name]
    _state["remap"], _state["rgb"] = _build_remap(old, new)
    _state["grid_old"], _state["grid_new"] = old["grid_a"], new["grid_a"]
    C.update(new)
    for mid, m in config.MASTERS.items():
        m["color"], m["color_read"] = new[f"m{mid}"], new[f"m{mid}_read"]
    _state["name"] = name
    _state["serial"] += 1
    global STYLESHEET
    STYLESHEET = build_stylesheet()
    import pyqtgraph as pg
    pg.setConfigOptions(background=C["card"], foreground=C["text2"])
    for ref in list(_callbacks):
        fn = ref()
        if fn is None:
            _callbacks.remove(ref)
        else:
            fn()
    if app is not None:
        if force or app.styleSheet():
            app.setStyleSheet(STYLESHEET)
        retheme_widgets(app)
    return True


def retheme_widgets(app):
    """Re-theme everything that already exists: inline style sheets, rich-text labels, plots, widgets with retheme()."""
    from PyQt6.QtWidgets import QLabel
    import pyqtgraph as pg
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                new = remap_text(ss)
                if new != ss:
                    w.setStyleSheet(new)
            if isinstance(w, QLabel):
                t = w.text()
                if t and ("#" in t or "rgb" in t):
                    nt = remap_text(t)
                    if nt != t:
                        w.setText(nt)
            if isinstance(w, pg.PlotWidget):
                restyle_plot(w)
            hook = getattr(w, "retheme", None)
            if callable(hook):
                hook()
            w.update()
        except RuntimeError:                 # widget deleted while iterating
            continue


def restyle_plot(pw):
    """Re-theme one pyqtgraph PlotWidget in place: background, axes, grid, title and every item's colours."""
    import pyqtgraph as pg
    pi = pw.getPlotItem()
    pw.setBackground(remap_qcolor(pw.backgroundBrush().color()))
    for name in ("left", "bottom", "top", "right"):
        ax = pi.getAxis(name)
        ax.setPen(pg.mkPen(C["border"]))
        ax.setTextPen(pg.mkPen(C["text2"]))
        if ax.label.isVisible() and getattr(ax, "labelText", ""):
            ax.setLabel(ax.labelText, ax.labelUnits, ax.labelUnitPrefix, color=C["text2"])
    gx, gy = pi.ctrl.xGridCheck.isChecked(), pi.ctrl.yGridCheck.isChecked()
    if gx or gy:
        # each plot keeps its grid strength RELATIVE to the theme's base grid opacity (kept as a float: no rounding drift
        # when switching back and forth through the integer slider)
        rel = getattr(pi, "_grid_rel", None)
        if rel is None:
            rel = pi._grid_rel = (pi.ctrl.gridAlphaSlider.value() / 255.0) / _state["grid_old"]
        pi.showGrid(x=gx, y=gy, alpha=min(1.0, rel * _state["grid_new"]))
    title = getattr(pi.titleLabel, "text", "")
    if title:
        pi.setTitle(remap_text(title))
    for it in list(pi.items):
        try:
            if isinstance(it, (pg.PlotDataItem, pg.PlotCurveItem)):
                pen = it.opts.get("pen")
                if pen is not None:
                    it.setPen(remap_pen(pg.mkPen(pen)))
            elif isinstance(it, pg.BarGraphItem):
                brush = it.opts.get("brush")
                if brush is not None:
                    it.setOpts(brush=pg.mkBrush(remap_qcolor(pg.mkBrush(brush).color())))
            elif isinstance(it, pg.InfiniteLine):
                it.setPen(remap_pen(it.pen))
                if it.label is not None:
                    it.label.setColor(remap_qcolor(it.label.color))
            elif isinstance(it, pg.TextItem):
                it.setColor(remap_qcolor(it.color))
        except (AttributeError, TypeError, RuntimeError):
            continue
