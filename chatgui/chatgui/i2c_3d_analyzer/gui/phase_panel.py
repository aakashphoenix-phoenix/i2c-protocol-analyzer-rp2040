"""Live phase banner shared by the 3D I²C network and the Packet Analyzer (both read the SAME playback cursor).

    [Master 2 (Pico 3) → OLED 0x3C · WRITE]  Phase 3/6: Address ACK
    START ▸ ADDRESS ▸ ACK/NACK ▸ DATA ▸ ACK/NACK ▸ STOP          (the active phase highlighted)
    Slave → Master · OLED 0x3C → Master 2 (Pico 3) · SDA=0 · SCL=9th clock · byte 0x78 · bits 0111 1000
"""
from __future__ import annotations

import html

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

import config
from analysis.playback import PHASE_SHORT, phase_info
from config import COLORS as C

SHORT_TARGET = {0x68: "RTC", 0x76: "BMP280", 0x77: "BMP280", 0x3C: "OLED", 0x3D: "OLED", 0x57: "EEPROM"}


def master_name(txn):
    m = config.MASTERS.get(getattr(txn, "master_id", None))
    return f"{m['label']} ({m['pico']})" if m else "Master UNKNOWN (not validated)"


def target_name(addr):
    return f"{SHORT_TARGET.get(addr, 'device')} 0x{addr:02X}" if addr is not None else "bus"


def describe(playback, held=False):
    """(info dict, master, target, arbitration text or None) for the stage on display, or None when idle.
    held: paused on this phase (Pause / Step) - the byte is shown complete (all 8 bits), as its dot has crossed."""
    txn, ev = playback.txn, playback.events
    if txn is None or not ev:
        return None
    i = playback.stage_index
    st = ev[i]
    addr = st.address if st.address is not None else txn.address
    master, target = master_name(txn), target_name(addr)
    info = phase_info(ev, i, 1.0 if held else playback.stage_fraction, master, target)
    arb = (getattr(txn, "presentation", None) or {}).get("arbitration")
    arb_text = arb["text"] if (arb and info["num"] == 1 and i == 0) else None
    if arb_text:
        info["name"] = "Arbitration & START"
    return info, master, target, arb_text


def banner_text(playback):
    """Plain one-line banner, e.g. '[Master 2 (Pico 3) → OLED 0x3C · WRITE] Phase 3/6: Address ACK'."""
    d = describe(playback)
    if d is None:
        return "IDLE — bus free, no transfer on display"
    info, master, target, _ = d
    return f"[{master} → {target} · {info['op']}] Phase {info['num']}/6: {info['name']}"


