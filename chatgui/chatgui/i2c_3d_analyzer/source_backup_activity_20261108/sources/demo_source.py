"""DEMO MODE data source.

Synthesises the SAME protocol lines the analyzer firmware sends (hello, edges,
stat), bit by bit, for the traffic this bench's master generates. The lines go
through the normal parser and decoder, so demo mode exercises the full
pipeline - but every value it produces is SIMULATED and the GUI labels it
"SIMULATED — NOT VERIFIED HARDWARE".
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import random
import time

from PyQt6.QtCore import QThread, pyqtSignal

TICK_NS = 100            # 10 MS/s simulated sampler
BIT = 100                # 100 ticks = 10 µs -> 100 kHz

FONT = {
    "0": "3E5149453E", "1": "00427F4000", "2": "4261514946", "3": "214145 4B31".replace(" ", ""),
    "4": "1814127F10", "5": "2745454539", "6": "3C4A494930", "7": "0171090503",
    "8": "3649494936", "9": "064949291E", ":": "0036360000", ".": "0060600000",
    " ": "0000000000", "-": "0808080808", "A": "7E1111117E", "B": "7F49494936",
    "C": "3E41414122", "D": "7F4141221C", "E": "7F49494941", "I": "00417F4100",
    "L": "7F40404040", "M": "7F020C027F", "O": "3E4141413E", "P": "7F09090906",
    "R": "7F09192946", "S": "4649494931", "T": "01017F0101", "h": "7F08040478",
    "a": "2054545478", "N": "7F0408107F", "K": "7F08142241",
    # remaining glyphs used by the controller firmware's font5()
    "H": "7F0808087F", "U": "3F4040403F", "X": "6314081463", "/": "2010080402",
}

CALIB = dict(T1=27504, T2=26435, T3=-1000, P1=36477, P2=-10685, P3=3024, P4=2855,
             P5=140, P6=-7, P7=15500, P8=-14600, P9=6000)


def text_columns(s: str) -> list[int]:
    out = []
    for ch in s:
        hx = FONT.get(ch, FONT[" "])
        out += [int(hx[i:i + 2], 16) for i in range(0, 10, 2)] + [0]
    return out


def to_bcd(v):
    return ((v // 10) << 4) | (v % 10)


class EdgeSynth:
    """Generates (tick, state) edges for open-drain I²C at ~100 kHz."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random(0)
        self.t = 0
        self.sda = 1
        self.scl = 1
        self.out: list = []

    def _set(self, sda=None, scl=None):
        if sda is not None:
            self.sda = sda
        if scl is not None:
            self.scl = scl
        s = (self.scl << 1) | self.sda
        if s != getattr(self, "last_s", 3):
            self.out.append((self.t, s))
            self.last_s = s

    def wait(self, ticks):
        self.t += int(ticks)

    def q(self):
        return BIT // 4 + self.rng.randint(-1, 1)

    def start(self):
        self._set(sda=1, scl=1); self.wait(self.q())
        self._set(sda=0); self.wait(self.q() * 2)
        self._set(scl=0); self.wait(self.q())

    def rstart(self):
        self._set(sda=1); self.wait(self.q() * 2)
        self._set(scl=1); self.wait(self.q() * 2)
        self._set(sda=0); self.wait(self.q() * 2)
        self._set(scl=0); self.wait(self.q())

    def stop(self):
        self._set(sda=0); self.wait(self.q())
        self._set(scl=1); self.wait(self.q() * 2)
        self._set(sda=1); self.wait(self.q() * 2)

    def bit(self, b):
        self._set(sda=b); self.wait(self.q())
        self._set(scl=1); self.wait(BIT // 2 + self.rng.randint(-1, 1))
        self._set(scl=0); self.wait(self.q())

    def byte(self, v, ack=True):
        for i in range(7, -1, -1):
            self.bit((v >> i) & 1)
        self.bit(0 if ack else 1)


# Simulated DS3231 epoch: the value the controller firmware writes when
# SET_RTC_ON_BOOT=1 (18 Oct 2026 08:22:00).  The PC wall clock is never used.
SIM_RTC_EPOCH = _dt.datetime(2026, 10, 18, 8, 22, 0)

# Controller firmware (firmware/native_rp2040_master/I2C.c) SSD1306 init list,
# sent one command per transaction as [0x00, cmd] by oled_command().
OLED_INIT = [0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14,
             0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12, 0x81, 0x7F, 0xD9, 0xF1,
             0xDB, 0x30, 0xA4, 0xA6, 0x2E, 0xAF]
OLED_WINDOW = [0x21, 0, 127, 0x22, 0, 7]


class DemoGenerator:
    """Pure generator (no Qt) so it can be unit-tested.

    Reproduces the bus traffic of the controller firmware I2C.c at 100 kHz:
    boot  - BMP280 chip-ID read (0xD0), calibration read (0x88, 24 bytes),
            config write F5=A0, ctrl_meas write F4=27, 26 SSD1306 commands;
    loop  - DS3231 register read (00..06, 7 bytes), BMP280 burst read
            (F7..FC, 6 bytes), SSD1306 window commands + 64 x 16-byte
            framebuffer writes, then sleep_ms(1000).
    Every register read is a combined write / repeated START / read ending with
    the controller's normal final-byte NACK.  Address NACKs are produced only
    when fault injection is explicitly requested (inject_absent_probe=True).
    The firmware's 2 s USB start-up delay is shortened to keep the demo brisk.
    """

    def __init__(self, seed=None, inject_absent_probe=False):
        self.rng = random.Random(seed)
        self.syn = EdgeSynth(self.rng)
        self.seq = 0
        self.next_stat = 0.5
        self.booted = False
        self.bus_free = 0
        self.inject_absent_probe = inject_absent_probe
        self.plan: list = []            # [(earliest_start_s or "+delay", builder)]
        self.loop_index = 0
        self.rtc_epoch = SIM_RTC_EPOCH

    def hello(self):
        return json.dumps({"t": "hello", "proto": 1, "fw": "DEMO generator (simulated)", "board": "simulated",
                           "sample_hz": int(1e9 / TICK_NS), "tick_ns": TICK_NS,
                           "caps": ["edges", "start_stop", "seq", "stats"]}).encode()

    # ---------------------------------------------------------------- txns
    def _gap_ticks(self):
        # Software overhead between Pico SDK calls on the controller (~30-60 us).
        return int(self.rng.uniform(30e3, 60e3) / TICK_NS)

    def _txn(self, at_s, build):
        s = self.syn
        t_start = max(int(at_s * 1e9 / TICK_NS), self.bus_free + self._gap_ticks())
        s.t = t_start
        s.out = []
        build(s)
        self.bus_free = s.t
        return s.out

    @staticmethod
    def _write(s, addr, data, nack_addr=False):
        s.start(); s.byte(addr << 1, ack=not nack_addr)
        if not nack_addr:
            for b in data:
                s.byte(b)
        s.stop()

    @staticmethod
    def _wr_rd(s, addr, reg, values):
        # i2c_write_timeout_us(..., nostop=true) then i2c_read_timeout_us(...)
        s.start(); s.byte(addr << 1); s.byte(reg)
        s.rstart(); s.byte((addr << 1) | 1)
        for i, v in enumerate(values):
            s.byte(v, ack=i < len(values) - 1)     # controller NACKs the final byte
        s.stop()

    def _bmp_raw(self, t_s):
        temp_raw = int(519888 + 900 * math.sin(t_s / 40) + self.rng.randint(-40, 40))
        press_raw = int(415148 + 300 * math.sin(t_s / 90) + self.rng.randint(-25, 25))
        return [press_raw >> 12 & 0xFF, press_raw >> 4 & 0xFF, (press_raw & 0xF) << 4,
                temp_raw >> 12 & 0xFF, temp_raw >> 4 & 0xFF, (temp_raw & 0xF) << 4]

    @staticmethod
    def _compensate(raw):
        from analysis.sensor_decoder import bmp280_compensate
        adc_p = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
        adc_t = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)
        return bmp280_compensate(CALIB, adc_t, adc_p)

    @staticmethod
    def _framebuffer(lines):
        fb = [0] * 1024
        for page, text in lines:
            for x, c in enumerate(text_columns(text)[:128]):
                fb[page * 128 + x] = c
        return fb

    def _queue_boot(self):
        q = self.plan.append
        q((0.02, lambda s: self._wr_rd(s, 0x76, 0xD0, [0x58])))
        cal = []
        for k in ["T1", "T2", "T3", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]:
            v = CALIB[k] & 0xFFFF
            cal += [v & 0xFF, v >> 8]
        q((0.0, lambda s: self._wr_rd(s, 0x76, 0x88, cal)))
        q((0.0, lambda s: self._write(s, 0x76, [0xF5, 0xA0])))
        q((0.0, lambda s: self._write(s, 0x76, [0xF4, 0x27])))
        q(("+0.1", None))                           # sleep_ms(100) in bmp_init()
        for cmd in OLED_INIT:
            q((0.0, lambda s, c=cmd: self._write(s, 0x3C, [0x00, c])))

    def _queue_loop(self):
        q = self.plan.append
        sim_s = self.loop_index
        self.loop_index += 1
        wall = self.rtc_epoch + _dt.timedelta(seconds=sim_s)
        rtc = [to_bcd(wall.second), to_bcd(wall.minute), to_bcd(wall.hour), (wall.isoweekday() % 7) + 1,
               to_bcd(wall.day), to_bcd(wall.month), to_bcd(wall.year % 100)]
        q((0.0, lambda s: self._wr_rd(s, 0x68, 0x00, rtc)))
        raw = self._bmp_raw(self.bus_free * TICK_NS / 1e9)
        q((0.0, lambda s: self._wr_rd(s, 0x76, 0xF7, raw)))
        temp, press = self._compensate(raw)
        fb = self._framebuffer([(0, "I2C MONITOR"), (2, f"TIME {wall:%H:%M:%S}"),
                                (3, f"DATE {wall:%d/%m/%Y}"), (5, f"TEMP {temp:.2f} C"),
                                (6, f"PRESS {press:.2f} HPA")])
        for cmd in OLED_WINDOW:
            q((0.0, lambda s, c=cmd: self._write(s, 0x3C, [0x00, c])))
        for i in range(0, 1024, 16):
            q((0.0, lambda s, chunk=fb[i:i + 16]: self._write(s, 0x3C, [0x40] + chunk)))
        if self.inject_absent_probe and self.loop_index % 7 == 0:
            # Explicit, opt-in fault injection: an absent address is NACKed.
            q((0.0, lambda s: self._write(s, 0x2A, [], nack_addr=True)))
        q(("+1.0", None))                           # sleep_ms(1000)

    def lines_until(self, now_s: float) -> list[bytes]:
        edges: list = []
        if not self.booted:
            self.booted = True
            edges.append((0, 3))          # initial line state: bus idle (firmware sends this too)
            self._queue_boot()
        while True:
            if not self.plan:
                self._queue_loop()
            at, build = self.plan[0]
            if build is None:                       # delay step
                self.plan.pop(0)
                self.bus_free += int(float(at[1:]) * 1e9 / TICK_NS)
                continue
            start_tick = max(int(at * 1e9 / TICK_NS), self.bus_free + 1)
            if start_tick * TICK_NS / 1e9 > now_s:
                break
            self.plan.pop(0)
            edges += self._txn(at, build)
        lines = []
        for i in range(0, len(edges), 400):
            chunk = edges[i:i + 400]
            prev = chunk[0][0]
            e = []
            for tk, st in chunk:
                e.append([tk - prev, st])
                prev = tk
            lines.append(json.dumps({"t": "edges", "seq": self.seq, "t0": chunk[0][0], "e": e},
                                    separators=(",", ":")).encode())
            self.seq += 1
        if now_s >= self.next_stat:
            lines.append(json.dumps({"t": "stat", "seq": self.seq, "ts": int(now_s * 1e9 / TICK_NS),
                                     "buf_pct": self.rng.randint(2, 9), "dropped": 0, "overflows": 0}).encode())
            self.seq += 1
            self.next_stat = now_s + 0.5
        return lines


class DemoSource(QThread):
    lines_ready = pyqtSignal(list)
    status = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._run = True

    def stop(self):
        self._run = False

    def run(self):
        gen = DemoGenerator()
        self.lines_ready.emit([gen.hello()])
        t0 = time.monotonic()
        while self._run:
            lines = gen.lines_until(time.monotonic() - t0)
            if lines:
                self.lines_ready.emit(lines)
            self.msleep(50)
