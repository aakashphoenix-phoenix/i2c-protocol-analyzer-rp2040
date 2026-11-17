"""Sensor Monitoring page — DS3231, BMP280 and OLED, interpreted from decoded bus bytes only."""
from __future__ import annotations

import numpy as np
import time
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QGridLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from config import COLORS as C
from gui.widgets import Card, fmt_time_ns, glow_curve, label, styled_plot

# DS3231 register 0x03 (1..7) is user-defined; the masters' firmware (I2C.c weekday_1_sunday) and the demo use
# 1 = Sunday, so that is the convention shown here
WEEKDAYS = ["", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
CARD_MIN_W = 360          # below this a card's big values would clip; the grid reflows instead


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
    if rtc is None or rtc == "INVALID":
        time_s = date_s = "N/A"
    else:
        h, m, s, day, mo, yr = rtc
        time_s, date_s = f"{h:02d}:{m:02d}:{s:02d}", f"{day:02d}/{mo:02d}/{yr:04d}"
    temp_s = f"{temp_c:.2f} C" if temp_c is not None else "N/A"
    press_s = f"{press_hpa:.2f} HPA" if press_hpa is not None else "N/A"
    return ["I2C MONITOR", "", f"TIME {time_s}", f"DATE {date_s}", "", f"TEMP {temp_s}", f"PRESS {press_s}"]


# Controller firmware oled_text() rows: y = 0, 16, 26, 40, 50 -> SSD1306 pages 0, 2, 3, 5, 6.
OLED_PAGES = {0: 0, 2: 2, 3: 3, 5: 5, 6: 6}    # preview line index -> page


def oled_image(lines):
    """128x64 monochrome image of the lines, drawn with the firmware's 5x7 font (6 px pitch)."""
    from sources.demo_source import text_columns
    img = QImage(128, 64, QImage.Format.Format_RGB32)
    img.fill(QColor("#000000"))
    on = QColor("#38D9FF").rgb()
    for idx, page in OLED_PAGES.items():
        cols = text_columns(lines[idx])[:128]
        for x, byte in enumerate(cols):
            for bit in range(8):
                if byte >> bit & 1:
                    img.setPixel(x, page * 8 + bit, on)
    return img


def oled_capture_image(rows):
    """128x64 image of the OLED RAM rebuilt from the CAPTURED writes to 0x3C (SensorDecoder.oled_pixels)."""
    img = QImage(128, 64, QImage.Format.Format_RGB32)
    img.fill(QColor("#000000"))
    on = QColor("#38D9FF").rgb()
    for y, row in enumerate(rows[:64]):
        for x, v in enumerate(row[:128]):
            if v:
                img.setPixel(x, y, on)
    return img


class SensorPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._cols = None
        self._tele_mid = None            # the ONE master whose telemetry is shown (sticky)
        self._trend_src = None           # ("bus",) | ("master", id): the ONE source of both trend graphs
        self._trend_ctx = None
        self._trend_sig = None
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
        # a dot on every reading: the first reading (and a slow sequence that reads the sensor once per cycle) is
        # visible at once, before a line can be drawn between two readings
        self.d_t = self.p_t.plot([], [], pen=None, symbol="o", symbolSize=6, symbolBrush=C["amber"], symbolPen=None)
        self.d_p = self.p_p.plot([], [], pen=None, symbol="o", symbolSize=6, symbolBrush=C["cyan"], symbolPen=None)
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
        self.screen = label("")
        self.screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.screen.setMinimumHeight(64 * 2 + 16)
        self.screen.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.screen.setStyleSheet("background:#000000; border:1px solid #1C2E4A; border-radius:10px; padding:10px;")
        self.oled.add(self.screen)
        self.preview_src = label("", "cardHint", wrap=True)     # where every value on the screen comes from
        self.oled.add(self.preview_src)
        self.oled_cap = self.preview_src
        g, self.oled_kv = kv_grid([("addr", "Address"), ("status", "Address ACK status"), ("writes", "Write transactions"),
                                   ("bytes", "Bytes written (cmd / data)"), ("last", "Last observed"),
                                   ("disp", "Display state")])
        self.oled.add(g)
        self.oled.lay.addStretch(1)

        for card in (self.rtc, self.bmp, self.oled):
            card.setMinimumWidth(CARD_MIN_W)
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
    def _set_preview(self, lines, captured=None):
        self._preview_lines = lines
        self._captured_rows = captured
        self._draw_screen()

    def _draw_screen(self):
        lines = getattr(self, "_preview_lines", None)
        rows = getattr(self, "_captured_rows", None)
        if not lines and not rows:
            return
        avail = max(128, self.screen.width() - 24)
        scale = max(1, min(5, avail // 128))
        img = oled_capture_image(rows) if rows else oled_image(lines)
        self.screen.setPixmap(QPixmap.fromImage(img).scaled(
            128 * scale, 64 * scale, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))

    _fit_preview_font = _draw_screen          # resize hook keeps its old name

    # ------------------------------------------------------------------ one consistent source
    STALE_S = 15.0

    def _pick_sources(self, engine):
        """Sticky source selection (kept until it has been silent for STALE_S, then the freshest alternative):
        returns (master id whose telemetry is used, trend source).  Master 1 and Master 2 read the same sensor but
        at different instants, so their values are NEVER interleaved, and the bus-decoded history never alternates
        with master telemetry."""
        ctx = (engine.decoder, engine.source)
        if ctx != self._trend_ctx:
            self._trend_ctx, self._tele_mid, self._trend_src, self._trend_sig = ctx, None, None, None
        now = time.monotonic()
        by = dict(engine.master_telemetry_by_id) if engine.source != "demo" else {}
        if not by and engine.master_telemetry is not None and engine.source != "demo":
            by = {engine.master_telemetry.get("master_id", 1): engine.master_telemetry}

        def fresh(mid):
            return mid in by and now - by[mid]["received_at"] < self.STALE_S
        if not fresh(self._tele_mid):
            alive = [m for m in by if fresh(m)]
            self._tele_mid = max(alive, key=lambda m: by[m]["received_at"]) if alive else None
        hist = engine.sensors.bmp.history

        def limit(times, scale):
            """STALE_S, or 2.5 x the typical interval between this series' readings when they are slower (the guided
            flow reads the BMP280 once per cycle, every 25-80 s): a slow but regular series is not stale."""
            gaps = sorted((b - a) / scale for a, b in zip(times, times[1:]) if b > a)
            return max(self.STALE_S, 2.5 * gaps[len(gaps) // 2]) if gaps else self.STALE_S

        def bus_alive():
            if len(hist) < 1:
                return False
            return engine.source != "live" or engine.hw_now is None or (engine.hw_now - hist[-1][0]) / 1e9 < limit(
                [t for t, *_ in hist[-6:]], 1e9)

        def master_alive(mid):
            ser = engine.master_telemetry_series.get(mid)
            return bool(ser) and len(ser) >= 2 and now - ser[-1][0] < limit([t for t, *_ in list(ser)[-6:]], 1.0)
        cur = self._trend_src
        still = (cur == ("bus",) and bus_alive()) or (cur is not None and cur[0] == "master" and master_alive(cur[1]))
        if not still:
            if bus_alive():
                cur = ("bus",)
            else:
                cands = [m for m in engine.master_telemetry_series if master_alive(m)]
                cur = ("master", max(cands, key=lambda m: engine.master_telemetry_series[m][-1][0])) if cands else None
            self._trend_src = cur
        return self._tele_mid, self._trend_src, by

    def _draw_trends(self, engine, src):
        """Both trend graphs from ONE series; only touched when that series gained a point."""
        if src == ("bus",):
            h = engine.sensors.bmp.history[-600:]
            arr = np.array([(t, tc, p if p is not None else np.nan) for t, tc, p in h], dtype=float)
            x, temp, press = (arr[:, 0] - arr[-1, 0]) / 1e9, arr[:, 1], arr[:, 2]
            sig, name = (src, len(engine.sensors.bmp.history), h[-1][0]), "bus-decoded 0x76"
        elif src is not None:
            h = list(engine.master_telemetry_series[src[1]])[-600:]
            x = np.array([t - h[-1][0] for t, _, _ in h], dtype=float)
            temp, press = np.array([v for _, v, _ in h], float), np.array([v for _, _, v in h], float)
            sig, name = (src, len(h), h[-1][0]), f"Master {src[1]} telemetry"
        else:
            sig, name = (None,), "no data"
        if sig == self._trend_sig:
            return
        self._trend_sig = sig
        for pw, title, y, curve in ((self.p_t, "Temperature trend", None, self.c_t), (self.p_p, "Pressure trend", None, self.c_p)):
            pw.getPlotItem().setTitle(f"<span style='color:{C['text']};font-size:11pt'>{title}</span> "
                                      f"<span style='color:{C['text2']};font-size:9pt'>· {name}</span>")
        if src is None:
            for c in (self.c_t, self.c_p, self.d_t, self.d_p):
                c.setData([], [])
            return
        for curve, dots, y in ((self.c_t, self.d_t, temp), (self.c_p, self.d_p, press)):
            ok = np.isfinite(y)                            # a missing value is skipped: one continuous line
            curve.setData(x[ok], y[ok])
            dots.setData(x[ok], y[ok])

    def refresh(self, engine):
        sen = engine.sensors
        reg = engine.registry
        now = engine.hw_now or 0
        t0 = engine.packets[0].t_start if engine.packets else 0
        demo = engine.source == "demo"
        tag = "  (DEMO — simulated)" if demo else ""

        def ago(t):
            return f"{fmt_time_ns(now - t)} ago (t = {(t - t0) / 1e9:.3f} s)" if t is not None else "—"

        # Real master USB telemetry is an optional source distinct from decoded packets: ONE master at a time.
        tele_mid, trend_src, by = self._pick_sources(engine)
        tele = by.get(tele_mid) if tele_mid is not None else None
        if tele and time.monotonic() - tele["received_at"] > 5.0:
            tele = None
        use_bus_values = bool(sen.bmp.temperature) and (trend_src is None or trend_src == ("bus",))
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
            self.rtc_reason.setText("Real DS3231 value provided directly by Master " + str(tele_mid) + " over USB, not decoded from passive capture")
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
        self.bmp_kv["calib"].setText("SIMULATED standard set (demo / presentation: not captured)" if b.calib_simulated
                                     else "captured ✓" if b.calib else "not captured")
        if use_bus_values or (b.temperature and not tele):
            self.temp.setText(f"{b.temperature.value:.2f} °C")
            self.press.setText(f"{b.pressure.value:.2f} hPa" if b.pressure else "—")
            self.bmp_kv["raw"].setText(b.temperature.raw)
            self.bmp_kv["last"].setText(ago(b.temperature.t_ns) + tag)
            self.bmp_reason.setText("Bosch datasheet compensation applied to the observed raw ADC bytes using "
                                    + ("the SIMULATED standard calibration words (the simulated sensor's own set)."
                                       if b.calib_simulated else "the observed calibration words.") + tag)
            preview_t = b.temperature.value
            preview_p = b.pressure.value if b.pressure else None
            bmp_src = "bus-decoded BMP280 bytes" + (" (simulated)" if demo else "")
        elif tele:
            self.temp.setText(f"{tele['temperature_c']:.2f} °C")
            self.press.setText(f"{tele['pressure_hpa']:.2f} hPa")
            self.bmp_kv["raw"].setText("Not provided by master telemetry")
            self.bmp_kv["last"].setText("Master USB (live, not passive capture)")
            self.bmp_kv["calib"].setText("Compensated by master C firmware")
            self.bmp_reason.setText(f"Real BMP280 readings from Master {tele_mid} USB, independent of decoded analyzer packets")
            preview_t, preview_p, bmp_src = tele["temperature_c"], tele["pressure_hpa"], "master USB telemetry"
        else:
            self.temp.setText("—")
            self.press.setText("—")
            self.bmp_reason.setText(b.reason)
        self.bmp_kv["txn"].setText(f"{d76.transactions:,}" if d76 else "0")
        if self.isVisible():
            self._draw_trends(engine, trend_src)

        # ---- OLED dashboard preview (same values as the two cards above, nothing invented)
        if sen.oled.data_bytes and not demo:
            # the OLED's RAM rebuilt from the bytes captured on the bus: what the real screen shows (controller 2.6-live:
            # Master 1 time / temperature / pressure on the top lines, Master 2 on the lower lines)
            self._set_preview(None, sen.oled_pixels())
            self.preview_src.setText(f"CAPTURED: the SSD1306 RAM rebuilt from the {sen.oled.data_bytes:,} display bytes "
                                     "both masters wrote to 0x3C on the bus (window / addressing commands applied) - "
                                     "Master 1's live time, temperature and pressure on top, Master 2's below.")
        else:
            self._set_preview(oled_preview_lines(preview_rtc, preview_t, preview_p))
            self.preview_src.setText(f"Screen drawn in the controller firmware's OLED layout from the values above — "
                                     f"time/date: {rtc_src}; temperature/pressure: {bmp_src}. Updates with every new "
                                     f"reading; N/A means no valid reading. It is not a capture of the OLED's own pixels."
                                     + tag)

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
