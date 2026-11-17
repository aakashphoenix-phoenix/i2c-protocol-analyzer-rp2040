"""Digital Waveform Viewer — SDA/SCL drawn ONLY from hardware-timestamped edge records."""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from config import COLORS as C
from gui.widgets import Card, fmt_time_ns, label

SCL_Y, SDA_Y = 1.6, 0.0
WINDOWS = {"200 µs": 2e5, "1 ms": 1e6, "5 ms": 5e6, "20 ms": 2e7, "100 ms": 1e8}


class WaveformPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.txn = None
        self.origin = 0.0
        self.last_draw = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        bar = Card()
        r = QHBoxLayout()
        self.follow = QCheckBox("Follow live")
        self.follow.setChecked(True)
        self.win = QComboBox()
        self.win.addItems(list(WINDOWS))
        self.win.setCurrentText("1 ms")
        self.prev = QPushButton("◀ Previous packet")
        self.next = QPushButton("Next packet ▶")
        self.fit = QPushButton("Fit packet")
        self.cur = QCheckBox("Cursors A/B")
        self.cur.setChecked(True)
        self.ann = QCheckBox("Annotations")
        self.ann.setChecked(True)
        for w in (self.follow, label("Window", "cardHint"), self.win, self.prev, self.next, self.fit, self.cur, self.ann):
            r.addWidget(w)
        r.addStretch(1)
        self.title = label("", "cardTitle")
        r.addWidget(self.title)
        bar.add(r)
        lay.addWidget(bar)

        pc = Card()
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#070B14")
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=False, alpha=0.15)
        pi.getAxis("left").setTicks([[(SCL_Y + 0.5, "SCL"), (SDA_Y + 0.5, "SDA")]])
        pi.getAxis("left").setWidth(48)
        pi.getAxis("bottom").setLabel("time", units="µs", color=C["text2"])
        pi.getAxis("bottom").enableAutoSIPrefix(False)
        for ax in ("left", "bottom"):
            pi.getAxis(ax).setPen(pg.mkPen(C["border"]))
            pi.getAxis(ax).setTextPen(pg.mkPen(C["text2"]))
        pi.setYRange(-0.4, 3.6, padding=0)
        pi.getViewBox().setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        halo_scl, halo_sda = pg.mkColor(C["scl"]), pg.mkColor(C["sda"])
        halo_scl.setAlpha(50)
        halo_sda.setAlpha(50)
        self.c_scl_h = self.plot.plot([], [], pen=pg.mkPen(halo_scl, width=6), stepMode="left")
        self.c_sda_h = self.plot.plot([], [], pen=pg.mkPen(halo_sda, width=6), stepMode="left")
        self.c_scl = self.plot.plot([], [], pen=pg.mkPen(C["scl"], width=2), stepMode="left")
        self.c_sda = self.plot.plot([], [], pen=pg.mkPen(C["sda"], width=2), stepMode="left")
        self.ann_items: list = []
        self.ca = pg.InfiniteLine(pos=0, movable=True, pen=pg.mkPen(C["blue"], width=1.5, style=Qt.PenStyle.DashLine),
                                  label="A", labelOpts={"color": C["blue"], "position": 0.97})
        self.cb = pg.InfiniteLine(pos=10, movable=True, pen=pg.mkPen(C["blue"], width=1.5, style=Qt.PenStyle.DashLine),
                                  label="B", labelOpts={"color": C["blue"], "position": 0.97})
        for c in (self.ca, self.cb):
            pi.addItem(c)
            c.sigPositionChanged.connect(self._cursor_text)
        self.msg = pg.TextItem("", color=C["amber"], anchor=(0.5, 0.5))
        self.msg.setZValue(100)
        pi.addItem(self.msg)
        pc.add(self.plot, 1)
        self.info = label("", "cardHint")
        self.cursor_lbl = label("", "mono")
        row = QHBoxLayout()
        row.addWidget(self.info, 1)
        row.addWidget(self.cursor_lbl)
        pc.add(row)
        lay.addWidget(pc, 1)

        self.follow.toggled.connect(lambda v: self._redraw(force=True))
        self.win.currentTextChanged.connect(lambda v: self._redraw(force=True))
        self.prev.clicked.connect(lambda: self._step(-1))
        self.next.clicked.connect(lambda: self._step(1))
        self.fit.clicked.connect(lambda: self._redraw(force=True, fit=True))
        self.cur.toggled.connect(lambda v: (self.ca.setVisible(v), self.cb.setVisible(v), self._cursor_text()))
        self.ann.toggled.connect(lambda v: self._redraw(force=True))

    # ------------------------------------------------------------------ API
    def show_packet(self, txn):
        self.txn = txn
        self.follow.setChecked(False)
        self._redraw(force=True, fit=True)

    def _step(self, d):
        pk = self.ctx.engine.packets
        if not pk:
            return
        if self.txn is None or self.txn not in pk:
            i = len(pk) - 1
        else:
            i = max(0, min(len(pk) - 1, pk.index(self.txn) + d))
        self.show_packet(pk[i])

    def refresh(self, engine):
        if self.follow.isChecked() and time.monotonic() - self.last_draw > 0.2:
            self._redraw()

    # ------------------------------------------------------------------ drawing
    def _clear_ann(self):
        pi = self.plot.getPlotItem()
        for it in self.ann_items:
            pi.removeItem(it)
        self.ann_items.clear()

    def _message(self, text):
        self.msg.setText(text)
        vr = self.plot.getPlotItem().viewRange()
        self.msg.setPos((vr[0][0] + vr[0][1]) / 2, 1.9)

    def _redraw(self, force=False, fit=False):
        self.last_draw = time.monotonic()
        eng = self.ctx.engine
        self.msg.setText("")
        if eng.source == "none" and not eng.edges:
            self._set_curves([], [])
            self._message("No capture. Connect the analyzer and start a capture.")
            return
        if eng.caps.known and not eng.caps.has("edges"):
            self._set_curves([], [])
            self._message("Waveform unavailable — raw capture data required.\n"
                          "The connected analyzer firmware does not report edge records.")
            return
        if self.follow.isChecked():
            if eng.hw_now is None:
                return
            t1 = eng.hw_now
            t0 = t1 - WINDOWS[self.win.currentText()]
            self.title.setText("Live · " + self.win.currentText())
            self.txn = None
        else:
            t = self.txn
            if t is None:
                return
            pad = max(20_000.0, t.duration_ns * 0.06)
            t0, t1 = t.t_start - pad, (t.t_end or t.t_start) + pad
            self.title.setText(f"Packet #{t.id} · {eng.registry.name(t.address)[0]} · {t.operation}")
            if not eng.edges_available(t0, t1):
                self._set_curves([], [])
                self._clear_ann()
                self._message("Raw edges for this packet are no longer in memory (or fall in a capture gap).\n"
                              "Decoded data is still available in the Packet Analyzer.")
                return
        ts, ss = eng.edges_between(t0, t1)
        self.origin = t0
        if len(ts) == 0:
            self._set_curves([], [])
            self._message("No edges in this window (bus idle).")
            return
        x = (np.clip(ts, t0, t1) - t0) / 1e3
        x = np.append(x, (t1 - t0) / 1e3)
        scl = ((ss >> 1) & 1).astype(float)
        sda = (ss & 1).astype(float)
        self._set_curves(x, (np.append(scl, scl[-1]) + SCL_Y, np.append(sda, sda[-1]) + SDA_Y))
        if self.follow.isChecked() or fit:
            self.plot.getPlotItem().setXRange(0, (t1 - t0) / 1e3, padding=0.01)
            span = (t1 - t0) / 1e3
            self.ca.setValue(span * 0.3)
            self.cb.setValue(span * 0.3 + min(10.0, span * 0.1))
        self._annotate(eng, t0, t1)
        tick = eng.caps.tick_ns or eng.tick_ns
        demo = " · SIMULATED (demo)" if eng.source == "demo" else (" · recorded" if eng.source == "replay" else "")
        self.info.setText(f"{len(ts):,} hardware edge records · sampling {1e3 / tick:.0f} MS/s "
                          f"({tick:.0f} ns resolution){demo} · square-wave = logic level between recorded edges")
        self._cursor_text()

    def _set_curves(self, x, ys):
        if not len(x):
            for c in (self.c_scl, self.c_sda, self.c_scl_h, self.c_sda_h):
                c.setData([], [])
            return
        scl, sda = ys
        self.c_scl.setData(x, scl)
        self.c_scl_h.setData(x, scl)
        self.c_sda.setData(x, sda)
        self.c_sda_h.setData(x, sda)

    def _annotate(self, eng, t0, t1):
        self._clear_ann()
        if not self.ann.isChecked():
            return
        pi = self.plot.getPlotItem()
        txns = [t for t in eng.packets[-400:] if (t.t_end or t.t_start) >= t0 and t.t_start <= t1]
        nbytes = sum(len(t.bytes) for t in txns)
        dense = nbytes > 160
        ev_col = {"S": C["green"], "Sr": C["green"], "P": C["red"]}
        for t in txns:
            for tt, k in t.events:
                if t0 <= tt <= t1:
                    ln = pg.InfiniteLine(pos=(tt - t0) / 1e3, angle=90,
                                         pen=pg.mkPen(ev_col[k], width=1.2, style=Qt.PenStyle.DotLine),
                                         label=k, labelOpts={"color": ev_col[k], "position": 0.06})
                    pi.addItem(ln)
                    self.ann_items.append(ln)
            if dense:
                continue
            for b in t.bytes:
                if not (t0 <= b.t_first <= t1):
                    continue
                fr = t.frames[b.frame]
                if b.kind.startswith("addr"):
                    txt = f"0x{fr.addr:02X} {fr.rw}" if fr.addr is not None else f"{b.value:02X}"
                    col = C["cyan"] if fr.rw == "R" else C["blue"]
                else:
                    txt = f"{b.value:02X}"
                    col = C["text"]
                xa, xb = (b.t_first - t0) / 1e3, (b.t_ack - t0) / 1e3
                bar = pg.PlotCurveItem([xa - 2, xb - 4], [2.95, 2.95], pen=pg.mkPen(col, width=3))
                lab = pg.TextItem(txt, color=col, anchor=(0.5, 1.0))
                lab.setPos((xa + xb) / 2 - 3, 3.4)
                ak = pg.TextItem("A" if b.ack else "N", color=C["green"] if b.ack else C["amber"], anchor=(0.5, 1.0))
                ak.setPos(xb, 3.4)
                for it in (bar, lab, ak):
                    pi.addItem(it)
                    self.ann_items.append(it)
        if dense:
            self.info.setText(self.info.text() + " · zoom in / pick a packet for byte annotations")

    def _cursor_text(self):
        if not self.cur.isChecked():
            self.cursor_lbl.setText("")
            return
        dt_us = abs(self.cb.value() - self.ca.value())
        f = f" → 1/Δt = {1e3 / dt_us:.2f} kHz" if dt_us > 0 else ""
        self.cursor_lbl.setText(f"A {self.ca.value():.2f} µs · B {self.cb.value():.2f} µs · Δt {fmt_time_ns(dt_us * 1e3)}{f}")
