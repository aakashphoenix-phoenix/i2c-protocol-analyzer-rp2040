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
from collections import deque

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


# Simulated bus timing per controller SCL setting, in 100 ns ticks.  Every value
# is at or above the UM10204 Rev. 7.0 Table 10 minimum for that mode, with margin
# for the +/-1 tick jitter:
#   Standard-mode: tLOW 4.7 us, tHIGH 4.0 us, tHD;STA 4.0 us, tSU;STA 4.7 us,
#                  tSU;STO 4.0 us, tSU;DAT 250 ns, tBUF 4.7 us
#   Fast-mode:     tLOW 1.3 us, tHIGH 0.6 us, tHD;STA 0.6 us, tSU;STA 0.6 us,
#                  tSU;STO 0.6 us, tSU;DAT 100 ns, tBUF 1.3 us
# SCL LOW is longer than HIGH, like the RP2040 I2C block (lcnt 3/5 of the period).
SIM_TIMING = {
    100_000: dict(low=55, high=45, hold=15, hd_sta=50, su_sta=50, su_sto=50, idle=50),
    400_000: dict(low=15, high=10, hold=4, hd_sta=8, su_sta=8, su_sto=8, idle=15),
}


class EdgeSynth:
    """Generates (tick, state) edges for open-drain I²C at a simulated SCL rate."""

    def __init__(self, rng=None, scl_hz=100_000, table=None):
        self.rng = rng or random.Random(0)
        self.t = 0
        self.sda = 1
        self.scl = 1
        self.out: list = []
        self.table = SIM_TIMING if table is None else table
        self.set_rate(scl_hz)

    def set_rate(self, scl_hz):
        if scl_hz not in self.table:
            raise ValueError(f"unsupported simulated SCL rate {scl_hz}")
        self.scl_hz = scl_hz
        self.tm = self.table[scl_hz]

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

    def j(self, ticks):
        # Fractional timing values (the measured bench profile) are dithered so their MEAN is exact; whole-tick
        # values take the original path (no extra random draw, so seeded legacy streams are unchanged).
        base = int(ticks)
        if ticks != base:
            base += self.rng.random() < ticks - base
        return base + self.rng.randint(-1, 1)

    def q(self):                                  # kept for older callers
        return self.tm["hold"]

    def start(self):
        tm = self.tm
        self._set(sda=1, scl=1); self.wait(self.j(tm["idle"]))
        self._set(sda=0); self.wait(self.j(tm["hd_sta"]))           # tHD;STA
        self._set(scl=0); self.wait(self.j(tm["hold"]))

    def rstart(self):
        tm = self.tm
        self._set(sda=1); self.wait(self.j(tm["low"] - tm["hold"]))   # SDA released during SCL LOW
        self._set(scl=1); self.wait(self.j(tm["su_sta"]))            # tSU;STA
        self._set(sda=0); self.wait(self.j(tm["hd_sta"]))            # tHD;STA
        self._set(scl=0); self.wait(self.j(tm["hold"]))

    def stop(self):
        tm = self.tm
        self._set(sda=0); self.wait(self.j(tm["low"] - tm["hold"]))
        self._set(scl=1); self.wait(self.j(tm["su_sto"]))            # tSU;STO
        self._set(sda=1); self.wait(self.j(tm["idle"]))              # bus free

    def bit(self, b):
        # SDA changes `hold` after SCL fell, is set up for the rest of LOW, sampled at the rise.
        tm = self.tm
        self._set(sda=b); self.wait(self.j(tm["low"] - tm["hold"]))
        self._set(scl=1); self.wait(self.j(tm["high"]))
        self._set(scl=0); self.wait(self.j(tm["hold"]))

    def byte(self, v, ack=True):
        for i in range(7, -1, -1):
            self.bit((v >> i) & 1)
        self.bit(0 if ack else 1)


# Simulated DS3231 epoch: the value the controller firmware writes when
# SET_RTC_ON_BOOT=1 (Monday 16 Nov 2026 08:22:00, the review date).  The PC wall clock is never used.
SIM_RTC_EPOCH = _dt.datetime(2026, 11, 16, 8, 22, 0)

# Controller firmware (firmware/native_rp2040_master/I2C.c) SSD1306 init list,
# sent one command per transaction as [0x00, cmd] by oled_command().
OLED_INIT = [0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14,
             0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12, 0x81, 0x7F, 0xD9, 0xF1,
             0xDB, 0x30, 0xA4, 0xA6, 0x2E, 0xAF]
OLED_WINDOW = [0x21, 0, 127, 0x22, 0, 7]


# ===================================================================== hardware-grounded multi-master bench
# Every constant below comes from the live bench measurement of 2026-11-15
# (work/measurements/MM_RESULTS_20261115.md, mm_before_summary.json: analyzer COM28 + Master 1 COM32 + Master 2 COM34,
# 30 s, 2419 packets, all attributed) or from the controller firmware itself (firmware/native_rp2040_master/I2C.c).
# The simulation REPRODUCES that behaviour; it is never evidence about hardware and is labelled SIMULATED everywhere.
BENCH_MEASURED = dict(
    source="work/measurements/MM_RESULTS_20261115.md (live bench, 30 s, 2026-11-15)",
    seconds=30.0, packets=2419, acks=38290, nacks=312, read_end_nacks=312, unexpected_nacks=0,
    nack_rate_pct=0.81, scl_hz_median=94_570, scl_high_ns=4_444, scl_low_ns=6_131, bus_free_min_ns=46_500,
    per_30s={"0x3C w2": 186, "0x3C w17": 1921, "0x68 w1 r16": 58, "0x76 w1 r6": 242, "0x76 w1 r1": 6,
             "0x76 w1 r24": 6},
    by_master={1: 2295, 2: 124},
)

# Bus timing in 100 ns ticks.  100 kHz requested -> 94.6 kHz measured (SCL HIGH 4.44 us, LOW 6.13 us: the RP2040
# divider plus the open-drain rise time of ~2 kOhm pull-ups).  Fractional values are dithered (EdgeSynth.j).
# 400 kHz was NOT measured on this bench: nominal Fast-mode timing, simulated only.
BENCH_TIMING = {
    100_000: dict(low=61.3, high=44.4, hold=15, hd_sta=50, su_sta=50, su_sto=50, idle=2),
    400_000: dict(low=15, high=10, hold=4, hd_sta=8, su_sta=8, su_sto=8, idle=2),
}

