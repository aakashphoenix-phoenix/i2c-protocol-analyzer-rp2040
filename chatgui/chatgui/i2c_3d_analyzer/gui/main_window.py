"""Main window: header/toolbar, sidebar navigation, page stack, data-source control."""
from __future__ import annotations

import datetime as _dt
import gc
import logging
import json
import os
import time
import re

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QSizePolicy, QFileDialog, QFrame,
                             QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                             QStackedWidget, QStatusBar, QTextEdit, QVBoxLayout, QWidget)

import config
from analysis.capture_engine import CaptureEngine
from analysis.playback import FollowQueue, Playback, RealtimeQueue
from communication.protocol_parser import ProtocolError, capabilities_from_hello, parse_line
from communication.serial_manager import SerialWorker, list_ports, port_serials
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
from gui.theme import apply_theme, current_theme, set_mono, tint
from gui.widgets import FlowLayout, MOTION, ElidedLabel, StatusPill, label
from models.packet_model import PacketModel
from sources.demo_source import DemoSource
from storage.capture_storage import export_packets_csv, export_session_report, load_capture, save_capture

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
        mw = max(self.min_w, w.minimumSizeHint().width())
        lay = w.layout()

        def min_height(width):
            # minimumSizeHint() ignores word-wrapped labels (they need one more line when narrower): ask the layout for the
            # height it needs AT THIS WIDTH, otherwise a wrapped label is squeezed over its neighbour
            mh = max(self.min_h, w.minimumSizeHint().height())
            if lay is not None and lay.hasHeightForWidth():
                mh = max(mh, lay.minimumHeightForWidth(width))
            return mh
        mh = min_height(max(mw, vp.width()))
        need_v = vp.height() < mh
        need_h = vp.width() < mw
        width = max(mw, vp.width() - (self.verticalScrollBar().sizeHint().width() if need_v else 0))
        mh = min_height(width)
        need_v = vp.height() < mh
        width = max(mw, vp.width() - (self.verticalScrollBar().sizeHint().width() if need_v else 0))
        height = max(mh, vp.height() - (self.horizontalScrollBar().sizeHint().height() if need_h else 0))
        w.resize(width, height)


NAV_ROW_H = 34
NAV = [("dashboard", "Live Dashboard"), ("network", "3D I²C Network"), ("packets", "Packet Analyzer"),
       ("waveform", "Digital Waveform"), ("performance", "Performance Monitoring"), ("sensor", "Sensor Monitoring"),
       ("chip", "Device Activity"), ("error", "Error & Diagnostics"), ("history", "Capture History"),
       ("settings", "Settings")]
NAV_INDEX = {key: i for i, (key, _name) in enumerate(NAV)}
HIDDEN_NAV = ("performance", "chip", "history")   # Performance Monitoring, Device Activity, Capture History


