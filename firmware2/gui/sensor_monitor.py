"""Sensor Monitoring page — DS3231, BMP280 and OLED, interpreted from decoded bus bytes only."""
from __future__ import annotations

import numpy as np
import time
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from config import COLORS as C
from gui.widgets import Card, fmt_time_ns, glow_curve, label, styled_plot

WEEKDAYS = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def kv_grid(rows):
    g = QGridLayout()
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(5)
    out = {}
    for i, (k, name) in enumerate(rows):
        g.addWidget(label(name, "cardHint"), i, 0)
        v = label("—", "mono")
        g.addWidget(v, i, 1)
        out[k] = v
    return g, out


class SensorPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        outer.addWidget(sc)
        body = QWidget()
        body.setObjectName("scrollBody")
        sc.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)
        lay.addWidget(label("Sensor values come either from complete decoded I2C bytes (with observed calibration), "
                            "or from the separate MASTER USB port, clearly labelled as master telemetry. "
                            "The analyzer never invents sensor values.", "cardHint", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(14)

        # ---------------- DS3231
        self.rtc = Card("DS3231 RTC", "0x68")
        self.clock = label("--:--:--", "clock")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.date = label("", "cardTitle")
        self.date.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rtc.add(self.clock)
        self.rtc.add(self.date)
        g, self.rtc_kv = kv_grid([("raw", "Raw registers 00–06"), ("last", "Last observed read"), ("txn", "Transactions"),
                                  ("reads", "Valid time reads"), ("status", "Clock activity")])
        self.rtc.add(g)
        self.rtc_reason = label("", "cardHint", wrap=True)
        self.rtc.add(self.rtc_reason)
        self.rtc.lay.addStretch(1)
        row.addWidget(self.rtc, 1)

        # ---------------- BMP280
        self.bmp = Card("BMP280 environmental sensor", "0x76 · temperature + pressure (no humidity)")
        vals = QHBoxLayout()
        tcol = QVBoxLayout()
        self.temp = label("—", "bigValue")
        self.temp.setStyleSheet(f"color:{C['amber']}")
        tcol.addWidget(label("TEMPERATURE", "metricName"))
        tcol.addWidget(self.temp)
        pcol = QVBoxLayout()
        self.press = label("—", "bigValue")
        self.press.setStyleSheet(f"color:{C['cyan']}")
        pcol.addWidget(label("PRESSURE", "metricName"))
        pcol.addWidget(self.press)
        vals.addLayout(tcol)
        vals.addLayout(pcol)
        self.bmp.add(vals)
        self.p_t = styled_plot("Temperature trend", "°C", "", 130)
        self.p_p = styled_plot("Pressure trend", "hPa", "", 130)
        self.c_t = glow_curve(self.p_t, C["amber"])
        self.c_p = glow_curve(self.p_p, C["cyan"])
        for pw in (self.p_t, self.p_p):
            pw.setMaximumHeight(190)
            self.bmp.add(pw)
        g, self.bmp_kv = kv_grid([("id", "Identity"), ("calib", "Calibration (0x88–0x9F)"), ("raw", "Raw data F7–FC"),
                                  ("last", "Last update"), ("txn", "Transactions")])
        self.bmp.add(g)
        self.bmp_reason = label("", "cardHint", wrap=True)
        self.bmp.add(self.bmp_reason)
        row.addWidget(self.bmp, 1)

        # ---------------- OLED
        self.oled = Card("OLED display", "SSD1306")
        self.screen = label("")
        self.screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.screen.setMinimumHeight(64 * 3 + 16)
        self.screen.setStyleSheet("background:#02060C; border:1px solid #1C2E4A; border-radius:10px;")
        self.oled.add(self.screen)
        self.oled_cap = label("", "cardHint", wrap=True)
        self.oled.add(self.oled_cap)
        g, self.oled_kv = kv_grid([("addr", "Address"), ("status", "Address ACK status"), ("writes", "Write transactions"),
                                   ("bytes", "Bytes written (cmd / data)"), ("last", "Last observed"),
                                   ("disp", "Display state")])
        self.oled.add(g)
        self.oled.lay.addStretch(1)
        row.addWidget(self.oled, 1)
        lay.addLayout(row)
        lay.addStretch(1)

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
        elif tele:
            dt = tele["rtc"]
            self.clock.setText(f"{dt:%H:%M:%S}")
            self.date.setText(f"{dt:%a  %d-%m-%Y} · Master USB")
            self.rtc_kv["raw"].setText("Not available (master USB telemetry)")
            self.rtc_kv["last"].setText("Received via master USB")
            self.rtc_reason.setText("Real DS3231 value provided directly by Pico 1, not decoded from passive capture")
        else:
            self.clock.setText("--:--:--")
            self.date.setText("Awaiting valid RTC data")
            self.rtc_reason.setText(r.reason)
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
        elif tele:
            self.temp.setText(f"{tele['temperature_c']:.2f} °C")
            self.press.setText(f"{tele['pressure_hpa']:.2f} hPa")
            self.bmp_kv["raw"].setText("Not provided by master telemetry")
            self.bmp_kv["last"].setText("Master USB (live, not passive capture)")
            self.bmp_kv["calib"].setText("Compensated by master C firmware")
            self.bmp_reason.setText("Real BMP280 readings from Pico 1 USB, independent of decoded analyzer packets")
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

        # ---- OLED
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
            w = max(256, min(self.screen.width() - 16, 128 * 4))
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