# Controller roles (I2C.c MASTER_ID 1/2).  clock_* = measured fit of each controller's microsecond clock onto the
# analyzer clock (offset and crystal drift), so the GUI has to ALIGN the simulated telemetry exactly as on hardware.
BENCH_MASTERS = {
    1: dict(name="Master 1", board="pico", bmp_ms=200, rtc_ms=1000, offset_ms=0, idle_us=40, backoff_us=150,
            drives_oled=True, clock_offset_us=121_409.06, drift_ppm=-28.58),
    2: dict(name="Master 2", board="pico_w", bmp_ms=330, rtc_ms=1100, offset_ms=97, idle_us=70, backoff_us=230,
            drives_oled=False, clock_offset_us=118_193.01, drift_ppm=13.55, oled_poll=True),
}
# Master 2 (Pico 3) also writes to the shared SSD1306 in the demo (SIMULATED): once per display cycle one command
# transfer [0x00, 0xA6] (normal display - harmless between Master 1's frames).  The OLED is write-only on I2C.
BENCH_IDENTITY_MS = 10_000
BENCH_HOLDOFF_US = 120          # POST_TXN_HOLDOFF_US
BENCH_IDLE_JITTER_US = 16       # IDLE_JITTER_US
BENCH_MAX_ATTEMPTS = 6
BENCH_BACKOFF_JITTER_US = 200
# Measured software time from one transfer's STOP to the SAME controller's next idle check (TXLOG on), µs (mean, sd):
# OLED->OLED gap 437 µs median, BMP->RTC 560 µs, RTC->first OLED command 1687 µs, minus the idle window.
BENCH_GAP_US = dict(default=(385.0, 20.0), after_bmp=(510.0, 40.0), before_frame=(1635.0, 100.0))
BENCH_POLL_US = (2.0, 14.0)     # bus-free -> idle-window start while waiting (measured M2->M1 52 µs, M1->M2 87 µs)
BMP_CONVERSION_S = 1.0057       # config 0xA0: t_sb 1000 ms + ~5.7 ms measurement (88 % identical reads measured)

# ---- turn-taking profile (schedule="turns"): firmware I2C.c 2.2-turns (SCHEDULE_TURNS), measured live 2026-11-17
# (work/measurements/MM_TURNS_20261117.md, mm_turns.jsonl + mm_turns_com32/34.log: 70 s, 1168 packets, 16 turns).
BENCH_TURNS_MEASURED = dict(
    source="work/measurements/MM_TURNS_20261117.md (live bench, 70 s, 2026-11-17)",
    seconds=70.0, packets=1168, acks=19368, nacks=48, read_end_nacks=48, unexpected_nacks=0,
    gap_s=(4.0003, 4.0008, 4.0014), turns=16, transfers_per_turn=(72, 74), first_turn_transfers=103,
    turn_ms={1: (144.0, 148.8), 2: (146.7, 150.9)}, by_master={1: 584, 2: 584},
    per_turn={"0x68 w1 r16": 1, "0x76 w1 r6": 1, "0x3C w2": 6, "0x3C w17": 64},   # + identity (D0, 88) every 2nd turn
    arb_lost=0, busy_waits=0,
)
BENCH_TURN_GAP_MS = 4000          # TURN_GAP_MS: idle bus between the end of one turn and the next
BENCH_TURN_LATENCY_US = (200.0, 1300.0)   # 1 ms observation loop + printf: measured gaps 4.0003-4.0014 s
# The firmware's first turn comes 2 s (USB wait) + 4.5 s (TURN_GAP_MS + TURN_TAKEOVER_EXTRA_MS) after power-up.  The demo
# starts the rotation after BENCH_TURN_FIRST_S instead, like BOOT_DELAY_S shortens the USB wait; every later gap is 4 s.
BENCH_TURN_FIRST_S = 0.25
# Measured STOP -> next idle check of the SAME controller inside a turn (TXLOG on), µs (mean, sd), idle window removed:
# RTC -> BMP280 460/472 µs, identity reads 410-427 µs, BMP read -> first OLED command 846/883 µs (frame rendering),
# OLED -> OLED 367-436 µs on the bus (Master 1 / Master 2; the Pico W is consistently a little slower).
BENCH_TURN_GAP_US = {
    1: dict(after_rtc=(420.0, 15.0), bmp=(365.0, 20.0), before_frame=(798.0, 35.0), default=(345.0, 25.0)),
    2: dict(after_rtc=(397.0, 10.0), bmp=(345.0, 20.0), before_frame=(805.0, 60.0), default=(355.0, 20.0)),
}


class _SimBMP280:
    """The shared BMP280: data registers change once per conversion, so most polls read identical bytes."""

    def __init__(self, rng):
        self.rng = rng
        self.configured_at = None
        self.ctrl = [0x00, 0x00]          # F4, F5 after power-on (sleep mode)
        self.cache = {}

    def configure(self, t_s, reg, value):
        if reg == 0xF5:
            self.ctrl[1] = value
        elif reg == 0xF4:
            self.ctrl[0] = value
        if self.ctrl == [0x27, 0xA0] and self.configured_at is None:
            self.configured_at = t_s

    def data(self, t_s):
        if self.configured_at is None or t_s < self.configured_at + 0.0057:
            return [0x80, 0x00, 0x00, 0x80, 0x00, 0x00]      # reset value 0x80000: no conversion yet
        k = int((t_s - self.configured_at - 0.0057) / BMP_CONVERSION_S)
        if k not in self.cache:
            tc = self.configured_at + k * BMP_CONVERSION_S
            temp_raw = int(519888 + 900 * math.sin(tc / 40) + self.rng.randint(-40, 40))
            press_raw = int(415148 + 300 * math.sin(tc / 90) + self.rng.randint(-25, 25))
            self.cache = {k: [press_raw >> 12 & 0xFF, press_raw >> 4 & 0xFF, (press_raw & 0xF) << 4,
                              temp_raw >> 12 & 0xFF, temp_raw >> 4 & 0xFF, (temp_raw & 0xF) << 4]}
        return list(self.cache[k])