class MainWindow(QMainWindow):
    SPEEDS = (0.1, 0.25, 0.4, 0.5)       # review playback speeds (no 1x / 2x)
    MAX_CATCHUP_S = 0.3                  # wall seconds of playback one refresh may cover (nominal refresh 0.1 s)
    RT_MIN_ON_SCREEN_S = 0.08            # real time: a packet stays at least this long (>= 2 frames of the 3D view)
    # Garbage-collector pacing.  A FULL (generation-2) pass blocks every Python thread, the analyzer's serial reader
    # too, for a time that grows with the capture (measured on the live bench: 0.05 s at 3 k packets, 0.46 s at 30 k,
    # one pass every 20-40 s).  Windows' usbser driver queues only ~25 kB (~0.5 s of the 53 kB/s analyzer stream,
    # whatever set_buffer_size asks for), after which the Pico drops characters: a corrupted line, a lost record and a
    # capture marked unreliable (Health UNKNOWN).  So every GC_FREEZE_S the young generations are collected (cheap) and
    # everything older is frozen: full passes then scan only objects created since, and stay ~tens of ms.
    GC_FREEZE_S = 15.0
    DEFAULT_SPEED = 0.25
    @property
    def master_worker(self):
        """First running controller link (single-master compatibility)."""
        return next((w for w in self.master_workers.values() if w is not None and w.isRunning()), None)

    def _master_running(self, mid):
        w = self.master_workers.get(mid)
        return w is not None and w.isRunning()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{config.APP_NAME} — {config.APP_SUBTITLE}")
        self.setWindowIcon(icon("logo", C["cyan"], C["cyan"], 64))
        # (org, app) alone ignores QSettings.setDefaultFormat(); spelling the format out keeps production on the native
        # store (unchanged) while letting tests / tools redirect settings to a private ini file instead of the registry
        self.settings = QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, "I2CAnalyzer", "I2C3DAnalyzer")
        self.engine = CaptureEngine()
        self.engine.set_oled_address(int(self.settings.value("oled_addr", config.OLED_ADDRESS_DEFAULT)))
        self.playback = Playback()
        self._playback_wall = time.monotonic()
        self._playback_context = self.engine.decoder
        self.packet_model = PacketModel(self.engine)
        # saved theme ('dark' default | 'presentation'): applied before any widget exists
        saved = str(self.settings.value("theme", config.THEME_DEFAULT))
        if saved not in config.THEMES:
            saved = config.THEME_DEFAULT
        if saved != current_theme():
            apply_theme(QApplication.instance(), saved)
        self.autoscroll = self.settings.value("autoscroll", True, bool)
        MOTION["animations"] = self.settings.value("animations", True, bool)
        MOTION["reduced"] = self.settings.value("reduced_motion", False, bool)
        self.worker: SerialWorker | None = None
        self.master_workers: dict = {mid: None for mid in config.MASTERS}   # one COM port per controller
        self._master_rtc = None
        self._master_bmp = None
        self.demo: DemoSource | None = None
        self.presentation = None             # PresentationBus while the scripted review sequence runs (SIMULATED)
        self.guided = None                   # GuidedFlow while the GUI conducts the REAL masters one transfer at a time
        self.caps = None
        self.capturing = False
        self.display_paused = False
        self.stage_hold = False          # local Pause of the stage playback (3D page stage bar): independent of display_paused
        self._follow_queue = FollowQueue()   # fair round-robin of the newest unplayed packet per (address, master)
        self._rt_queue = RealtimeQueue()     # "Real-time" playback: every packet in capture order, skipping to stay current
        self._follow_shown = []              # the last few packets follow mode already played (never replayed)
        # 3D network lifecycle: "running" (follows live traffic) -> Stop -> "draining" (the packet on display finishes its
        # ADDR / DATA / ACK-NACK stages and every dot reaches its receiver, nothing new starts) -> "stopped" (frozen).
        # Start capture / Connect all / Demo / a recording flush everything queued before Stop and run again.
        self.net_run_state = "running"
        self._drain_deadline = None
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

        # ---------------- header row 1: identity, link state, session
        head = QFrame()
        head.setObjectName("header")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(18, 8, 18, 8)
        hv.setSpacing(6)
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        self.logo_lbl = QLabel()
        self.logo_lbl.setPixmap(pixmap("logo", C["cyan"], 30))
        r1.addWidget(self.logo_lbl)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        tcol.addWidget(label(config.APP_NAME, "appTitle"))
        self.app_sub = ElidedLabel(config.APP_SUBTITLE + "  ·  RP2040 passive analyzer", "appSub")
        self.app_sub.setMinimumWidth(120)
        tcol.addWidget(self.app_sub)
        r1.addLayout(tcol, 0)
        r1.addSpacing(8)
        self.pill = StatusPill()
        r1.addWidget(self.pill)
        self.hwinfo = ElidedLabel("", "appSub")           # firmware / board / rate: elided, full text in the tooltip
        r1.addWidget(self.hwinfo, 1)
        self.lbl_session = ElidedLabel("Session —", "appSub", Qt.TextElideMode.ElideMiddle)
        set_mono(self.lbl_session)                         # session ids are timestamps: fixed-pitch digits
        self.lbl_session.setMinimumWidth(60)
        self.lbl_dur = label("00:00:00", "mono")
        self.lbl_dur.setStyleSheet(f"color:{C['cyan']}; font-size:15px;")
        r1.addWidget(self.lbl_session)
        r1.addWidget(self.lbl_dur)
        self.btn_demo = QPushButton("Demo mode")
        self.btn_demo.setObjectName("demo")
        self.btn_demo.setCheckable(True)
        self.btn_demo.setToolTip("Simulated records through the full decoder. Clearly labelled as DEMO everywhere.")
        self.btn_demo.toggled.connect(self.toggle_demo)
        # one-click light, high-contrast preset for projectors / screenshots (kept in sync with Settings, persisted)
        self.btn_theme = QPushButton("Presentation theme")
        self.btn_theme.setObjectName("theme")
        self.btn_theme.setCheckable(True)
        self.btn_theme.setChecked(current_theme() == "presentation")
        self.btn_theme.setToolTip("Switch to the light, high-contrast presentation theme (and back). Same as Settings → "
                                  "Display & animation.")
        self.btn_theme.toggled.connect(lambda on: self.set_theme("presentation" if on else "dark"))
        self.btn_present = QPushButton("Presentation mode")
        self.btn_present.setObjectName("demo")
        self.btn_present.setCheckable(True)
        self.btn_present.setToolTip("Scripted review sequence (SIMULATED): Master 1 then Master 2 (Pico 3), each with the "
                                    "RTC, BMP280 and OLED, a WRITE then a READ, strictly one transfer at a time, phase by "
                                    "phase, with a bus-arbitration example. Never presented as hardware traffic.")
        self.btn_present.toggled.connect(self.toggle_presentation)
        r1.addWidget(self.btn_theme)
        r1.addWidget(self.btn_present)
        r1.addWidget(self.btn_demo)
        hv.addLayout(r1)

        # ---------------- header row 2: capture controls + compact port status strip [Ports ▾]
        r2 = FlowLayout(hspace=8, vspace=6)        # wraps on a narrow (laptop) screen instead of widening the window
        self.btn_refresh = QPushButton("Refresh ports")
        self.btn_connect = QPushButton("Connect")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_connect_all = QPushButton("Connect all")
        self.btn_connect_all.setObjectName("primary")
        self.btn_connect_all.setToolTip("One click: opens Master 1, Master 2 and the analyzer (masters first, so every "
                                        "packet can be attributed) and starts the capture")
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background:{C['border']};")
        self.btn_start = QPushButton("● Start capture")
        self.btn_start.setObjectName("primary")
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_pause = QPushButton("Pause display")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip("Pause / resume the display — capture continues in the background  [Space]")
        self.btn_clear = QPushButton("Clear")
        self.btn_save = QPushButton("Save capture")
        self.btn_csv = QPushButton("Export CSV")
        self.btn_report = QPushButton("Export Report")
        self.btn_report.setToolTip("One-click session summary (HTML or Markdown): overview, bus health & timing, Master 1 vs "
                                   "Master 2 and arbitration, devices, decoded sensor readings. Verified values only.")
        for w in (self.btn_connect_all, sep, self.btn_start, self.btn_stop, self.btn_pause, self.btn_clear):
            r2.addWidget(w)
        # compact status strip: "● Analyzer · ● M1 · ● M2   [Ports ▾]" (the full port rows live in the panel below)
        self.port_strip = QFrame()
        self.port_strip.setObjectName("portStrip")
        sl = QHBoxLayout(self.port_strip)
        sl.setContentsMargins(12, 2, 4, 2)
        sl.setSpacing(8)
        self.port_strip_label = QLabel()
        self.port_strip_label.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.port_strip_label)
        self.btn_ports = QPushButton("Ports ▾")
        self.btn_ports.setObjectName("portToggle")
        self.btn_ports.setCheckable(True)
        self.btn_ports.setToolTip("Show / hide the Analyzer, Master 1 and Master 2 COM-port rows")
        self.btn_ports.toggled.connect(self.set_ports_expanded)
        sl.addWidget(self.btn_ports)
        r2.addWidget(self.port_strip)
        r2.addWidget(self.btn_save)
        r2.addWidget(self.btn_csv)
        r2.addWidget(self.btn_report)
        hv.addLayout(r2)

        # ---------------- collapsible port panel: one row per USB port (analyzer + one per controller)
        # Telemetry, arbitration reports and SCL/RTC commands come over the controller ports; never bus packets
        # (the analyzer owns those).  Auto-collapses once all three ports are connected.
        self.port_panel = QFrame()
        self.port_panel.setObjectName("portPanel")
        pg_ = QGridLayout(self.port_panel)
        pg_.setContentsMargins(12, 8, 12, 8)
        pg_.setHorizontalSpacing(10)
        pg_.setVerticalSpacing(6)
        pg_.setColumnStretch(1, 1)
        pg_.setColumnMinimumWidth(0, 158)
        pg_.setColumnMinimumWidth(3, 250)
        self.port = QComboBox()
        self._setup_port_combo(self.port)
        self._tip_combo(self.port, "COM port of the passive analyzer Pico (usually " + config.ANALYZER_PORT_HINT + ")")
        arow = QHBoxLayout()
        arow.setSpacing(8)
        for b in (self.btn_refresh, self.btn_connect, self.btn_disconnect):
            arow.addWidget(b)
        self.analyzer_state = ElidedLabel("", "portBadge")
        self.analyzer_state.setMinimumWidth(120)
        pg_.addWidget(label("Analyzer · RP2040 tap", "appSub"), 0, 0)
        pg_.addWidget(self.port, 0, 1)
        pg_.addLayout(arow, 0, 2)
        pg_.addWidget(self.analyzer_state, 0, 3)
        self.master_ports, self.master_states, self.master_toggles, self._master_tip = {}, {}, {}, {}
        for n, (mid, m) in enumerate(config.MASTERS.items(), start=1):
            combo = QComboBox()
            self._setup_port_combo(combo)
            self._master_tip[mid] = (f"COM port of {m['label']} ({m['pico']}, {m['board']}; usually {m['port_hint']}). "
                                     "Without it its packets can only be attributed by configured ownership.")
            self._tip_combo(combo, self._master_tip[mid])
            tog = QPushButton("Connect")
            tog.setMinimumWidth(96)
            tog.clicked.connect(lambda _=False, k=mid: self.toggle_master(k))
            st = ElidedLabel("", "portBadge")             # "● COM32 · fw 2.0-multimaster": elided + tooltip, never cut
            st.setMinimumWidth(120)
            mrow = QHBoxLayout()
            mrow.setSpacing(8)
            mrow.addWidget(tog)
            mrow.addStretch(1)
            pg_.addWidget(label(f"{m['label']} · {m['board']}", "appSub"), n, 0)
            pg_.addWidget(combo, n, 1)
            pg_.addLayout(mrow, n, 2)
            pg_.addWidget(st, n, 3)
            self.master_ports[mid], self.master_states[mid], self.master_toggles[mid] = combo, st, tog
        # aliases (single-master code paths / tools): Connect and Disconnect are one toggle per master now
        self.btn_master_connects = self.btn_master_disconnects = self.master_toggles
        self.master_port, self.master_state = self.master_ports[1], self.master_states[1]
        self.btn_master_connect = self.btn_master_disconnect = self.master_toggles[1]
        hv.addWidget(self.port_panel)
        self._ports_expanded = True
        self._all_ports_prev = False

        # ---------------- header row 3: playback + controller SCL + SCL status
        r4 = FlowLayout(hspace=8, vspace=6)        # wraps on a narrow (laptop) screen instead of widening the window
        r4.addWidget(label("Playback", "appSub"))
        self.visual_speed = QComboBox()
        # "Real-time": every captured packet in capture order, each in about the real time to the next one (condensed
        # START / ADDR / ACK / DATA / ACK / STOP), skipping packets rather than falling behind.  The slow review speeds
        # (no 1x / 2x) play every phase long enough to explain it.
        self.visual_speed.addItem("Real-time", "rt")
        for speed in self.SPEEDS:
            self.visual_speed.addItem(f"{speed:g}×", speed)
        self.visual_speed.setCurrentIndex(0)
        self.visual_speed.setToolTip("Real-time (default): live packets in capture order, never more than 2 s behind the "
                                     "bus (packets that cannot be shown in time are skipped and counted). 0.1×-0.5×: "
                                     "slow phase-by-phase review. Acquisition is never changed.")
        self.visual_speed.currentIndexChanged.connect(self._apply_visual_speed)
        self.playback.realtime, self.playback.speed = True, 1.0          # the default selection applies from the start
        r4.addWidget(self.visual_speed)
        self.rt_info = label("", "cardHint")
        self.rt_info.setToolTip("Real-time playback: packets shown / skipped to keep up with the bus")
        r4.addWidget(self.rt_info)
        self.btn_play = QPushButton("⏸ Pause")
        self.btn_play.setToolTip("Pause on the current phase / play on  [P]")
        self.btn_play.clicked.connect(lambda: self.set_stage_hold(not self.stage_hold))
        r4.addWidget(self.btn_play)
        self.btn_step = QPushButton("Step ▸ next phase")
        self.btn_step.setToolTip("Pause and advance exactly one phase (START → ADDRESS → ACK/NACK → DATA → ACK/NACK → "
                                 "STOP); after STOP it starts the next transfer  [N]")
        self.btn_step.clicked.connect(self.step_phase)
        r4.addWidget(self.btn_step)
        self.btn_nack = QPushButton("NACK next transfer")
        self.btn_nack.setCheckable(True)
        self.btn_nack.setToolTip("Presentation mode / ▶ Guided flow: the NEXT transfer is not acknowledged at its address "
                                 "(SDA stays HIGH at the 9th clock), so a NACK can be shown next to the normal ACKs.  In "
                                 "the guided flow this is REAL: the master due next addresses a device that is not on "
                                 "the bus (0x2A / 0x45 / 0x13 / 0x5B)")
        self.btn_nack.setEnabled(False)
        self.btn_nack.toggled.connect(self._nack_toggled)
        r4.addWidget(self.btn_nack)
        self.btn_prev_packet = QPushButton("◀ Packet")
        self.btn_next_packet = QPushButton("Packet ▶")
        self.btn_prev_packet.setToolTip("Previous packet  [←]")
        self.btn_next_packet.setToolTip("Next packet  [→]")
        self.btn_prev_packet.clicked.connect(lambda: self.step_packet(-1))
        self.btn_next_packet.clicked.connect(lambda: self.step_packet(1))
        r4.addWidget(self.btn_prev_packet); r4.addWidget(self.btn_next_packet)
        self.btn_follow = QPushButton("Follow latest")
        self.btn_follow.setToolTip("Inspection shows selected events; all retained packets remain in the table  [F]")
        self.btn_follow.clicked.connect(self.follow_latest)
        r4.addWidget(self.btn_follow)
        sep2 = QFrame()
        sep2.setFixedWidth(1)
        sep2.setStyleSheet(f"background:{C['border']};")
        r4.addWidget(sep2)
        # Physical controller SCL setting: separate from visual playback speed.
        r4.addWidget(label("SCL", "appSub"))
        self.scl_target = QComboBox()
        for mid, m in config.MASTERS.items():
            self.scl_target.addItem(f"{m['label']} ({m['board']})", [mid])
        self.scl_target.addItem("Both masters", list(config.MASTERS))
        self.scl_target.setCurrentIndex(self.scl_target.count() - 1)
        self.scl_target.setToolTip("Which controller(s) receive the SCL command. With two masters the bus runs at "
                                   "the slower master's clock while both are clocking (clock synchronisation), so "
                                   "set both to the same rate.")
        r4.addWidget(self.scl_target)
        self.scl_choice = QComboBox()
        for hz, text in config.CONTROLLER_SCL_OPTIONS:
            self.scl_choice.addItem(text, hz)
        self.scl_choice.setToolTip("Rates every configured part supports (DS3231/SSD1306 ≤ 400 kHz). Sent only to the "
                                   "CONTROLLER Pico's own USB port after you press Apply; the analyzer never changes SCL.")
        r4.addWidget(self.scl_choice)
        self.btn_scl_apply = QPushButton("Apply")
        self.btn_scl_apply.clicked.connect(self.apply_scl)
        r4.addWidget(self.btn_scl_apply)
        # kHz read-out: fixed-pitch digits; a long sentence is elided in the middle (the setting at the start and the
        # measured rolling kHz at the end both stay visible) and is complete in the tooltip
        self.scl_status = ElidedLabel("Controller SCL: not known · measured SCL appears once cycles are captured",
                                      "cardHint", Qt.TextElideMode.ElideMiddle)
        set_mono(self.scl_status)
        self.scl_status.setMinimumWidth(0)
        self.scl_status.setFixedWidth(430)        # elided; the full sentence is the tooltip
        r4.addWidget(self.scl_status)
        hv.addLayout(r4)

        # ---------------- header row 4: live review controls (REAL hardware only; commands go to the master Picos)
        # a wrapping row: ~12 controls need ~2300 px in one line - wider than a 1536 px laptop screen, which pushed the
        # whole window (and the right side of every page) off the screen
        r5 = FlowLayout(hspace=8, vspace=6)
        r5.addWidget(label("Live review", "appSub"))
        self.btn_guided = QPushButton("▶ Guided flow")
        self.btn_guided.setCheckable(True)
        self.btn_guided.setObjectName("primary")
        self.btn_guided.setToolTip("REAL hardware, one transfer at a time: Master 1 RTC 0x68 → BMP280 0x76 → OLED 0x3C, then "
                                   "Master 2 RTC → BMP280 → OLED, repeating. Each packet is played completely - every "
                                   "byte and its own ACK / NACK - before the next one is sent. With 'Inject errors' an "
                                   "invalid address (rotating 0x2A / 0x45 / 0x13 / 0x5B, alternating masters) or an "
                                   "arbitration between different slaves follows every cycle. (Controller firmware "
                                   "2.6-live, CONDUCT mode; the OLED shows each master's live time / temperature / "
                                   "pressure.)")
        self.btn_guided.toggled.connect(self.toggle_guided)
        r5.addWidget(self.btn_guided)
        self.guided_pace = QComboBox()
        for sec in (0.15, 0.25, 0.4, 0.6):
            self.guided_pace.addItem(f"{sec:g} s / stage", sec)
        self.guided_pace.setCurrentIndex(1)
        self.guided_pace.setToolTip("How long each protocol stage (START, address, every byte, every ACK / NACK, STOP) is "
                                    "shown in the guided flow")
        self.guided_pace.currentIndexChanged.connect(
            lambda *_: setattr(self.guided, "stage_s", self.guided_pace.currentData()) if self.guided else None)
        r5.addWidget(self.guided_pace)
        self.chk_inject = QCheckBox("Inject errors")
        self.chk_inject.setChecked(True)
        self.chk_inject.setToolTip("After every cycle: an invalid address (Address NACK) or an arbitration, alternating")
        self.chk_inject.toggled.connect(
            lambda v: setattr(self.guided, "inject_errors", v) if self.guided else None)
        r5.addWidget(self.chk_inject)
        self.btn_seq = QPushButton("Spaced sequence (SEQ)")
        self.btn_seq.setCheckable(True)
        self.btn_seq.setToolTip("SEQ ON: Master 1 RTC 0x68 → gap → BME280/BMP280 0x76 → gap → OLED 0x3C (command, gap, "
                                "data), then Master 2 in the same order, one transfer at a time with the selected idle gap "
                                "(SDA = SCL = 1) between them, so every packet stands alone on the waveform. "
                                "SEQ OFF: the normal 4 s turns.  (Controller firmware 2.4-turns.)")
        self.btn_seq.toggled.connect(self.toggle_seq)
        r5.addWidget(self.btn_seq)
        self.seq_gap = QComboBox()
        for ms in (10, 100, 500):
            self.seq_gap.addItem(f"{ms} ms gap", ms)
        self.seq_gap.setCurrentIndex(2)
        self.seq_gap.setToolTip("Idle bus between two transfers in SEQ mode (2 × this between the two masters' turns)")
        self.seq_gap.currentIndexChanged.connect(self._seq_gap_changed)
        r5.addWidget(self.seq_gap)
        self.btn_seq.hide()                  # superseded by the guided flow (still supported by the firmware)
        self.seq_gap.hide()
        sep3 = QFrame()
        sep3.setFixedWidth(1)
        sep3.setStyleSheet(f"background:{C['border']};")
        r5.addWidget(sep3)
        self.probe_master = QComboBox()
        for mid, m in config.MASTERS.items():
            self.probe_master.addItem(m["label"], mid)
        self.probe_master.setToolTip("Which master sends the probe")
        self.probe_addr = QLineEdit("0x2A")
        self.probe_addr.setFixedWidth(64)
        self.probe_addr.setToolTip("7-bit address to probe (hex). 0x2A has no device on this bench: a real Address NACK")
        self.btn_probe = QPushButton("Send invalid address")
        self.btn_probe.setToolTip("The selected master sends ONE real write to this address in its next turn. With no "
                                  "slave there, SDA stays HIGH at the 9th clock: a genuine Address NACK on the bus.")
        self.btn_probe.clicked.connect(self.send_probe)
        for w in (self.probe_master, self.probe_addr, self.btn_probe):
            r5.addWidget(w)
        self.btn_arb = QPushButton("Trigger arbitration")
        self.btn_arb.setToolTip("Both masters start back-to-back transfers with no idle window (Master 1 → 0x76, Master 2 → "
                                "0x68): their STARTs coincide and Master 1 really loses arbitration on the wired-AND bus "
                                "(reported by its own firmware, retried - never a NACK)")
        self.btn_arb.clicked.connect(self.trigger_arbitration)
        r5.addWidget(self.btn_arb)
        self.btn_vis_err = QPushButton("⚠ Visualize last error")
        self.btn_vis_err.setToolTip("Open the newest error packet (address NACK, data NACK, early read NACK, incomplete) in "
                                    "the Digital Waveform, paused and fitted, every field labelled above SCL / SDA")
        self.btn_vis_err.clicked.connect(self.visualize_last_error)
        self.btn_vis_arb = QPushButton("⚡ Visualize last arbitration")
        self.btn_vis_arb.setToolTip("Open the winning bus packet of the newest arbitration loss in the Digital Waveform, "
                                    "with the collision marked in amber above the traces")
        self.btn_vis_arb.clicked.connect(self.visualize_last_arbitration)
        r5.addWidget(self.btn_vis_err)
        r5.addWidget(self.btn_vis_arb)
        hv.addLayout(r5)
        # a mouse click on a header button must not steal the keyboard focus from the window: Space / arrows /
        # F / + / - (global shortcuts) keep working after every click; Tab still reaches every button
        for b in head.findChildren(QPushButton):
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
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
        sv.setContentsMargins(0, 8, 0, 8)
        sv.setSpacing(2)
        sv.addWidget(label("  NAVIGATION", "metricName"))
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(18, 18))
        for ic, name in NAV:
            it = QListWidgetItem(icon(ic), "  " + name)
            it.setSizeHint(QSize(0, NAV_ROW_H))
            self.nav.addItem(it)
        for key in HIDDEN_NAV:                 # not part of the review: hidden (the pages still exist)
            self.nav.item(NAV_INDEX[key]).setHidden(True)
        # every entry always fits (the last one, Settings, used to be cut off above the footer)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setMinimumHeight(len(NAV) * (NAV_ROW_H + 2) + 6)
        sv.addWidget(self.nav, 1)
        self.side_mode = label("", "cardHint", wrap=True)
        self.side_mode.setContentsMargins(16, 0, 12, 0)
        self.side_mode.setStyleSheet("font-size:8pt;")
        self.side_mode.setMaximumHeight(64)          # never squeezes the nav list; full caps are in the tooltip
        self.side_mode.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
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
            sa = FitScroll(900, 480 if p is self.p_net else 640)   # works on 1366x768 screens too; the 3D page is compact
            p.setObjectName("scrollBody")
            sa.setWidget(p)
            self.stack.addWidget(sa)
        body.addWidget(self.stack, 1)
        bw = QWidget()
        bw.setLayout(body)
        v.addWidget(bw, 1)
        self.status = QStatusBar()
        self.status.setStyleSheet(self._status_css())
        self.setStatusBar(self.status)

        self.nav.currentRowChanged.connect(self._goto)
        self.nav.setCurrentRow(0)
        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_connect.clicked.connect(self.connect_analyzer)
        self.btn_connect_all.clicked.connect(self.connect_all)
        self.btn_disconnect.clicked.connect(self.disconnect_analyzer)
        self.btn_start.clicked.connect(self.start_capture)
        self.btn_stop.clicked.connect(self.stop_capture)
        self.btn_pause.toggled.connect(self._pause_display)
        self.btn_clear.clicked.connect(self.clear_capture)
        self.btn_save.clicked.connect(self.save_capture)
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_report.clicked.connect(self.export_report)
        self._build_shortcuts()
        self.set_ports_expanded(True)
        self._refresh_port_strip()

    # ================================================================== theme (dark default | light presentation)
    @staticmethod
    def _status_css():
        return f"QStatusBar{{background:{C['status_bg']};color:{C['text2']};border-top:1px solid {C['border']};}}"

    def set_theme(self, name):
        """Switch theme live (header button and Settings both call this): persists, re-themes, keeps both controls in sync."""
        if name not in config.THEMES:
            return False
        self.settings.setValue("theme", name)
        changed = apply_theme(QApplication.instance(), name, force=True)
        self.on_theme_changed()
        if changed:
            self.toast(f"Theme: {config.THEME_LABELS[name]}")
        return changed

    def on_theme_changed(self):
        """Everything that caches a colour or an icon, after gui.theme.apply_theme() has reloaded the palette and
        re-themed the generic parts (application style sheet, inline style sheets, plots, widgets with retheme())."""
        name = current_theme()
        for btn_state, widget in ((name == "presentation", self.btn_theme),):
            if widget.isChecked() != btn_state:
                widget.blockSignals(True)
                widget.setChecked(btn_state)
                widget.blockSignals(False)
        if hasattr(self, "p_set"):
            self.p_set.sync_theme(name)
        self.setWindowIcon(icon("logo", C["cyan"], C["cyan"], 64))
        self.logo_lbl.setPixmap(pixmap("logo", C["cyan"], 30))
        for i, (ic, _name) in enumerate(NAV):
            self.nav.item(i).setIcon(icon(ic))
        self.status.setStyleSheet(self._status_css())
        self.lbl_dur.setStyleSheet(f"color:{C['cyan']}; font-size:15px;")
        self._set_state()                        # status pill, banner, port badges and strip, side text
        self.p_dash.refresh_health(self.engine)
        self.pages[self.stack.currentIndex()].refresh(self.engine)
        self.update()

    # ================================================================== collapsible port bar
    @staticmethod
    def _tip_combo(combo, tip):
        """Fixed explanation + the (possibly elided) selected port text, kept up to date by _setup_port_combo."""
        combo.setProperty("baseTip", tip)
        combo.setToolTip(tip + chr(10) + combo.currentText() if combo.currentText() else tip)

    @staticmethod
    def _setup_port_combo(combo):
        """Port selector that elides long 'COMx — description' text instead of widening the header."""
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(18)
        combo.setMinimumWidth(200)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        combo.currentIndexChanged.connect(lambda _=0, c=combo: c.setToolTip(
            (c.property("baseTip") or "") + ("\n" if c.property("baseTip") else "") + c.currentText()))

    def set_ports_expanded(self, on: bool):
        """Show / hide the full Analyzer / Master 1 / Master 2 port rows ([Ports ▾] toggles this)."""
        on = bool(on)
        self._ports_expanded = on
        self.port_panel.setVisible(on)
        self.btn_ports.setText("Ports ▴" if on else "Ports ▾")
        if self.btn_ports.isChecked() != on:
            self.btn_ports.blockSignals(True)
            self.btn_ports.setChecked(on)
            self.btn_ports.blockSignals(False)

    def _port_states(self):
        """(analyzer, {master id: bool}) connection state; analyzer is 'on' | 'wait' (open, no handshake) | 'off'."""
        an = "off"
        if self.worker is not None and self.worker.isRunning():
            an = "on" if self.caps is not None else "wait"
        return an, {mid: self._master_running(mid) for mid in self.master_ports}

    def _refresh_port_strip(self):
        """Compact '● Analyzer · ● M1 · ● M2' strip + the analyzer badge; auto-collapses the port rows when all 3 ports
        are connected (and re-opens them when one drops, so the missing port is in front of the user)."""
        an, masters = self._port_states()
        demo_on = self.demo is not None and self.demo.isRunning()
        col = {"on": C["green"], "wait": C["amber"], "off": C["red"]}
        if demo_on:
            col = {k: C["demo"] for k in col}
        parts, tips = [], []
        a_dot = col[an]
        parts.append(f"<span style='color:{a_dot}'>●</span> Analyzer")
        a_txt = {"on": "connected", "wait": "open — waiting for handshake", "off": "NOT CONNECTED"}[an]
        port = self.worker.port if (self.worker is not None and an != "off") else ""
        tips.append(f"Analyzer: {a_txt}" + (f" on {port}" if port else ""))
        for mid, on in masters.items():
            m = config.MASTERS[mid]
            dot = C["demo"] if demo_on else (C["green"] if on else C["red"])
            short = "M" + str(mid)
            parts.append(f"<span style='color:{dot}'>●</span> {short}")
            w = self.master_workers.get(mid)
            tips.append(f"{m['label']} ({m['board']}): " + (
                "SIMULATED controller (demo bench), no hardware" if demo_on and self.engine.masters.state[mid].simulated
                else f"connected on {w.port}" if on and w else "NOT CONNECTED"))
        self.port_strip_label.setText("  ·  ".join(parts))
        tip = "\n".join(tips) + ("\nDemo mode: simulated stream, no hardware" if demo_on else "")
        self.port_strip.setToolTip(tip)
        self.port_strip_label.setToolTip(tip)
        # analyzer badge in the port panel (same look as the master badges)
        if an == "on":
            fw = f" · fw {self.caps.firmware}" if getattr(self.caps, "firmware", None) else ""
            text, color, bg = f"● {port}{fw}", C["green"], tint(C["green"], 0.10)
        elif an == "wait":
            text, color, bg = f"● {port} · waiting for handshake", C["amber"], tint(C["amber"], 0.10)
        else:
            text, color, bg = "● NOT CONNECTED", C["red"], tint(C["red"], 0.12)
        self.analyzer_state.setText(text)
        self.analyzer_state.setToolTip(text if an != "off" else "The analyzer is not connected: pick its COM port and "
                                                                 "press Connect, or use Connect all.")
        style = f"color:{color}; background:{bg}; border:1px solid {color}; border-radius:9px; padding:2px 8px; font-weight:600;"
        if self.analyzer_state.styleSheet() != style:
            self.analyzer_state.setStyleSheet(style)
        # nothing to pick while the simulator runs, and nothing left to pick once every real port is open: fold the rows
        all_ports = demo_on or (an != "off" and all(masters.values()))
        if all_ports != self._all_ports_prev:
            self._all_ports_prev = all_ports
            self.set_ports_expanded(not all_ports)

    # ================================================================== global keyboard shortcuts
    INPUT_WIDGETS = (QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit)

    def _build_shortcuts(self):
        """Window-level QShortcuts.  They stay out of the way of typing: whenever the focus is in an editable / input
        widget the matching shortcuts are disabled (so the key reaches that widget) and the handler re-checks."""
        self._shortcuts = {}                       # action -> [QShortcut]

        def add(action, keys, slot, repeat=True):
            for key in keys:
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.ShortcutContext.WindowShortcut)
                sc.setAutoRepeat(repeat)
                sc.activated.connect(lambda a=action, f=slot: self._run_shortcut(a, f))
                self._shortcuts.setdefault(action, []).append(sc)

        add("pause", ["Space"], lambda: (self.btn_pause.toggle(), self._sync_playback()), repeat=False)
        add("prev", ["Left"], lambda: self.step_packet(-1))
        add("next", ["Right"], lambda: self.step_packet(1))
        add("follow", ["F"], self.follow_latest, repeat=False)
        add("play_pause", ["P"], lambda: self.set_stage_hold(not self.stage_hold), repeat=False)
        add("step_phase", ["N"], self.step_phase, repeat=True)
        add("zoom_in", ["+", "="], lambda: self.zoom_waveform(+1))
        add("zoom_out", ["-"], lambda: self.zoom_waveform(-1))
        QApplication.instance().focusChanged.connect(self._update_shortcuts)
        self._update_shortcuts()

    def _shortcut_blocked(self, action):
        """True when the focused widget needs this key itself (typing, spin boxes, combo popups, focused buttons)."""
        fw = QApplication.focusWidget()
        if QApplication.activePopupWidget() is not None:
            return True
        if isinstance(fw, self.INPUT_WIDGETS):
            return True
        return action == "pause" and isinstance(fw, QAbstractButton)       # Space presses the focused button

    def _update_shortcuts(self, *_):
        for action, scs in getattr(self, "_shortcuts", {}).items():
            blocked = self._shortcut_blocked(action)
            for sc in scs:
                sc.setEnabled(not blocked)

    def _run_shortcut(self, action, slot):
        if self._shortcut_blocked(action):
            return
        slot()

    def step_packet(self, d):
        """Previous (-1) / next (+1) packet in the waveform (shared selection: table, detail, 3D)."""
        self.p_wave._step(d)

    # ------------------------------------------------------------------ stage bar (3D network page)
    def set_stage_hold(self, on):
        """Freeze (True) / resume (False) the stage playback.  While held, the cursor neither advances nor jumps to a newer
        packet; capture, the packet table and every other page keep running, so nothing fights live traffic."""
        self.stage_hold = bool(on)
        if hasattr(self, "btn_play"):
            self.btn_play.setText("▶ Play" if self.stage_hold else "⏸ Pause")
        if hasattr(self, "p_net"):
            self.p_net.stage_bar.set_hold(self.stage_hold)
            self.p_net.update_stage_bar(self.playback, self.stage_hold)

    def stage_goto(self, index):
        """Jump to stage `index` of the packet on display and hold there (a click on a chip)."""
        pb = self.playback
        if not pb.events:
            return
        self.set_stage_hold(True)
        for net in self._nets():
            net.finish_dots()                        # the phase on screen completes first: never two phases at once
        pb.position = float(max(0, min(len(pb.events) - 1, int(index))))
        self._sync_playback()

    def stage_step(self, d):
        """Previous / next stage of the packet on display (byte by byte); freezes playback first."""
        pb = self.playback
        if not pb.events:
            return
        self.stage_goto(pb.stage_index + d)

    def step_phase(self):
        """Pause and show exactly the next phase.  After the last phase (STOP) of a transfer the NEXT transfer starts at
        its first phase - in presentation mode the bus releases it, live it is the next packet of the fair queue."""
        self.set_stage_hold(True)
        pb = self.playback
        if pb.events and not pb.finished and pb.stage_index < len(pb.events) - 1:
            self.stage_goto(pb.stage_index + 1)
            return
        nxt = None
        if self.guided is not None and self.net_run_state == "running":
            g = self.guided
            if g.state == "wait":
                self.toast(f"Waiting for the real packet: {g.label(g.step)}")
                return
            g.tick(time.monotonic(), force=True)          # sends the next command; its packet is played held at phase 1
            if g.state == "wait":
                self.toast(f"Sent to the hardware: {g.label(g.step)} - its packet opens at phase 1 (Step ▸ again)")
            return
        if self.net_run_state == "running":
            if self.presentation is not None:
                nxt = self._release_presentation_transfer(select=False)
            else:
                nxt = self._follow_next(getattr(self, "device_filter", None))
        if nxt is None:
            self.toast("No further transfer yet" if self.net_run_state == "running" else
                       "Stopped — press Connect all / Start capture / Presentation mode to run again")
            return
        self.playback.select(nxt, pinned=False, full_pace=True)
        self._follow_shown.append(nxt)
        del self._follow_shown[:-12]
        self.stage_goto(0)

    def follow_latest(self):
        """Un-pin and follow the newest packet (of the device filter, if any)."""
        self.set_stage_hold(False)
        self.follow_device(getattr(self, "device_filter", None))
        self.p_wave.follow.setChecked(True)

    def zoom_waveform(self, direction):
        """+ / - zoom the Digital Waveform time axis (only meaningful while that page is showing)."""
        if self.stack.currentIndex() != 3:
            self.toast("Zoom + / − applies to the Digital Waveform page")
            return
        (self.p_wave.zoom_in_view if direction > 0 else self.p_wave.zoom_out_view)()

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
        self._sync_playback()          # the diagram that just became visible shows the current stage (and its dot) at once

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
            self.follow_device(key)
            if key is None:
                return
            self.p_net._select(key)
            if switch:
                self.nav.setCurrentRow(2)
            else:
                self.toast(f"Packet tables filtered to 0x{key:02X} — see Packet Analyzer or Device Activity")
        elif key in ("master", "master2", "analyzer"):
            self.p_net._select(key)

    def follow_device(self, addr):
        """Un-pin and show the newest captured packet for addr (or any device).

        Display-only: capture flags, worker and engine records are not touched."""
        self.playback.pinned = False
        match = None
        for t in reversed(self.engine.packets[-5000:]):
            if addr is None or any(f.addr == addr for f in t.frames):
                match = t
                break
        self._follow_latest_txn = match
        if match is not None:
            self.playback.select(match, pinned=False)
            self._follow_shown.append(match)
            del self._follow_shown[:-12]
            self.p_pk.show_detail(match)
            self.p_wave.show_packet(match, synchronize=False)
        self._sync_playback()

    def _chosen_speed(self):
        """Visual playback speed currently selected in the toolbar (Real-time plays at 1x of its own packet budget)."""
        combo = getattr(self, "visual_speed", None)
        speed = combo.currentData() if combo is not None else None
        if speed == "rt":
            return 1.0
        return float(speed) if speed else self.DEFAULT_SPEED

    def _realtime(self):
        combo = getattr(self, "visual_speed", None)
        return combo is not None and combo.currentData() == "rt"

    def _apply_visual_speed(self, *_):
        self.playback.realtime = self._realtime()
        self.playback.speed = self._chosen_speed()
        self._rt_queue.clear()

    def bus_now_ns(self):
        """The analyzer's current capture time: the newest segment or edge-stream entry (its idle heartbeats keep it
        moving while the bus is quiet).  None before any capture data."""
        e = self.engine
        v = max(e.hw_now or 0, getattr(e, "stream_now", None) or 0)
        return v or None

    def _follow_next(self, focus):
        """Next packet for follow mode: no device filter -> the fair queue (0x68 / 0x76 / 0x3C and both masters take turns);
        a device filter -> the newest matching packet.  A packet that was already shown is never replayed."""
        if focus is None and self._realtime() and self.presentation is None:
            # Real-time: strictly the next captured packet (an unattributed one plays as unattributed after the short
            # telemetry wait - a master is never guessed); the fair queue is not used
            txn = self._rt_queue.pop(can_attribute=self.engine.masters.can_attribute, bus_now_ns=self.bus_now_ns())
            if hasattr(self, "rt_info"):
                q = self._rt_queue
                self.rt_info.setText(f"live order · {len(q)} queued · {q.skipped:,} skipped" if q.skipped or len(q) else
                                     "live order")
            return txn
        if focus is None:
            txn = self._follow_queue.pop(can_attribute=self.engine.masters.can_attribute)
            # while a controller is connected and clock-aligned, a packet that no master claimed within the telemetry
            # grace period is not animated (it stays listed in the Packet Analyzer as unattributed): the 3D view only
            # ever plays transfers it can name as Master 1 (Pico 1) or Master 2 (Pico 3) - never a guessed master
            while (txn is not None and getattr(txn, "master_id", None) is None
                   and self.engine.masters.can_attribute()):
                txn = self._follow_queue.pop(can_attribute=self.engine.masters.can_attribute)
            if txn is not None:
                return txn
            if len(self._follow_queue):
                return None             # queued packets are waiting briefly for controller telemetry: never skip past them
        latest = getattr(self, "_follow_latest_txn", None)
        if latest is not None and latest is not self.playback.txn and not any(latest is s for s in self._follow_shown):
            # same short wait for controller telemetry as the fair queue (device-filter path); the wait is measured from the
            # first refresh that wanted this packet, so a fast-polling device cannot keep restarting it
            if FollowQueue.awaiting_telemetry(latest) and self.engine.masters.can_attribute():
                now = time.monotonic()
                since = self._follow_wait_since = getattr(self, "_follow_wait_since", None) or now
                if now - since < FollowQueue.ATTRIBUTION_SETTLE_S:
                    return None
                if getattr(latest, "master_id", None) is None:
                    return None         # still no master report: held (never animated as an unnamed master) until one
                    #                     arrives - then it plays entirely on that master's wire - or a newer packet comes
            self._follow_wait_since = None
            return latest
        self._follow_wait_since = None
        return None

    # ================================================================== live review controls (real hardware only)
    def _review_masters(self):
        return [mid for mid in self.master_ports if self._master_running(mid)]

    def _review_send(self, mids, cmd):
        sent = []
        for mid in mids:
            w = self.master_workers.get(mid)
            if w is not None and w.isRunning():
                w.send_controller(cmd)
                sent.append(config.MASTERS[mid]["label"])
        if sent:
            self.engine.log("info", "Session", "Review command", f"sent '{cmd}' to {', '.join(sent)}")
        return sent

    def _request_calibration(self, force=False):
        """DO CAL (controller 2.6-live): one master re-reads the BMP280 chip ID + calibration (0x88-0x9F) ON THE BUS, so a
        capture started after the masters booted still decodes temperature / pressure from the captured bytes.  Sent only
        while capturing live, only until the calibration has been captured, at most every 8 s."""
        if not self.capturing or self.engine.source != "live" or self.demo is not None or self.presentation is not None:
            return False
        if self.engine.sensors.bmp.calib and not self.engine.sensors.bmp.calib_simulated:
            return False
        now = time.monotonic()
        if not force and now - getattr(self, "_cal_requested_at", -1e9) < 8.0:
            return False
        mids = self._review_masters()
        if not mids:
            return False
        self._cal_requested_at = now
        return bool(self._review_send([1 if 1 in mids else mids[0]], "DO CAL"))

    def _review_allowed(self):
        if self.engine.source != "live" or self.demo is not None or self.presentation is not None:
            self.toast("Live review commands work in Real Hardware Mode only (Connect all, then capture)")
            return False
        if not self._review_masters():
            self.toast("Connect the master COM ports first (Connect all)")
            return False
        return True

    def toggle_seq(self, on):
        if not self._review_allowed():
            self.btn_seq.blockSignals(True)
            self.btn_seq.setChecked(False)
            self.btn_seq.blockSignals(False)
            return
        cmd = f"SEQ ON {self.seq_gap.currentData()}" if on else "SEQ OFF"
        sent = self._review_send(self._review_masters(), cmd)
        self.toast(f"Sent '{cmd}' to {', '.join(sent)}")

    def _seq_gap_changed(self, *_):
        if self.btn_seq.isChecked() and self.engine.source == "live":
            self._review_send(self._review_masters(), f"SEQ ON {self.seq_gap.currentData()}")

    def send_probe(self):
        if not self._review_allowed():
            return
        txt = self.probe_addr.text().strip().lower()
        try:
            a = int(txt[2:] if txt.startswith("0x") else txt, 16)
        except ValueError:
            a = -1
        if not 0x08 <= a <= 0x77:
            self.toast("Enter a 7-bit address 0x08..0x77 (hex), e.g. 0x2A")
            return
        mid = self.probe_master.currentData()
        if not self._review_send([mid], f"PROBE {a:02X}"):
            self.toast(f"{config.MASTERS[mid]['label']} is not connected")
            return
        self.toast(f"{config.MASTERS[mid]['label']} will write to 0x{a:02X} in its next turn: an absent address answers "
                   "with an Address NACK")

    def trigger_arbitration(self):
        if not self._review_allowed():
            return
        mids = self._review_masters()
        if len(mids) < 2:
            self.toast("Arbitration needs BOTH masters connected: connect Master 1 and Master 2")
            return
        self._review_send(mids, "ARB")              # back to back: the two masters start within USB latency
        self.toast("ARB sent to both masters: Master 1 (0x76) vs Master 2 (0x68) - watch Device Activity → Arbitration")

    def last_error_packet(self):
        from analysis.frame_view import is_error
        for t in reversed(self.engine.packets[-20000:]):
            if is_error(self.engine, t):
                return t
        return None

    # ================================================================== guided flow (REAL hardware, conducted)
    def toggle_guided(self, on):
        from gui.guided_flow import GuidedFlow
        if on:
            if not self._review_allowed() or len(self._review_masters()) < 2:
                if self._review_masters() and len(self._review_masters()) < 2:
                    self.toast("The guided flow needs BOTH masters connected (Connect all)")
                self.btn_guided.blockSignals(True)
                self.btn_guided.setChecked(False)
                self.btn_guided.blockSignals(False)
                return
            self.set_stage_hold(False)
            self.p_wave.follow.setChecked(False)          # the waveform shows each guided packet, decoded
            self.guided = GuidedFlow(self, stage_s=self.guided_pace.currentData(),
                                     inject_errors=self.chk_inject.isChecked())
            self.guided.start()
            self.set_ports_expanded(False)          # like presentation mode: the diagram gets the height
            self.toast("Guided flow: Master 1 RTC → BMP280 → OLED, then Master 2, one real transfer at a time")
        elif self.guided is not None:
            self.guided.stop()
            self.guided = None
            self.btn_nack.blockSignals(True)
            self.btn_nack.setChecked(False)
            self.btn_nack.blockSignals(False)
            self.p_wave.follow.setChecked(True)
            self.toast("Guided flow stopped: the masters run their own turns again")
        self._set_state()

    def _nack_toggled(self, on):
        """NACK next transfer: presentation mode reads the button when it releases a transfer; the guided flow queues a
        REAL invalid-address write by the master due next (the button stays down until that packet is played)."""
        if on and self.guided is not None:
            st = self.guided.queue_nack()
            self.toast(f"Next transfer: {self.guided.label(st)} - a real Address NACK")

    def play_guided(self, txn, stage_s, step):
        """Play one conducted packet completely: every stage (each byte and its own ACK / NACK) for stage_s seconds."""
        self.playback.select(txn, pinned=False, stage_s=stage_s)
        self._follow_shown.append(txn)
        del self._follow_shown[:-12]
        self.p_wave.show_packet(txn, synchronize=False)
        if self.stage_hold:
            self.stage_goto(0)                            # Step ▸ mode: the new packet opens at phase 1, held
        if step.get("user"):
            self.btn_nack.blockSignals(True)
            self.btn_nack.setChecked(False)               # one-shot, as in presentation mode
            self.btn_nack.blockSignals(False)
        if step.get("kind") == "PROBE":
            self.toast(f"Injected error: {config.MASTERS[step['mid']]['label']} addressed 0x{step['addr']:02X} - no "
                       "device there, so SDA stayed HIGH at the 9th clock: Address NACK (⚠ Visualize last error)")
        elif step.get("kind") == "ARB":
            ev = step.get("arb_event")
            loser = config.MASTERS.get(getattr(ev, "master_id", None), {}).get("label", "a master")
            self.toast(f"Injected arbitration: {loser} lost (wired-AND bus) and retried - the winner's packet is "
                       "playing (⚡ Visualize last arbitration shows the collision)")

    def visualize_packet(self, txn):
        """Open this exact packet in the Digital Waveform: playback pinned on it, Follow live off, fitted, labelled."""
        if txn is None:
            return
        self.set_stage_hold(True)
        self.select_transaction(txn)
        self.nav.setCurrentRow(NAV_INDEX["waveform"])
        QApplication.processEvents()
        self.p_wave.visualize(txn)

    def visualize_last_error(self):
        t = self.last_error_packet()
        if t is None:
            self.toast("No error packet captured yet (address / data NACK, early read NACK, incomplete transfer)")
            return
        self.visualize_packet(t)

    def last_arbitration(self):
        tr = self.engine.masters
        for ev in reversed(list(tr.arb_events)):
            if ev.winner is None:
                tr._resolve_winner(ev)
            if ev.winner is not None:
                return ev
        return None

    def visualize_last_arbitration(self):
        ev = self.last_arbitration()
        if ev is None:
            self.toast("No arbitration loss with a matched winning packet yet (Trigger arbitration, with both masters "
                       "connected and aligned)")
            return
        self.visualize_packet(ev.winner)

    def select_transaction(self, txn):
        self.playback.select(txn)
        self.p_pk.show_detail(txn)
        self.p_wave.show_packet(txn, synchronize=False)
        self._sync_playback()

    def _sync_playback(self):
        self.playback.held = self.stage_hold          # the banners show a held phase's byte complete
        shown = self.isVisible()
        for net in (self.p_dash.net, self.p_net.net, self.p_dev.net):
            if shown and not net.isVisible():
                continue          # a diagram on another tab: updated within one refresh of showing it (HUD text, devices,
                #                   stage and dots cost ~6 ms each, three times per refresh, on the GUI thread)
            net.set_devices(self.engine.registry.node_list(self.engine.hw_now or 0))
            net.show_stage(self.playback, self.engine)
        self.p_wave.show_stage(self.playback.current)
        pb = self.playback
        if hasattr(self, "p_pk"):
            self.p_pk.show_live(pb, self.net_run_state, held=self.stage_hold)   # same cursor as the 3D view
        if pb.txn is not None and not pb.finished:
            self.packet_model.set_live(pb.txn, pb.events, pb.stage_index)
        else:
            self.packet_model.set_live(None)
        if hasattr(self, "p_net"):
            self.p_net.update_stage_bar(self.playback, self.stage_hold)
        t = self.playback.txn.t_start if self.playback.txn is not None else None
        for page in (self.p_dash, self.p_perf):
            if hasattr(page, "mark_selected"):
                page.mark_selected(t, self.engine)

    def show_waveform(self, row):
        txn = self.packet_model.txn(row)
        self.nav.setCurrentRow(3)
        self.select_transaction(txn)

    def jump_to_packet(self, txn, view="packets"):
        """Open txn from another page (Error & Diagnostics): the Packet Analyzer with its row selected, or the Digital
        Waveform.  The shared selection (details, waveform, 3D playback, graph markers) follows either way."""
        if view == "waveform":
            self.nav.setCurrentRow(NAV_INDEX["waveform"])
            self.select_transaction(txn)
            return True
        self.nav.setCurrentRow(NAV_INDEX["packets"])
        shown = self.p_pk.select_packet(txn)           # selecting the row drives select_transaction itself
        if not shown:
            self.select_transaction(txn)
            self.toast(f"Packet #{txn.id} is hidden by the Packet Analyzer filters — its details are shown")
        return shown

    # ================================================================== state / banner
    def _set_state(self):
        e = self.engine
        connected = self.worker is not None and self.worker.isRunning()
        demo_on = self.demo is not None and self.demo.isRunning()
        replay_on = self.replay_timer.isActive() or (e.source == "replay") or (
            e.source == "demo" and getattr(self, "replay_simulated", False))
        pres_on = self.presentation is not None
        self.btn_connect.setEnabled(not connected and not demo_on and not pres_on)
        self.btn_disconnect.setEnabled(connected)
        self.port.setEnabled(not connected)
        self.btn_start.setEnabled(connected and self.caps is not None and not self.capturing)
        self.btn_stop.setEnabled(((connected and self.capturing) or demo_on or pres_on or self.replay_timer.isActive())
                                 and self.net_run_state != "draining")
        self.btn_stop.setText("■ Stopping…" if self.net_run_state == "draining" else "■ Stop")
        self.btn_demo.setEnabled(not connected)
        self.btn_present.setEnabled(not connected)
        self.btn_nack.setEnabled(pres_on or self.guided is not None)
        any_master = False
        for mid in self.master_ports:
            on = self._master_running(mid)
            any_master |= on
            self.master_ports[mid].setEnabled(not on)
            self.master_toggles[mid].setText("Disconnect" if on else "Connect")
            self.master_toggles[mid].setEnabled(on or not demo_on)
        self._refresh_master_pills()
        self.scl_status.setToolTip(self.scl_status.text())
        missing_masters = [config.MASTERS[m]["label"] for m in self.master_ports if not self._master_running(m)]
        all_on = connected and not missing_masters and self.capturing      # stopped: the button resumes, never disconnects
        self.btn_connect_all.setText("Disconnect all" if all_on else "Connect all")
        self.btn_connect_all.setEnabled(not demo_on and not pres_on)
        self.btn_scl_apply.setEnabled(any_master or demo_on)
        live_hw = e.source == "live" and not demo_on and self.presentation is None
        for b in (self.btn_seq, self.seq_gap, self.probe_master, self.probe_addr, self.btn_probe):
            b.setEnabled(live_hw and any_master)
        self.btn_arb.setEnabled(live_hw and len(self._review_masters()) == 2)
        self.btn_scl_apply.setText("Apply (SIMULATED)" if demo_on else "Apply")
        if pres_on:
            self.pill.set_state("PRESENTATION · scripted · SIMULATED", C["demo"], True)
        elif self.guided is not None and self.capturing:
            self.pill.set_state("GUIDED · REAL HARDWARE", C["green"], True)
        elif demo_on:
            self.pill.set_state("DEMO · simulated stream", C["demo"], True)
        elif e.source == "demo" and replay_on:
            self.pill.set_state("Recorded DEMO capture · SIMULATED", C["demo"], self.replay_timer.isActive())
        elif self.capturing:
            self.pill.set_state(f"Capturing · {self.worker.port}", C["green"], True)
        elif connected:
            self.pill.set_state(f"Connected · {self.worker.port}", C["cyan"], False)
        elif replay_on:
            self.pill.set_state("Recorded capture", C["replay"], self.replay_timer.isActive())
        else:
            self.pill.set_state("Disconnected", C["gray"], False)
        # banner
        if e.source == "demo":
            self.banner.setObjectName("bannerDemo")
            self.banner.setText("DEMO · SIMULATED — NOT VERIFIED HARDWARE" + (
                "  ·  recorded demo capture (replay)" if getattr(self, "replay_simulated", False) else ""))
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
        elif self.capturing and e.source == "live" and missing_masters:
            self.banner.setObjectName("bannerWarn")
            self.banner.setText(f"{' and '.join(missing_masters)} not connected: packets that master drives are shown "
                                "as unattributed. Press Connect all (it opens the masters before the analyzer).")
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
        capline = ("<br>caps: " + ", ".join(sorted(caps.caps))) if caps else ""
        self.side_mode.setText(mode + capline)
        self.side_mode.setToolTip(("Firmware caps: " + ", ".join(sorted(caps.caps))) if caps else "")
        if caps:
            self.hwinfo.setText(f"  {caps.firmware} · {caps.board} · {caps.sample_hz / 1e6:g} MS/s · protocol v{caps.proto}")
        elif connected:
            self.hwinfo.setText("  waiting for analyzer handshake…")
        else:
            self.hwinfo.setText("")

        self.p_wave.refresh(e)  # invalidate old plots even when display is paused

    def _refresh_master_pills(self):
        """Red 'NOT CONNECTED' / green 'connected' badge per master, so a missing port is obvious."""
        demo_on = self.demo is not None and self.demo.isRunning()
        for mid, lb in self.master_states.items():
            m = config.MASTERS[mid]
            w = self.master_workers.get(mid)
            on = w is not None and w.isRunning()
            if demo_on and self.engine.masters.state[mid].simulated:
                text = "● SIMULATED · demo bench"
                style = f"color:{C['demo']}; background:{tint(C['demo'], 0.12)}; border:1px solid {C['demo']};"
                tip = (f"{m['label']} ({m['pico']}, {m['board']}): SIMULATED controller telemetry from the demo bench "
                       "(I2C.c schedule, measured 2026-11-15 timing) — SIMULATED, NOT VERIFIED HARDWARE")
            elif on:
                hello = self.engine.masters.state[mid].hello or {}
                fw = f" · fw {hello['fw_version']}" if hello.get("fw_version") else ""
                text = f"● {w.port}{fw}"
                style = f"color:{C['green']}; background:{tint(C['green'], 0.10)}; border:1px solid {C['green']};"
                tip = f"{m['label']} ({m['pico']}, {m['board']}) connected on {w.port}{fw}"
            else:
                text = "● NOT CONNECTED"
                style = f"color:{C['red']}; background:{tint(C['red'], 0.12)}; border:1px solid {C['red']};"
                tip = (f"{m['label']} ({m['pico']}, {m['board']}) is not connected: its packets cannot be "
                       "attributed. Pick its COM port and press Connect, or use Connect all.")
            if lb.text() != text:
                lb.setText(text)
            style += " border-radius:9px; padding:2px 8px; font-weight:600;"
            if lb.styleSheet() != style:            # re-applying an unchanged style sheet re-polishes (flicker)
                lb.setStyleSheet(style)
            if lb.toolTip() != tip:
                lb.setToolTip(tip)
        self._refresh_port_strip()

    def _new_session(self):
        self.session_id = _dt.datetime.now().strftime("S%Y%m%d-%H%M%S")
        self.lbl_session.setText(f"Session {self.session_id}")
        self.capture_t0 = time.monotonic()
        self.engine.capture_wall_start = _dt.datetime.now()

    # ================================================================== serial
    def refresh_ports(self):
        cur = self.port.currentData()
        master_cur = {mid: c.currentData() for mid, c in self.master_ports.items()}
        self.port.clear()
        for c in self.master_ports.values():
            c.clear()
        ports = list_ports()
        for mid, c in self.master_ports.items():
            c.addItem(f"{config.MASTERS[mid]['label']}: not connected", None)
        for dev, desc, is_pico in ports:
            text = ("★ " if is_pico else "") + f"{dev} — {desc}"
            self.port.addItem(text, dev)
            for c in self.master_ports.values():
                c.addItem(text, dev)
        if not ports:
            self.port.addItem("No serial ports found", None)
        # keep the previous choice; else pre-select by the board's USB serial number (stable when Windows
        # renumbers COM ports after a re-flash), else this bench's usual port.  Never auto-connects.
        by_serial = {sn: dev for dev, sn in port_serials().items()}

        def pick(combo, current, serial_no, hint):
            for cand in (current, by_serial.get(serial_no), hint):
                i = combo.findData(cand) if cand else -1
                if i >= 0:
                    combo.setCurrentIndex(i)
                    return
        pick(self.port, cur, config.ANALYZER_SERIAL, config.ANALYZER_PORT_HINT)
        for mid, c in self.master_ports.items():
            pick(c, master_cur[mid], config.MASTERS[mid].get("serial"), config.MASTERS[mid]["port_hint"])
        self.toast(f"{len(ports)} serial port(s) found" + (" · ★ = Raspberry Pi board" if any(p[2] for p in ports) else ""))

    def connect_analyzer(self):
        dev = self.port.currentData()
        if not dev:
            self.toast("Select the analyzer's serial port first")
            return
        self._stop_sources()
        self.caps = None
        if any(w is not None and w.isRunning() and w.port == dev for w in self.master_workers.values()):
            self.toast("Analyzer and masters must use different COM ports")
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

    def connect_master(self, mid=1):
        name = config.MASTERS[mid]["label"]
        dev = self.master_ports[mid].currentData()
        if not dev:
            self.toast(f"Select {name}'s COM port, or leave it disconnected")
            return
        busy = [self.worker] + [w for k, w in self.master_workers.items() if k != mid]
        if any(w is not None and w.isRunning() and w.port == dev for w in busy):
            self.toast(f"{dev} is already open for another board: each Pico has its own COM port")
            return
        w = SerialWorker(dev, self, telemetry=True)
        self.master_workers[mid] = w
        w.connected.connect(lambda port, k=mid: (self.engine.masters.set_connection(k, port, True),
                                                 self._set_state()))
        w.lines_ready.connect(lambda lines, k=mid: self._on_master_lines(lines, k))
        # identify + enable the per-transfer log used for attribution (+ query SCL), RE-SENDING each until the
        # controller confirms it: the first bytes after DTR can be dropped without any error
        w.handshake_failed.connect(lambda missing, k=mid: self._on_master_handshake_failed(k, missing))
        w.require_handshake()
        w.failed.connect(lambda msg, k=mid: self._on_master_failed(msg, k))
        w.disconnected.connect(lambda reason, k=mid: self._on_master_disconnected(reason, k))
        w.start()
        self._set_state()

    def _on_master_handshake_failed(self, mid, missing):
        name = config.MASTERS[mid]["label"]
        self.engine.log("warning", "Multi-master", "Controller did not answer",
                        f"{name} ({self.master_workers[mid].port if self.master_workers.get(mid) else '?'}) never "
                        f"confirmed {', '.join(missing)} after 10 s: wrong COM port (analyzer?), firmware older than "
                        "I2C.c 2.0, or the Pico is not running the master firmware. Its packets cannot be attributed "
                        "by telemetry.")
        self.toast(f"{name}: no reply to {', '.join(missing)} — check the COM port / firmware", 8000)

    def _on_master_failed(self, message, mid=1):
        self.master_workers[mid] = None
        self.engine.masters.set_connection(mid, None, False)
        QMessageBox.warning(self, f"{config.MASTERS[mid]['label']} connection failed", message)
        self._set_state()

    def _on_master_disconnected(self, reason, mid=1):
        self.master_workers[mid] = None
        self.engine.masters.set_connection(mid, None, False)
        self.toast(f"{config.MASTERS[mid]['label']}: {reason}")
        self._set_state()

    def toggle_master(self, mid):
        if self._master_running(mid):
            self.disconnect_master(mid)
        else:
            self.connect_master(mid)

    def connect_all(self):
        """One click: masters first (so every packet can be attributed), then the analyzer, then the capture."""
        if self.btn_connect_all.text() == "Disconnect all":
            return self.disconnect_all()
        missing = [n for n, c in [("Analyzer", self.port)] + [(config.MASTERS[m]["label"], c)
                                                               for m, c in self.master_ports.items()]
                   if not c.currentData()]
        if missing:
            self.toast("Select a COM port for: " + ", ".join(missing))
            return
        # Resume after Stop: whatever was queued / in flight before Stop is flushed now; the network restarts from the
        # live data that arrives AFTER this click (start_capture() also clears the capture and the decoder).
        self._resume_network("Connect all")
        for mid in self.master_ports:
            if not self._master_running(mid):
                self.connect_master(mid)
        if not (self.worker and self.worker.isRunning()):
            self.connect_analyzer()
        elif self.caps is not None and not self.capturing and all(self._master_running(m) for m in self.master_ports):
            self.start_capture()                     # everything is still connected (Stop only paused the capture)
            self.toast("Resumed: capturing fresh live data with both masters attributed")
            return
        self._all_deadline = time.monotonic() + 10
        if not hasattr(self, "_all_timer"):
            self._all_timer = QTimer(self)
            self._all_timer.timeout.connect(self._connect_all_tick)
        self._all_timer.start(200)
        self.toast("Connecting masters and analyzer…")

    def _connect_all_tick(self):
        masters_on = all(self._master_running(m) for m in self.master_ports)
        an_on = self.worker is not None and self.worker.isRunning()
        if masters_on and an_on and self.caps is not None:
            self._all_timer.stop()
            if not self.capturing:
                self.start_capture()
            self.toast("All connected: capturing with both masters attributed")
        elif time.monotonic() > self._all_deadline:
            self._all_timer.stop()
            bad = [config.MASTERS[m]["label"] for m in self.master_ports if not self._master_running(m)]
            if not an_on:
                bad.insert(0, "Analyzer")
            elif self.caps is None:
                bad.insert(0, "Analyzer (no handshake)")
            self.toast("Connect all incomplete: " + (", ".join(bad) or "unknown") + " did not connect")

    def disconnect_all(self):
        if hasattr(self, "_all_timer"):
            self._all_timer.stop()
        if self.capturing:
            self.stop_capture()
        self.disconnect_analyzer()
        for mid in list(self.master_ports):
            self.disconnect_master(mid)

    def disconnect_master(self, mid=1):
        w = self.master_workers.get(mid)
        if w:
            w.send_controller("TXLOG 0")
            w.stop()
            w.wait(1500)

    def apply_scl(self):
        """Send the selected rate to the controller (or the SIMULATED demo controller)."""
        hz = self.scl_choice.currentData()
        if self.demo is not None and self.demo.isRunning():
            self.demo.request_scl(hz)
            self.engine.record_scl_setting(hz, hz, "applied", "SIMULATED demo controller",
                                           "simulated edge timing only")
            self.toast(f"SIMULATED: demo controller SCL set to {hz / 1e3:g} kHz")
        elif any(self._master_running(k) for k in self.scl_target.currentData()):
            sent = []
            for k in self.scl_target.currentData():
                if self._master_running(k):
                    self.master_workers[k].send_controller(f"SCL {hz}")
                    sent.append(f"{config.MASTERS[k]['label']} ({self.master_workers[k].port})")
            missing = [config.MASTERS[k]["label"] for k in self.scl_target.currentData() if not self._master_running(k)]
            self.scl_status.setText(f"Sent 'SCL {hz}' to {', '.join(sent)} — awaiting reply; bus change is confirmed "
                                    "only by analyzer measurements" + (f" · NOT sent to {', '.join(missing)} "
                                                                       "(port not connected)" if missing else ""))
            return
        else:
            self.toast("Connect the selected master's COM port first, or start Demo mode")
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
        per = getattr(e, "scl_settings", {})
        both = (" · per master: " + ", ".join(f"{config.MASTERS[k]['label']} {v['applied_hz'] / 1e3:.3f} kHz"
                                              for k, v in sorted(per.items()) if v.get("applied_hz"))) if per else ""
        if len({v.get("applied_hz") for v in per.values()}) > 1:
            both += " — DIFFERENT rates: during arbitration the bus runs at the slower clock"
        self.scl_status.setText(f"{sim}Controller SCL setting ({st['source']}): {st['result']} · requested "
                                f"{st['requested_hz'] / 1e3:g} kHz · reported {st['applied_hz'] / 1e3:.3f} kHz ("
                                f"{'simulated setting' if st['source'].startswith('SIMULATED') else 'divider'}, "
                                f"not a measurement) · {bus} · {measured}{both}")

    def _on_master_lines(self, lines, slot=1):
        """Controller USB lines (one port per master); NEVER used to generate I2C packets.

        JSON records go through the engine's multi-master tracker; plain text from older
        single-master firmware still yields RTC/BMP telemetry."""
        legacy = getattr(self, "_legacy_sensor", {})
        self._legacy_sensor = legacy
        for d, text in self.engine.ingest_master_lines(slot, lines):
            if d is not None:
                w = self.master_workers.get(slot)
                resent = self.__dict__.setdefault("_txlog_resent", {})
                if (d.get("type") == "master_stats" and d.get("txlog") is False and w is not None and w.isRunning()
                        and time.monotonic() - resent.get(slot, 0) > 2.0):
                    resent[slot] = time.monotonic()
                    w.send_controller("TXLOG 1")          # e.g. the controller rebooted: logging is off again
                if d.get("type") == "scl_config":
                    self._update_scl_status()
                elif d.get("type") == "hello":
                    self._refresh_master_pills()
                    self._request_calibration()           # a master connected during the capture
                continue
            if not text:
                continue
            # Also accept the *existing* master firmware's printed RTC and BMP lines.
            match = re.search(r"RTC\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{2})/(\d{2})/(\d{4})", text)
            if match:
                h, m, sec, day, month, year = map(int, match.groups())
                legacy.setdefault(slot, {})["rtc"] = f"{year:04d}-{month:02d}-{day:02d}T{h:02d}:{m:02d}:{sec:02d}"
            match = re.search(r"BMP280:?\s+([+-]?\d+(?:\.\d+)?)\s+C,?\s+([+-]?\d+(?:\.\d+)?)\s+hPa", text)
            if match:
                legacy.setdefault(slot, {})["bmp"] = (float(match.group(1)), float(match.group(2)))
            got = legacy.get(slot, {})
            if got.get("rtc") and got.get("bmp"):
                self.engine.update_master_telemetry(got["rtc"], *got["bmp"], master_id=slot)
                got["bmp"] = None

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
        self.replay_simulated = False
        self._resume_network("Start capture")
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
        if self.guided is None and self._review_masters():
            # a master left in CONDUCT mode (a closed / crashed guided flow) starts nothing by itself: resume its turns
            self._review_send(self._review_masters(), "CONDUCT OFF")
        self._request_calibration(force=True)        # BMP280 calibration on the bus: the Sensor page decodes it
        self.engine.log("info", "Session", "Capture started", f"session {self.session_id} on {self.worker.port}")
        self._set_state()

    def stop_capture(self):
        if self.guided is not None:
            self.btn_guided.setChecked(False)        # toggle_guided(False): CONDUCT OFF, the masters resume their turns
        self.engine.capture_active = False
        if self.presentation is not None:
            self.btn_present.setChecked(False)       # toggle_presentation(False) -> _begin_network_stop()
            self._begin_network_stop()
            return
        if self.demo:
            self.btn_demo.setChecked(False)          # toggle_demo(False) -> _begin_network_stop()
            self._begin_network_stop()
            return
        if self.replay_timer.isActive():
            self.stop_replay()
            self._begin_network_stop()
            return
        if self.worker and self.capturing:
            self.worker.send("stop")
            self.engine.log("info", "Session", "Capture stopped", f"session {self.session_id}")
        self.capturing = False
        self._begin_network_stop()
        self._set_state()

    # ------------------------------------------------------------------ 3D network stop / resume
    def _nets(self):
        return (self.p_dash.net, self.p_net.net, self.p_dev.net)

    def _dots_in_flight(self):
        """Dots still travelling on a diagram that is actually animating (a hidden / minimised one drops its dots)."""
        return any(not q.done for n in self._nets() if n._animating() and not n.playback_paused for q in n.particles)

    def _begin_network_stop(self):
        """Stop pressed: no new or queued packet may start.  The packet on display keeps playing until its last stage
        (ADDR, DATA, ACK / NACK, STOP) has been shown AND every dot has reached its receiver; then the network freezes."""
        if self.net_run_state != "running":
            return
        self._follow_queue.clear()                   # queued-but-unplayed packets never start
        self._rt_queue.clear()
        self._follow_latest_txn = None
        self._follow_wait_since = None
        if self.stage_hold:
            self.set_stage_hold(False)               # a frozen stage would never finish
        pb = self.playback
        busy = (pb.txn is not None and not pb.finished) or self._dots_in_flight()
        if self.display_paused or not busy:
            self._finish_network_stop()
            return
        self.net_run_state = "draining"
        self._drain_deadline = time.monotonic() + pb.total_seconds() + 3.0      # safety net: never hang in 'Stopping…'
        for n in self._nets():
            n.set_run_state("draining", getattr(pb.txn, "id", None))
        self.engine.log("info", "Session", "Stopping 3D network",
                        f"finishing packet #{getattr(pb.txn, 'id', '?')} (no new packets start)")
        self.toast("Stopping: finishing the current transaction in the 3D network…")
        self._set_state()
        self._sync_playback()                        # the HUD says STOPPING / STOPPED at once

    def _finish_network_stop(self):
        self.net_run_state = "stopped"
        self._drain_deadline = None
        self._follow_queue.clear()
        self._rt_queue.clear()
        for n in self._nets():
            n.set_run_state("stopped", getattr(self.playback.txn, "id", None))
        self.engine.log("info", "Session", "3D network stopped",
                        "current transaction completed; press Connect all / Start capture to resume with fresh data")
        self.toast("3D network stopped — the last transaction completed")
        self._set_state()
        self._sync_playback()                        # the HUD says STOPPING / STOPPED at once

    def _check_network_drain(self):
        if self.net_run_state != "draining":
            return
        done = self.playback.finished and not self._dots_in_flight()
        if done or time.monotonic() > (self._drain_deadline or 0):
            self._finish_network_stop()

    def _resume_network(self, why=""):
        """Run again from fresh data: drop everything queued or shown before Stop (queue, latest, dots, old packet)."""
        was = self.net_run_state
        self.net_run_state = "running"
        self._drain_deadline = None
        self._follow_queue.clear()
        self._rt_queue.clear()
        self._follow_shown.clear()
        self._follow_latest_txn = None
        self._follow_wait_since = None
        self.engine.take_activity()                  # stale decoded activity from before Stop is discarded
        self.playback = Playback()
        self.playback.speed = self._chosen_speed()
        for n in self._nets():
            n.set_run_state("running")
            n._drop_particles()
        if was != "running":
            self.engine.log("info", "Session", "3D network resumed", f"{why}: fresh live data only")
        self._sync_playback()

    # ================================================================== demo
    def toggle_demo(self, on):
        if on:
            self._stop_sources()
            self.replay_simulated = False
            self._resume_network("Demo")
            self.engine.clear()
            self.packet_model.sync()
            self.engine.set_source("demo")
            self._new_session()
            self.demo = DemoSource(self, scl_hz=self.scl_choice.currentData())
            self.engine.record_scl_setting(self.demo.gen.scl_hz, self.demo.gen.scl_hz, "boot",
                                           "SIMULATED demo controller", "simulated edge timing only")
            # SIMULATED controller telemetry (two-controller bench): same multi-master attribution path as hardware
            self.demo.master_lines_ready.connect(self._on_demo_master_lines)
            self.demo.lines_ready.connect(self.engine.ingest_lines)
            self.demo.start()
            self.engine.log("warning", "Session", "Demo mode", "Simulated data — not live hardware")
        else:
            if self.demo:
                self._halt_demo()
                self.engine.log("info", "Session", "Demo stopped", "demo data remains on screen, still labelled DEMO")
                self._begin_network_stop()
        self._set_state()

    # ================================================================== presentation (scripted, SIMULATED)
    def toggle_presentation(self, on):
        from sources.presentation_source import PresentationBus
        if on:
            if self.worker is not None and self.worker.isRunning():
                self.btn_present.setChecked(False)
                self.toast("Disconnect the analyzer first: presentation mode is a SIMULATED sequence")
                return
            self._stop_sources()
            if self.btn_demo.isChecked():
                self.btn_demo.blockSignals(True)
                self.btn_demo.setChecked(False)
                self.btn_demo.blockSignals(False)
            if self._realtime():                     # the scripted review is explained phase by phase: slow pace
                self.visual_speed.setCurrentIndex(self.visual_speed.findData(self.DEFAULT_SPEED))
            self.replay_simulated = False
            self._resume_network("Presentation")
            self.engine.clear()
            self.packet_model.sync()
            self.engine.set_source("demo")           # SIMULATED in every view, table, CSV and report
            self.engine.sensors.simulated_calib = True   # scripted BMP280 reads: the simulated sensor's own calibration
            self._new_session()
            self.presentation = PresentationBus(scl_hz=self.scl_choice.currentData() or 100_000)
            self.presentation.seed_sensors(self.engine.sensors)
            self.engine.ingest_lines([self.presentation.hello()])
            for mid, rec in self.presentation.controller_start():   # the two SIMULATED controllers say hello
                self.engine.ingest_master_record(mid, rec)
            self.engine.take_activity()
            self.engine.log("warning", "Session", "Presentation mode",
                            "Scripted review sequence — SIMULATED, not live hardware. Master 1 then Master 2 (Pico 3), "
                            "each with RTC / BMP280 / OLED, WRITE then READ, one transfer at a time")
            self.set_ports_expanded(False)          # COM ports play no part here: the diagram gets the height
            self.toast("Presentation mode: one transfer at a time, phase by phase (SIMULATED)")
        elif self.presentation is not None:
            self.presentation = None
            self.engine.sensors.simulated_calib = False
            self.btn_nack.setChecked(False)
            self.engine.log("info", "Session", "Presentation stopped", "the transfer on display finishes first")
            self._begin_network_stop()
        self._set_state()

    def _release_presentation_transfer(self, select=True):
        """THE bus scheduler step: hand out exactly one scripted transfer (only called when the previous one has been
        shown completely, STOP included, and no dot is in flight).  Returns the decoded packet."""
        bus = self.presentation
        if bus is None:
            return None
        bus.inject_nack = self.btn_nack.isChecked()
        lines, meta = bus.next_transfer()
        if meta["nack"]:
            self.btn_nack.setChecked(False)          # one-shot
        bus.seed_sensors(self.engine.sensors, meta)   # full SIMULATED RTC / BMP280 registers behind the 2-byte reads
        n0 = len(self.engine.packets)
        self.engine.ingest_lines(lines)
        new = self.engine.packets[n0:]
        # what the master reports about this transfer (txn / arb_lost / master_stats): Device Activity's controller,
        # master x target and arbitration tables and the Multi-master events of Error & Diagnostics come from these
        for mid, rec in meta.get("controller", ()):
            self.engine.ingest_master_record(mid, rec)
        # AFTER every record: the scheduler released this transfer for exactly this master, so the packet always names
        # Master 1 (Pico 1) or Master 2 (Pico 3) - never 'Master ? (unattributed)' (a telemetry match to the same master
        # is kept; anything else is overridden)
        for t in new:
            self.engine.masters._attribute(t, meta["master"], "presentation",
                                           force=getattr(t, "master_id", None) != meta["master"])
            t.notes.extend(meta["notes"])
            t.presentation = meta
            if meta["op"] == "R" and not meta["nack"] and meta["addr"] in (0x68, 0x76):
                bus.decode_sensors(self.engine.sensors, t.t_start, t)
        self.engine.take_activity()                  # the scheduler, not the live follow queue, decides what plays
        self.packet_model.sync()
        if not new:
            return None
        txn = new[-1]
        if select:
            self.playback.select(txn, pinned=False, full_pace=True)
            self._follow_shown.append(txn)
            del self._follow_shown[:-12]
        return txn

    # ================================================================== replay
    def open_capture(self, path, speed=0):
        try:
            meta, lines = load_capture(path)
        except Exception as e:
            QMessageBox.warning(self, "Cannot open capture", str(e))
            return
        self._stop_sources()
        self._resume_network("Recording")
        self.btn_demo.blockSignals(True)
        self.btn_demo.setChecked(False)
        self.btn_demo.blockSignals(False)
        self.engine.clear()
        self.packet_model.sync()
        self.replay_simulated = bool(meta.get("simulated"))
        # A recording of the simulator stays SIMULATED in every view, table, CSV and report (not "recorded hardware").
        self.engine.set_source("demo" if self.replay_simulated else "replay")
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

    def _on_demo_master_lines(self, mid, lines):
        """SIMULATED controller records (every one carries "simulated": true); never real hardware telemetry."""
        if self.demo is None or self.engine.source != "demo":
            return
        for d, _ in self.engine.ingest_master_lines(mid, lines):
            if d is not None and d.get("type") == "hello":
                self._refresh_master_pills()

    def _halt_demo(self):
        # disconnect first so already-queued simulated batches can never reach a later capture
        for sig in (self.demo.lines_ready, self.demo.master_lines_ready):
            try:
                sig.disconnect()
            except TypeError:
                pass
        self.demo.stop()
        self.demo.wait(1000)
        self.demo = None

    def _stop_sources(self):
        if self.presentation is not None:
            self.presentation = None
            self.btn_present.blockSignals(True)
            self.btn_present.setChecked(False)
            self.btn_present.blockSignals(False)
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

    def export_report(self):
        """Save the session summary report (HTML or Markdown, chosen by the file type)."""
        if not (self.engine.packets or self.engine.raw_lines):
            self.toast("Nothing captured yet — start a capture, Demo mode or open a recording first")
            return
        demo = self.engine.source == "demo"
        sid = re.sub(r"[^\w.-]+", "_", self.session_id).strip("_") or _dt.datetime.now().strftime("S%Y%m%d-%H%M%S")
        name = f"report_{sid}{'_DEMO' if demo else ''}.html"
        p, selected = QFileDialog.getSaveFileName(self, "Export session report", os.path.join(self.captures_dir(), name),
                                                  "HTML Report (*.html);;Markdown Report (*.md)")
        if not p:
            return
        if not os.path.splitext(p)[1]:
            p += ".md" if "Markdown" in selected else ".html"
        try:
            fmt = export_session_report(p, self.engine, self.session_id if self.session_id != "—" else "")
        except OSError as exc:
            QMessageBox.warning(self, "Cannot write report", str(exc))
            return
        self.toast(f"Saved {'Markdown' if fmt == 'md' else 'HTML'} report{' (DEMO · SIMULATED)' if demo else ''}: {p}")

    # ================================================================== refresh loop
    def _pace_gc(self, now=None):
        """See GC_FREEZE_S: collect generations 0 + 1 and freeze the survivors, so a full collection never stalls the
        serial reader long enough to lose analyzer data."""
        now = time.monotonic() if now is None else now
        last = getattr(self, "_gc_frozen_at", None)
        if last is None:
            self._gc_frozen_at = now
        elif now - last >= self.GC_FREEZE_S:
            self._gc_frozen_at = now
            gc.collect(1)
            gc.freeze()

    def _refresh(self):
        act = self.engine.take_activity()
        if self.presentation is not None:
            for mid, rec in self.presentation.controller_heartbeat(time.monotonic()):
                self.engine.ingest_master_record(mid, rec)
        if self.net_run_state != "running" or self.presentation is not None or self.guided is not None:
            act = []        # Stop: nothing new is queued or followed; presentation / guided flow: the scheduler decides
        nets = (self.p_dash.net, self.p_net.net, self.p_dev.net)
        now_playback = time.monotonic()
        elapsed = now_playback - self._playback_wall
        self._playback_wall = now_playback
        self._pace_gc()
        if self._playback_context is not self.engine.decoder:
            self._playback_context = self.engine.decoder
            gc.unfreeze()            # a new capture / demo / replay: the previous session's objects are scanned again
            self.playback = Playback()
            self.playback.speed = self._chosen_speed()  # a new capture / demo / replay keeps the speed picked in the toolbar
            self.playback.realtime = self._realtime()
            self.stage_hold = False                    # a new capture starts playing again
            self._follow_latest_txn = None
            self._follow_queue.clear()
            self._rt_queue.clear()
            self._follow_shown.clear()
        # Track the newest packet even while the display is paused (pause = rendering only),
        # so Resume continues from current traffic instead of waiting for the next packet.
        focus = getattr(self, "device_filter", None)
        if focus is not None:
            act = [t for t in act if any(f.addr == focus for f in t.frames)]
        if act:
            self._follow_latest_txn = act[-1]
            if focus is None:
                self._follow_queue.offer(act)         # collected even while paused / held: bounded, one per bucket
                if self._realtime():
                    self._rt_queue.offer(act)         # capture order; skips (counted) rather than falling behind
        if focus is not None:
            self._follow_queue.clear()                # a device filter snaps to the newest matching packet instead
            self._rt_queue.clear()
        if not self.display_paused and not self.stage_hold:
            # ONE transfer on the bus at a time: the next one starts only when the previous one finished (STOP shown)
            # AND its last dot has arrived
            rt = self._realtime() and self.presentation is None and self.guided is None and focus is None
            # real time: the next packet starts as soon as this one is shown (its last dot may still be arriving)
            idle = (self.playback.txn is None or self.playback.finished) and (rt or not self._dots_in_flight())
            if idle and rt and now_playback - getattr(self, "_rt_selected_at", 0.0) < self.RT_MIN_ON_SCREEN_S:
                idle = False          # after a stall two refreshes can run back to back: never replace a packet unseen
            if idle and not self.playback.pinned and self.net_run_state == "running":
                if self.presentation is not None:
                    self._release_presentation_transfer()
                elif self.guided is not None:
                    self.guided.tick(now_playback)
                else:
                    nxt = self._follow_next(focus)
                    if nxt is not None:
                        self.playback.select(nxt, pinned=False, packet_s=self._rt_queue.budget_s(
                            nxt, self.bus_now_ns(), self.timer.interval() / 1000.0) if rt else None)
                        self._rt_selected_at = now_playback
                        self._follow_shown.append(nxt)
                        del self._follow_shown[:-12]
            # Never jump over a stage after a GUI stall: each stage is shown (and its dot spawned) at least once.
            pos0 = self.playback.position
            # a GUI stall is never made up by shortening the next phase (that read as a fast-forward): at most
            # MAX_CATCHUP_S of wall time is played per refresh, the rest of a stall makes playback lag instead
            self.playback.advance(min(elapsed, self.MAX_CATCHUP_S, self.playback.max_step_seconds()))
            if (int(self.playback.position) > int(pos0) and self._dots_in_flight()
                    and self.playback.rt_packet_s is None):
                # no phase overlap: the next phase waits until the current phase's dot has reached its receiver
                self.playback.position = int(pos0) + 0.999
        elif (self.stage_hold and self.guided is not None and self.guided.state == "wait"
              and self.net_run_state == "running"):
            self.guided.tick(now_playback)            # Step ▸ sent a command: pick its packet up (opens held at phase 1)
        self._sync_playback()
        self._check_network_drain()
        new_arb, self._arb_seen = self.engine.masters.new_arb_events_since(getattr(self, "_arb_seen", 0))
        if new_arb and not self.display_paused and self.net_run_state == "running":
            for net in nets:
                net.show_contention(new_arb)
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
        if self.guided is not None:
            self.guided.stop()                       # CONDUCT OFF: never leave the masters waiting for commands
            self.guided = None
        self._stop_sources()
        if self.worker:
            if self.capturing:
                self.worker.send("stop")
            self.worker.stop()
            self.worker.wait(1500)
        for w in self.master_workers.values():
            if w:
                w.stop()
                w.wait(1500)
        e.accept()
