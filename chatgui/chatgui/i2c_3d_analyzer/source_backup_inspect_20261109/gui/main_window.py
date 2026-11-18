"""Main window: header/toolbar, sidebar navigation, page stack, data-source control."""
from __future__ import annotations

import datetime as _dt
import logging
import json
import os
import time
import re

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QSettings, QSize, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                             QScrollArea, QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

import config
from analysis.capture_engine import CaptureEngine
from analysis.playback import Playback
from communication.protocol_parser import ProtocolError, capabilities_from_hello, parse_line
from communication.serial_manager import SerialWorker, list_ports
from config import COLORS as C
from gui.capture_history import CaptureHistoryPage
from gui.dashboard import Dashboard
from gui.device_activity import DeviceActivityPage
from gui.error_monitor import ErrorPage
from gui.icons import icon, pixmap
from gui.network_page import NetworkPage
from gui.packet_analyzer import PacketAnalyzerPage
from gui.performance_monitor import PerformancePage
from gui.sensor_monitor import SensorPage
from gui.settings_page import SettingsPage
from gui.waveform_viewer import WaveformPage
from gui.widgets import MOTION, StatusPill, label
from models.packet_model import PacketModel
from sources.demo_source import DemoSource
from storage.capture_storage import export_packets_csv, load_capture, save_capture

class FitScroll(QScrollArea):
    """Page fills the viewport exactly; scrollbars appear only below a minimum size."""

    def __init__(self, min_w, min_h):
        super().__init__()
        self.min_w, self.min_h = min_w, min_h
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def showEvent(self, e):
        super().showEvent(e)
        self._fit()

    def _fit(self):
        w = self.widget()
        if w is None:
            return
        vp = self.maximumViewportSize()
        mh = max(self.min_h, w.minimumSizeHint().height())
        mw = max(self.min_w, w.minimumSizeHint().width())
        need_v = vp.height() < mh
        need_h = vp.width() < mw
        width = max(mw, vp.width() - (self.verticalScrollBar().sizeHint().width() if need_v else 0))
        height = max(mh, vp.height() - (self.horizontalScrollBar().sizeHint().height() if need_h else 0))
        w.resize(width, height)


