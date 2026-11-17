"""Capture History page — save/open/replay recorded captures and export results."""
from __future__ import annotations

import json
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton,
                             QSplitter, QTextEdit, QVBoxLayout, QWidget)

from config import COLORS as C
from gui.widgets import Card, label
from storage.capture_storage import export_packets_csv, export_packets_json, export_statistics_csv


class CaptureHistoryPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        act = Card("Session")
        r = QHBoxLayout()
        b_save = QPushButton("Save session…")
        b_save.setObjectName("primary")
        b_open = QPushButton("Open capture file…")
        b_csv = QPushButton("Export packets CSV")
        b_json = QPushButton("Export packets JSON")
        b_stats = QPushButton("Export performance statistics")
        for b in (b_save, b_open, b_csv, b_json, b_stats):
            r.addWidget(b)
        r.addStretch(1)
        act.add(r)
        lay.addWidget(act)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(10)
        lc = Card("Saved captures", "")
        self.folder_lbl = label("", "cardHint", wrap=True)
        lc.add(self.folder_lbl)
        self.list = QListWidget()
        self.list.setStyleSheet(f"QListWidget{{background:#0D1526;border:1px solid {C['border']};border-radius:10px;}}"
                                "QListWidget::item{padding:8px;}")
        lc.add(self.list, 1)
        rr = QHBoxLayout()
        b_ref = QPushButton("Refresh list")
        rr.addWidget(b_ref)
        rr.addStretch(1)
        lc.add(rr)
        split.addWidget(lc)
        rc = Card("Capture metadata & playback")
        self.meta = QTextEdit()
        self.meta.setReadOnly(True)
        rc.add(self.meta, 1)
        pr = QHBoxLayout()
        self.speed = QComboBox()
        self.speed.addItems(["Instant (decode whole file)", "1× real time", "5×", "20×"])
        self.b_play = QPushButton("▶ Load && play")
        self.b_play.setObjectName("primary")
        self.b_pause = QPushButton("Pause")
        self.b_pause.setCheckable(True)
        self.b_stop = QPushButton("Stop")
        for w in (label("Playback", "cardHint"), self.speed, self.b_play, self.b_pause, self.b_stop):
            pr.addWidget(w)
        pr.addStretch(1)
        rc.add(pr)
        rc.add(label("Playback is recorded data, not live traffic: the GUI shows a RECORDED CAPTURE banner, and the "
                     "original hardware timestamps are re-decoded unchanged.", "cardHint", wrap=True))
        split.addWidget(rc)
        split.setSizes([480, 900])
        lay.addWidget(split, 1)

        b_save.clicked.connect(ctx.save_capture)
        b_open.clicked.connect(self._open)
        b_csv.clicked.connect(lambda: self._export("csv"))
        b_json.clicked.connect(lambda: self._export("json"))
        b_stats.clicked.connect(lambda: self._export("stats"))
        b_ref.clicked.connect(self.scan)
        self.list.currentItemChanged.connect(self._show_meta)
        self.list.itemDoubleClicked.connect(lambda it: self._play())
        self.b_play.clicked.connect(self._play)
        self.b_pause.toggled.connect(ctx.pause_replay)
        self.b_stop.clicked.connect(ctx.stop_replay)
        self.scan()

    def scan(self):
        folder = self.ctx.captures_dir()
        self.folder_lbl.setText(f"Folder: {folder}")
        self.list.clear()
        if not os.path.isdir(folder):
            return
        files = sorted((f for f in os.listdir(folder) if f.endswith(".i2ccap")),
                       key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True)
        for f in files:
            p = os.path.join(folder, f)
            it = QListWidgetItem(f"{f}\n{os.path.getsize(p) / 1024:.0f} KB")
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.list.addItem(it)

    def _show_meta(self, it):
        if it is None:
            return
        p = it.data(Qt.ItemDataRole.UserRole)
        try:
            with open(p, "rb") as f:
                meta = json.loads(f.readline()).get("capture", {})
        except Exception as e:
            self.meta.setPlainText(f"Cannot read: {e}")
            return
        warn = "⚠ SIMULATED (demo mode) capture\n\n" if meta.get("simulated") else ""
        self.meta.setPlainText(warn + json.dumps(meta, indent=2))

    def _open(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open capture", self.ctx.captures_dir(), "I²C capture (*.i2ccap)")
        if p:
            it = QListWidgetItem(os.path.basename(p))
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.list.insertItem(0, it)
            self.list.setCurrentItem(it)

    def _play(self):
        it = self.list.currentItem()
        if it is None:
            self.ctx.toast("Select a capture first")
            return
        speed = [0, 1, 5, 20][self.speed.currentIndex()]
        self.b_pause.setChecked(False)
        self.ctx.open_capture(it.data(Qt.ItemDataRole.UserRole), speed)

    def _export(self, kind):
        eng = self.ctx.engine
        if not eng.packets and kind != "stats":
            self.ctx.toast("Nothing to export yet")
            return
        ext = {"csv": "CSV (*.csv)", "json": "JSON (*.json)", "stats": "CSV (*.csv)"}[kind]
        default = {"csv": "packets.csv", "json": "packets.json", "stats": "statistics.csv"}[kind]
        p, _ = QFileDialog.getSaveFileName(self, "Export", os.path.join(self.ctx.captures_dir(), default), ext)
        if not p:
            return
        if kind == "csv":
            n = export_packets_csv(p, eng)
        elif kind == "json":
            n = export_packets_json(p, eng)
        else:
            export_statistics_csv(p, eng)
            n = None
        self.ctx.toast(f"Exported {n} packets" if n is not None else "Statistics exported")

    def refresh(self, engine):
        pass
