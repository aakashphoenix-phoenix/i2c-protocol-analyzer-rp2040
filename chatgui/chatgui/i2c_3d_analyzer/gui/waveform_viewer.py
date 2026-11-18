"""Digital Waveform Viewer — SDA/SCL drawn ONLY from hardware-timestamped edge records."""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainterPath, QPen
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QGraphicsItem, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QTextEdit,
                             QToolTip,
                             QVBoxLayout, QWidget)

from config import COLORS as C
from gui.widgets import Card, CommBanner, FlowLayout, HudRow, fmt_time_ns, label

SCL_Y, SDA_Y = 1.6, 0.0
# Vertical layout (data units; SDA 0..1, bit digits 1.25, SCL 1.6..2.6).  With the decoder track ON the plot grows upwards:
#   2.7 .. 3.7  free band that holds the A/B cursor letters (never under a box)     3.9 .. 4.3  decoder track (y = TRACK_Y)
Y_CLASSIC_MAX, Y_TRACK_MAX, TRACK_Y = 4.0, 4.6, 4.1
CURSOR_FRACS_CLASSIC = (0.97, 0.86)          # A / B letter position along their lines (1 = top) without the track
CURSOR_FRACS_TRACK = (0.74, 0.68)            # = data y 3.3 / 3.0: between SCL and the track
AUTO_WINDOW = "Auto (SCL)"
# Time base choices (ns).  "Auto (SCL)" follows the bus clock: see WaveformPage._window_ns.
WINDOWS = {AUTO_WINDOW: None, "50 µs": 5e4, "100 µs": 1e5, "200 µs": 2e5, "250 µs": 2.5e5, "500 µs": 5e5,
           "1 ms": 1e6, "2 ms": 2e6, "5 ms": 5e6, "20 ms": 2e7, "100 ms": 1e8}
# Auto: window = AUTO_SCL_US_KHZ / f_kHz microseconds, snapped to the nearest of AUTO_SNAPS_US.  A byte is 9 SCL clocks,
# so the window always holds the same ~100 clocks: 100 kHz -> 1 ms, 400 kHz -> 250 µs, 1 MHz -> 100 µs, and a byte stays
# ~125 px wide on a 1400 px plot at any bus speed (room for address / data / bit digits / ACK labels).
AUTO_SCL_US_KHZ = 1e5
AUTO_SNAPS_US = (50, 100, 250, 500, 1000, 2000, 5000)
DEFAULT_SCL_HZ = 100_000     # until a packet has been measured and no rate has been applied
# Annotation tiers by pixels per byte (9 clocks): below MIN_LABEL_BYTE_PX only the S/P markers are drawn.
MIN_LABEL_BYTE_PX = 26
BIT_MIN_PX = 10              # per-bit digits (compact font) once one bit is at least this wide
TIER_PX = (200, 90, 55)      # pixels per byte for tier 4 (verbose) / 3 (ADDR, W=0, raw byte, DATA 0x..) / 2 (short)
WIDE_PEN_MAX_VERTICES = 220   # above this a curve is a 1 px hairline: Qt strokes wide pens at ~0.25 ms per vertex
HALO_MAX_VERTICES = 60        # the soft glow (2nd wide pen per trace) only for very sparse traces
DWELL_S = 0.8           # a displayed live snapshot / sweep stays at least this long before a newer one replaces it
AUTO_TIMEOUT_S = 1.5     # Auto trigger: no new complete trigger for this long -> free-run (idle level shown flat)
TRIGGERS = ["Auto", "START", "Address", "Roll"]
MAX_EVENT_LINES = 200    # more S/Sr/P markers than this in view: the dotted lines are dropped (traces stay)
LABEL_H_PX = 15          # collision height of one annotation line


def timing_summary(txn):
    """One-line packet timing shared by the waveform header (UNKNOWN where not captured)."""
    from analysis.playback import format_capture_ns
    tm = txn.timing

    def v(key):
        return fmt_time_ns(tm.get(key)) if tm.get(key) is not None else "UNKNOWN"
    hz = tm.get('frequency_hz')
    freq = (f"SCL {hz / 1e3:.2f} kHz [{tm.get('frequency_scope', 'scope unknown')}; {tm.get('frequency_source', '')}]"
            if hz else "SCL UNKNOWN (no captured cycles)")
    long_low = tm.get('long_low_ns') or []
    return (f"{freq} · HIGH {v('high_ns')} / LOW {v('low_ns')} · START {format_capture_ns(tm.get('start_ns'))} · "
            f"STOP {format_capture_ns(tm.get('stop_ns')) if tm.get('stop_ns') else 'UNKNOWN (not captured)'} · "
            f"transaction {fmt_time_ns(txn.duration_ns) if txn.t_end is not None else 'UNKNOWN'} · "
            f"gap from previous STOP {v('bus_free_ns')} · SDA setup min {v('setup_min_ns')} / hold min {v('hold_min_ns')} · "
            f"long SCL LOW periods: {len(long_low)} (cause undetermined)")


def _ink(fill):
    """Text colour that reads on a box of this fill (dark on the bright cyan/green/amber, light on blue/red/gray)."""
    c = QColor(fill)
    # fixed ink colours (NOT the theme text colour: on the presentation theme the accents are dark and the theme text is
    # dark too, which made the labels unreadable); whichever of the two has the better WCAG contrast on this fill
    def lum(q):
        f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(q.redF()) + 0.7152 * f(q.greenF()) + 0.0722 * f(q.blueF())
    lf = lum(c)
    dark, light = "#06101E", "#FFFFFF"
    return dark if (lf + 0.05) / (lum(QColor(dark)) + 0.05) >= (1.05) / (lf + 0.05) else light


class DecoderTrack(QGraphicsItem):
    """Saleae/PulseView-style protocol decoder row: coloured rounded boxes aligned to the exact SCL timestamps.

    Segments are in DATA coordinates (x in us, as plotted); the boxes are laid out and painted in SCENE pixels at paint
    time, so pan / zoom never wait for a re-layout, corners stay round and text stays crisp.  Boxes never overlap (each one
    is clipped to the start of the next); text adapts to the box width ('DATA 0xF7' -> 'F7' -> a coloured bar)."""
    H = 20.0               # box height in px
    GAP = 1.5              # px between neighbouring boxes
    MIN_W = 3.0            # a box is at least this wide (a bar)

    def __init__(self, viewbox):
        super().__init__()
        self.vb = viewbox
        self.segs: list = []
        self.boxes: list = []            # last layout: dicts with rect (scene px), kind, text, color, tip
        self.band = QRectF()
        self.hover = None
        self._font = None
        self.setZValue(60)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)       # pan / zoom / cursor drags pass straight through
        viewbox.sigRangeChanged.connect(lambda *_: self.update())
        viewbox.sigResized.connect(lambda *_: self.update())

    # ---- data
    def set_segments(self, segs):
        self.segs = list(segs)
        self.hover = None
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        return self.vb.sceneBoundingRect()

    def shape(self):
        path = QPainterPath()
        path.addRect(self.band if not self.band.isNull() else QRectF())
        return path

    def _track_font(self):
        if self._font is None:
            f = QFont(QApplication.font())
            f.setPointSizeF(max(7.5, f.pointSizeF() * 0.85) if f.pointSizeF() > 0 else 8.5)
            f.setBold(True)
            self._font = f
        return self._font

    # ---- layout (scene px)
    def layout(self):
        """Boxes for the current view: [{rect, kind, text, color, tip, seg}], left to right, pairwise disjoint."""
        vb = self.vb
        view = vb.viewRect()
        view_scene = vb.sceneBoundingRect()
        p0 = vb.mapViewToScene(QPointF(view.left(), TRACK_Y))
        p1 = vb.mapViewToScene(QPointF(view.right(), TRACK_Y))
        span = view.right() - view.left()
        if span <= 0 or p1.x() <= p0.x():
            self.boxes, self.band = [], QRectF()
            return self.boxes
        k = (p1.x() - p0.x()) / span
        cy = p0.y()
        top = cy - self.H / 2
        self.band = QRectF(view_scene.left(), top, view_scene.width(), self.H)
        order = sorted((sg for sg in self.segs if sg["x1"] >= view.left() - span and sg["x0"] <= view.right() + span),
                       key=lambda sg: (sg["x0"], sg["x1"]))
        fm = QFontMetricsF(self._track_font())
        raw = []
        for sg in order:
            x0 = p0.x() + (sg["x0"] - view.left()) * k
            x1 = max(x0 + self.MIN_W, p0.x() + (sg["x1"] - view.left()) * k)
            raw.append([x0, x1, sg])
        boxes = []
        for i, (x0, x1, sg) in enumerate(raw):
            if i + 1 < len(raw):
                x1 = min(x1, raw[i + 1][0] - self.GAP)            # never overlap the next box
            if x1 - x0 < 1.0 or x1 < view_scene.left() or x0 > view_scene.right():
                continue
            width = x1 - x0
            text = ""
            for cand in sg["texts"]:
                if cand and fm.horizontalAdvance(cand) + (4 if len(cand) <= 2 else 8) <= width:
                    text = cand
                    break
            boxes.append(dict(rect=QRectF(x0, top, width, self.H), kind=sg["kind"], text=text, color=sg["color"],
                              tip=sg["tip"], seg=sg))
        self.boxes = boxes
        return boxes

    def box_at(self, pos):
        for b in self.boxes:
            if b["rect"].contains(pos):
                return b
        return None

    # ---- painting
    def paint(self, painter, option, widget=None):
        boxes = self.layout()
        if not boxes:
            return
        painter.save()
        painter.setClipRect(self.vb.sceneBoundingRect())
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setFont(self._track_font())
        for b in boxes:
            col = QColor(b["color"])
            fill = QColor(col)
            fill.setAlpha(232)
            edge = QColor(col).darker(165)
            hot = b is self.hover
            painter.setPen(QPen(QColor(C["text"]) if hot else edge, 1.6 if hot else 1.0))
            painter.setBrush(fill)
            r = b["rect"]
            painter.drawRoundedRect(r, 4.0, 4.0)
            if b["text"]:
                painter.setPen(QColor(_ink(b["color"])))
                painter.drawText(r, Qt.AlignmentFlag.AlignCenter, b["text"])
        painter.restore()

    # ---- hover tooltips
    def hoverMoveEvent(self, ev):
        b = self.box_at(ev.pos())
        if b is not self.hover:
            self.hover = b
            self.update()
        if b is not None:
            QToolTip.showText(ev.screenPos(), b["tip"])
        else:
            QToolTip.hideText()

    def hoverLeaveEvent(self, ev):
        if self.hover is not None:
            self.hover = None
            self.update()
        QToolTip.hideText()


class WaveformPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.txn = None
        self.origin = 0.0
        self.last_draw = 0.0
        self._display_context = None
        self._shown = None            # the window on display: t0, t1, txns (kept until a newer VALID snapshot)
        self._shown_snap = None       # engine.waveform_snapshots of that window
        self._shown_at = 0.0
        self._valid_ctx = None
        self._info_base = ""
        self._plan = None             # window of the sweep on display (trigger) or of the rolling time base
        self._sweep_key = None        # identity of the triggering transaction on display
        self._sweep_wall = 0.0
        self._free_run = False
        self._draw_key = None
        self._cursor_anchor = None
        self._cursors_init = False    # A / B are placed ONCE (first drawn window); after that only the user moves them
        self._dense = 0               # pen tier: 0 wide + halo · 1 wide · 2 hairline (many vertices)
        self._auto_w = None           # effective Auto (SCL) window (ns) the display was last laid out for
        self._fonts = None
        self._text_pool, self._line_pool = [], {}
        self._ann_key = None
        self.label_px = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        bar = Card()
        r = FlowLayout(hspace=10, vspace=6)               # controls wrap onto a second row instead of overlapping
        self.follow = QCheckBox("Follow live")
        self.follow.setChecked(True)
        self.win = QComboBox()
        self.win.addItems(list(WINDOWS))
        self.win.setCurrentText(AUTO_WINDOW)
        self.win.setToolTip("Auto (SCL): the time base follows the bus clock (measured packets, else the applied SCL setting): 100 kHz → 1 ms, 400 kHz → 250 µs, so every SCL pulse and bit stays readable. Pick a fixed window to override it.")
        self.trigger = QComboBox()
        self.trigger.addItems(TRIGGERS)
        self.trigger.setToolTip("Auto: hold the last complete START-triggered sweep, free-run (idle shown flat) when "
                                "none arrives for 1.5 s · START: hold the last complete sweep until the next one · "
                                "Address: START of the selected device only · Roll: continuous rolling time base")
        self.prev = QPushButton("◀ Previous packet")
        self.next = QPushButton("Next packet ▶")
        self.fit = QPushButton("Fit packet")
        self.cur = QCheckBox("Cursors A/B")
        self.cur.setChecked(True)
        self.ann = QCheckBox("Annotations")
        self.ann.setChecked(True)
        self.track = QCheckBox("Decoder track")
        self.track.setChecked(True)
        self.track.setToolTip("Protocol decoder row above SCL/SDA (like Saleae / PulseView): [S] [ADDR 0x.. W|R] [DATA 0x..] "
                              "[ACK|NACK] [Sr] [P] as coloured boxes on the exact clock timestamps - write blue, read cyan, "
                              "ACK green, NACK red. Off = the classic text labels above SCL.")
        for w in (self.follow, label("Window", "cardHint"), self.win, label("Trigger", "cardHint"), self.trigger, self.prev, self.next, self.fit, self.cur, self.ann, self.track):
            r.addWidget(w)
        bar.add(r)
        self.title = HudRow()                              # one elided line (full text in the tooltip)
        self.title.setObjectName("cardTitle")
        bar.add(self.title)
        extra = FlowLayout(hspace=10, vspace=6)
        self.zoom_in = QPushButton("Zoom +")
        self.zoom_out = QPushButton("Zoom −")
        self.refresh_rate = QComboBox()
        self.refresh_rate.addItems(["5 Hz", "10 Hz", "20 Hz"])
        self.refresh_rate.setCurrentText("5 Hz")
        extra.addWidget(self.zoom_in); extra.addWidget(self.zoom_out)
        self.btn_export = QPushButton("Export view (VCD / CSV)…")
        self.btn_export.setToolTip("Save the CAPTURED SDA/SCL edges of the visible time window exactly as recorded: VCD "
                                   "(PulseView / sigrok / GTKWave - add the I2C decoder for a reference decode) or CSV")
        self.btn_export.clicked.connect(lambda: self.export_view())
        extra.addWidget(self.btn_export)
        extra.addWidget(label("Display refresh")); extra.addWidget(self.refresh_rate)
        extra.addWidget(label("Scroll wheel zoom · drag pan", "cardHint"))
        self.dev_lbl = label("Device filter: all", "cardHint")
        extra.addWidget(self.dev_lbl)
        bar.add(extra)
        lay.addWidget(bar)

        pc = Card()
        self.banner = CommBanner()                         # which master <-> which slave is on the bus
        pc.add(self.banner)
        # error / arbitration cause of the packet on display, directly above the traces (red: error, amber: arbitration)
        self.alert = QLabel("")
        self.alert.setWordWrap(True)
        self.alert.setTextFormat(Qt.TextFormat.RichText)
        self.alert.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.alert.hide()
        pc.add(self.alert)
        self._alert_key = None
        self._arb_items = []
        self.plot = pg.PlotWidget()
        self.plot.setBackground(C["plot_deep"])
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=False, alpha=0.15)
        pi.getAxis("left").setTicks([[(SCL_Y + 0.5, "SCL"), (SDA_Y + 0.5, "SDA")]])
        pi.getAxis("left").setWidth(48)
        pi.getAxis("bottom").setLabel("time", units="µs", color=C["text2"])
        pi.getAxis("bottom").enableAutoSIPrefix(False)
        for ax in ("left", "bottom"):
            pi.getAxis(ax).setPen(pg.mkPen(C["border"]))
            pi.getAxis(ax).setTextPen(pg.mkPen(C["text2"]))
        pi.setYRange(-0.4, Y_TRACK_MAX, padding=0)
        pi.getViewBox().setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        halo_scl, halo_sda = pg.mkColor(C["scl"]), pg.mkColor(C["sda"])
        halo_scl.setAlpha(50)
        halo_sda.setAlpha(50)
        self.c_scl_h = self.plot.plot([], [], pen=pg.mkPen(halo_scl, width=6), stepMode="right")
        self.c_sda_h = self.plot.plot([], [], pen=pg.mkPen(halo_sda, width=6), stepMode="right")
        self.c_scl = self.plot.plot([], [], pen=pg.mkPen(C["scl"], width=2), stepMode="right")
        self.c_sda = self.plot.plot([], [], pen=pg.mkPen(C["sda"], width=2), stepMode="right")
        for c in (self.c_scl_h, self.c_sda_h, self.c_scl, self.c_sda):
            c.setClipToView(True)                                          # only the visible range is processed
            c.setDownsampling(ds=1, auto=True, method="peak")               # min/max per pixel: glitches survive
        self.ann_items: list = []
        self._cursor_fracs = CURSOR_FRACS_TRACK
        self.decoder_track = DecoderTrack(pi.getViewBox())
        self.plot.scene().addItem(self.decoder_track)            # scene-level: laid out in pixels, clipped to the plot
        self.playhead = pg.InfiniteLine(angle=90, pen=pg.mkPen(C["cyan"], style=Qt.PenStyle.DashLine))
        pi.addItem(self.playhead)
        self.playhead.hide()
        self.ca = pg.InfiniteLine(pos=0, movable=True, pen=pg.mkPen(C["blue"], width=1.5, style=Qt.PenStyle.DashLine),
                                  label="A", labelOpts={"color": C["blue"], "position": CURSOR_FRACS_TRACK[0]})
        self.cb = pg.InfiniteLine(pos=10, movable=True, pen=pg.mkPen(C["blue"], width=1.5, style=Qt.PenStyle.DashLine),
                                  label="B", labelOpts={"color": C["blue"], "position": CURSOR_FRACS_TRACK[1]})   # lower than A
        for c in (self.ca, self.cb):
            pi.addItem(c)
            c.sigPositionChanged.connect(self._cursor_text)
        self.msg = pg.TextItem("", color=C["amber"], anchor=(0.5, 0.5))
        self.msg.setZValue(100)
        pi.addItem(self.msg)
        self.plot.setMinimumHeight(300)          # long timing text must never squash the waveform
        pc.add(self.plot, 1)
        self.info = label("", "cardHint", wrap=True)
        self.cursor_lbl = label("", "mono", wrap=True)
        for lb in (self.info, self.cursor_lbl):
            # the readout wraps onto as many lines as it needs and its card always grows to show them all (the
            # waveform keeps its own minimum height): no measurement line is ever cut off at the bottom
            lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
            lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lb.installEventFilter(self)                   # its height follows ITS width (resize -> re-fit)
        pc.add(self.info)
        pc.add(self.cursor_lbl)
        lay.addWidget(pc, 1)
        # every protocol field of the packet on display (Packet ◀ / ▶, a table row, Visualize error / arbitration)
        fc = Card("I²C Communication Data Frame — the packet on display, field by field")
        self.frame_box = QTextEdit()
        self.frame_box.setReadOnly(True)
        self.frame_box.setMinimumHeight(170)
        self.frame_box.setMaximumHeight(260)
        fc.add(self.frame_box)
        lay.addWidget(fc)
        self._frame_txn = None

        if hasattr(self.ctx, "set_refresh"):
            self.refresh_rate.currentTextChanged.connect(lambda txt: self.ctx.set_refresh(int(1000/int(txt.split()[0]))))
        self.zoom_in.setToolTip("Zoom the time axis in  [+]")
        self.zoom_out.setToolTip("Zoom the time axis out  [−]")
        self.zoom_in.clicked.connect(self.zoom_in_view)
        self.zoom_out.clicked.connect(self.zoom_out_view)
        self._xr_timer = QTimer(self)
        self._xr_timer.setSingleShot(True)
        self._xr_timer.timeout.connect(self._reannotate)
        self.plot.getPlotItem().sigXRangeChanged.connect(lambda *_: self._xr_timer.start(60))   # coalesce wheel/drag
        # A held sweep is never redrawn, so labels laid out while the page was hidden / narrower than now would stay
        # in their small-tier form forever: re-lay them out whenever the plot's pixel size changes.
        self.plot.getPlotItem().getViewBox().sigResized.connect(lambda *_: self._xr_timer.start(60))
        self.follow.toggled.connect(lambda v: self._redraw(force=True))
        self.win.currentTextChanged.connect(lambda v: self._reset_sweep())
        self.trigger.currentTextChanged.connect(lambda v: self._reset_sweep())
        self.prev.clicked.connect(lambda: self._step(-1))
        self.next.clicked.connect(lambda: self._step(1))
        self.fit.clicked.connect(lambda: self._redraw(force=True, fit=True, user_fit=True))
        self.cur.toggled.connect(lambda v: (self.ca.setVisible(v), self.cb.setVisible(v), self._cursor_text()))
        self.ann.toggled.connect(lambda v: self._redraw(force=True))
        self.track.toggled.connect(lambda v: self._apply_track_mode())

    def retheme(self):
        """Theme switch: annotations, decoder-track boxes and curve pens are rebuilt in the new palette."""
        self._ann_key = None
        self._redraw(force=True)

    def export_view(self, path=None):
        """Captured edges of the visible window -> VCD or CSV (by the file extension)."""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        from storage.signal_export import write_edges_csv, write_vcd
        eng = self.ctx.engine
        (x0, x1), _ = self.plot.getPlotItem().viewRange()
        t0, t1 = self.origin + x0 * 1e3, self.origin + x1 * 1e3
        if not eng.edges or t1 < eng.edges[0][0] or any(t0 <= g <= t1 for g in eng.edge_gaps):
            QMessageBox.information(self, "Nothing to export", "No captured raw edges for the visible window (they are "
                                    "kept for the most recent seconds only, and never across a data-loss gap).")
            return None
        if not path:
            path, _flt = QFileDialog.getSaveFileName(self, "Export captured SDA/SCL edges", "i2c_capture.vcd",
                                                     "VCD (*.vcd);;CSV (*.csv)")
        if not path:
            return None
        ts, ss = eng.edges_between(t0, t1)
        samples = [(float(t), int(s)) for t, s in zip(ts, ss)]
        try:
            n = (write_edges_csv(path, samples) if path.lower().endswith(".csv") else
                 write_vcd(path, samples, eng.tick_ns, "Digital Waveform view", eng.source))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return None
        if hasattr(self.ctx, "toast"):
            self.ctx.toast(f"Exported {n:,} captured edge(s): {path}")
        return path

    def zoom_in_view(self):
        """Halve the visible time span (buttons and the global + / = shortcut)."""
        self.plot.getPlotItem().getViewBox().scaleBy((0.5, 1))

    def zoom_out_view(self):
        """Double the visible time span (buttons and the global − shortcut)."""
        self.plot.getPlotItem().getViewBox().scaleBy((2, 1))

    def _apply_track_mode(self, redraw=True):
        """Decoder track on: taller plot, track above everything, A/B letters in the free band below it.  Off: the classic
        layout (y-range and cursor letters exactly as before; the per-byte text rows return)."""
        on = self.track.isChecked()
        self.plot.getPlotItem().setYRange(-0.4, Y_TRACK_MAX if on else Y_CLASSIC_MAX, padding=0)
        self._cursor_fracs = CURSOR_FRACS_TRACK if on else CURSOR_FRACS_CLASSIC
        for ln, frac in zip((self.ca, self.cb), self._cursor_fracs):
            ln.label.setPosition(frac)
        self.decoder_track.setVisible(on)
        if not on:
            self.decoder_track.set_segments([])
        if redraw:
            self._ann_key = None
            self._redraw(force=True)

    # ------------------------------------------------------------------ API
    def show_packet(self, txn, synchronize=True):
        if synchronize and hasattr(self.ctx, "select_transaction"):
            self.ctx.select_transaction(txn)
            return
        self._display_context = (self.ctx.engine.decoder, self.ctx.engine.source, self.ctx.engine.caps)
        self.txn = txn
        self.follow.setChecked(False)
        self._redraw(force=True, fit=True)
        self._update_packet_panels(txn, force=True)

    def visualize(self, txn):
        """Error / arbitration review: 'Follow live' off, this exact packet fitted, its error cause or arbitration in the
        banner above the traces, every field in the decoder track and the frame breakdown below."""
        self.show_packet(txn, synchronize=False)

    def _clear_arb_marks(self):
        pi = self.plot.getPlotItem()
        for it in self._arb_items:
            pi.removeItem(it)
        self._arb_items = []

    def _update_packet_panels(self, txn, force=False):
        """Banner (error cause / arbitration), amber collision marker and the I²C data-frame breakdown of `txn`."""
        from analysis.frame_view import arbitration_events, arbitration_text, collision_point
        from gui.frame_html import alert_html, frame_table_html
        eng = self.ctx.engine
        key = (id(txn), getattr(txn, "master_id", None), eng.masters.arb_total, self.origin, len(eng.packets) if txn is None else 0)
        if key == self._alert_key and not force:
            return
        self._alert_key = key
        self._clear_arb_marks()
        if txn is None:
            self.alert.hide()
            self.frame_box.setHtml("<p>No packet on display: select one (Packet ◀ / ▶, a table row, Visualize error / "
                                   "arbitration).</p>")
            return
        text, kind = alert_html(eng, txn)
        if text:
            col = C["red"] if kind == "error" else C["amber"]
            self.alert.setText(text)
            self.alert.setStyleSheet(f"border:2px solid {col}; border-radius:6px; padding:6px 10px;")
            self.alert.show()
        else:
            self.alert.hide()
        self.frame_box.setHtml(frame_table_html(eng, txn))
        if txn is self.txn and not self.follow.isChecked():
            pi = self.plot.getPlotItem()
            for ev in arbitration_events(eng, txn)[-2:]:
                t_ns, _bit, where = collision_point(txn, ev)
                x = (t_ns - self.origin) / 1e3
                line = pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen(C["amber"], width=3))
                lbl = pg.TextItem(arbitration_text(eng, ev).split(" — ")[0] + f" · {where}", color=C["amber"],
                                  anchor=(0, 0))
                lbl.setPos(x, Y_TRACK_MAX - 0.05)
                lbl.setZValue(120)
                for it in (line, lbl):
                    pi.addItem(it)
                    self._arb_items.append(it)

    def show_stage(self, stage):
        visible = stage is not None and stage.timestamp is not None and self.txn is not None
        self.playhead.setVisible(visible)
        if visible:
            self.playhead.setValue((stage.timestamp-self.origin)/1e3)

    device_filter = None

    def set_device_filter(self, addr):
        """Step/follow only packets addressing this device. Timestamps are untouched."""
        self.device_filter = addr
        self.dev_lbl.setText(f"Device filter: 0x{addr:02X}" if addr is not None else "Device filter: all")

    def _step(self, d):
        pk = self.ctx.engine.packets
        if self.device_filter is not None:
            pk = [t for t in pk if any(f.addr == self.device_filter for f in t.frames)]
        if not pk:
            return
        if self.txn is None or self.txn not in pk:
            i = len(pk) - 1
        else:
            i = max(0, min(len(pk) - 1, pk.index(self.txn) + d))
        self.show_packet(pk[i])

    def refresh(self, engine):
        # A new engine decoder denotes a cleared/new capture. Even a paused
        # packet view must not retain graphics from a different data source.
        context = (engine.decoder, engine.source, engine.caps)
        now = time.monotonic()
        if context != self._display_context:
            self._display_context = context
            self.txn = None
            self._shown = self._shown_snap = self._valid_ctx = None
            self._plan = self._sweep_key = self._draw_key = self._cursor_anchor = None
            self._free_run = False
            self._redraw()
            return
        if not self.follow.isChecked():
            self.banner.update_from(engine, self.txn)
            return
        rate = 1 / int(self.refresh_rate.currentText().split()[0])
        kind = self._kind(engine)
        if self.win.currentText() == AUTO_WINDOW:
            w = self._window_ns()
            if w != self._auto_w:                          # the bus clock changed (or first packets measured)
                first, self._auto_w = self._auto_w is None, w
                if not first:
                    self._reset_sweep()
        if kind == "snapshot":
            self._refresh_segment(engine, now, rate)
        else:
            self._refresh_continuous(engine, kind, now, rate)

    # ------------------------------------------------------------------ continuous sources: stream / legacy edges
    @staticmethod
    def _kind(eng):
        """'stream' = continuous RLE edge stream (new analyzer firmware) · 'snapshot' = isolated 2 ms windows (older
        segment firmware) · 'edges' = legacy continuous raw edges."""
        if getattr(eng, "stream_active", False):
            return "stream"
        return "snapshot" if eng.caps.has("segment") else "edges"

    @staticmethod
    def _t_now(eng, kind):
        return eng.stream_now if kind == "stream" else eng.hw_now

    def _reset_sweep(self):
        """Trigger mode / window changed: forget the held sweep and show the new setting immediately."""
        self._plan = self._sweep_key = self._draw_key = self._cursor_anchor = None
        self._free_run = False
        self._redraw(force=True)

    def scl_hz(self, eng=None):
        """(rate in Hz, where it came from): the median of the recently MEASURED packets, else the SCL rate applied to
        the controller(s), else 100 kHz.  Measurement wins: a requested rate only matters until packets show the truth."""
        eng = eng or self.ctx.engine
        vals = sorted(t.timing.get("frequency_hz") for t in eng.packets[-12:] if t.timing.get("frequency_hz"))
        if len(vals) >= 3:
            return vals[len(vals) // 2], "measured"
        st = getattr(eng, "scl_setting", None)
        if st and (st.get("applied_hz") or st.get("requested_hz")):
            return float(st.get("applied_hz") or st.get("requested_hz")), "applied setting"
        if vals:
            return vals[len(vals) // 2], "measured"
        return float(DEFAULT_SCL_HZ), "default"

    def _window_ns(self):
        """The effective time base in ns (the combo's value, or the SCL-derived one for Auto)."""
        w = WINDOWS[self.win.currentText()]
        if w is not None:
            return w
        hz, _src = self.scl_hz()
        want_us = AUTO_SCL_US_KHZ / max(1.0, hz / 1e3)
        snap = min(AUTO_SNAPS_US, key=lambda u: abs(np.log(u / want_us)))
        return snap * 1e3

    def _window_text(self):
        w = self._window_ns()
        us = w / 1e3
        text = f"{us:g} µs" if us < 1000 else f"{us / 1e3:g} ms"
        if self.win.currentText() == AUTO_WINDOW:
            hz, src = self.scl_hz()
            return f"{text} (Auto SCL {hz / 1e3:g} kHz, {src})"
        return text

    def _roll_plan(self, eng, t_now):
        w = self._window_ns()
        return dict(t0=t_now - w, t1=t_now, key=None, label=f"Rolling time base · {self._window_text()}")

    def _candidate(self, eng, kind, t_now, mode):
        """The newest COMPLETE START-triggered sweep (window around one finished transaction), or None.

        Mode 'Address' only triggers on the selected device.  A sweep is complete once the stream has reached the end
        of its window, so what is shown is never a half-drawn sweep."""
        w = self._window_ns()
        records = eng.waveform_transactions if kind == "stream" else eng.packets
        want = self.device_filter if mode == "Address" else None
        for t in reversed(records[-60:]):
            if t.t_end is None or t.status != "ok":
                continue
            if want is not None and not any(f.addr == want for f in t.frames):
                continue
            dur = t.t_end - t.t_start
            auto = self.win.currentText() == AUTO_WINDOW
            long_pkt = dur > (0.9 if auto else 0.85) * w
            # A fixed window widens to hold a long packet.  Auto (SCL) keeps its bit-level scale instead: the whole packet
            # is plotted but the view starts on its first window (START, address, first bytes); drag to pan along it.
            win = w if (not long_pkt or auto) else dur * 1.2
            span_ns = (dur + 0.2 * w) if (long_pkt and auto) else win
            t0 = t.t_start - 0.1 * win
            t1 = t0 + span_ns
            if t1 > t_now:
                continue            # still being swept: use the newest COMPLETE one (busy buses never go quiet)
            addr = t.address
            note = (f" · first {win / 1e3:g} µs of {dur / 1e3:.0f} µs — drag to pan" if long_pkt and auto else "")
            return dict(t0=t0, t1=t1, key=(round(t.t_start), round(t.t_end)),
                        view_us=(win / 1e3 if long_pkt and auto else None),
                        label=f"Triggered sweep · {mode} · packet at "
                              f"{'0x%02X' % addr if addr is not None else '?'} · {self._window_text()}{note}")
        return None

    def _refresh_continuous(self, eng, kind, now, rate):
        """Oscilloscope behaviour: a held, complete triggered sweep (zero flicker) that is replaced only by the next
        complete trigger, with a continuously rolling time base (idle = flat level) as the free-run fallback."""
        t_now = self._t_now(eng, kind)
        if t_now is None:
            if self._shown is None and now - self.last_draw > rate:
                self._redraw()
            return
        mode = self.trigger.currentText()
        cand = None if mode == "Roll" else self._candidate(eng, kind, t_now, mode)
        if (cand is not None and cand["key"] != self._sweep_key
                and (self._shown is None or self._sweep_key is None or self._free_run
                     or now - self._shown_at >= DWELL_S)):
            self._plan, self._sweep_key, self._free_run, self._sweep_wall = cand, cand["key"], False, now
            self._redraw()
            return
        timed_out = mode == "Auto" and self._sweep_key is not None and now - self._sweep_wall > AUTO_TIMEOUT_S
        if mode == "Roll" or self._plan is None or self._free_run or timed_out:
            if now - self.last_draw >= rate:                       # free-run: roll with the stream, throttled
                self._free_run = True
                self._plan = self._roll_plan(eng, t_now)
                self._redraw()
            return
        self._compose_info(eng)                                    # holding the sweep: nothing to redraw
        self.banner.update_from(eng, self._banner_txn(eng))

    def _refresh_segment(self, engine, now, rate):
        """Live segment analyzer: the window on display only changes when a newer VALID snapshot (one that decoded
        at least one transaction) has arrived AND the current one has been shown for DWELL_S.  Idle windows and
        the gaps between bursts never blank or replace it; only the age text updates."""
        snap = engine.waveform_snapshots
        valid = bool(engine.waveform_transactions)
        if self._shown is None:
            if now - self.last_draw > rate:
                self._redraw()
        elif snap != self._shown_snap and valid and now - self._shown_at >= DWELL_S:
            self._redraw()
        else:
            self._compose_info(engine)
            self.banner.update_from(engine, self._banner_txn(engine))

    def _banner_txn(self, engine):
        """The packet the header banner describes: the one CURSOR A is inside (when A sits on a packet of the window on
        display), so the banner names the packet measured at the bottom; else the selected packet (Follow off) or the
        newest packet on screen."""
        if self._shown is not None:
            t0, t1 = self._shown["t0"], self._shown["t1"]
            on_screen = [t for t in reversed(engine.packets[-400:]) if t.t_start <= t1 and (t.t_end or t.t_start) >= t0]
            if self.cur.isChecked() and hasattr(self, "ca"):
                a_ns = self.origin + self.ca.value() * 1e3
                for t in on_screen:
                    if t.t_start <= a_ns <= (t.t_end or t.t_start):
                        return t
            if not self.follow.isChecked():
                return self.txn
            return on_screen[0] if on_screen else None
        return self.txn if not self.follow.isChecked() else None

    # ------------------------------------------------------------------ annotation items are REUSED
    def _release_ann(self):
        """Hide every annotation and return it to its pool (no remove/add: nothing flashes or is rebuilt)."""
        for it in self.ann_items:
            it.setVisible(False)
            if isinstance(it, pg.TextItem):
                self._text_pool.append(it)
            else:
                self._line_pool.setdefault(getattr(it, "_kind", "?"), []).append(it)
        self.ann_items.clear()

    _clear_ann = _release_ann

    def _label_fonts(self):
        if self._fonts is None:
            base = QFont(QApplication.font())
            small = QFont(base)
            small.setPointSizeF(max(6.5, base.pointSizeF() * 0.8))    # compact: one bit digit per ~11 px
            self._fonts = (base, small)
        return self._fonts

    def _text(self, text, color, anchor=(0.5, 0.5), small=False):
        norm, compact = self._label_fonts()
        if self._text_pool:
            lab = self._text_pool.pop()
            if lab.toPlainText() != text:
                lab.setText(text)
            lab.setColor(color)
            lab.anchor = pg.Point(anchor)
        else:
            lab = pg.TextItem(text, color=color, anchor=anchor)
            lab.setZValue(50)
            self.plot.getPlotItem().addItem(lab, ignoreBounds=True)
        # Compact bit digits get no document margin (4 px each side by default): a digit is then only as wide as its
        # glyph, centres exactly on its clock pulse and fits the ~11-14 px bit pitch at 400 kHz.
        lab.textItem.document().setDocumentMargin(0 if small else 4)
        lab.setFont(compact if small else norm)                       # also re-lays the text out (updateTextPos)
        return lab

    def _event_line(self, kind, color):
        pool = self._line_pool.get(kind)
        if pool:
            ln = pool.pop()
        else:
            ln = pg.InfiniteLine(angle=90, pen=pg.mkPen(color, width=1.2, style=Qt.PenStyle.DotLine),
                                 label=kind, labelOpts={"color": color, "position": 0.06})
            ln._kind = kind
            self.plot.getPlotItem().addItem(ln, ignoreBounds=True)
        return ln

    def _message(self, text):
        self.msg.setText(text)
        vr = self.plot.getPlotItem().viewRange()
        self.msg.setPos((vr[0][0] + vr[0][1]) / 2, 1.9)

    def _no_data(self, message):
        self._set_curves([], [])
        self._release_ann()
        self.decoder_track.set_segments([])         # no stale protocol boxes next to a "no waveform" message
        self._shown = self._shown_snap = self._valid_ctx = None
        self._ann_key = None
        self.title.setText("No raw waveform")
        self._info_base = message.replace("\n", " ")
        self.info.setText(self._info_base)
        self.cursor_lbl.clear()
        self.ca.hide()
        self.cb.hide()
        self._message(message)

    def _keeps_last(self, eng):
        """Live segment mode with a valid window already on screen: keep it instead of blanking."""
        return (self.follow.isChecked() and (eng.caps.has("segment") or getattr(eng, "stream_active", False))
                and self._shown is not None and self._valid_ctx == (eng.decoder, eng.source, eng.caps))

    def _soft_no_data(self, eng, message):
        if self._keeps_last(eng):
            self.info.setText(self._info_base + " · waiting for the next measured window")
            return
        self._no_data(message)

    def _compose_info(self, eng, dense=False):
        """Info line = stable base text + (age of the live window, whole seconds) + (zoom hint)."""
        text = self._info_base
        if (eng.caps.has("segment") and not getattr(eng, "stream_active", False) and self.follow.isChecked()
                and eng.waveform_received_at is not None):
            text += f" · received {int(time.monotonic() - eng.waveform_received_at)} s ago"
        if dense:
            text += " · zoom in / pick a packet for byte annotations"
        if self.txn is not None:
            text += " · " + timing_summary(self.txn)
        if text != self.info.text():
            self.info.setText(text)
            self._fit_labels()

    # ------------------------------------------------------------------ drawing
    def _fit_labels(self):
        """Give the info / cursor readout exactly the height their wrapped text needs at their CURRENT width (a label
        that has not been laid out yet keeps one line per text line until its first resize re-fits it)."""
        for lb in (getattr(self, "info", None), getattr(self, "cursor_lbl", None)):
            if lb is None:
                continue
            if not lb.text():
                need = 0
            elif lb.width() > 50 and lb.isVisible():
                need = lb.heightForWidth(lb.width())
                if need < 0:
                    need = lb.sizeHint().height()
            else:
                need = lb.fontMetrics().lineSpacing() * (lb.text().count("\n") + 1) + 4
            if lb.minimumHeight() != need:
                lb.setMinimumHeight(need)

    def eventFilter(self, obj, ev):
        from PyQt6.QtCore import QEvent
        if ev.type() == QEvent.Type.Resize and obj in (getattr(self, "info", None), getattr(self, "cursor_lbl", None)):
            if ev.oldSize().width() != ev.size().width():
                QTimer.singleShot(0, self._fit_labels)    # after this layout pass: never re-enter the layout
        return super().eventFilter(obj, ev)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_labels()

    def _place_default_cursors(self, span):
        self.ca.setValue(span * 0.3)
        self.cb.setValue(span * 0.3 + min(10.0, span * 0.1))

    def _redraw(self, force=False, fit=False, user_fit=False):
        self.last_draw = time.monotonic()
        eng = self.ctx.engine
        self.msg.setText("")
        kind = self._kind(eng)
        live_seg = self.follow.isChecked() and kind == "snapshot"
        continuous = self.follow.isChecked() and kind != "snapshot"
        if eng.source == "none" and not eng.edges:
            self._no_data("No capture. Connect the analyzer and start a capture.")
            return
        if eng.caps.known and not eng.caps.has("edges"):
            self._no_data("Waveform unavailable — raw capture data required.\n"
                          "The connected analyzer firmware does not report edge records.")
            return
        if self.follow.isChecked():
            if kind == "snapshot":
                if not eng.edges:
                    self._soft_no_data(eng, "Waiting for a measured waveform window.\n"
                                            "Start capture and check SDA/SCL traffic.")
                    return
                t1 = eng.edges[-1][0]
                t0 = max(eng.edges[0][0], t1 - self._window_ns())
                self.title.setText("Latest hardware snapshot · " + self._window_text())
            else:
                t_now = self._t_now(eng, kind)
                if t_now is None:
                    self._soft_no_data(eng, "Waiting for the continuous edge stream." if kind == "stream"
                                       else "Waiting for timestamped raw capture data.")
                    return
                if not eng.edges:               # nothing was ever sampled: the level is unknown, never assumed idle
                    self._soft_no_data(eng, "No raw samples in this window; bus state is unknown.")
                    return
                if self._plan is None:
                    self._plan = self._roll_plan(eng, t_now)
                t0, t1 = self._plan["t0"], self._plan["t1"]
                self.title.setText(self._plan["label"])
            self.txn = None
        else:
            t = self.txn
            if t is None:
                self._no_data("Select a packet from this capture or enable Follow live.")
                return
            pad = max(20_000.0, t.duration_ns * 0.06)
            t0, t1 = t.t_start - pad, (t.t_end or t.t_start) + pad
            from analysis.multi_master import attribution_text
            self.title.setText(f"Packet #{t.id} · {eng.registry.name(t.address)[0]} · {t.operation} · "
                               f"master: {attribution_text(t)}")
            self.title.setToolTip(self.title.text())
            if not eng.edges_available(t.t_start, t.t_end or t.t_start):
                self._no_data("Raw edges for this packet are no longer in memory (or fall in a capture gap).\n"
                              "Decoded data is still available in the Packet Analyzer.")
                return
        if eng.edges:
            t0 = max(t0, eng.edges[0][0])
            if not continuous:
                t1 = min(t1, eng.edges[-1][0])
        # A live snapshot is an isolated window: gaps BETWEEN snapshots are not inside it.  A continuous view never
        # blanks: real data loss is drawn as a GAP marker, idle time as the held (flat) level.
        if not live_seg and not continuous and any(t0 <= g <= t1 for g in eng.edge_gaps):
            self._no_data("Capture gap in this window — select a contiguous packet; raw records retained")
            return
        ts, ss = eng.edges_between(t0, t1)
        if len(ts) == 0:
            self._soft_no_data(eng, "No raw samples in this window; bus state is unknown.")
            return
        key = (kind, round(t0), round(t1), self.win.currentText(), self.trigger.currentText(), self.ann.isChecked(),
               len(ts), float(ts[-1]), round(self.plot.getPlotItem().getViewBox().width()))
        if not force and not fit and key == self._draw_key:
            self.banner.update_from(eng, self._banner_txn(eng))      # same window, same samples: draw nothing
            return
        self._draw_key = key
        self.origin = t0
        self._window_end = t1
        x = (np.clip(ts, t0, t1) - t0) / 1e3
        x = np.append(x, (t1 - t0) / 1e3)
        scl = ((ss >> 1) & 1).astype(float)
        sda = (ss & 1).astype(float)
        self._set_curves(x, (np.append(scl, scl[-1]) + SCL_Y, np.append(sda, sda[-1]) + SDA_Y))
        txns = self._txns_for(eng, t0, t1)
        self._shown = dict(t0=t0, t1=t1, txns=txns)
        self._shown_snap = eng.waveform_snapshots
        self._shown_at = time.monotonic()
        self._valid_ctx = (eng.decoder, eng.source, eng.caps)
        if self.follow.isChecked() or fit:
            view_us = (self._plan or {}).get("view_us") if (continuous and not fit) else None
            span = view_us or (t1 - t0) / 1e3                # a long packet in Auto: first window only (pan for the rest)
            self.plot.getPlotItem().setXRange(0, span, padding=0.035)
            # Cursors A / B are placed ONCE (the first window drawn).  New packets, sweeps, rolling windows and redraws
            # never move them again: they stay exactly where the user put them, so any two points can be measured.
            # Only an explicit "Fit packet" brings them back - and only if they are outside the visible time range.
            outside = not all(0.0 <= c.value() <= span for c in (self.ca, self.cb))
            if not self._cursors_init or (user_fit and outside):
                self._place_default_cursors(span)
                self._cursors_init = True
        self.ca.setVisible(self.cur.isChecked())
        self.cb.setVisible(self.cur.isChecked())
        tick = eng.caps.tick_ns or eng.tick_ns
        demo = " · DEMO — SIMULATED DATA · SIMULATED — NOT VERIFIED HARDWARE" if eng.source == "demo" else (" · recorded" if eng.source == "replay" else "")
        if kind == "snapshot":
            demo += " · measured window; intervals between windows are not shown"
        elif kind == "stream":
            demo += (" · continuous RLE edge stream: idle bus time is the held level (flat HIGH), "
                     f"{eng.stream_gaps} data-loss gap(s)")
        provenance = "simulated state records" if eng.source == "demo" else "captured state records"
        self._info_base = (f"{len(ts):,} {provenance} · sampling {1e3 / tick:.0f} MS/s "
                           f"({tick:.0f} ns resolution){demo} · square-wave = logic level between recorded edges")
        # The readout first, and its height applied NOW: laying the labels out first and growing the readout after it
        # shrank the plot under labels already placed, so two could touch until the 60 ms re-layout timer fired.
        self._cursor_text()
        from PyQt6.QtCore import QEvent
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        self._annotate(txns, t0, t1, force=True)
        self.banner.update_from(eng, self._banner_txn(eng) if self.follow.isChecked() else self.txn)
        self._update_packet_panels(self._banner_txn(eng) if self.follow.isChecked() else self.txn)

    def _set_curves(self, x, ys):
        if not len(x):
            for c in (self.c_scl, self.c_sda, self.c_scl_h, self.c_sda_h):
                c.setData([], [])
            return
        scl, sda = ys
        # Qt strokes any pen wider than 1 px by building the stroke outline of the whole path (~0.25 ms per vertex,
        # more with the halo's second pen): a 5 ms window of 100 kHz traffic (6000 vertices) took 4 s per paint and
        # starved the GUI thread, and with it the serial readers.  A 1 px cosmetic pen is a hairline (11 ms for 400
        # vertices).  Measured at 400 kHz scale (200 vertices): wide+halo 138 ms, wide 57 ms, hairline 11 ms.
        n = len(x)
        tier = 2 if n > WIDE_PEN_MAX_VERTICES else 1 if n > HALO_MAX_VERTICES else 0
        if tier != self._dense:
            self._dense = tier
            for c, base in ((self.c_scl, C["scl"]), (self.c_sda, C["sda"])):
                c.setPen(pg.mkPen(base, width=1 if tier == 2 else 2))
            self.c_scl_h.setVisible(tier == 0)
            self.c_sda_h.setVisible(tier == 0)
        self.c_scl.setData(x, scl)
        self.c_sda.setData(x, sda)
        if tier == 0:
            self.c_scl_h.setData(x, scl)
            self.c_sda_h.setData(x, sda)

    def _txns_for(self, eng, t0, t1):
        records = eng.waveform_transactions if eng.caps.has("segment") else eng.packets
        found = [t for t in records[-400:] if (t.t_end or t.t_start) >= t0 and t.t_start <= t1]
        if not found and eng.caps.has("segment"):
            # An older window (a packet opened from a table, Visualize error / arbitration): its raw-edge decode may have
            # scrolled out of the recent list (the ARB test alone makes ~400 transfers), but a packet whose two decodes
            # agreed carries the same measured bit times - draw from it (found by binary search, time ordered).
            import bisect
            pk = eng.packets
            i = bisect.bisect_left(pk, t0 - 5e6, key=lambda t: t.t_start)
            while i < len(pk) and pk[i].t_start <= t1:
                t = pk[i]
                if (t.t_end or t.t_start) >= t0 and t.bytes and all(b.bit_times for b in t.bytes):
                    found.append(t)
                i += 1
        return found

    def _reannotate(self):
        """Zoom/pan changed the visible range: lay the labels out again (same displayed window, same data)."""
        eng = self.ctx.engine
        if not hasattr(self, "_window_end") or (self._shown is None and self.txn is None):
            return
        if self._shown is not None and self.follow.isChecked() and eng.caps.has("segment"):
            txns = self._shown["txns"]                    # the snapshot on screen, not whatever arrived since
        else:
            txns = self._txns_for(eng, self.origin, self._window_end)
        self._annotate(txns, self.origin, self._window_end)

    def _annotate(self, txns, t0, t1, force=False):
        pi = self.plot.getPlotItem()
        vb = pi.getViewBox()
        vx, vy = pi.viewRange()
        key = (round(vx[0], 2), round(vx[1], 2), round(vb.width()), self.ann.isChecked(), id(self._shown), t0, t1,
               tuple(id(t) for t in txns))
        if not force and key == self._ann_key:
            return                                        # nothing changed: leave the labels exactly as they are
        self._ann_key = key
        self._release_ann()
        track_on = self.track.isChecked()
        if not self.ann.isChecked():
            self.decoder_track.set_segments([])
            self._compose_info(self.ctx.engine)
            return
        span = max(1e-9, vx[1] - vx[0])
        vb_w, vb_h = max(200.0, float(vb.width())), max(120.0, float(vb.height()))
        yspan = max(1e-9, vy[1] - vy[0])
        placed = []                                       # pixel rects of every label shown: real collision test
        self.label_px = []
        # The classic text rows are 0.3 y-units apart (ACK 2.85 · ADDR / DATA 3.15 · R/W 3.55 · raw byte 3.78).  In a
        # short plot (the data-frame card below the waveform takes ~230 px: the plot can be ~260 px) that is less than
        # one full-size label (~22 px), so labels collided and were DROPPED (a read frame lost its ADDR / R=1 and its
        # ACK / NACK).  Then every row uses the compact font (~12 px) instead: all labels stay, none overlaps.
        probe = self._text("ACK", C["text"])
        full_h = probe.boundingRect().height()
        probe.setVisible(False)
        self._text_pool.append(probe)
        compact_rows = 0.3 / yspan * vb_h < full_h + 2

        def px(x_us):
            return (x_us - vx[0]) / span * vb_w

        def py(y):
            return (vy[1] - y) / yspan * vb_h

        if self.cur.isChecked():                          # the A/B cursor letters own a strip at the top of their lines
            for ln, frac in zip((self.ca, self.cb), self._cursor_fracs):      # exactly where each letter is drawn
                cx = px(ln.value())
                top = (1 - frac) * vb_h
                placed.append((cx - 3, cx + 16, max(0.0, top - 4), top + 18))
        n_reserved = len(placed)

        def put(text, x_us, y, color, anchor=0.5, small=False, slide=True, within=None):
            """Show the label only if it is fully inside the plot and touches no other label.  A label blocked by
            a neighbour or a cursor letter may slide sideways (up to ~60 % of its width) before it is dropped; compact
            bit digits never slide (they must stay on their own clock pulse) and use a tighter margin."""
            small = small or compact_rows
            lab = self._text(text, color, anchor=(anchor, 0.5), small=small)
            w = lab.boundingRect().width()
            # the label's REAL rendered height (a fixed 15 px under-estimated the ~22 px text: two rows only 21 px apart
            # in a shorter plot overlapped by a pixel instead of sliding aside)
            h = lab.boundingRect().height() if small else max(LABEL_H_PX, lab.boundingRect().height())
            pad = 1 if small else 3
            y0, y1 = py(y) - h / 2, py(y) + h / 2
            # within=(lo_us, hi_us): the label's own byte.  Then it may also slide further (a short plot puts the A/B cursor
            # letters on the R/W row), but only to positions that keep it inside that byte - never onto a neighbour.
            slides = (0.0, 0.6, -0.6, 1.2, -1.2, 1.8, -1.8) if (slide and within) else (0.0, 0.6, -0.6) if slide else (0.0,)
            for slide_f in slides:
                dx = slide_f * (w + 10)
                x0 = px(x_us) + dx - w * anchor - pad
                x1 = x0 + w + 2 * pad
                if abs(slide_f) > 0.6 and not (px(within[0]) <= x0 and x1 <= px(within[1])):
                    continue
                if x0 < 0 or x1 > vb_w or y0 < 0 or y1 > vb_h or any(
                        x0 < b[1] and x1 > b[0] and y0 < b[3] and y1 > b[2] for b in placed):
                    continue
                placed.append((x0, x1, y0, y1))
                lab.setPos(x_us + dx / vb_w * span, y)
                lab.setVisible(True)
                self.ann_items.append(lab)
                return True
            self._text_pool.append(lab)
            lab.setVisible(False)
            return False

        ev_col = {"S": C["cyan"], "Sr": C["blue"], "P": C["text"]}
        for g in [g for g in self.ctx.engine.edge_gaps if t0 <= g <= t1][-20:]:   # REAL data loss, never idle time
            ln = self._event_line("GAP", C["amber"])
            ln.setPos((g - t0) / 1e3)
            ln.setVisible(True)
            ln.label.setVisible(True)
            self.ann_items.append(ln)
        nbytes = sum(len(t.bytes) for t in txns)
        dense = nbytes > 160
        events = [(tt, k) for t in txns for tt, k in t.events if t0 <= tt <= t1]
        if len(events) <= MAX_EVENT_LINES:
            last_px = -1e9
            for tt, k in sorted(events):
                ln = self._event_line(k, ev_col[k])
                ln.setPos((tt - t0) / 1e3)
                ln.setVisible(True)
                show_label = px((tt - t0) / 1e3) - last_px >= 16        # dense markers keep their line, drop the letter
                ln.label.setVisible(show_label)
                if show_label:
                    last_px = px((tt - t0) / 1e3)
                self.ann_items.append(ln)
        for t in ([] if dense else txns):
            # One style per packet: bytes differ by < 1 us (22.0 vs 23.0 us at 400 kHz), which would otherwise flip single
            # bytes across a tier boundary.  The packet's median byte width decides for all of its bytes.
            spans = sorted(bb.t_ack - bb.t_first for bb in t.bytes if bb.bit_times)
            nominal_px = (spans[len(spans) // 2] / 1e3 / span * vb_w) if spans else 0.0
            for b in t.bytes:
                if not (t0 <= b.t_first <= t1) or not b.bit_times:
                    continue
                if (b.t_ack - t0) / 1e3 < vx[0] or (b.t_first - t0) / 1e3 > vx[1]:
                    continue                                                    # panned/zoomed out of view: no label work
                fr = t.frames[b.frame]
                bpx = nominal_px                                              # pixels available to a byte of this packet
                if bpx < MIN_LABEL_BYTE_PX:
                    continue                                                    # zoomed far out: markers only
                col = (C["cyan"] if fr.rw == "R" else C["blue"]) if b.kind.startswith("addr") else C["text"]
                mid = (b.bit_times[0] + b.bit_times[7]) / 2
                if track_on:
                    bit_px = bpx / 9.0
                    if bit_px >= BIT_MIN_PX:                 # the track owns ADDR / DATA / ACK; bit digits stay between SDA and SCL
                        pitch = (b.bit_times[7] - b.bit_times[0]) / 7.0
                        for i, tt in enumerate(b.bit_times):
                            put(str((b.value >> (7 - i)) & 1), (tt + 0.25 * pitch - t0) / 1e3, 1.25, C["text2"],
                                small=True, slide=False)
                    continue
                # 4: wide (verbose) · 3: whole-byte at 400 kHz-in-250 µs scale (address + R/W + raw byte, DATA 0x..)
                # 2: short · 1: hex only.  Bit digits are independent: they appear as soon as a bit is BIT_MIN_PX wide.
                tier = 4 if bpx >= TIER_PX[0] else 3 if bpx >= TIER_PX[1] else 2 if bpx >= TIER_PX[2] else 1
                if b.kind == "addr":
                    if tier >= 3:
                        # 7 address bits (MSB first) are separate from the 8th R/W bit; the 8-bit byte on the wire
                        # (0xEC = 0x76<<1 | 0) is shown below them.
                        put(f"ADDR 0x{fr.addr:02X}" + (" (7-bit)" if tier == 4 else ""),
                            ((b.bit_times[0] + b.bit_times[6]) / 2 - t0) / 1e3, 3.15, col)
                        put("R=1" if fr.rw == "R" else "W=0", (b.bit_times[7] - t0) / 1e3, 3.55, col,
                            within=((b.t_first - t0) / 1e3, (b.t_ack - t0) / 1e3))
                        put((f"byte 0x{b.value:02X} = 0x{fr.addr:02X}<<1 | {1 if fr.rw == 'R' else 0}" if tier == 4
                             else f"0x{b.value:02X}"), (mid - t0) / 1e3, 3.78, C["text2"])
                    else:
                        put((f"0x{fr.addr:02X} {fr.rw}" if tier == 2 else f"0x{fr.addr:02X}"), (mid - t0) / 1e3, 3.15, col)
                else:
                    full = f"DATA 0x{b.value:02X}" if b.kind == "data" else f"ADDR10 0x{b.value:02X}"
                    put(full if tier >= 3 else f"{b.value:02X}", (mid - t0) / 1e3, 3.15, col)
                if b.ack:
                    ack_txt = "ACK" if tier >= 3 else "A"
                else:
                    ack_txt, kind_nack = "NACK", "plain"
                    if b.kind == "data" and fr.rw == "R":
                        nd = [x for x in t.bytes if x.frame == b.frame and x.kind == "data"]
                        last = nd and nd[-1] is b
                        boundary = b.frame + 1 < len(t.frames) or any(k == "P" for _, k in t.events)
                        kind_nack = "end" if last and boundary else "end?" if last else "unexpected"
                    full_nack = {"plain": "NACK", "end": "NACK (end of read)",
                                 "end?": "NACK (read end, STOP not captured)", "unexpected": "NACK (unexpected)"}[kind_nack]
                    short_nack = {"plain": "NACK", "end": "NACK (end)", "end?": "NACK (end?)",
                                  "unexpected": "NACK unexp."}[kind_nack]
                    ack_txt = (full_nack if tier == 4 else short_nack if tier == 3
                               else "N!" if kind_nack in ("unexpected", "plain") else "N")
                put(ack_txt, (b.t_ack - t0) / 1e3, 2.85, C["green"] if b.ack else C["red"])
                bit_px = bpx / 9.0                           # a byte is 9 clocks: 8 bits + ACK
                if bit_px >= BIT_MIN_PX:                     # bit digits only when each bit has room (compact font)
                    pitch = (b.bit_times[7] - b.bit_times[0]) / 7.0
                    for i, tt in enumerate(b.bit_times):
                        # sampled at the SCL rising edge; centre the digit on the HIGH pulse (~quarter of a period on)
                        put(str((b.value >> (7 - i)) & 1), (tt + 0.25 * pitch - t0) / 1e3, 1.25, C["text2"],
                            small=True, slide=False)
            if t.status != "ok" and t.t_end is not None and t0 <= t.t_end <= t1 and not track_on:
                # Truncated/ambiguous: stop at the last verified event, never invent bits.  Its row sits just under the
                # top edge in PIXELS: a fixed y = 3.93 only fitted plots taller than ~470 px and was silently dropped
                probe = self._text("INCOMPLETE", C["amber"])            # the real rendered height of one label
                lab_h = max(LABEL_H_PX, probe.boundingRect().height())
                probe.setVisible(False)
                self._text_pool.append(probe)
                y_top = min(3.93, vy[1] - (lab_h / 2 + 2) * yspan / vb_h)
                put(f"{t.status.upper()} — last verified event", (t.t_end - t0) / 1e3, y_top, C["amber"], anchor=1.0)
        self.decoder_track.set_segments(self._track_segments(txns, t0, t1, vx) if track_on else [])
        self.label_px = placed[n_reserved:]               # the labels actually shown (not the cursor letters)
        self._compose_info(self.ctx.engine, dense=dense)

    @staticmethod
    def _nack_kind(t, b):
        """'plain' | 'end' (controller ends a read: expected) | 'end?' (STOP not captured) | 'unexpected' - the same
        classification as the packet table, unchanged."""
        fr = t.frames[b.frame]
        if b.kind == "data" and fr.rw == "R":
            nd = [x for x in t.bytes if x.frame == b.frame and x.kind == "data"]
            last = nd and nd[-1] is b
            boundary = b.frame + 1 < len(t.frames) or any(k == "P" for _, k in t.events)
            return "end" if last and boundary else "end?" if last else "unexpected"
        return "plain"

    def _track_segments(self, txns, t0, t1, vx):
        """Decoder boxes (data coordinates, us) from the decoded transactions: START / Sr / STOP at their event times, one
        box per byte from its first to its last SCL clock, one per ACK/NACK slot - all on the EXACT recorded timestamps."""
        eng = self.ctx.engine
        segs = []
        lo, hi = vx[0] - (vx[1] - vx[0]), vx[1] + (vx[1] - vx[0])

        def us(ns):
            return (ns - t0) / 1e3

        for t in txns:
            pitches = sorted((b.bit_times[7] - b.bit_times[0]) / 7.0 for b in t.bytes if b.bit_times)
            pitch = pitches[len(pitches) // 2] if pitches else 2500.0           # ns per SCL clock (this packet's median)
            mid = getattr(t, "master_id", None)
            who = {1: "Master 1", 2: "Master 2"}.get(mid, "controller (unattributed)")
            starts = sorted(b.bit_times[0] for b in t.bytes if b.bit_times)
            for tt, k in t.events:
                if not (t0 <= tt <= t1) or not lo <= us(tt) <= hi:
                    continue
                end = tt + pitch
                nxt = [s0 for s0 in starts if s0 > tt]
                if k in ("S", "Sr") and nxt:
                    end = min(end, nxt[0])
                col = C["gray"] if k == "P" else C["amber"]
                name = {"S": "START", "Sr": "Repeated START", "P": "STOP"}[k]
                segs.append(dict(x0=us(tt), x1=us(end), kind=k, color=col, texts=(k, k[0]),
                                 tip=f"{name} — SDA {'falls' if k != 'P' else 'rises'} while SCL is HIGH · {who} · "
                                     f"t = {fmt_time_ns(tt - t.t_start)} into the packet"))
            for b in t.bytes:
                if not b.bit_times or not (t0 <= b.t_first <= t1) or not lo <= us(b.t_first) <= hi:
                    continue
                fr = t.frames[b.frame]
                bp = (b.bit_times[7] - b.bit_times[0]) / 7.0
                rw = fr.rw if fr.rw in ("R", "W") else "W"
                col = C["cyan"] if rw == "R" else C["blue"]
                dev = eng.registry.name(fr.addr)[0] if fr.addr is not None else "?"
                when = f"t = {fmt_time_ns(b.bit_times[0] - t.t_start)} into the packet"
                if b.kind == "addr":
                    texts = (f"ADDR 0x{fr.addr:02X} {rw}", f"0x{fr.addr:02X} {rw}", f"0x{fr.addr:02X}", rw)
                    tip = (f"ADDRESS 0x{fr.addr:02X} (7-bit) · {'READ  R=1' if rw == 'R' else 'WRITE  W=0'} · byte on the wire "
                           f"0x{b.value:02X} = 0x{fr.addr:02X}<<1 | {1 if rw == 'R' else 0} · {dev} · {who} · {when}")
                elif b.kind == "data":
                    texts = (f"DATA 0x{b.value:02X}", f"{b.value:02X}")
                    tip = (f"DATA 0x{b.value:02X} ({b.value}, {b.value:08b}b) · "
                           f"{'READ: target → controller' if rw == 'R' else 'WRITE: controller → target'} · {dev} · {who} · {when}")
                else:
                    texts = (f"ADDR10 0x{b.value:02X}", f"{b.value:02X}")
                    tip = f"10-bit address byte 0x{b.value:02X} · {dev} · {who} · {when}"
                segs.append(dict(x0=us(b.bit_times[0]), x1=us(b.bit_times[7] + bp), kind=b.kind, color=col, texts=texts, tip=tip))
                a0 = b.t_ack
                if b.ack:
                    segs.append(dict(x0=us(a0), x1=us(a0 + bp), kind="ack", color=C["green"], texts=("ACK", "A"),
                                     tip=f"ACK — SDA pulled LOW at the 9th clock by the "
                                         f"{'controller (read data)' if b.kind == 'data' and rw == 'R' else 'target'} · {dev} · "
                                         f"t = {fmt_time_ns(a0 - t.t_start)} into the packet"))
                else:
                    nk = self._nack_kind(t, b)
                    texts = {"plain": ("NACK", "N"), "end": ("NACK (end)", "NACK", "N"),
                             "end?": ("NACK (end?)", "NACK", "N"), "unexpected": ("NACK (unexp.)", "NACK!", "N!")}[nk]
                    why = {"plain": "address / written byte not acknowledged (unexpected)",
                           "end": "normal end of read: the controller left SDA HIGH on the last byte (expected, still counted)",
                           "end?": "final read NACK but STOP / Sr not captured (unverified)",
                           "unexpected": "controller NACK before the final read byte (unexpected)"}[nk]
                    segs.append(dict(x0=us(a0), x1=us(a0 + bp), kind="nack", color=C["red"], texts=texts,
                                     tip=f"NACK — SDA HIGH at the 9th clock: {why} · {dev} · "
                                         f"t = {fmt_time_ns(a0 - t.t_start)} into the packet"))
            if t.status != "ok" and t.t_end is not None and t0 <= t.t_end <= t1:
                segs.append(dict(x0=us(t.t_end), x1=us(t.t_end + 3 * pitch), kind="status", color=C["amber"],
                                 texts=(t.status.upper(), t.status[:3].upper()),
                                 tip=f"{t.status.upper()} — last verified event: nothing after this was decoded, no bits are invented"))
        return segs

    def _cycle_at(self, t_ns):
        """The individual SCL cycle (rise -> next rise, HIGH / LOW split, duty) under capture time t_ns, or None."""
        from analysis import scl_timing
        eng = self.ctx.engine
        records = eng.waveform_transactions if eng.caps.has("segment") else eng.packets
        near = [t for t in records[-400:] if t.t_start <= t_ns <= (t.t_end or t.t_start)]
        cyc = scl_timing.cycle_at(near, t_ns, eng.edges_between if eng.edges else None)
        if cyc is not None:
            cyc["duty_pct"] = (100.0 * cyc["high_ns"] / cyc["period_ns"]
                               if cyc["high_ns"] is not None and cyc["period_ns"] else None)
        return cyc

    def cursor_measurement(self):
        """Cursor A and cursor B: graph position, capture time and the individual SCL cycle under each; the A <-> B delta
        and its reciprocal ("capture frequency" 1/dt).  'cycle' = the cycle at A (kept for older callers)."""
        x_a, x_b = self.ca.value(), self.cb.value()
        a_ns = self.origin + x_a * 1e3
        b_ns = self.origin + x_b * 1e3
        dt = abs(b_ns - a_ns)
        cyc_a, cyc_b = self._cycle_at(a_ns), self._cycle_at(b_ns)
        return dict(x_a_us=x_a, x_b_us=x_b, a_ns=a_ns, b_ns=b_ns, dt_ns=dt, freq_hz=1e9 / dt if dt > 0 else None,
                    cycle=cyc_a, cycle_a=cyc_a, cycle_b=cyc_b)

    @staticmethod
    def _cycle_line(name, x_us, t_ns, c):
        from analysis.playback import format_capture_ns
        head = (f"Cursor {name}: x_{name} = {x_us:.2f} µs · capture time {format_capture_ns(t_ns)} "
                f"({t_ns / 1e3:,.3f} µs)")
        if not c:
            return head + (f" · SCL cycle at {name}: UNKNOWN (no captured SCL cycle here: idle bus, START/STOP boundary "
                           "or capture gap)")
        hl = (f" · HIGH {c['high_ns'] / 1e3:.2f} µs · LOW {c['low_ns'] / 1e3:.2f} µs · duty {c['duty_pct']:.1f} %"
              if c["high_ns"] is not None else " · HIGH / LOW UNKNOWN (no raw edges)")
        return (f"{head} · SCL cycle at {name} (packet #{c['txn_id']}): T_{name} = rise→next rise "
                f"{c['period_ns'] / 1e3:.2f} µs · f_{name} = 1/T_{name} = {c['frequency_hz'] / 1e3:.3f} kHz{hl}")

    def _cursor_text(self):
        if not self.cur.isChecked():
            self.cursor_lbl.setText("")
            self._fit_labels()
            return
        m = self.cursor_measurement()
        lines = [self._cycle_line("A", m["x_a_us"], m["a_ns"], m["cycle_a"]),
                 self._cycle_line("B", m["x_b_us"], m["b_ns"], m["cycle_b"])]
        ta, tb = m["a_ns"] / 1e3, m["b_ns"] / 1e3
        dt = m["dt_ns"] / 1e3
        delta = f"Δ (A ↔ B): Δt = |t_B − t_A| = |{tb:,.3f} − {ta:,.3f}| µs = {dt:,.3f} µs"
        if m["freq_hz"]:
            delta += f" · Capture Frequency (1/Δt) = 1 / {dt:,.3f} µs = {m['freq_hz'] / 1e3:.3f} kHz"
        else:
            delta += " · Capture Frequency (1/Δt): undefined (A and B at the same time)"
        lines.append(delta)
        text = "\n".join(lines)
        if text != self.cursor_lbl.text():
            self.cursor_lbl.setText(text)
            self._fit_labels()
        eng = self.ctx.engine
        if hasattr(self, "banner") and self._shown is not None:
            self.banner.update_from(eng, self._banner_txn(eng))   # the header names the packet under cursor A