class _SimController:
    """One simulated controller running the I2C.c main loop (schedule, idle window, hold-off, back-off)."""

    def __init__(self, mid, cfg, bench):
        self.mid, self.cfg, self.bench = mid, cfg, bench
        self.rng = bench.rng
        self.queue: deque = deque()
        self.check_at = None              # bus ns at which the idle check for queue[0] starts
        self.window_ns = 0
        self.poll_ns = 0
        self.attempt = 1
        self.arb = 0
        self.seq = 0
        self.stats = {k: 0 for k in ("txn_ok", "addr_nack", "data_nack", "arb_lost", "retries", "gave_up",
                                     "busy_waits", "busy_timeouts", "timeouts", "other_aborts", "recoveries",
                                     "recovery_skipped", "controller_resets")}
        boot_s = DemoGenerator.BOOT_DELAY_S + cfg["offset_ms"] / 1000.0
        self.boot_ns = boot_s * 1e9
        self.loop_started = False
        self.next_bmp = self.next_rtc = self.next_identity = None
        self.next_stats_ns = None
        self.display_count = 0
        self.last_reading = None          # compensated (temp, press) of this master's last good BMP read
        self.out: list = []
        # turn-taking firmware (schedule="turns"): one turn = RTC -> BMP280 -> OLED, then the other master's turn
        self.turns = bench.schedule == "turns"
        self.turn_no = 0
        self.in_turn = False
        self.turn_t0 = self.turn_seq0 = 0
        self.turn_rtc_wall = None
        self.bmp_ok = self.oled_ok = False
        self.next_identity_ns = None
        self._emit_boot_records()
        if self.turns:
            self.next_stats_ns = self.boot_ns + 1e9   # turns_main(): stats every second from boot
        else:
            self._queue_boot()

    @property
    def draws_oled(self):
        return self.turns or self.cfg["drives_oled"]

    # ------------------------------------------------------------- clock / telemetry
    def clock_us(self, t_ns):
        """This controller's time_us_64() at analyzer time t_ns (measured offset + drift)."""
        c = self.cfg
        return int((t_ns / 1000.0 + c["clock_offset_us"]) / (1.0 + c["drift_ppm"] * 1e-6))

    def emit(self, d):
        d = {**d, "source": d.get("source", "controller"), "master_id": self.mid, "simulated": True}
        self.out.append(json.dumps(d, separators=(",", ":")).encode())

    def _emit_boot_records(self):
        c = self.cfg
        if self.turns:
            self.emit({"type": "hello", "role": "controller", "name": c["name"], "board": c["board"],
                       "fw": "SIMULATED I2C multi-master controller (demo)", "fw_version": "2.2-turns (SIMULATED)",
                       "sda_pin": 4, "scl_pin": 5, "scl_hz": self.bench.scl_hz, "drives_oled": True,
                       "oled_cmd_writes": False, "schedule": "turns", "turn_gap_ms": BENCH_TURN_GAP_MS,
                       "turn_takeover_ms": BENCH_TURN_GAP_MS + 500, "peer_timeout_ms": 3 * (BENCH_TURN_GAP_MS + 500),
                       "turn_order": [0x68, 0x76, 0x3C], "first_turn_owner": 1, "identity_ms": BENCH_IDENTITY_MS,
                       "idle_window_us": c["idle_us"], "holdoff_us": BENCH_HOLDOFF_US,
                       "max_attempts": BENCH_MAX_ATTEMPTS, "txlog": True})
        else:
            self.emit({"type": "hello", "role": "controller", "name": c["name"], "board": c["board"],
                       "fw": "SIMULATED I2C multi-master controller (demo)",
                       "fw_version": "2.0-multimaster (SIMULATED)",
                       "sda_pin": 4, "scl_pin": 5, "scl_hz": self.bench.scl_hz, "drives_oled": c["drives_oled"],
                       "schedule": "free",
                       "periods_ms": {"bmp": c["bmp_ms"], "rtc": c["rtc_ms"], "identity": BENCH_IDENTITY_MS},
                       "offset_ms": c["offset_ms"], "idle_window_us": c["idle_us"], "holdoff_us": BENCH_HOLDOFF_US,
                       "max_attempts": BENCH_MAX_ATTEMPTS, "txlog": True})
        self.emit({"type": "txlog", "enabled": True})
        # this controller's microsecond clock mapped onto the analyzer clock, known at boot (the simulation's own
        # offset): the GUI locks attribution at once instead of after NO_PRIOR_MIN_SPAN_NS of traffic, so no early demo
        # packet is left unattributed; crystal drift is then tracked by every matched transfer
        self.emit({"type": "master_clock", "offset_ns": self.boot_ns - self.clock_us(self.boot_ns) * 1000.0})
        self.scl_record("boot")

    def scl_record(self, result):
        hz = self.bench.scl_hz
        self.emit({"type": "scl_config", "result": result, "requested_hz": hz, "applied_hz": hz,
                   "supported_hz": [100_000, 400_000], "reason": "SIMULATED controller setting"})

    def stats_record(self, t_ns):
        self.emit({"type": "master_stats", "t_us": self.clock_us(t_ns), **self.stats, "txlog": True,
                   "last_timeout": {"t_us": 0, "addr": 0, "wlen": 0, "rlen": 0, "attempt": 0, "ic_status": 0,
                                    "sda": 0, "scl": 0}})

    def stats_until(self, t_ns):
        while self.next_stats_ns is not None and self.next_stats_ns <= t_ns:
            self.stats_record(self.next_stats_ns)
            self.next_stats_ns += 1e9

    # ------------------------------------------------------------- firmware program
    def _gap(self, kind):
        if self.turns:
            g = BENCH_TURN_GAP_US[self.mid]
            mu, sd = g.get(kind, g["default"])
            return max(0.75 * mu, self.rng.gauss(mu, sd)) * 1000.0
        mu, sd = BENCH_GAP_US[kind]
        return max(0.75 * mu, self.rng.gauss(mu, sd)) * 1000.0

    def _add(self, addr, w=(), r=0, kind="", nack_addr=False, gap="default", delay_ns=0.0, data=None):
        self.queue.append(dict(addr=addr, w=list(w or ()), rlen=r, kind=kind, nack_addr=nack_addr, gap=gap,
                               delay_ns=delay_ns, data=data))

    def _queue_identity(self):
        self._add(0x76, [0xD0], 1, "bmp_id")
        self._add(0x76, [0x88], 24, "bmp_cal")

    def _queue_boot(self):
        self._queue_identity()
        self._add(0x76, [0xF4], 2, "bmp_ctrl")           # configured already? (shared sensor, I2C.c bmp_init)
        if self.cfg["drives_oled"]:
            for cmd in OLED_INIT:
                self._add(0x3C, [0x00, cmd], 0, "oled_init")

    def _queue_loop(self, now_ns):
        """One firmware main-loop pass at now_ns: queue whatever is due, else return the next due time."""
        if not self.loop_started:
            self.loop_started = True
            self.next_bmp = self.next_rtc = now_ns
            self.next_identity = now_ns + BENCH_IDENTITY_MS * 1e6
            self.next_stats_ns = now_ns + 1e9
        due = False
        if now_ns >= self.next_bmp:
            self.next_bmp += self.cfg["bmp_ms"] * 1e6
            self._add(0x76, [0xF7], 6, "bmp_read", gap="after_bmp")
            due = True
        if now_ns >= self.next_identity:
            self.next_identity += BENCH_IDENTITY_MS * 1e6
            self._queue_identity()
            due = True
        if now_ns >= self.next_rtc:
            self.next_rtc += self.cfg["rtc_ms"] * 1e6
            self.display_count += 1
            due = True
            self._add(0x68, [0x00], 16, "rtc_read", gap="before_frame" if self.cfg["drives_oled"] else "default")
            if self.cfg["drives_oled"]:
                for cmd in OLED_WINDOW:
                    self._add(0x3C, [0x00, cmd], 0, "oled_window")
                for i in range(0, 1024, 16):
                    self._add(0x3C, None, 0, "oled_data", data=i)       # bytes rendered when the RTC was read
            if self.cfg.get("oled_poll"):
                self._add(0x3C, [0x00, 0xA6], 0, "oled_cmd")              # Master 2 -> OLED: WRITE only
            if self.cfg["drives_oled"]:
                if self.bench.inject_absent_probe and self.display_count % 7 == 0:
                    # Explicit, opt-in fault injection (I2C.c NACK_TEST_ADDR): an absent address is NACKed.
                    self._add(0x2A, [0x00], 0, "absent_probe", nack_addr=True)
        if due:
            return None
        nxt = min(self.next_bmp, self.next_identity, self.next_rtc)
        return nxt + self.rng.uniform(0.03e6, 1.1e6)      # 1 ms loop sleep + loop overhead

    def _queue_turn(self, now_ns):
        """I2C.c run_turn(): RTC 0x68 -> BMP280 0x76 (init / identity when due, then data) -> OLED 0x3C."""
        self.turn_no += 1
        self.in_turn = True
        self.turn_t0, self.turn_seq0 = now_ns, self.seq
        self.stats_until(now_ns)
        reason = "first_turn" if self.turn_no == 1 and self.mid == 1 else "after_peer_turn"
        self.emit({"type": "turn_start", "turn": self.turn_no, "t_us": self.clock_us(now_ns), "reason": reason,
                   "idle_before_us": int(self.bench.turn_idle_ns / 1000)})
        self._add(0x68, [0x00], 16, "rtc_read", gap="after_rtc")
        if not self.bmp_ok:
            self._add(0x76, [0xD0], 1, "bmp_id", gap="bmp")
            self._add(0x76, [0x88], 24, "bmp_cal", gap="bmp")
            self._add(0x76, [0xF4], 2, "bmp_ctrl", gap="bmp")       # configured already? (shared sensor)
            self.bmp_ok = True
            self.next_identity_ns = now_ns + BENCH_IDENTITY_MS * 1e6
        elif now_ns >= self.next_identity_ns:
            self.next_identity_ns = now_ns + BENCH_IDENTITY_MS * 1e6
            self._add(0x76, [0xD0], 1, "bmp_id", gap="bmp")
            self._add(0x76, [0x88], 24, "bmp_cal", gap="bmp")
        self._add(0x76, [0xF7], 6, "bmp_read", gap="before_frame")
        if not self.oled_ok:
            for cmd in OLED_INIT:
                self._add(0x3C, [0x00, cmd], 0, "oled_init")
            self.oled_ok = True
        for cmd in OLED_WINDOW:
            self._add(0x3C, [0x00, cmd], 0, "oled_window")
        for i in range(0, 1024, 16):
            self._add(0x3C, None, 0, "oled_data", data=i)
        if self.bench.inject_absent_probe and self.mid == 1 and self.turn_no % 7 == 0:
            self._add(0x2A, [0x00], 0, "absent_probe", nack_addr=True)   # explicit, opt-in fault injection

    def _end_turn(self, end_ns):
        self.in_turn = False
        if self.turn_rtc_wall is not None and self.last_reading:
            temp, press = self.last_reading
            self.emit({"type": "sensor_telemetry", "version": 1, "source": "master",
                       "rtc": f"{self.turn_rtc_wall:%Y-%m-%dT%H:%M:%S}", "temperature_c": round(temp, 2),
                       "pressure_hpa": round(press, 2)})
        self.emit({"type": "turn_end", "turn": self.turn_no, "t_us": self.clock_us(end_ns),
                   "dur_us": int((end_ns - self.turn_t0) / 1000), "transfers": self.seq - self.turn_seq0,
                   "rtc": "ok", "bmp": "ok" if self.last_reading else "invalid", "oled": "ok"})
        self.bench.turn_done(self.mid, end_ns)

    def ensure_next(self, prev_end_ns):
        """Make sure queue[0] exists and has an idle-check start time."""
        if self.check_at is not None:
            return
        if self.turns and not self.queue:
            b = self.bench
            if b.turn_owner != self.mid:
                self.check_at = float("inf")            # not our turn: we only watch the bus
                return
            self._queue_turn(max(prev_end_ns, b.next_turn_ns))
            prev_end_ns = self.turn_t0
        t = max(prev_end_ns, self.boot_ns)
        while not self.queue:
            nxt = self._queue_loop(t)
            if nxt is not None:
                t = nxt
        self.check_at = t + self.queue[0].pop("delay_ns", 0.0)
        self.window_ns = (self.cfg["idle_us"] + self.rng.randint(0, BENCH_IDLE_JITTER_US)) * 1000.0
        self.poll_ns = self.rng.uniform(*BENCH_POLL_US) * 1000.0

    def start_time(self, bus_free_ns):
        """START edge time if nobody else takes the bus first (wait_bus_idle)."""
        if self.check_at >= bus_free_ns:
            return self.check_at + self.window_ns
        return bus_free_ns + self.poll_ns + self.window_ns

    # ------------------------------------------------------------- results
    def finish(self, item, start_ns, end_ns, result, w, wbus, rbus):
        """Report one logical transfer exactly like I2C.c report_txn() (b0 = first REQUESTED write byte)."""
        self.stats_until(start_ns)
        self.seq += 1
        if self.arb:
            self.stats["retries"] += self.attempt - 1
        key = {"ok": "txn_ok", "addr_nack": "addr_nack", "data_nack": "data_nack", "arb_lost": "gave_up"}[result]
        self.stats[key] += 1
        self.emit({"type": "txn", "seq": self.seq, "t_us": self.clock_us(start_ns - 2000),
                   "dur_us": int((end_ns - start_ns) / 1000 + 3), "addr": item["addr"], "wlen": wbus, "rlen": rbus,
                   "b0": w[0] if w else -1, "result": result, "attempts": self.attempt, "arb_lost": self.arb})
        self.queue.popleft()
        self.check_at = None
        self._waited = False
        self.attempt, self.arb = 1, 0
        gap = self._gap(item["gap"])
        if item["kind"] == "bmp_ctrl" and item.get("read") is not None and item["read"] != [0x27, 0xA0]:
            # bmp_init(): not configured yet -> config F5=A0, ctrl_meas F4=27, then sleep_ms(10)
            self.queue.appendleft(dict(addr=0x76, w=[0xF4, 0x27], rlen=0, kind="bmp_cfg", nack_addr=False,
                                       gap="default", delay_ns=0.0, data=None))
            self.queue.appendleft(dict(addr=0x76, w=[0xF5, 0xA0], rlen=0, kind="bmp_cfg", nack_addr=False,
                                       gap="default", delay_ns=0.0, data=None))
        if item["kind"] == "bmp_cfg" and item["w"][0] == 0xF4:
            gap += 10e6
        if self.turns and self.in_turn and not self.queue:
            self._end_turn(end_ns)
        return end_ns + gap

    def lost_arbitration(self, item, start_ns, byte_index, winner_end_ns):
        self.arb += 1
        self.stats["arb_lost"] += 1
        backoff = self.cfg["backoff_us"] * self.attempt + self.rng.randrange(BENCH_BACKOFF_JITTER_US)
        retry = self.attempt < BENCH_MAX_ATTEMPTS
        self.stats_until(start_ns)
        self.emit({"type": "arb_lost", "t_us": self.clock_us(start_ns - 2000), "addr": item["addr"],
                   "attempt": self.attempt, "byte": byte_index, "abort_source": 1 << 12, "backoff_us": backoff,
                   "retry": retry})
        if not retry:
            return self.finish(item, start_ns, start_ns, "arb_lost", item["w"] or [0x40], 0, 0)
        self.attempt += 1
        self.check_at = start_ns + (byte_index + 1) * 9 * 1e9 / self.bench.scl_hz + backoff * 1000.0
        self.window_ns = (self.cfg["idle_us"] + self.rng.randint(0, BENCH_IDLE_JITTER_US)) * 1000.0
        return None