NAV = [("dashboard", "Live Dashboard"), ("network", "3D I²C Network"), ("packets", "Packet Analyzer"),
       ("waveform", "Digital Waveform"), ("performance", "Performance Monitoring"), ("sensor", "Sensor Monitoring"),
       ("chip", "Device Activity"), ("error", "Error & Diagnostics"), ("history", "Capture History"),
       ("settings", "Settings")]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{config.APP_NAME} — {config.APP_SUBTITLE}")
        self.setWindowIcon(icon("logo", C["cyan"], C["cyan"], 64))
        self.settings = QSettings("I2CAnalyzer", "I2C3DAnalyzer")
        self.engine = CaptureEngine()
        self.engine.set_oled_address(int(self.settings.value("oled_addr", config.OLED_ADDRESS_DEFAULT)))
        self.playback = Playback()
        self._playback_wall = time.monotonic()
        self._playback_context = self.engine.decoder
        self.packet_model = PacketModel(self.engine)
        self.autoscroll = self.settings.value("autoscroll", True, bool)
        MOTION["animations"] = self.settings.value("animations", True, bool)
        MOTION["reduced"] = self.settings.value("reduced_motion", False, bool)
        self.worker: SerialWorker | None = None
        self.master_worker: SerialWorker | None = None
        self._master_rtc = None
        self._master_bmp = None
        self.demo: DemoSource | None = None
        self.caps = None
        self.capturing = False
        self.display_paused = False
        self.session_id = "—"
        self.capture_t0: float | None = None
        self.replay_lines: list = []
        self.replay_idx = 0
        self.replay_speed = 0
        self.replay_t0 = 0.0
        self.replay_hw0 = None
        self.replay_paused = False
        self.replay_timer = QTimer(self)
        self.replay_timer.timeout.connect(self._replay_tick)
        self._build()
        self.setMinimumSize(1180, 680)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(int(self.settings.value("refresh_ms", config.GUI_REFRESH_MS)))
        self.refresh_ports()
        self._set_state()

    # ================================================================== layout
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ---------------- header row 1
        head = QFrame()
        head.setObjectName("header")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(18, 10, 18, 10)
        hv.setSpacing(8)
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(pixmap("logo", C["cyan"], 34))
        r1.addWidget(logo)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        tcol.addWidget(label(config.APP_NAME, "appTitle"))
        tcol.addWidget(label(config.APP_SUBTITLE + "  ·  RP2040 passive analyzer", "appSub"))
        r1.addLayout(tcol)
        r1.addSpacing(14)
        self.pill = StatusPill()
        r1.addWidget(self.pill)
        self.hwinfo = label("", "appSub")
        r1.addWidget(self.hwinfo, 1)
        self.lbl_session = label("Session —", "appSub")
        self.lbl_dur = label("00:00:00", "mono")
        self.lbl_dur.setStyleSheet(f"color:{C['cyan']}; font-size:15px;")
        r1.addWidget(self.lbl_session)
        r1.addWidget(self.lbl_dur)
        self.btn_demo = QPushButton("Demo mode")
        self.btn_demo.setObjectName("demo")
        self.btn_demo.setCheckable(True)
        self.btn_demo.setToolTip("Simulated records through the full decoder. Clearly labelled as DEMO everywhere.")
        self.btn_demo.toggled.connect(self.toggle_demo)
        r1.addWidget(self.btn_demo)
        hv.addLayout(r1)

        # ---------------- header row 2 (toolbar)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(label("Analyzer port", "appSub"))
        self.port = QComboBox()
        self.port.setMinimumWidth(260)
        r2.addWidget(self.port)
        self.btn_refresh = QPushButton("Refresh ports")
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setObjectName("primary")
        self.btn_disconnect = QPushButton("Disconnect")
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background:{C['border']};")
        self.btn_start = QPushButton("● Start capture")
        self.btn_start.setObjectName("primary")
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_pause = QPushButton("Pause display")
        self.btn_pause.setCheckable(True)
        self.btn_clear = QPushButton("Clear")
        self.btn_save = QPushButton("Save capture")
        self.btn_csv = QPushButton("Export CSV")
        for w in (self.btn_refresh, self.btn_connect, self.btn_disconnect, sep, self.btn_start, self.btn_stop,
                  self.btn_pause, self.btn_clear):
            r2.addWidget(w)
        r2.addStretch(1)
        r2.addWidget(self.btn_save)
        r2.addWidget(self.btn_csv)
        hv.addLayout(r2)
        playback_bar = QHBoxLayout()
        playback_bar.addWidget(label("Visual event playback (acquisition unchanged)", "appSub"))
        self.visual_speed = QComboBox()
        for speed in (0.25, 0.5, 1.0, 2.0): self.visual_speed.addItem(f"{speed:g}×", speed)
        self.visual_speed.setCurrentIndex(2)
        self.visual_speed.currentIndexChanged.connect(lambda: setattr(self.playback, 'speed', self.visual_speed.currentData()))
        playback_bar.addWidget(self.visual_speed)
        previous = QPushButton("◀ Packet"); following = QPushButton("Packet ▶")
        previous.clicked.connect(lambda: self.p_wave._step(-1))
        following.clicked.connect(lambda: self.p_wave._step(1))
        playback_bar.addWidget(previous); playback_bar.addWidget(following)
        follow = QPushButton("Follow latest")
        follow.clicked.connect(lambda: (setattr(self.playback, 'pinned', False), self.p_wave.follow.setChecked(True)))
        playback_bar.addWidget(follow)
        playback_bar.addWidget(label("Inspection shows selected events; all retained packets remain in table", "cardHint"))
        playback_bar.addStretch(1)
        hv.addLayout(playback_bar)
        # Optional second USB port for real sensor telemetry (not bus packets).
        r3 = QHBoxLayout()
        r3.addWidget(label("Master telemetry (optional)", "appSub"))
        self.master_port = QComboBox()
        self.master_port.setMinimumWidth(260)
        r3.addWidget(self.master_port)
        self.btn_master_connect = QPushButton("Connect master")
        self.btn_master_disconnect = QPushButton("Disconnect master")
        self.master_state = label("Not connected", "cardHint")
        r3.addWidget(self.btn_master_connect)
        r3.addWidget(self.btn_master_disconnect)
        r3.addWidget(self.master_state, 1)
        # Physical controller SCL setting: separate from visual playback speed.
        r3.addWidget(label("Controller SCL setting (physical bus)", "appSub"))
        self.scl_choice = QComboBox()
        for hz, text in config.CONTROLLER_SCL_OPTIONS:
            self.scl_choice.addItem(text, hz)
        self.scl_choice.setToolTip("Rates every configured part supports (DS3231/SSD1306 ≤ 400 kHz). Sent only to the "
                                   "CONTROLLER Pico's own USB port after you press Apply; the analyzer never changes SCL.")
        r3.addWidget(self.scl_choice)
        self.btn_scl_apply = QPushButton("Apply to controller")
        self.btn_scl_apply.clicked.connect(self.apply_scl)
        r3.addWidget(self.btn_scl_apply)
        hv.addLayout(r3)
        self.scl_status = label("Controller SCL: not known · measured SCL appears once cycles are captured", "cardHint", wrap=True)
        hv.addWidget(self.scl_status)
        v.addWidget(head)

        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.hide()
        v.addWidget(self.banner)

        # ---------------- body
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(232)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 14, 0, 14)
        sv.addWidget(label("  NAVIGATION", "metricName"))
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(20, 20))
        for ic, name in NAV:
            it = QListWidgetItem(icon(ic), "  " + name)
            self.nav.addItem(it)
        sv.addWidget(self.nav, 1)
        self.side_mode = label("", "cardHint", wrap=True)
        self.side_mode.setContentsMargins(16, 0, 12, 0)
        sv.addWidget(self.side_mode)
        body.addWidget(side)

        self.stack = QStackedWidget()
        self.p_dash = Dashboard(self)
        self.p_net = NetworkPage(self)
        self.p_pk = PacketAnalyzerPage(self)
        self.p_wave = WaveformPage(self)
        self.p_perf = PerformancePage(self)
        self.p_sens = SensorPage(self)
        self.p_dev = DeviceActivityPage(self)
        self.p_err = ErrorPage(self)
        self.p_hist = CaptureHistoryPage(self)
        self.p_set = SettingsPage(self)
        self.pages = [self.p_dash, self.p_net, self.p_pk, self.p_wave, self.p_perf, self.p_sens, self.p_dev,
                      self.p_err, self.p_hist, self.p_set]
        self.gl_pages = {0, 1, 6}
        for p in self.pages:
            if isinstance(p, (PerformancePage, SensorPage)):      # already scroll internally
                self.stack.addWidget(p)
                continue
            sa = FitScroll(900, 640)   # works on 1366x768 screens too
            p.setObjectName("scrollBody")
            sa.setWidget(p)
            self.stack.addWidget(sa)
        body.addWidget(self.stack, 1)
        bw = QWidget()
        bw.setLayout(body)
        v.addWidget(bw, 1)
        self.status = QStatusBar()
        self.status.setStyleSheet(f"QStatusBar{{background:#0B1222;color:{C['text2']};border-top:1px solid {C['border']};}}")
        self.setStatusBar(self.status)

        self.nav.currentRowChanged.connect(self._goto)
        self.nav.setCurrentRow(0)
        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_connect.clicked.connect(self.connect_analyzer)
        self.btn_disconnect.clicked.connect(self.disconnect_analyzer)
        self.btn_start.clicked.connect(self.start_capture)
        self.btn_stop.clicked.connect(self.stop_capture)
        self.btn_pause.toggled.connect(self._pause_display)
        self.btn_clear.clicked.connect(self.clear_capture)
        self.btn_save.clicked.connect(self.save_capture)
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_master_connect.clicked.connect(self.connect_master)
        self.btn_master_disconnect.clicked.connect(self.disconnect_master)

    def _goto(self, i):
        self.stack.setCurrentIndex(i)
        page = self.stack.widget(i)
        if MOTION["animations"] and not MOTION["reduced"] and i not in self.gl_pages:
            eff = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(eff)
            a = QPropertyAnimation(eff, b"opacity", page)
            a.setDuration(180)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            a.finished.connect(lambda: page.setGraphicsEffect(None))
            a.start()
            self._fade = a
        self.pages[i].refresh(self.engine)

    # ================================================================== context API (used by pages)
    def toast(self, msg, ms=4000):
        self.status.showMessage(msg, ms)

    def set_refresh(self, ms):
        if hasattr(self, "timer"):
            self.timer.setInterval(ms)

    def captures_dir(self):
        d = self.settings.value("captures_dir", os.path.join(os.path.expanduser("~"), "Documents", "I2C_Captures"))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    def focus_device(self, key, switch=False):
        """One device filter for packet tables, waveform stepping and both 3D focus rings.
        Filtering never changes records or timestamps; key=None clears it."""
        if key is None or isinstance(key, int):
            self.device_filter = key
            self.p_pk.set_address_filter(key)
            self.p_dev.select(key)
            self.p_dash.proxy.set(addr=key)
            self.p_wave.set_device_filter(key)
            for net in (self.p_dash.net, self.p_net.net, self.p_dev.net):
                net.set_focus(key)
            if key is None:
                return
            self.p_net._select(key)
            if switch:
                self.nav.setCurrentRow(2)
            else:
                self.toast(f"Packet tables filtered to 0x{key:02X} — see Packet Analyzer or Device Activity")
        elif key in ("master", "analyzer"):
            self.p_net._select(key)

    def select_transaction(self, txn):
        self.playback.select(txn)
        self.p_pk.show_detail(txn)
        self.p_wave.show_packet(txn, synchronize=False)
        self._sync_playback()

    def _sync_playback(self):
        for net in (self.p_dash.net, self.p_net.net, self.p_dev.net):
            net.set_devices(self.engine.registry.node_list(self.engine.hw_now or 0))
            net.show_stage(self.playback, self.engine)
        self.p_wave.show_stage(self.playback.current)
        t = self.playback.txn.t_start if self.playback.txn is not None else None
        for page in (self.p_dash, self.p_perf):
            if hasattr(page, "mark_selected"):
                page.mark_selected(t, self.engine)

    def show_waveform(self, row):
        txn = self.packet_model.txn(row)
        self.nav.setCurrentRow(3)
        self.select_transaction(txn)

    # ================================================================== state / banner
    def _set_state(self):
        e = self.engine
        connected = self.worker is not None and self.worker.isRunning()
        demo_on = self.demo is not None and self.demo.isRunning()
        replay_on = self.replay_timer.isActive() or (e.source == "replay")
        self.btn_connect.setEnabled(not connected and not demo_on)
        self.btn_disconnect.setEnabled(connected)
        self.port.setEnabled(not connected)
        self.btn_start.setEnabled(connected and self.caps is not None and not self.capturing)
        self.btn_stop.setEnabled((connected and self.capturing) or demo_on or self.replay_timer.isActive())
        self.btn_demo.setEnabled(not connected)
        master_connected = self.master_worker is not None and self.master_worker.isRunning()
        self.master_port.setEnabled(not master_connected)
        self.btn_master_connect.setEnabled(not master_connected and not demo_on)
        self.btn_scl_apply.setEnabled(master_connected or demo_on)
        self.btn_scl_apply.setText("Apply (SIMULATED demo)" if demo_on else "Apply to controller")
        self.btn_master_disconnect.setEnabled(master_connected)
        if demo_on:
            self.pill.set_state("DEMO · simulated stream", C["demo"], True)
        elif self.capturing:
            self.pill.set_state(f"Capturing · {self.worker.port}", C["green"], True)
        elif connected:
            self.pill.set_state(f"Connected · {self.worker.port}", C["cyan"], False)
        elif replay_on:
            self.pill.set_state("Recorded capture", "#8C7BFF", self.replay_timer.isActive())
        else:
            self.pill.set_state("Disconnected", C["gray"], False)
        # banner
        if e.source == "demo":
            self.banner.setObjectName("bannerDemo")
            self.banner.setText("DEMO · SIMULATED — NOT VERIFIED HARDWARE")
            self.banner.show()
        elif e.source == "replay":
            self.banner.setObjectName("bannerReplay")
            self.banner.setText("RECORDED CAPTURE — NOT LIVE TRAFFIC" + (
                "  ·  (this recording was made in DEMO mode — simulated)" if getattr(self, "replay_simulated", False) else ""))
            self.banner.show()
        elif connected and self.caps is None and time.monotonic() - getattr(self, "connect_t", 0) > config.HELLO_TIMEOUT_S:
            self.banner.setObjectName("bannerWarn")
            self.banner.setText("No compatible handshake from analyzer. Flash the JSON segment firmware from the "
                                "firmware bundle (or a supported raw-edge firmware); plain START/STOP text is insufficient.")
            self.banner.show()
        else:
            self.banner.hide()
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        mode = {"demo": f"<span style='color:{C['demo']}'><b>Demo Mode</b> · simulated</span>",
                "replay": "<b>Offline review</b> · recorded data", "live": "<b>Real Hardware Mode</b>",
                "none": "<b>Real Hardware Mode</b> · no data"}[e.source]
        # Header/sidebar describe the displayed source, not a stale handshake
        # retained from a previously connected hardware session.
        caps = (e.caps if e.caps.known else None) if e.source in {"demo", "replay"} else (
            self.caps or (e.caps if e.caps.known else None))
        capline = ("<br>Firmware caps: " + ", ".join(sorted(caps.caps))) if caps else ""
        self.side_mode.setText(mode + capline)
        if caps:
            self.hwinfo.setText(f"  {caps.firmware} · {caps.board} · {caps.sample_hz / 1e6:g} MS/s · protocol v{caps.proto}")
        elif connected:
            self.hwinfo.setText("  waiting for analyzer handshake…")
        else:
            self.hwinfo.setText("")

        self.p_wave.refresh(e)  # invalidate old plots even when display is paused

    def _new_session(self):
        self.session_id = _dt.datetime.now().strftime("S%Y%m%d-%H%M%S")
        self.lbl_session.setText(f"Session {self.session_id}")
        self.capture_t0 = time.monotonic()
        self.engine.capture_wall_start = _dt.datetime.now()

    # ================================================================== serial
    def refresh_ports(self):
        cur = self.port.currentData()
        master_cur = self.master_port.currentData()
        self.port.clear()
        self.master_port.clear()
        ports = list_ports()
        self.master_port.addItem("No master sensor telemetry", None)
        for dev, desc, is_pico in ports:
            text = ("★ " if is_pico else "") + f"{dev} — {desc}"
            self.port.addItem(text, dev)
            self.master_port.addItem(text, dev)
        if not ports:
            self.port.addItem("No serial ports found", None)
        i = self.port.findData(cur)
        if i >= 0:
            self.port.setCurrentIndex(i)
        i = self.master_port.findData(master_cur)
        if i >= 0:
            self.master_port.setCurrentIndex(i)
        self.toast(f"{len(ports)} serial port(s) found" + (" · ★ = Raspberry Pi board" if any(p[2] for p in ports) else ""))

    def connect_analyzer(self):
        dev = self.port.currentData()
        if not dev:
            self.toast("Select the analyzer's serial port first")
            return
        self._stop_sources()
        self.caps = None
        if self.master_worker and self.master_worker.isRunning() and self.master_worker.port == dev:
            self.toast("Analyzer and master must use different COM ports")
            return
        self.worker = SerialWorker(dev, self)
        self.worker.lines_ready.connect(self._on_lines)
        self.worker.connected.connect(lambda p: (self.toast(f"Opened {p}"), self._set_state()))
        self.worker.failed.connect(self._on_fail)
        self.worker.disconnected.connect(self._on_disconnect)
        self.connect_t = time.monotonic()
        self.worker.start()
        QTimer.singleShot(int(config.HELLO_TIMEOUT_S * 1000) + 200, self._set_state)
        self._set_state()

    def disconnect_analyzer(self):
        if self.capturing:
            self.stop_capture()
        if self.worker:
            self.worker.stop()
            self.worker.wait(1500)

    def connect_master(self):
        dev = self.master_port.currentData()
        if not dev:
            self.toast("Select the master Pico's COM port, or leave master disconnected")
            return
        if self.worker and self.worker.isRunning() and self.worker.port == dev:
            self.toast("Select a different port for master telemetry")
            return
        self.master_worker = SerialWorker(dev, self, telemetry=True)
        self.master_worker.connected.connect(lambda port: (self.master_state.setText(f"Master {port}: USB telemetry"),
                                                       self._set_state()))
        self.master_worker.lines_ready.connect(self._on_master_lines)
        self.master_worker.send_controller("SCL?")       # query the controller's current setting
        self.master_worker.failed.connect(self._on_master_failed)
        self.master_worker.disconnected.connect(self._on_master_disconnected)
        self.master_worker.start()
        self._set_state()

    def _on_master_failed(self, message):
        self.master_worker = None
        QMessageBox.warning(self, "Master connection failed", message)
        self._set_state()

    def _on_master_disconnected(self, reason):
        self.master_worker = None
        self.master_state.setText("Not connected")
        self.toast(f"Master: {reason}")
        self._set_state()

    def disconnect_master(self):
        if self.master_worker:
            self.master_worker.stop()
            self.master_worker.wait(1500)

    def apply_scl(self):
        """Send the selected rate to the controller (or the SIMULATED demo controller)."""
        hz = self.scl_choice.currentData()
        if self.demo is not None and self.demo.isRunning():
            self.demo.request_scl(hz)
            self.engine.record_scl_setting(hz, hz, "applied", "SIMULATED demo controller",
                                           "simulated edge timing only")
            self.toast(f"SIMULATED: demo controller SCL set to {hz / 1e3:g} kHz")
        elif self.master_worker is not None and self.master_worker.isRunning():
            self.master_worker.send_controller(f"SCL {hz}")
            self.scl_status.setText(f"Sent 'SCL {hz}' to controller port {self.master_worker.port} — awaiting its reply; "
                                    "bus change is confirmed only by analyzer measurements")
        else:
            self.toast("Connect the controller (master) port first, or start Demo mode")
        self._update_scl_status()

    def _update_scl_status(self):
        e = self.engine
        s = e.perf.compute()
        roll = f"{s.roll_freq_hz / 1e3:.2f} kHz" if s.roll_freq_hz else "UNKNOWN"
        sim = "SIMULATED · " if e.source == "demo" else ""
        st = e.scl_setting
        measured = f"measured rolling 1 s mean: {roll}"
        if st is None:
            self.scl_status.setText(f"{sim}Controller SCL setting: not known · {measured}")
            return
        c = e.scl_confirmation()
        bus = {"PENDING": f"bus confirmation PENDING ({c['packets']} measured packets since change)",
               "CONFIRMED": f"bus CONFIRMED: median packet measurement {(c['measured_hz'] or 0) / 1e3:.2f} kHz",
               "MISMATCH": f"bus MISMATCH: median packet measurement {(c['measured_hz'] or 0) / 1e3:.2f} kHz",
               "UNKNOWN": "bus effect UNKNOWN"}[c["state"]]
        self.scl_status.setText(f"{sim}Controller SCL setting ({st['source']}): {st['result']} · requested "
                                f"{st['requested_hz'] / 1e3:g} kHz · reported {st['applied_hz'] / 1e3:.3f} kHz ("
                                f"{'simulated setting' if st['source'].startswith('SIMULATED') else 'divider'}, "
                                f"not a measurement) · {bus} · {measured}")

    def _on_master_lines(self, lines):
        """A separately labeled sensor source; NEVER used to generate I2C packets."""
        for line in lines:
            try:
                text = line.decode("utf-8", "replace").strip()
            except Exception:
                continue
            try:
                d = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                d = None
            if isinstance(d, dict) and d.get("type") == "scl_config" and d.get("source") == "controller":
                self.engine.record_scl_setting(d.get("requested_hz"), d.get("applied_hz"), d.get("result"),
                                               "controller firmware", d.get("reason", ""))
                self._update_scl_status()
                continue
            if isinstance(d, dict) and d.get("type") == "sensor_telemetry" and d.get("source") == "master":
                self.engine.update_master_telemetry(d.get("rtc"), d.get("temperature_c"), d.get("pressure_hpa"))
                continue
            # Also accept the *existing* master firmware's printed RTC and BMP lines.
            match = re.search(r"RTC\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{2})/(\d{2})/(\d{4})", text)
            if match:
                h, m, sec, day, month, year = map(int, match.groups())
                self._master_rtc = f"{year:04d}-{month:02d}-{day:02d}T{h:02d}:{m:02d}:{sec:02d}"
            match = re.search(r"BMP280:?\s+([+-]?\d+(?:\.\d+)?)\s+C,?\s+([+-]?\d+(?:\.\d+)?)\s+hPa", text)
            if match:
                self._master_bmp = (float(match.group(1)), float(match.group(2)))
            if self._master_rtc and self._master_bmp:
                self.engine.update_master_telemetry(self._master_rtc, *self._master_bmp)
                self._master_bmp = None

    def _on_fail(self, msg):
        self.worker = None
        QMessageBox.warning(self, "Connection failed", msg)
        self._set_state()

    def _on_disconnect(self, reason):
        self.engine.stream_connected = False
        self.engine.capture_active = False
        was = self.capturing
        self.capturing = False
        self.worker = None
        if was or "lost" in reason:
            self.engine.log("error", "Transport", "Analyzer disconnected", reason, t_hw=self.engine.hw_now)
        self.toast(f"Analyzer: {reason}")
        self._set_state()

    def _on_lines(self, lines):
        self.engine.stream_connected = self.worker is not None
        if self.capturing:
            self.engine.ingest_lines(lines)
            if self.caps is None and self.engine.caps.known:
                self.caps = self.engine.caps
                # Data can arrive in same batch as the handshake.
                if self.worker:
                    self.worker.native_commands = self.caps.has("segment")
                    self.worker.handshake_received = True
                self._set_state()
            return
        for ln in lines:            # not capturing: only listen for the handshake
            try:
                rec = parse_line(ln)
            except ProtocolError as exc:
                self.engine.transport_error(f"{exc}; preview={ln[:100]!r}. Check analyzer COM port/firmware")
                continue
            if rec.type == "hello" and self.worker:
                self.worker.handshake_received = True
                self.worker.native_commands = rec.data.get("wire") == "rp2040_segment_v1"
                self.caps = capabilities_from_hello(rec)
                self.engine.set_caps(self.caps)
                self.engine.log("info", "Session", "Analyzer handshake",
                                f"{self.caps.firmware} · protocol v{self.caps.proto} · caps: {', '.join(sorted(self.caps.caps))}")
                self._set_state()

    def start_capture(self):
        if not (self.worker and self.worker.isRunning()):
            return
        self.engine.clear()
        self.packet_model.sync()
        self.engine.set_source("live")
        if self.caps:
            self.engine.set_caps(self.caps)
        self._new_session()
        self.capturing = True
        self.engine.stream_connected = True
        self.engine.capture_active = True
        self.worker.send("start")
        self.engine.log("info", "Session", "Capture started", f"session {self.session_id} on {self.worker.port}")
        self._set_state()

    def stop_capture(self):
        self.engine.capture_active = False
        if self.demo:
            self.btn_demo.setChecked(False)
            return
        if self.replay_timer.isActive():
            self.stop_replay()
            return
        if self.worker and self.capturing:
            self.worker.send("stop")
            self.engine.log("info", "Session", "Capture stopped", f"session {self.session_id}")
        self.capturing = False
        self._set_state()

    # ================================================================== demo
    def toggle_demo(self, on):
        if on:
            self._stop_sources()
            self.engine.clear()
            self.packet_model.sync()
            self.engine.set_source("demo")
            self._new_session()
            self.demo = DemoSource(self, scl_hz=self.scl_choice.currentData())
            self.engine.record_scl_setting(self.demo.gen.scl_hz, self.demo.gen.scl_hz, "boot",
                                           "SIMULATED demo controller", "simulated edge timing only")
            self.demo.lines_ready.connect(self.engine.ingest_lines)
            self.demo.start()
            self.engine.log("warning", "Session", "Demo mode", "Simulated data — not live hardware")
        else:
            if self.demo:
                self._halt_demo()
                self.engine.log("info", "Session", "Demo stopped", "demo data remains on screen, still labelled DEMO")
        self._set_state()

    # ================================================================== replay
    def open_capture(self, path, speed=0):
        try:
            meta, lines = load_capture(path)
        except Exception as e:
            QMessageBox.warning(self, "Cannot open capture", str(e))
            return
        self._stop_sources()
        self.btn_demo.blockSignals(True)
        self.btn_demo.setChecked(False)
        self.btn_demo.blockSignals(False)
        self.engine.clear()
        self.packet_model.sync()
        self.engine.set_source("replay")
        self.replay_simulated = bool(meta.get("simulated"))
        if meta.get("oled_address"):
            self.engine.set_oled_address(int(meta["oled_address"]))
        self.session_id = os.path.basename(path)
        self.lbl_session.setText(f"Recording {self.session_id}")
        self.engine.log("info", "Session", "Recording opened",
                        f"{os.path.basename(path)} · saved {meta.get('saved_at')} · source {meta.get('source')}")
        if speed == 0:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                for i in range(0, len(lines), 2000):
                    self.engine.ingest_lines(lines[i:i + 2000])
            finally:
                QApplication.restoreOverrideCursor()
            self.toast(f"Decoded {len(self.engine.packets):,} packets from {len(lines):,} records")
        else:
            self.replay_lines = [(self._hw_time(ln), ln) for ln in lines]
            self.replay_idx = 0
            self.replay_speed = speed
            self.replay_hw0 = next((t for t, _ in self.replay_lines if t is not None), 0)
            self.replay_t0 = time.monotonic()
            self.replay_paused = False
            self.replay_timer.start(30)
        self._set_state()
        self.nav.setCurrentRow(0)

    @staticmethod
    def _hw_time(ln):
        try:
            d = json.loads(ln)
        except Exception:
            return None
        return d.get("t0", d.get("ts"))

    def _replay_tick(self):
        if self.replay_paused:
            return
        tick = self.engine.tick_ns
        elapsed_ns = (time.monotonic() - self.replay_t0) * 1e9 * self.replay_speed
        batch = []
        while self.replay_idx < len(self.replay_lines):
            t, ln = self.replay_lines[self.replay_idx]
            if t is not None and (t - self.replay_hw0) * tick > elapsed_ns:
                break
            batch.append(ln)
            self.replay_idx += 1
        if batch:
            self.engine.ingest_lines(batch)
        if self.replay_idx >= len(self.replay_lines):
            self.replay_timer.stop()
            self.toast("Playback finished")
            self._set_state()

    def pause_replay(self, on):
        if on:
            self.replay_paused = True
            self._pause_at = time.monotonic()
        elif self.replay_paused:
            self.replay_t0 += time.monotonic() - getattr(self, "_pause_at", time.monotonic())
            self.replay_paused = False

    def stop_replay(self):
        self.replay_timer.stop()
        self._set_state()

    def _halt_demo(self):
        # disconnect first so already-queued simulated batches can never reach a later capture
        try:
            self.demo.lines_ready.disconnect()
        except TypeError:
            pass
        self.demo.stop()
        self.demo.wait(1000)
        self.demo = None

    def _stop_sources(self):
        if self.demo:
            self._halt_demo()
        self.replay_timer.stop()

    # ================================================================== misc actions
    def _pause_display(self, on):
        self.display_paused = on
        self.btn_pause.setText("Resume display" if on else "Pause display")
        for net in (self.p_dash.net, self.p_net.net, self.p_dev.net):
            net.playback_paused = on
        self.toast("Display paused — capture continues in the background" if on else "Display resumed")

    def clear_capture(self):
        src = self.engine.source
        caps = self.engine.caps
        self.engine.clear()
        self.engine.set_caps(caps)
        self.engine.set_source(src if (self.capturing or self.demo) else "none")
        self.packet_model.sync()
        self.p_pk.proxy.set(min_id=0)
        if self.capturing or self.demo:
            self._new_session()
        self._set_state()

    def save_capture(self):
        if not self.engine.raw_lines:
            self.toast("Nothing captured yet")
            return
        name = f"capture_{self.session_id}{'_DEMO' if self.engine.source == 'demo' else ''}.i2ccap"
        p, _ = QFileDialog.getSaveFileName(self, "Save capture", os.path.join(self.captures_dir(), name),
                                           "I²C capture (*.i2ccap)")
        if p:
            n = save_capture(p, self.engine)
            self.toast(f"Saved {n:,} records to {p}")
            self.p_hist.scan()

    def export_csv(self):
        if not self.engine.packets:
            self.toast("No packets to export")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Export packets", os.path.join(self.captures_dir(),
                                           f"packets_{self.session_id}.csv"), "CSV (*.csv)")
        if p:
            self.toast(f"Exported {export_packets_csv(p, self.engine):,} packets")

    # ================================================================== refresh loop
    def _refresh(self):
        act = self.engine.take_activity()
        nets = (self.p_dash.net, self.p_net.net, self.p_dev.net)
        now_playback = time.monotonic()
        elapsed = now_playback - self._playback_wall
        self._playback_wall = now_playback
        if self._playback_context is not self.engine.decoder:
            self._playback_context = self.engine.decoder
            self.playback = Playback()
        if not self.display_paused:
            focus = getattr(self, "device_filter", None)
            if focus is not None:
                act = [t for t in act if any(f.addr == focus for f in t.frames)]
            if act and not self.playback.pinned and self.playback.finished:
                self.playback.select(act[-1], pinned=False)
            self.playback.advance(elapsed)
        self._sync_playback()
        if self.capture_t0 is not None and (self.capturing or self.demo):
            s = int(time.monotonic() - self.capture_t0)
            self.lbl_dur.setText(f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}")
        self.p_dash.refresh_health(self.engine)
        if self.display_paused:
            if self.stack.currentIndex() == 6:
                self.p_dev.refresh(self.engine)
            return
        i = self.stack.currentIndex()
        reg = self.engine.registry
        now = self.engine.hw_now or 0
        self.p_dash.net.set_devices(reg.node_list(now))
        page = self.pages[i]
        page.refresh(self.engine)
        self._ticks = getattr(self, "_ticks", 0) + 1
        now_wall = time.monotonic()
        if self.capturing and now_wall - getattr(self, "_last_pipeline_log", 0) >= 2:
            logging.getLogger(__name__).info(
                "GUI refresh: records=%s rejected=%s packets=%s waveform_points=%s snapshots=%s capturing_status=%s",
                self.engine.records_received, self.engine.transport_errors, len(self.engine.packets),
                len(self.engine.edges), self.engine.waveform_snapshots, self.engine.fw_stats.get("capturing"))
            self._last_pipeline_log = now_wall
        if self._ticks % 8 == 0:
            self._update_scl_status()
            self._set_state()

    def changeEvent(self, e):
        from PyQt6.QtCore import QEvent
        if e.type() == QEvent.Type.WindowStateChange:
            active = not self.isMinimized()
            for n in (self.p_dash.net, self.p_net.net, self.p_dev.net):
                n.set_active(active)
        super().changeEvent(e)

    def closeEvent(self, e):
        self._stop_sources()
        if self.worker:
            if self.capturing:
                self.worker.send("stop")
            self.worker.stop()
            self.worker.wait(1500)
        if self.master_worker:
            self.master_worker.stop()
            self.master_worker.wait(1500)
        e.accept()
