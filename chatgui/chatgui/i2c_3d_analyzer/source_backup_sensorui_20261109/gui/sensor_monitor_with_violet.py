"""Sensor Monitoring page — DS3231, BMP280 and OLED, interpreted from decoded bus bytes only."""
from __future__ import annotations

import html
import numpy as np
import time
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPixmap
from PyQt6.QtWidgets import QGridLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from config import COLORS as C
from gui.widgets import Card, fmt_time_ns, glow_curve, label, styled_plot

WEEKDAYS = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CARD_MIN_W = 360          # below this a card's big values would clip; the grid reflows instead
OLED_FG, OLED_BG = "#5FE6FF", "#0B1238"


def kv_grid(rows):
    g = QGridLayout()
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(5)
    g.setColumnStretch(1, 1)
    out = {}
    for i, (k, name) in enumerate(rows):
        n = label(name, "cardHint", wrap=True)
        n.setMinimumWidth(120)
        g.addWidget(n, i, 0, Qt.AlignmentFlag.AlignTop)
        v = label("—", "mono", wrap=True)          # long values wrap instead of being clipped
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        g.addWidget(v, i, 1, Qt.AlignmentFlag.AlignTop)
        out[k] = v
    return g, out


def oled_preview_lines(rtc, temp_c, press_hpa):
    """The controller firmware's OLED text layout (I2C.c oled_text calls), filled with
    the dashboard's decoded values.  Unavailable values are shown as such, never invented."""
    if rtc is None:
        time_s = date_s = "UNAVAILABLE"
    elif rtc == "INVALID":
        time_s = date_s = "INVALID (OSF)"
    else:
        h, m, s, day, mo, yr = rtc
        time_s, date_s = f"{h:02d}:{m:02d}:{s:02d}", f"{day:02d}/{mo:02d}/{yr:04d}"
    temp_s = f"{temp_c:.2f} C" if temp_c is not None else "UNAVAILABLE"
    press_s = f"{press_hpa:.2f} HPA" if press_hpa is not None else "UNAVAILABLE"
    return ["I2C MONITOR", "", f"TIME {time_s}", f"DATE {date_s}", "", f"TEMP {temp_s}", f"PRESS {press_s}"]


class SensorPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._cols = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # reflow, never cut off sideways
        # Always reserve the vertical scrollbar: otherwise it appearing/disappearing changes the
        # width by ~15 px and the 3/2-column choice could flip back and forth near the threshold.
        sc.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        outer.addWidget(sc)
        self._scroll = sc
        body = QWidget()
        body.setObjectName("scrollBody")
        sc.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)
        lay.addWidget(label("Sensor values come either from complete decoded I2C bytes (with observed calibration), "
                            "or from the separate MASTER USB port, clearly labelled as master telemetry. "
                            "The analyzer never invents sensor values.", "cardHint", wrap=True))
        self.grid = QGridLayout()
        self.grid.setSpacing(14)

        # ---------------- DS3231
        self.rtc = Card("DS3231 RTC", "0x68")
        self.clock = label("--:--:--", "clock")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.date = label("", "cardTitle", wrap=True)
        self.date.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rtc.add(self.clock)
        self.rtc.add(self.date)
        g, self.rtc_kv = kv_grid([("raw", "Raw registers 00–06"), ("last", "Last observed read"), ("txn", "Transactions"),
                                  ("reads", "Valid time reads"), ("status", "Clock activity")])
        self.rtc.add(g)
        self.rtc_reason = label("", "cardHint", wrap=True)
        self.rtc.add(self.rtc_reason)
        self.rtc.lay.addStretch(1)

        # ---------------- BMP280
        self.bmp = Card("BMP280 environmental sensor", "0x76 · temperature + pressure (no humidity)")
        self.vals = QGridLayout()
        self.vals.setHorizontalSpacing(18)
        self.temp = label("—", "bigValue")
        self.temp.setStyleSheet(f"color:{C['amber']}")
        self.press = label("—", "bigValue")
        self.press.setStyleSheet(f"color:{C['cyan']}")
        self._tname, self._pname = label("TEMPERATURE", "metricName"), label("PRESSURE", "metricName")
        self.bmp.add(self.vals)
        self.p_t = styled_plot("Temperature trend", "°C", "", 130)
        self.p_p = styled_plot("Pressure trend", "hPa", "", 130)
        self.c_t = glow_curve(self.p_t, C["amber"])
        self.c_p = glow_curve(self.p_p, C["cyan"])
        for pw in (self.p_t, self.p_p):
            pw.setMinimumHeight(150)
            pw.setMaximumHeight(190)
            self.bmp.add(pw)
        g, self.bmp_kv = kv_grid([("id", "Identity"), ("calib", "Calibration (0x88–0x9F)"), ("raw", "Raw data F7–FC"),
                                  ("last", "Last update"), ("txn", "Transactions")])
        self.bmp.add(g)
        self.bmp_reason = label("", "cardHint", wrap=True)
        self.bmp.add(self.bmp_reason)
        self.bmp.lay.addStretch(1)                  # keep content top-aligned when the row is taller

        # ---------------- OLED
        self.oled = Card("OLED display", "SSD1306 · 0x3C")
        self.oled.add(label("Dashboard preview — firmware screen layout filled with the decoded DS3231 and BMP280 "
                            "values shown on this page (not captured OLED pixels)", "cardHint", wrap=True))
        self.preview = label("")
        self.preview.setTextFormat(Qt.TextFormat.RichText)
        self.preview.setStyleSheet(f"background:{OLED_BG}; color:{OLED_FG}; border:1px solid #1C2E4A; "
                                   "border-radius:10px; padding:14px 16px;")
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.oled.add(self.preview)
        self.preview_src = label("", "cardHint", wrap=True)
        self.oled.add(self.preview_src)
        self.oled.add(label("Captured SSD1306 framebuffer — reconstructed from observed bus bytes only",
                            "cardHint", wrap=True))
        self.screen = label("")
        self.screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.screen.setMinimumHeight(64 * 2 + 16)
        self.screen.setStyleSheet("background:#02060C; border:1px solid #1C2E4A; border-radius:10px;")
        self.oled.add(self.screen)
        self.oled_cap = label("", "cardHint", wrap=True)
        self.oled.add(self.oled_cap)
        g, self.oled_kv = kv_grid([("addr", "Address"), ("status", "Address ACK status"), ("writes", "Write transactions"),
                                   ("bytes", "Bytes written (cmd / data)"), ("last", "Last observed"),
                                   ("disp", "Display state")])
        self.oled.add(g)
        self.oled.lay.addStretch(1)

        for card in (self.rtc, self.bmp, self.oled):
            card.setMinimumWidth(CARD_MIN_W)
            if hasattr(card, "hint"):
                card.hint.setWordWrap(True)
        lay.addLayout(self.grid)
        lay.addStretch(1)
        self._reflow(3)
        self._set_preview(oled_preview_lines(None, None, None))

    # ------------------------------------------------------------------ responsive layout
    def _reflow(self, cols):
        """3 cards per row when they fit, otherwise 2 (OLED below) or 1 per row. Never overlapping."""
        self._cols = cols
        for card in (self.rtc, self.bmp, self.oled):
            self.grid.removeWidget(card)
        for c in range(3):
            self.grid.setColumnStretch(c, 0)
        if cols == 3:
            spots = [(0, 0, 1), (0, 1, 1), (0, 2, 1)]
        elif cols == 2:
            spots = [(0, 0, 1), (0, 1, 1), (1, 0, 2)]
        else:
            spots = [(0, 0, 1), (1, 0, 1), (2, 0, 1)]
        for card, (r, c, span) in zip((self.rtc, self.bmp, self.oled), spots):
            self.grid.addWidget(card, r, c, 1, span)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        # BMP big values: side by side, or stacked when the BMP card itself is narrow.
        for w in (self._tname, self.temp, self._pname, self.press):
            self.vals.removeWidget(w)
        bmp_w = self._usable_width() / cols if cols < 3 else self._usable_width() / 3
        if bmp_w < 560:      # "978.30 hPa" + "30.43 °C" side by side need ~520 px at the big font
            places = [(0, 0), (1, 0), (2, 0), (3, 0)]
        else:
            places = [(0, 0), (1, 0), (0, 1), (1, 1)]
        for w, (r, c) in zip((self._tname, self.temp, self._pname, self.press), places):
            self.vals.addWidget(w, r, c)

    def _usable_width(self):
        return self._scroll.viewport().width() - 36

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = self._usable_width()
        cols = 3 if w >= 3 * CARD_MIN_W + 2 * 14 + 60 else 2 if w >= 2 * CARD_MIN_W + 14 else 1
        self._reflow(cols)
        self._fit_preview_font()

    # ------------------------------------------------------------------ OLED preview
    def _set_preview(self, lines):
        self._preview_lines = lines
        self._fit_preview_font()

    def _fit_preview_font(self):
        """Largest font (up to 30 px) where the longest line fits.  The size is written into the
        label's own HTML because the app stylesheet would override QLabel.setFont()."""
        lines = getattr(self, "_preview_lines", None)
        if not lines:
            return
        avail = max(160, self.preview.width() - 40)
        longest = max(len(ln) for ln in lines)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setBold(True)
        size = 30
        while size > 9:
            f.setPixelSize(size)
            if QFontMetrics(f).horizontalAdvance("M" * longest) <= avail:
                break
            size -= 1
        body = "<br>".join(html.escape(ln) if ln else "&nbsp;" for ln in lines)
        self.preview.setText(f"<div style='white-space:pre; font-family:Consolas, monospace; font-weight:700; "
                             f"font-size:{size}px; line-height:130%'>{body}</div>")

    def refresh(self, engine):
        sen = engine.sensors
        reg = engine.registry
        now = engine.hw_now or 0
        t0 = engine.packets[0].t_start if engine.packets else 0
        demo = engine.source == "demo"
        tag = "  (DEMO — simulated)" if demo else ""

        def ago(t):
            return f"{fmt_time_ns(now - t)} ago (t = {(t - t0) / 1e9:.3f} s)" if t is not None else "—"

        # Real master USB telemetry is an optional source distinct from decoded packets.
        tele = engine.master_telemetry if engine.source != "demo" else None
        if tele and time.monotonic() - tele["received_at"] > 5.0:
            tele = None
        preview_rtc, rtc_src = None, "unavailable"
        preview_t = preview_p = None
        bmp_src = "unavailable"
        # ---- RTC
        r = sen.rtc
        d68 = reg.devices.get(0x68)
        if r.time:
            h, m, s_, wd, day, mo, yr = r.time.value
            self.clock.setText(f"{h:02d}:{m:02d}:{s_:02d}")
            self.date.setText(f"{WEEKDAYS[wd]}  {day:02d}-{mo:02d}-{yr}{tag}")
            self.rtc_kv["raw"].setText(r.time.raw)
            self.rtc_kv["last"].setText(ago(r.time.t_ns))
            self.rtc_reason.setText("")
            preview_rtc, rtc_src = (h, m, s_, day, mo, yr), "bus-decoded DS3231 read" + (" (simulated)" if demo else "")
        elif tele:
            dt = tele["rtc"]
            self.clock.setText(f"{dt:%H:%M:%S}")
            self.date.setText(f"{dt:%a  %d-%m-%Y} · Master USB")
            self.rtc_kv["raw"].setText("Not available (master USB telemetry)")
            self.rtc_kv["last"].setText("Received via master USB")
            self.rtc_reason.setText("Real DS3231 value provided directly by Pico 1, not decoded from passive capture")
            preview_rtc = (dt.hour, dt.minute, dt.second, dt.day, dt.month, dt.year)
            rtc_src = "master USB telemetry"
        else:
            self.clock.setText("--:--:--")
            self.date.setText("Awaiting valid RTC data")
            self.rtc_reason.setText(r.reason)
            if getattr(r, "osf_t", None) is not None:
                preview_rtc, rtc_src = "INVALID", "DS3231 oscillator-stop flag set"
        self.rtc_kv["txn"].setText(f"{d68.transactions:,}" if d68 else "0")
        self.rtc_kv["reads"].setText(str(r.reads))
        self.rtc_kv["status"].setText(reg.status(0x68, now))

        # ---- BMP280
        b = sen.bmp
        addr = 0x76 if 0x76 in reg.devices or 0x77 not in reg.devices else 0x77
        d76 = reg.devices.get(addr)
        name, conf = reg.name(addr)
        self.bmp_kv["id"].setText(f"0x{addr:02X} · {name} ({conf.lower()}"
                                  + (f": {d76.confirm_detail})" if d76 and d76.confirm_detail else ")"))
        self.bmp_kv["calib"].setText("captured ✓" if b.calib else "not captured")
        if b.temperature:
            self.temp.setText(f"{b.temperature.value:.2f} °C")
            self.press.setText(f"{b.pressure.value:.2f} hPa" if b.pressure else "—")
            self.bmp_kv["raw"].setText(b.temperature.raw)
            self.bmp_kv["last"].setText(ago(b.temperature.t_ns) + tag)
            self.bmp_reason.setText("Bosch datasheet compensation applied to the observed raw ADC bytes "
                                    "using the observed calibration words." + tag)
            preview_t = b.temperature.value
            preview_p = b.pressure.value if b.pressure else None
            bmp_src = "bus-decoded BMP280 bytes" + (" (simulated)" if demo else "")
        elif tele:
            self.temp.setText(f"{tele['temperature_c']:.2f} °C")
            self.press.setText(f"{tele['pressure_hpa']:.2f} hPa")
            self.bmp_kv["raw"].setText("Not provided by master telemetry")
            self.bmp_kv["last"].setText("Master USB (live, not passive capture)")
            self.bmp_kv["calib"].setText("Compensated by master C firmware")
            self.bmp_reason.setText("Real BMP280 readings from Pico 1 USB, independent of decoded analyzer packets")
            preview_t, preview_p, bmp_src = tele["temperature_c"], tele["pressure_hpa"], "master USB telemetry"
        else:
            self.temp.setText("—")
            self.press.setText("—")
            self.bmp_reason.setText(b.reason)
        self.bmp_kv["txn"].setText(f"{d76.transactions:,}" if d76 else "0")
        if tele and not b.history and engine.master_telemetry_history and self.isVisible():
            h = list(engine.master_telemetry_history)[-600:]
            x = np.array([t - h[-1][0] for t, _, _ in h])
            self.c_t.setData(x, [temp for _, temp, _ in h])
            self.c_p.setData(x, [press for _, _, press in h])
        elif b.history and self.isVisible():
            arr = np.array([(t, tc, p if p is not None else np.nan) for t, tc, p in b.history[-600:]], dtype=float)
            x = (arr[:, 0] - arr[-1, 0]) / 1e9
            self.c_t.setData(x, arr[:, 1])
            self.c_p.setData(x, arr[:, 2])

        # ---- OLED dashboard preview (same values as the two cards above, nothing invented)
        self._set_preview(oled_preview_lines(preview_rtc, preview_t, preview_p))
        self.preview_src.setText(f"Time/date: {rtc_src} · temperature/pressure: {bmp_src}. This is a preview of the "
                                 "firmware's layout, not proof of what the physical OLED shows." + tag)

        # ---- OLED captured bytes
        o = sen.oled
        oa = sen.oled_addr
        dO = reg.devices.get(oa)
        self.oled_kv["addr"].setText(f"0x{oa:02X} (configured in Settings)")
        if dO:
            self.oled_kv["status"].setText(f"{dO.acks:,} ACK · {dO.addr_nacks} address NACK")
        self.oled_kv["writes"].setText(f"{o.writes:,}")
        self.oled_kv["bytes"].setText(f"{o.cmd_bytes + o.data_bytes:,}  ({o.cmd_bytes:,} / {o.data_bytes:,})")
        self.oled_kv["last"].setText(ago(o.last_t))
        self.oled_kv["disp"].setText({True: "ON (0xAF seen)", False: "OFF (0xAE)", None: "unknown"}[o.display_on])
        if o.data_bytes and self.isVisible():
            px = sen.oled_pixels()
            img = QImage(128, 64, QImage.Format.Format_RGB32)
            on, off = QColor("#7FE8FF").rgb(), QColor("#03080F").rgb()
            for y, rowp in enumerate(px):
                for x, v in enumerate(rowp):
                    img.setPixel(x, y, on if v else off)
            w = max(128, min(self.screen.width() - 16, 128 * 3))
            self.screen.setPixmap(QPixmap.fromImage(img).scaledToWidth(w, Qt.TransformationMode.FastTransformation))
            cov = o.coverage * 100
            exact = o.init_seen and cov >= 99.9 and not engine.lost_records
            self.oled_cap.setText(
                ("Reconstructed from captured SSD1306 commands and display data. " +
                 ("Full initialisation and every framebuffer byte were captured in this session, so this matches what "
                  "the controller was sent." if exact else
                  f"NOT exact: {cov:.0f} % of the framebuffer was observed"
                  + ("" if o.init_seen else ", initialisation was not captured")
                  + ". Start capture before resetting the master for a complete picture.")) + tag)
        elif not o.data_bytes:
            self.screen.setText("No display data captured yet")
            self.oled_cap.setText("")
