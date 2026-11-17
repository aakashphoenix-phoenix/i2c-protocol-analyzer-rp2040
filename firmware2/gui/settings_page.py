"""Settings page (persisted with QSettings)."""
from __future__ import annotations

from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox,
                             QVBoxLayout, QWidget)

from config import APP_VERSION, COLORS as C
from gui.widgets import MOTION, Card, label


class HexSpin(QSpinBox):
    def textFromValue(self, v):
        return f"0x{v:02X}"

    def valueFromText(self, t):
        try:
            return int(t, 16)
        except ValueError:
            return self.value()


class SettingsPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        s = ctx.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)
        row = QHBoxLayout()
        row.setSpacing(14)

        hw = Card("Hardware")
        f = QFormLayout()
        f.setVerticalSpacing(10)
        self.oled = HexSpin()
        self.oled.setRange(0x08, 0x77)
        self.oled.setValue(int(s.value("oled_addr", 0x3C)))
        self.oled.valueChanged.connect(self._oled)
        f.addRow("OLED I²C address", self.oled)
        self.folder = QLineEdit(ctx.captures_dir())
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        fr = QHBoxLayout()
        fr.addWidget(self.folder, 1)
        fr.addWidget(b)
        f.addRow("Captures folder", fr)
        hw.add(f)
        hw.add(label("Configured names (DS3231 0x68, BMP280 0x76, OLED) are shown as <b>Configured</b> until bus "
                     "traffic confirms them. The configured 100 kHz bus speed is never displayed as a measurement.",
                     "cardHint", wrap=True))
        hw.lay.addStretch(1)
        row.addWidget(hw, 1)

        vis = Card("Display & animation")
        self.anim = QCheckBox("Enable animations (pulses, glow, transitions)")
        self.anim.setChecked(s.value("animations", True, bool))
        self.reduced = QCheckBox("Reduced motion (no auto-rotate, shorter effects)")
        self.reduced.setChecked(s.value("reduced_motion", False, bool))
        self.use3d = QCheckBox("Use OpenGL 3D network view (restart to apply; turn off if the 3D view is black)")
        self.use3d.setChecked(s.value("use_3d", True, bool))
        self.rot = QCheckBox("Auto-rotate 3D camera on the network page")
        self.rot.setChecked(s.value("auto_rotate", False, bool))
        self.auto = QCheckBox("Auto-scroll packet tables")
        self.auto.setChecked(s.value("autoscroll", True, bool))
        self.rate = QSpinBox()
        self.rate.setRange(50, 1000)
        self.rate.setSuffix(" ms")
        self.rate.setValue(int(s.value("refresh_ms", 100)))
        for w in (self.anim, self.reduced, self.use3d, self.rot, self.auto):
            vis.add(w)
        rf = QFormLayout()
        rf.addRow("GUI refresh interval", self.rate)
        vis.add(rf)
        vis.lay.addStretch(1)
        row.addWidget(vis, 1)
        lay.addLayout(row)

        about = Card("Data integrity rules this application follows")
        about.add(label(
            "• <b>Real Hardware Mode</b> is the default. With no analyzer connected nothing is measured or animated.<br>"
            f"• <b style='color:{C['demo']}'>Demo Mode</b> synthesises protocol records for testing; every screen then "
            "shows <b>DEMO DATA — NOT LIVE HARDWARE</b> and saved files are marked simulated.<br>"
            "• Waveforms are drawn only from hardware edge records. Sensor values only from decoded bus bytes.<br>"
            "• Features the connected firmware does not report (see its capabilities in the toolbar) are shown as unavailable.<br>"
            f"<br>Version {APP_VERSION}", wrap=True))
        lay.addWidget(about)
        lay.addStretch(1)

        for w in (self.anim, self.reduced, self.use3d, self.rot, self.auto):
            w.toggled.connect(self._save)
        self.rate.valueChanged.connect(self._save)
        self.folder.editingFinished.connect(self._save)
        self._save()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Captures folder", self.folder.text())
        if d:
            self.folder.setText(d)
            self._save()

    def _oled(self, v):
        self.ctx.settings.setValue("oled_addr", v)
        self.ctx.engine.set_oled_address(v)

    def _save(self):
        s = self.ctx.settings
        s.setValue("animations", self.anim.isChecked())
        s.setValue("reduced_motion", self.reduced.isChecked())
        s.setValue("use_3d", self.use3d.isChecked())
        s.setValue("auto_rotate", self.rot.isChecked())
        s.setValue("autoscroll", self.auto.isChecked())
        s.setValue("refresh_ms", self.rate.value())
        s.setValue("captures_dir", self.folder.text())
        MOTION["animations"] = self.anim.isChecked()
        MOTION["reduced"] = self.reduced.isChecked()
        self.ctx.autoscroll = self.auto.isChecked()
        self.ctx.set_refresh(self.rate.value())

    def refresh(self, engine):
        pass