class _BenchSim:
    """Two simulated controllers sharing one simulated bus, observed by a simulated passive analyzer."""

    def __init__(self, gen, scl_hz, inject_absent_probe, schedule="free"):
        self.gen = gen
        self.rng = gen.rng
        self.schedule = schedule
        # turn taking: Master 1 owns the first turn; turn_done() hands the bus over after BENCH_TURN_GAP_MS idle
        self.turn_owner = 1
        self.next_turn_ns = BENCH_TURN_FIRST_S * 1e9
        self.turn_idle_ns = (BENCH_TURN_GAP_MS + 500) * 1e6
        self.turn_log: list = []          # (master_id, turn, start_ns, end_ns)
        self.syn = EdgeSynth(self.rng, scl_hz, table=BENCH_TIMING)
        self.inject_absent_probe = inject_absent_probe
        self.bmp = _SimBMP280(self.rng)
        self.bus_free_ns = 0.0
        self.pending_scl = None
        self.ctl = {mid: _SimController(mid, cfg, self) for mid, cfg in BENCH_MASTERS.items()}
        self.ready = {mid: c.boot_ns for mid, c in self.ctl.items()}
        self.bus_log: list = []           # (mid or (mid, mid) when merged, addr, start_ns, end_ns, result)
        # One scripted contention early in the run (both firmware loops running, different transfers due within
        # CONTEND_WINDOW_NS): the later controller's idle check is moved to the same instant, so both see an idle bus
        # and start together.  The outcome is then decided bit by bit (wired-AND) exactly like any natural collision,
        # so the review always shows at least one real arbitration loss + retry within the first second or so.
        self.contended = False

    CONTEND_WINDOW_NS = 20e6

    def turn_done(self, mid, end_ns):
        c = self.ctl[mid]
        self.turn_log.append((mid, c.turn_no, c.turn_t0, end_ns))
        other = 2 if mid == 1 else 1
        latency = self.rng.uniform(*BENCH_TURN_LATENCY_US) * 1000.0
        self.turn_idle_ns = BENCH_TURN_GAP_MS * 1e6 + latency
        self.next_turn_ns = end_ns + self.turn_idle_ns
        self.turn_owner = other
        self.ctl[other].check_at = None                 # the waiting master saw the idle gap: its turn is next

    def _scripted_contention(self, c1, c2, s1, s2):
        if self.contended or not (c1.loop_started and c2.loop_started) or s2 - s1 >= self.CONTEND_WINDOW_NS:
            return False
        i1, i2 = c1.queue[0], c2.queue[0]
        if i1["nack_addr"] or i2["nack_addr"]:
            return False
        # identical transfers merge into one bus transaction (no arbitration); the OLED data rows differ by content
        return (i1["addr"], i1["w"], i1["rlen"], i1["data"]) != (i2["addr"], i2["w"], i2["rlen"], i2["data"])

    @property
    def scl_hz(self):
        return self.syn.scl_hz

    # ------------------------------------------------------------- bus contents of one transfer
    def _payload(self, c, item, t_ns):
        addr, kind = item["addr"], item["kind"]
        t_s = t_ns / 1e9
        if kind == "oled_data":
            fb = c.framebuffer
            return [0x40] + fb[item["data"]:item["data"] + 16], []
        if item["rlen"] == 0:
            return item["w"], []
        reg = item["w"][0]
        if addr == 0x76 and reg == 0xD0:
            return item["w"], [0x58]
        if addr == 0x76 and reg == 0x88:
            return item["w"], self.gen._calib_bytes()
        if addr == 0x76 and reg == 0xF4:
            return item["w"], list(self.bmp.ctrl)
        if addr == 0x76 and reg == 0xF7:
            return item["w"], self.bmp.data(t_s)
        if addr == 0x68:
            wall = self.gen.rtc_epoch + _dt.timedelta(seconds=int(t_s))
            return item["w"], [to_bcd(wall.second), to_bcd(wall.minute), to_bcd(wall.hour),
                               (wall.isoweekday() % 7) + 1, to_bcd(wall.day), to_bcd(wall.month),
                               to_bcd(wall.year % 100)] + [0] * 7 + [0x1C, 0x08]
        return item["w"], [0] * item["rlen"]

    def _after_read(self, c, item, data, t_ns):
        kind = item["kind"]
        if kind == "bmp_ctrl":
            item["read"] = data
        elif kind == "bmp_cfg":
            self.bmp.configure(t_ns / 1e9, item["w"][0], item["w"][1])
        elif kind == "bmp_read":
            adc_p = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
            if adc_p != 0x80000:
                c.last_reading = self.gen._compensate(data)
            if c.turns:
                wall = c.turn_rtc_wall
                lines = [(0, f"MASTER {c.mid} TURN {c.turn_no}"), (2, f"TIME {wall:%H:%M:%S}"),
                         (3, f"DATE {wall:%d/%m/%Y}")]
                lines += ([(5, f"TEMP {c.last_reading[0]:.2f} C"), (6, f"PRESS {c.last_reading[1]:.2f} HPA")]
                          if c.last_reading else [(5, "BMP ERROR")])
                c.framebuffer = self.gen._framebuffer(lines)
        elif kind == "rtc_read" and c.turns:
            c.turn_rtc_wall = self.gen.rtc_epoch + _dt.timedelta(seconds=int(t_ns / 1e9))
        elif kind == "rtc_read":
            wall = self.gen.rtc_epoch + _dt.timedelta(seconds=int(t_ns / 1e9))
            if c.last_reading:
                temp, press = c.last_reading
                c.emit({"type": "sensor_telemetry", "version": 1, "source": "master",
                        "rtc": f"{wall:%Y-%m-%dT%H:%M:%S}", "temperature_c": round(temp, 2),
                        "pressure_hpa": round(press, 2)})
            if c.cfg["drives_oled"]:
                lines = [(0, "I2C MONITOR"), (2, f"TIME {wall:%H:%M:%S}"), (3, f"DATE {wall:%d/%m/%Y}")]
                lines += ([(5, f"TEMP {c.last_reading[0]:.2f} C"), (6, f"PRESS {c.last_reading[1]:.2f} HPA")]
                          if c.last_reading else [(5, "BMP ERROR")])
                c.framebuffer = self.gen._framebuffer(lines)

    @staticmethod
    def _wire_bits(item, w):
        """Bytes the controller drives before the target answers (what arbitration compares)."""
        first = (item["addr"] << 1) | (0 if (w or item["rlen"] == 0) else 1)
        return [first] + list(w)

    # ------------------------------------------------------------- one bus transaction
    def _synth(self, addr, w, r, nack_addr, start_ns):
        s = self.syn
        tick0 = int(round(start_ns / TICK_NS)) - s.tm["idle"]
        s.t = max(tick0, int(self.bus_free_ns / TICK_NS) + 1)
        s.out = []
        if r and w:
            DemoGenerator._wr_rd(s, addr, w[0], r)
        elif r:
            s.start(); s.byte((addr << 1) | 1)
            for i, v in enumerate(r):
                s.byte(v, ack=i < len(r) - 1)
            s.stop()
        else:
            DemoGenerator._write(s, addr, w, nack_addr=nack_addr)
        return s.out, s.t * TICK_NS

    def step(self):
        """Commit the next bus transaction (or arbitration).  Returns the committed edge list."""
        for mid, c in self.ctl.items():
            c.ensure_next(self.ready[mid])
        cands = sorted(((c.start_time(self.bus_free_ns), mid) for mid, c in self.ctl.items()))
        (s1, m1), (s2, m2) = cands[0], cands[1]
        c1, c2 = self.ctl[m1], self.ctl[m2]
        if self.pending_scl is not None:
            self.syn.set_rate(self.pending_scl)
            self.pending_scl = None
            for c in self.ctl.values():
                c.scl_record("applied")
        scripted = self._scripted_contention(c1, c2, s1, s2)
        if scripted:
            s2 = s1                                  # the later controller's idle window ends at the same instant
        item1 = c1.queue[0]
        w1, r1 = self._payload(c1, item1, s1)
        edges = []
        if scripted or (s2 - s1 < 1000.0 and c2.check_at < s1):
            # Both controllers saw an idle bus and started together: real arbitration (UM10204 §3.7).
            item2 = c2.queue[0]
            w2, r2 = self._payload(c2, item2, s2)
            b1, b2 = self._wire_bits(item1, w1), self._wire_bits(item2, w2)
            if b1 == b2 and len(r1) == len(r2) and not item1["nack_addr"] and not item2["nack_addr"]:
                # Identical transfers never arbitrate: one bus transaction, both controllers report success.
                edges, end = self._synth(item1["addr"], w1, r1, False, s1)
                self._after_read(c1, item1, r1, s1)
                self._after_read(c2, item2, r2, s1)
                self.bus_log.append(((m1, m2), item1["addr"], s1, end, "ok"))
                self.ready[m1] = c1.finish(item1, s1, end, "ok", w1, len(w1), len(r1))
                self.ready[m2] = c2.finish(item2, s1, end, "ok", w2, len(w2), len(r2))
                self.bus_free_ns = end
                return edges
            if b1 == b2:
                # same address + written bytes: the reads diverge at the controller ACK/NACK bit; ACK (LOW) wins
                k = len(b1) + min(len(r1), len(r2))
                win_first = len(r1) > len(r2)
            else:
                k = next(i for i, (a, b) in enumerate(zip(b1 + [0x100], b2 + [0x100])) if a != b)
                win_first = b1 < b2                 # the first 0 bit where they differ wins (wired-AND)
            (cw, mw, iw, ww, rw, sw), (cl, ml, il) = (
                ((c1, m1, item1, w1, r1, s1), (c2, m2, item2)) if win_first else
                ((c2, m2, item2, w2, r2, s1), (c1, m1, item1)))
            nack = iw["nack_addr"]
            edges, end = self._synth(iw["addr"], ww, [] if nack else rw, nack, sw)
            if not nack:
                self._after_read(cw, iw, rw, sw)
            self.bus_log.append((mw, iw["addr"], sw, end, "addr_nack" if nack else "ok"))
            self.ready[mw] = cw.finish(iw, sw, end, "addr_nack" if nack else "ok", ww, 0 if nack else len(ww),
                                       0 if nack else len(rw))
            self.contended = True
            nxt = cl.lost_arbitration(il, sw, k, end)
            if nxt is not None:
                self.ready[ml] = nxt
            self.bus_free_ns = end
            return edges
        nack = item1["nack_addr"]
        edges, end = self._synth(item1["addr"], w1, [] if nack else r1, nack, s1)
        if not nack:
            self._after_read(c1, item1, r1, s1)
        self.bus_log.append((m1, item1["addr"], s1, end, "addr_nack" if nack else "ok"))
        self.ready[m1] = c1.finish(item1, s1, end, "addr_nack" if nack else "ok", w1, 0 if nack else len(w1),
                                   0 if nack else len(r1))
        # The other controller saw this START or a busy bus during its idle window: it waits (busy_waits).
        if c2.check_at is not None and c2.check_at < end:
            if not getattr(c2, "_waited", False):
                c2.stats["busy_waits"] += 1
                c2._waited = True
        self.bus_free_ns = end
        return edges

    def next_start_ns(self):
        for mid, c in self.ctl.items():
            c.ensure_next(self.ready[mid])
        return min(c.start_time(self.bus_free_ns) for c in self.ctl.values())