class PhasePanel(QWidget):
    def __init__(self, compact=False, parent=None):
        super().__init__(parent)
        self.compact = compact
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.banner = QLabel()
        self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.banner)
        self.chips = []
        row = QHBoxLayout()
        row.setSpacing(4)
        for k, name in enumerate(PHASE_SHORT):
            lb = QLabel(f"{k + 1} {name}")
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.chips.append(lb)
            row.addWidget(lb)
            if k < len(PHASE_SHORT) - 1:
                arrow = QLabel("▸")
                arrow.setObjectName("cardHint")
                row.addWidget(arrow)
        row.addStretch(1)
        self.chip_row = QWidget()
        self.chip_row.setLayout(row)
        # the chips may be clipped in a very narrow window, but they never force a minimum width on the diagram
        self.chip_row.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.chip_row.setMinimumWidth(0)
        row.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.chip_row)
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setWordWrap(True)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.detail)
        # FIXED heights: a phase change must never change the panel's height - that resized the OpenGL view below it and
        # re-fitted the whole 3D scene (~0.8 s each time), which stalled the animation
        from PyQt6.QtGui import QFont, QFontMetrics
        bf = QFont(self.font()); bf.setPointSize(10 if compact else 13); bf.setBold(True)
        self.banner.setFixedHeight(QFontMetrics(bf).height() + 6)
        self.banner.setWordWrap(False)
        self.detail.setFixedHeight(QFontMetrics(self.font()).lineSpacing() * 2 + 8)
        self.chip_row.setFixedHeight(QFontMetrics(self.font()).height() + 10)
        if compact:
            self.chip_row.hide()
            self.detail.hide()
        self._key = None
        self.text = ""
        self.active_phase = None
        self.show_playback(None)

    # ------------------------------------------------------------------
    def _chip_style(self, state):
        bg, fg, bd = {"active": (C["amber"], C["bg"], C["amber"]), "done": (C["card_hi"], C["green"], C["green"]),
                      "todo": (C["card"], C["text2"], C["border"])}[state]
        pt = 8 if self.compact else 9
        return (f"background:{bg}; color:{fg}; border:1px solid {bd}; border-radius:4px; padding:1px 6px; "
                f"font-size:{pt}pt; font-weight:{'bold' if state == 'active' else 'normal'};")

    def show_playback(self, playback, run_state="running", held=False, note=""):
        """Refresh from the playback cursor (cheap: only re-renders when the phase, bit, note or run state changed).
        note: an extra line (e.g. a live arbitration loss reported by a master) shown in the detail row."""
        d = describe(playback, held) if playback is not None else None
        bit = None if d is None else (d[0]["bit"], d[0]["kind"])
        key = (None if d is None else (id(playback.txn), playback.stage_index, bit), run_state, note)
        if key == self._key:
            return
        self._key = key
        pt = 10 if self.compact else 13
        if d is None:
            self.active_phase = None
            self.text = "IDLE — bus free, no transfer on display"
            word = {"stopped": " · ■ STOPPED", "draining": " · ■ STOPPING"}.get(run_state, "")
            self.banner.setText(f"<span style='font-size:{pt}pt; color:{C['text2']}'><b>{self.text}{word}</b></span>")
            for lb in self.chips:
                lb.setStyleSheet(self._chip_style("todo"))
            self.detail.setText(f"<span style='color:{C['amber']}'>{html.escape(note)}</span>" if note else "")
            return
        info, master, target, arb = d
        txn = playback.txn
        mcol = config.MASTERS.get(getattr(txn, "master_id", None), {}).get("color", C["amber"])
        pcol = (C["ack"] if info["ack"] is True else C["nack"] if info["ack"] is False else
                C["amber"] if arb else C["text"])
        self.active_phase = info["num"]
        self.text = f"[{master} → {target} · {info['op']}] Phase {info['num']}/6: {info['name']}"
        sim = " · SIMULATED" if getattr(txn, "demo", False) else ""
        state = {"stopped": " · ■ STOPPED", "draining": " · ■ STOPPING after this transfer"}.get(run_state, "")
        self.banner.setText(
            f"<span style='font-size:{pt}pt'><b>[<span style='color:{mcol}'>{html.escape(master)}</span> → "
            f"{html.escape(target)} · {info['op']}]</b>&nbsp;&nbsp;<b style='color:{pcol}'>Phase {info['num']}/6: "
            f"{html.escape(info['name'])}</b><span style='color:{C['text2']}; font-size:{pt - 3}pt'>{sim}{state}"
            f"</span></span>")
        for k, lb in enumerate(self.chips):
            lb.setStyleSheet(self._chip_style("active" if k + 1 == info["num"] else
                                              "done" if k + 1 < info["num"] else "todo"))
        parts = [f"<b>{info['direction']}</b>: {html.escape(info['sender'])} → {html.escape(info['receiver'])}",
                 f"SDA = <b>{info['sda']}</b>", f"SCL = <b>{info['scl']}</b>"]
        if info["value"] is not None:
            parts.append(f"byte <b>0x{info['value']:02X}</b>")
        if info["bits"]:
            b, k = info["bits"], info["bit"]
            shown = "".join((f"<b style='color:{C['amber']}'>[{c}]</b>" if j == k else
                             f"<span style='color:{C['text']}'>{c}</span>" if j < k else
                             f"<span style='color:{C['text2']}'>·</span>") for j, c in enumerate(b))
            parts.append(f"bits b7→b0 {shown} (bit {k + 1}/8)")
        line = " · ".join(parts)
        extra = arb or note or info.get("detail", "")
        self.detail.setText(f"{line}<br><span style='color:{C['text2']}'>{html.escape(extra)}</span>")

    def retheme(self):
        self._key = None
