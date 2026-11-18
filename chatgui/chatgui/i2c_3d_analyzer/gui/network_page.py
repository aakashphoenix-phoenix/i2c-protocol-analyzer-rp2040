"""3D I²C Network page — large interactive topology with node inspector."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                             QVBoxLayout, QWidget)

from analysis.playback import stage_chips
from config import COLORS as C
from gui.network_3d import Network3D
from gui.theme import MONO_CSS, on_change, set_mono
from gui.widgets import Card, ElidedLabel, fmt_time_ns, label

CHIP_COL = {"cond": C["amber"], "addr": C["blue"], "data": C["cyan"], "ack": C["green"]}
on_change(lambda: CHIP_COL.update(cond=C["amber"], addr=C["blue"], data=C["cyan"], ack=C["green"]))


def chip_color(chip):
    return C["red"] if chip.kind == "ack" and chip.ack is False else CHIP_COL.get(chip.kind, C["text2"])


def chip_css(chip, state):
    """Style of one stage chip: 'active' filled + bold, 'done' dimmed, 'todo' outlined in its colour."""
    col = chip_color(chip)
    if state == "active":
        return (f"QPushButton#stageChip {{ background:{col}; border:1px solid {col}; color:{C['card']}; font-weight:700; }}")
    if state == "done":
        return (f"QPushButton#stageChip {{ background:{C['input_bg']}; border:1px solid {C['border']}; color:{C['text2']}; }}")
    return (f"QPushButton#stageChip {{ background:{C['input_bg']}; border:1px solid {col}; color:{col}; }}")


class StageBar(QFrame):
    """The active packet's stages as clickable chips (START -> 0x76 W -> ACK -> ... -> STOP) with a local
    Pause / Play and stage-step buttons.  Pure view: it emits requests, MainWindow owns the playback cursor."""

    stage_clicked = pyqtSignal(int)         # index into the packet's stages
    step_requested = pyqtSignal(int)        # -1 / +1
    hold_toggled = pyqtSignal(bool)         # True = freeze on the current stage

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stageBar")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label("STAGES", "metricName"))
        self.btn_hold = QPushButton("⏸ Pause")
        self.btn_hold.setCheckable(True)
        self.btn_hold.setToolTip("Freeze this packet on its current stage and step through it byte by byte. Live traffic "
                                 "keeps being captured and listed; Play resumes. (Independent of 'Pause display'.)")
        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        self.btn_prev.setToolTip("Previous stage (freezes playback)")
        self.btn_next.setToolTip("Next stage (freezes playback)")
        for b in (self.btn_hold, self.btn_prev, self.btn_next):
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            top.addWidget(b)
        self.btn_prev.setMinimumWidth(40)
        self.btn_next.setMinimumWidth(40)
        self.info = ElidedLabel("", "cardHint")
        top.addWidget(self.info, 1)
        v.addLayout(top)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setFixedHeight(44)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.viewport().setStyleSheet("background: transparent;")
        self.strip = QWidget()
        self.strip.setObjectName("stageStrip")
        self.strip.setStyleSheet("QWidget#stageStrip { background: transparent; }")
        self.row = QHBoxLayout(self.strip)
        self.row.setContentsMargins(2, 4, 2, 4)
        self.row.setSpacing(4)
        self.row.addStretch(1)
        self.scroll.setWidget(self.strip)
        v.addWidget(self.scroll)
        self.chips = []                     # [(QPushButton, Chip, state)]
        self.seps = []
        self._key = None
        self._active = None
        self.btn_hold.toggled.connect(self.hold_toggled.emit)
        self.btn_prev.clicked.connect(lambda: self.step_requested.emit(-1))
        self.btn_next.clicked.connect(lambda: self.step_requested.emit(1))
        self.set_hold(False)
        self._set_enabled(False)

    # ------------------------------------------------------------------ state
    def set_hold(self, on):
        """Mirror the playback hold state on the button (no signal)."""
        self.btn_hold.blockSignals(True)
        self.btn_hold.setChecked(on)
        self.btn_hold.blockSignals(False)
        self.btn_hold.setText("▶ Play" if on else "⏸ Pause")

    def _set_enabled(self, on, active=0, n=0):
        self.btn_hold.setEnabled(on)
        self.btn_prev.setEnabled(on and active > 0)
        self.btn_next.setEnabled(on and active < n - 1)

    def _clear(self):
        for w in [c[0] for c in self.chips] + self.seps:        # leave the layout NOW (deleteLater alone keeps them in it
            self.row.removeWidget(w)                            # until the next event-loop turn: transient 20 000 px strip)
            w.hide()
            w.deleteLater()
        self.chips, self.seps = [], []

    def _build(self, events):
        self._clear()
        for k, ch in enumerate(stage_chips(events)):
            if k:
                arrow = QLabel("→")
                arrow.setObjectName("cardHint")
                self.row.insertWidget(self.row.count() - 1, arrow)
                self.seps.append(arrow)
            b = QPushButton(ch.label)
            b.setObjectName("stageChip")
            b.setToolTip(ch.tip)
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            set_mono(b)
            b.clicked.connect(lambda _=False, i=ch.indices[0]: self.stage_clicked.emit(i))
            self.row.insertWidget(self.row.count() - 1, b)
            self.chips.append([b, ch, None])

    def update_from(self, playback, hold=False):
        """Rebuild the chips when the packet changed, otherwise only restyle what changed (called every refresh)."""
        evs = playback.events
        key = (id(playback.txn), len(evs)) if (playback.txn is not None and evs) else None
        rebuilt = key != self._key
        if rebuilt:
            self._key = key
            self._active = None
            self._build(evs if key else [])
        if key is None:
            self.info.setText("No packet selected — its stages appear here as soon as a packet is playing or picked")
            self._set_enabled(False)
            self.set_hold(hold)
            return
        active = playback.stage_index
        chip_active = next((n for n, (_, ch, _) in enumerate(self.chips) if active in ch.indices), 0)
        if rebuilt or chip_active != self._active:
            self._active = chip_active
            for n, c in enumerate(self.chips):
                state = "active" if n == chip_active else ("done" if n < chip_active else "todo")
                if c[2] != state:
                    c[2] = state
                    c[0].setStyleSheet(chip_css(c[1], state))
            self.scroll.ensureWidgetVisible(self.chips[chip_active][0], 60, 0)
        txn = playback.txn
        st = playback.current
        text = (f"Packet #{txn.id} · {txn.operation} · stage {active + 1}/{len(evs)}: {st.text}"
                + ("   ·   HELD (capture continues)" if hold else ""))
        if self.info.text() != text:
            self.info.setText(text)
        self._set_enabled(True, active, len(evs))
        if self.btn_hold.isChecked() != hold:
            self.set_hold(hold)


class NetworkPage(QWidget):
    SPLIT_SIZES = [1100, 340]           # startup diagram / node-inspector split (Reset view restores it)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.selected = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(10)
        main = Card("3D I²C network", "drag · Ctrl+scroll · click a node", shadow=False)
        main.title.setToolTip("Built from observed traffic only")
        main.hint.setToolTip("Drag to rotate · Ctrl + mouse wheel to zoom · click a node to inspect it")
        self.net = Network3D(prefer_3d=ctx.settings.value("use_3d", True, bool))
        self.net.node_clicked.connect(lambda k: ctx.focus_device(k) if isinstance(k, int) else self._select(k))
        main.add(self.net, 1)
        # clickable stage progress bar under the diagram (local Pause / Play + stage stepping)
        main.lay.setSpacing(6)                                   # tighter card: the diagram keeps its 272 px, the stage bar its place
        self.stage_bar = StageBar()
        self.stage_bar.stage_clicked.connect(ctx.stage_goto)
        self.stage_bar.step_requested.connect(ctx.stage_step)
        self.stage_bar.hold_toggled.connect(ctx.set_stage_hold)
        main.add(self.stage_bar)
        # view controls live in the card header (one row less: the stage bar must stay on screen at 1280x720)
        ctl = main.header
        # Reset view sits on the LEFT edge of the header (before the title), always easy to click
        self.btn_reset = QPushButton("⟲ Reset view")
        self.btn_reset.setToolTip("Restore the startup view: camera zoom / rotation / pan, label positions, auto-rotate "
                                  "off and the diagram / inspector split. (Zoom = Ctrl + mouse wheel.)")
        self.btn_reset.clicked.connect(self._reset)
        ctl.insertWidget(0, self.btn_reset)
        self.rot = QCheckBox("Auto-rotate")
        self.rot.setChecked(ctx.settings.value("auto_rotate", False, bool))
        self.rot.toggled.connect(self.net.set_auto_rotate)
        self.net.set_auto_rotate(self.rot.isChecked())
        ctl.addWidget(self.rot)
        self.renderer_lbl = ElidedLabel("", "cardHint")          # full text in the tooltip when the header is narrow
        self.renderer_lbl.setMinimumWidth(0)
        self._update_renderer_label()
        self.net.renderer_changed.connect(self._update_renderer_label)
        ctl.addWidget(self.renderer_lbl)
        split.addWidget(main)

        side = Card("Node inspector")
        self.info = label("Click a node in the scene.", wrap=True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        side.add(self.info, 1)
        self.btn_filter = QPushButton("Show this device's packets")
        self.btn_filter.setObjectName("primary")
        self.btn_filter.setEnabled(False)
        self.btn_filter.clicked.connect(lambda: ctx.focus_device(self.selected, switch=True))
        side.add(self.btn_filter)
        note = label("Names: <b>Confirmed</b> = ID register matched on the bus · <b>Configured</b> = what this bench is "
                     "wired as · <b>Likely</b> = common part at that address · <b>Unknown</b> = not in database. "
                     "An address alone cannot prove which chip is present.", "cardHint", wrap=True)
        side.add(note)
        side.setMinimumWidth(300)
        split.addWidget(side)
        split.setSizes(self.SPLIT_SIZES)
        self.split = split
        lay.addWidget(split, 1)

    def update_stage_bar(self, playback, hold=False):
        self.stage_bar.update_from(playback, hold)

    def _update_renderer_label(self):
        if self.net.is_3d:
            mode, why = "OpenGL 3D", "OpenGL 3D view"
        elif self.net._gl_fallback_done:
            mode, why = "2D (runtime fallback)", "2D view: fell back from 3D at runtime"
        else:
            mode, why = "2D fallback", "2D view: OpenGL unavailable or disabled"
        self.renderer_lbl.setText(f"Renderer: {mode}")
        self.renderer_lbl.setToolTip(f"Renderer: {why}")

    def _reset(self):
        """Back to the exact startup state of THIS (large) diagram; the dashboard box resets independently: auto-rotate
        off, the startup splitter split, then the startup camera / label layout once the view has its startup size."""
        self.rot.setChecked(False)
        self.split.setSizes(self.SPLIT_SIZES)
        self.net.reset_view()
        QTimer.singleShot(0, self.net.reset_view)          # again after the splitter's resize reached the 3D view

    def _select(self, key):
        self.selected = key
        self.net.set_selected(key)
        self.btn_filter.setEnabled(isinstance(key, int))
        self.refresh(self.ctx.engine)

    def refresh(self, engine):
        reg = engine.registry
        now = engine.hw_now or 0
        self.net.set_devices(reg.node_list(now))
        k = self.selected
        if isinstance(k, int) and k in reg.devices:
            d = reg.devices[k]
            h = engine.device_health(k)      # same per-address classification as the health policy
            name, conf = reg.name(k)
            t0 = engine.packets[0].t_start if engine.packets else 0
            rows = [("Address", f"0x{k:02X} ({'10-bit' if d.ten_bit else '7-bit'})"), ("Name", f"{name}"),
                    ("Confidence", conf + (f" — {d.confirm_detail}" if d.confirm_detail else "")),
                    ("Status", reg.status(k, now)), ("Transactions", f"{d.transactions:,}"),
                    ("Reads / writes", f"{d.reads:,} / {d.writes:,}"),
                    ("Bytes read / written", f"{d.bytes_read:,} / {d.bytes_written:,}"),
                    ("ACK / NACK (raw)", f"{d.acks:,} / {d.nacks:,}"),
                    ("NACK: address (target silent)", str(d.addr_nacks)),
                    ("NACK: end-of-read (sent by controller)", str(h['expected_read_nacks'])),
                    ("NACK: other unexpected", str(max(0, h['unexpected_nacks'] - d.addr_nacks))),
                    ("NACK: read end, STOP not captured", str(h['unverified_nacks'])),
                    ("Rate (5 s)", f"{reg.rate(k, now):.2f} txn/s"),
                    ("First seen", fmt_time_ns(d.first_seen - t0) if d.first_seen is not None else "—"),
                    ("Last seen", fmt_time_ns(d.last_seen - t0) if d.last_seen is not None else "—")]
            # multi-master: who talked to this target (bus packets attributed; arbitration from telemetry)
            mx = [r for r in engine.masters.matrix([k]) if r["bus_packets"] or r["arb_lost"] or r["ok"]]
            if mx:
                rows.append(("Bus packets by master", " · ".join(f"{r['master']} {r['bus_packets']:,}" for r in mx)))
                arb = [r for r in mx if r["arb_lost"]]
                rows.append(("Arbitration losses here (retried, not NACKs)",
                             " · ".join(f"{r['master']} {r['arb_lost']}" for r in arb) if arb else "none reported"))
            self.info.setText(f"<style>b {{ {MONO_CSS} }}</style><table cellspacing=6>" + "".join(
                f"<tr><td style='color:{C['text2']}'>{a}</td><td><b>{b}</b></td></tr>"
                for a, b in rows) + "</table>")
        elif k in ("master", "master2", "analyzer"):
            self.info.setText(self.net.describe(k, engine))
        elif not reg.devices:
            self.info.setText("No traffic decoded yet. Slave nodes appear here as soon as their address is seen "
                              "on the bus — including devices that were not configured.")