class DemoGenerator:
    """Pure generator (no Qt) so it can be unit-tested.

    Reproduces the bus traffic of the flashed controller firmware
    (I2C/I2C/I2C.c == firmware/native_rp2040_master/I2C.c) at 100 kHz:
    boot  - BMP280 chip-ID read (0xD0), calibration read (0x88, 24 bytes),
            config write F5=A0, ctrl_meas F4=27, 26 SSD1306 commands (0xAE first);
    loop  - BMP280 burst read F7..FC every 200 ms; every 1 s one combined DS3231
            read of registers 0x00..0x0F (time + status, OSF clear) followed by the
            SSD1306 window commands and 64 x 16-byte framebuffer writes; BMP280
            identity (0xD0 + calibration) every 10 s.  Bus scan is off (firmware
            default SCAN_PERIOD_MS 0).
    Every register read is a combined write / repeated START / read ending with
    the controller's normal final-byte NACK.  Address NACKs are produced only
    when fault injection is explicitly requested (inject_absent_probe=True).
    """


    def __init__(self, seed=None, inject_absent_probe=False, scl_hz=100_000, masters=1, schedule="free"):
        """masters=1: the original single-controller stream (unchanged, nominal 100 kHz timing).
        masters=2: the hardware-grounded two-controller bench (BENCH_* constants) with SIMULATED controller telemetry
        per master, collected with take_master_lines().  schedule (masters=2 only):
          "free"  - free-running I2C.c 2.0/2.1 schedule, measured 2026-11-15 (BENCH_MEASURED)
          "turns" - strict turn taking, I2C.c 2.2-turns, measured 2026-11-17 (BENCH_TURNS_MEASURED):
                    Master 1 RTC -> BMP280 -> OLED, 4 s idle bus, Master 2 RTC -> BMP280 -> OLED, 4 s idle, ..."""
        if masters not in (1, 2):
            raise ValueError("masters must be 1 or 2")
        if schedule not in ("free", "turns") or (schedule == "turns" and masters != 2):
            raise ValueError("schedule must be 'free', or 'turns' with masters=2")
        self.schedule = schedule
        self.rng = random.Random(seed)
        self.masters = masters
        self.pending_scl = None
        self.seq = 0
        self.next_stat = 0.5
        self.booted = False
        self.bus_free = 0
        self.inject_absent_probe = inject_absent_probe
        self.plan: list = []            # [(earliest_start_s or "+delay", builder)]
        self.loop_index = 0
        self.rtc_epoch = SIM_RTC_EPOCH
        self.last_raw = None
        self.next_bmp = self.next_display = self.next_identity = float("inf")
        self.bench = _BenchSim(self, scl_hz, inject_absent_probe, schedule) if masters == 2 else None
        self.syn = self.bench.syn if self.bench else EdgeSynth(self.rng, scl_hz)

    def hello(self):
        fw = ("DEMO generator (simulated multi-master bench, turn taking)" if self.bench and self.schedule == "turns"
              else "DEMO generator (simulated multi-master bench)" if self.bench else "DEMO generator (simulated)")
        return json.dumps({"t": "hello", "proto": 1, "fw": fw, "board": "simulated",
                           "sample_hz": int(1e9 / TICK_NS), "tick_ns": TICK_NS,
                           "caps": ["edges", "start_stop", "seq", "stats"]}).encode()

    def take_master_lines(self) -> dict:
        """{master_id: [SIMULATED controller JSON lines]} produced since the last call ({} for masters=1)."""
        if not self.bench:
            return {}
        out = {}
        for mid, c in self.bench.ctl.items():
            if c.out:
                out[mid], c.out = c.out, []
        return out

    # ---------------------------------------------------------------- txns
    def _gap_ticks(self):
        # Software overhead between Pico SDK calls on the controller (~30-60 us).
        return int(self.rng.uniform(30e3, 60e3) / TICK_NS)

    def request_scl(self, hz):
        """SIMULATED controller SCL setting: applied between transactions, like the firmware."""
        if hz not in SIM_TIMING:
            raise ValueError(f"unsupported simulated SCL rate {hz}")
        if self.bench:
            self.bench.pending_scl = hz
            return
        self.pending_scl = hz

    @property
    def scl_hz(self):
        return self.syn.scl_hz

    def _txn(self, at_s, build):
        s = self.syn
        if self.pending_scl is not None:
            s.set_rate(self.pending_scl)
            self.pending_scl = None
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

    # Firmware schedule (I2C.c): BMP_PERIOD_MS, DISPLAY_PERIOD_MS, IDENTITY_PERIOD_MS.
    BMP_PERIOD_S = 0.2
    DISPLAY_PERIOD_S = 1.0
    IDENTITY_PERIOD_S = 10.0
    BOOT_DELAY_S = 0.02          # firmware waits 2 s for USB; shortened for the demo

    def _calib_bytes(self):
        cal = []
        for k in ["T1", "T2", "T3", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]:
            v = CALIB[k] & 0xFFFF
            cal += [v & 0xFF, v >> 8]
        return cal

    def _queue_identity(self):
        q = self.plan.append
        q((0.0, lambda s: self._wr_rd(s, 0x76, 0xD0, [0x58])))              # chip ID
        q((0.0, lambda s: self._wr_rd(s, 0x76, 0x88, self._calib_bytes())))  # calibration

    def _queue_boot(self):
        q = self.plan.append
        q((self.BOOT_DELAY_S, lambda s: self._wr_rd(s, 0x76, 0xD0, [0x58])))
        q((0.0, lambda s: self._wr_rd(s, 0x76, 0x88, self._calib_bytes())))
        q((0.0, lambda s: self._write(s, 0x76, [0xF5, 0xA0])))
        q((0.0, lambda s: self._write(s, 0x76, [0xF4, 0x27])))
        q(("+0.01", None))                          # sleep_ms(10) in bmp_init()
        for cmd in OLED_INIT:                       # oled_init(): 0xAE first, one command per transfer
            q((0.0, lambda s, c=cmd: self._write(s, 0x3C, [0x00, c])))
        q(("loop", None))                           # main loop timers start here

    def _queue_display(self):
        q = self.plan.append
        sim_s = self.loop_index
        self.loop_index += 1
        wall = self.rtc_epoch + _dt.timedelta(seconds=sim_s)
        # read_rtc(): ONE combined read of 0x00..0x0F (time + control 0x0E + status 0x0F, OSF=0)
        rtc = [to_bcd(wall.second), to_bcd(wall.minute), to_bcd(wall.hour), (wall.isoweekday() % 7) + 1,
               to_bcd(wall.day), to_bcd(wall.month), to_bcd(wall.year % 100)] + [0] * 7 + [0x1C, 0x08]
        q((0.0, lambda s: self._wr_rd(s, 0x68, 0x00, rtc)))
        temp, press = self._compensate(self.last_raw) if self.last_raw else (None, None)
        lines = [(0, "I2C MONITOR"), (2, f"TIME {wall:%H:%M:%S}"), (3, f"DATE {wall:%d/%m/%Y}")]
        lines += ([(5, f"TEMP {temp:.2f} C"), (6, f"PRESS {press:.2f} HPA")] if temp is not None
                  else [(5, "BMP ERROR")])
        fb = self._framebuffer(lines)
        for cmd in OLED_WINDOW:
            q((0.0, lambda s, c=cmd: self._write(s, 0x3C, [0x00, c])))
        for i in range(0, 1024, 16):
            q((0.0, lambda s, chunk=fb[i:i + 16]: self._write(s, 0x3C, [0x40] + chunk)))
        if self.inject_absent_probe and self.loop_index % 7 == 0:
            # Explicit, opt-in fault injection: an absent address is NACKed.
            q((0.0, lambda s: self._write(s, 0x2A, [], nack_addr=True)))

    def _queue_loop(self):
        """One pass of the firmware main loop at the current bus time."""
        now = self.bus_free * TICK_NS / 1e9
        due = False
        if now >= self.next_bmp:
            self.next_bmp += self.BMP_PERIOD_S
            raw = self._bmp_raw(now)
            self.last_raw = raw
            self.plan.append((0.0, lambda s, r=raw: self._wr_rd(s, 0x76, 0xF7, r)))
            due = True
        if now >= self.next_identity:
            self.next_identity += self.IDENTITY_PERIOD_S
            self._queue_identity()
            due = True
        if now >= self.next_display:
            self.next_display += self.DISPLAY_PERIOD_S
            self._queue_display()
            due = True
        if not due:                                 # sleep until the next timer is reached
            nxt = min(self.next_bmp, self.next_identity, self.next_display)
            self.plan.append((f"+{max(0.001, nxt - now):.9f}", None))

    def lines_until(self, now_s: float) -> list[bytes]:
        edges: list = []
        if self.bench:
            return self._bench_lines_until(now_s)
        if not self.booted:
            self.booted = True
            edges.append((0, 3))          # initial line state: bus idle (firmware sends this too)
            self._queue_boot()
        while True:
            if not self.plan:
                self._queue_loop()
            at, build = self.plan[0]
            if build is None:                       # delay step or loop start marker
                self.plan.pop(0)
                if at == "loop":
                    t = self.bus_free * TICK_NS / 1e9
                    self.next_bmp = self.next_display = t
                    self.next_identity = t + self.IDENTITY_PERIOD_S
                else:
                    self.bus_free += int(float(at[1:]) * 1e9 / TICK_NS)
                continue
            start_tick = max(int(at * 1e9 / TICK_NS), self.bus_free + 1)
            if start_tick * TICK_NS / 1e9 > now_s:
                break
            self.plan.pop(0)
            edges += self._txn(at, build)
        return self._emit(edges, now_s)

    def _bench_lines_until(self, now_s):
        b = self.bench
        edges: list = []
        if not self.booted:
            self.booted = True
            edges.append((0, 3))          # initial line state: bus idle
        now_ns = now_s * 1e9
        while b.next_start_ns() <= now_ns:
            edges += b.step()
        for c in b.ctl.values():
            c.stats_until(now_ns)
        return self._emit(edges, now_s)

    def _emit(self, edges, now_s):
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
    """GUI demo stream.  Default: the hardware-grounded two-controller bench (masters=2), whose SIMULATED controller
    telemetry is emitted per master on master_lines_ready so multi-master attribution runs exactly as on hardware."""
    lines_ready = pyqtSignal(list)
    master_lines_ready = pyqtSignal(int, list)
    status = pyqtSignal(str)

    # Demo mode runs the turn-taking bench profile: the firmware flashed on the bench since 2026-11-17 (I2C.c 2.2-turns).
    DEFAULT_SCHEDULE = "turns"

    def __init__(self, parent=None, scl_hz=100_000, masters=2, seed=None, inject_absent_probe=False, schedule=None):
        super().__init__(parent)
        self._run = True
        schedule = schedule or (self.DEFAULT_SCHEDULE if masters == 2 else "free")
        self.gen = DemoGenerator(seed=seed, scl_hz=scl_hz, masters=masters, inject_absent_probe=inject_absent_probe,
                                 schedule=schedule)

    def request_scl(self, hz):
        """SIMULATED controller SCL change; applied by the generator between transactions."""
        self.gen.request_scl(hz)

    def stop(self):
        self._run = False

    def run(self):
        gen = self.gen
        self.lines_ready.emit([gen.hello()])
        t0 = time.monotonic()
        while self._run:
            lines = gen.lines_until(time.monotonic() - t0)
            for mid, mlines in gen.take_master_lines().items():   # controller reports first, like USB arrival order
                self.master_lines_ready.emit(mid, mlines)
            if lines:
                self.lines_ready.emit(lines)
            self.msleep(50)
