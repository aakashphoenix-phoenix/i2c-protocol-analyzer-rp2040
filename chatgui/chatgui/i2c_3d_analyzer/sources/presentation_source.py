"""PRESENTATION MODE (SIMULATED): a scripted, strictly sequential I²C review sequence.

One deterministic bus state machine - no threads, no timers of its own.  The GUI asks for the next transfer only after
the previous one has been shown completely (every phase, every dot, STOP); the bus then synthesises exactly that ONE
transfer as analyzer records (the same JSON-lines the analyzer firmware sends), so it goes through the normal parser,
decoder, Packet Analyzer, Digital Waveform and 3D view like any capture.  Everything it produces is labelled SIMULATED.

Bus timing = the MEASURED RP2040 bench (sources/demo_source.BENCH_TIMING, work/measurements/MM_RESULTS_20261115.md):
SCL HIGH 4.44 µs / LOW 6.13 µs -> ~94.6 kHz with the natural per-bit jitter, never an idealised 100.000 kHz square
wave; every transfer draws its own jitter and its own bus-idle gap (BENCH_GAP_US), so no two packets are clones.

Traffic: every transfer of the sequence is ONE uniform, concise I²C transfer with exactly 2 data bytes and 9 stages
    START · ADDRESS · R/W · ACK · DATA 1 · ACK · DATA 2 · ACK/NACK · STOP
so every pair takes the same ~27 s at 0.25x and nothing is fast-forwarded:
  * WRITE ("W", transmission, master -> target): the master sends both bytes, the target ACKs each one
  * READ  ("R", reception, target -> master): a plain read (address + R=1) from the target's current register
           pointer; the target sends both bytes, the master ACKs the first and NACKs the last (normal end of read)
Targets: DS3231 RTC 0x68 (BCD time / control / status registers) and BMP280 0x76 (ctrl_meas / config, temperature and
pressure ADC registers), each WRITTEN then READ; the SSD1306 OLED 0x3C is WRITE ONLY (on I2C it is only ever written):
a command transfer [0x00, cmd] and a display-data transfer [0x40, column].
Order of one cycle: Master 1 (Pico 1) with the RTC, the BMP280 and the OLED, then Master 2 (Pico 3) with the same three,
so BOTH masters transmit to the OLED 0x3C.
The 2-byte reads cannot carry a whole time / ADC record, so engine.sensors is seeded with the full RTC / BMP280
register files (seed_sensors) and Sensor Monitoring still decodes time, temperature and pressure (SIMULATED).
The first transfer of a cycle is contended: both masters START together and arbitrate bit by bit (UM10204 3.1.8);
Master 2 loses, waits for the STOP and gets the bus when Master 1's batch is done.
`inject_nack` (one-shot) makes the next target NACK its address, so ACK and NACK can both be demonstrated.

NOTE: on the real bench Master 2's firmware never drives the OLED (I2C.c DRIVES_OLED 0); this scripted sequence shows
the protocol for every master/target pair for the review and is never presented as hardware traffic.
"""
from __future__ import annotations

import json
import random

from sources.demo_source import BENCH_GAP_US, BENCH_TIMING, CALIB, FONT, TICK_NS, DemoGenerator, EdgeSynth

STAT_KEYS = ("txn_ok", "addr_nack", "data_nack", "arb_lost", "retries", "gave_up", "busy_waits", "busy_timeouts",
             "timeouts", "other_aborts", "recoveries", "recovery_skipped", "controller_resets")
CONTROLLERS = {1: dict(name="Master 1", board="pico", backoff_us=150), 2: dict(name="Master 2", board="pico_w",
                                                                              backoff_us=230)}

RTC, BMP, OLED = 0x68, 0x76, 0x3C
NAMES = {RTC: "DS3231 RTC", BMP: "BMP280", OLED: "SSD1306 OLED"}
def _glyph(ch):
    """The 5 display columns of one character of the controller firmware's font (I2C.c font5)."""
    hx = FONT[ch]
    return [int(hx[i:i + 2], 16) for i in range(0, 10, 2)]


def _bcd(v):
    return ((v // 10) << 4) | (v % 10)


# The SIMULATED sensor register files the sequence reads from (seeded into engine.sensors, see seed_sensors):
# DS3231 0x00..0x06 = 08:22:30, Monday 16-11-2026 (BCD), control 0x0E = 0x1C, status 0x0F = 0x08, aging 0x10,
# temperature 0x11 / 0x12 = 25.25 °C; BMP280 raw ADC values around the bench readings (~25 °C / ~1004 hPa).
RTC_REGS = {0x00: 0x30, 0x01: 0x22, 0x02: 0x08, 0x03: 0x02, 0x04: 0x16, 0x05: 0x11, 0x06: 0x26,
            0x0E: 0x1C, 0x0F: 0x08, 0x10: 0x00, 0x11: 0x19, 0x12: 0x40}
BMP_PRESS_RAW, BMP_TEMP_RAW = 415148, 519888
BMP_REGS = {0xD0: 0x58, 0xF4: 0x27, 0xF5: 0xA0,
            0xF7: BMP_PRESS_RAW >> 12 & 0xFF, 0xF8: BMP_PRESS_RAW >> 4 & 0xFF, 0xF9: (BMP_PRESS_RAW & 0xF) << 4,
            0xFA: BMP_TEMP_RAW >> 12 & 0xFF, 0xFB: BMP_TEMP_RAW >> 4 & 0xFF, 0xFC: (BMP_TEMP_RAW & 0xF) << 4}
for _i, _k in enumerate(["T1", "T2", "T3", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]):
    BMP_REGS[0x88 + 2 * _i], BMP_REGS[0x89 + 2 * _i] = CALIB[_k] & 0xFF, (CALIB[_k] & 0xFFFF) >> 8

# (master, target, op, data bytes on the wire, what it means) - every transfer exactly 2 data bytes / 9 stages
#   op 'W': transmission, master -> target (the bytes the master writes; the OLED is only ever written)
#   op 'R': reception, target -> master (the bytes the target returns; the master ACKs byte 1, NACKs byte 2)
SCRIPT = (
    (1, RTC, "W", [0x00, 0x30], "set the register pointer to 0x00 and write seconds = 30 (BCD)"),
    (1, RTC, "R", [0x22, 0x08], "read from the auto-incremented pointer 0x01: minutes = 22, hours = 08 (BCD)"),
    (1, BMP, "W", [0xF4, 0x27], "write ctrl_meas 0xF4 = 0x27 (normal mode, x1 temperature / pressure)"),
    (1, BMP, "R", [BMP_REGS[0xFA], BMP_REGS[0xFB]], "read temp_msb / temp_lsb 0xFA..0xFB (temperature ADC)"),
    (1, OLED, "W", [0x00, 0xAF], "command stream 0x00: 0xAF = display ON"),
    (1, OLED, "W", [0x40, _glyph("0")[0]], "display data 0x40: the first column of the glyph '0'"),
    (2, RTC, "W", [0x0E, 0x1C], "write the control register 0x0E = 0x1C (INTCN, square wave off)"),
    (2, RTC, "R", [0x08, 0x00], "read from the pointer 0x0F: status = 0x08 (EN32kHz, oscillator OK), aging = 0x00"),
    (2, BMP, "W", [0xF5, 0xA0], "write config 0xF5 = 0xA0 (standby 1 s, IIR filter off)"),
    (2, BMP, "R", [BMP_REGS[0xF7], BMP_REGS[0xF8]], "read press_msb / press_lsb 0xF7..0xF8 (pressure ADC)"),
    (2, OLED, "W", [0x00, 0xA6], "command stream 0x00: 0xA6 = normal (not inverted) display"),
    (2, OLED, "W", [0x40, _glyph("P")[0]], "display data 0x40: the first column of the glyph 'P'"),
)
# register a READ starts at (the target's pointer, left there by the firmware's preceding pointer write)
READ_FROM = {(1, RTC): 0x01, (1, BMP): 0xFA, (2, RTC): 0x0F, (2, BMP): 0xF7}


class PresentationBus:
    """The single bus scheduler of the review presentation: a queue of transfers released strictly one at a time."""

    def __init__(self, scl_hz=100_000, seed=7):
        self.seed = seed
        self.syn = EdgeSynth(random.Random(seed), scl_hz, table=BENCH_TIMING)     # measured bench timing
        self.seq = 0
        self.index = 0           # position in SCRIPT
        self.cycle = 0
        self.inject_nack = False
        self.started = False
        self.released = []       # meta of every transfer handed out (tests / report)
        # SIMULATED controller telemetry of both masters (what I2C.c would print on each master's USB port)
        self.ctl_seq = {mid: 0 for mid in CONTROLLERS}
        self.ctl_stats = {mid: {k: 0 for k in STAT_KEYS} for mid in CONTROLLERS}
        self.ctl_retry = {mid: 0 for mid in CONTROLLERS}     # arbitration losses the next transfer of a master won
        self.ctl_t_us = 0
        self._beat_wall = None

    # ------------------------------------------------------------------ SIMULATED controller telemetry
    def _ctl(self, mid, d):
        return mid, {**d, "source": "controller", "master_id": mid, "simulated": True}

    def _stats(self, mid):
        return self._ctl(mid, {"type": "master_stats", "t_us": self.ctl_t_us, **self.ctl_stats[mid], "txlog": True})

    def controller_start(self):
        """Records the two simulated controllers send when the sequence starts: hello, transfer log ON, their clock
        mapping onto the capture clock (the bus is synthesised on it, so offset 0) and a first counter snapshot."""
        out = []
        for mid, c in CONTROLLERS.items():
            out.append(self._ctl(mid, {"type": "hello", "role": "controller", "name": c["name"], "board": c["board"],
                                       "fw": "PRESENTATION controller (SIMULATED)",
                                       "fw_version": "2.0-presentation (SIMULATED)", "sda_pin": 4, "scl_pin": 5,
                                       "scl_hz": self.syn.scl_hz, "drives_oled": True, "txlog": True}))
            out.append(self._ctl(mid, {"type": "txlog", "enabled": True}))
            out.append(self._ctl(mid, {"type": "master_clock", "offset_ns": 0.0}))
            out.append(self._stats(mid))
        self._beat_wall = None
        return out

    def controller_heartbeat(self, wall):
        """Counter snapshots of both masters about once a second (I2C.c master_stats), so their health stays fresh
        between their transfers, which in a slow presentation can be minutes apart."""
        if self._beat_wall is not None and wall - self._beat_wall < 1.0:
            return []
        self._beat_wall = wall
        return [self._stats(mid) for mid in CONTROLLERS]

    def _controller_records(self, mid, addr, op, wbytes, rdata, nack, arbitration, edges):
        """What the master reports about the transfer it just made (I2C.c report_txn / arb_lost / master_stats)."""
        start = next((tk for tk, st in edges if st == 2), edges[0][0] if edges else 0)   # SDA fell with SCL HIGH
        end = edges[-1][0] if edges else start
        t_us = int(start * TICK_NS / 1000)
        self.ctl_t_us = max(self.ctl_t_us, int(end * TICK_NS / 1000))
        out = []
        if arbitration:
            loser = arbitration["loser"]
            out.append(self._ctl(loser, {"type": "arb_lost", "t_us": t_us, "addr": addr, "attempt": 1,
                                         "byte": 1, "abort_source": 1 << 12,
                                         "backoff_us": CONTROLLERS[loser]["backoff_us"], "retry": True}))
            self.ctl_stats[loser]["arb_lost"] += 1
            self.ctl_retry[loser] += 1
            out.append(self._stats(loser))
        lost = self.ctl_retry[mid]
        self.ctl_retry[mid] = 0
        self.ctl_stats[mid]["retries"] += lost
        self.ctl_stats[mid]["addr_nack" if nack else "txn_ok"] += 1
        self.ctl_seq[mid] += 1
        out.append(self._ctl(mid, {"type": "txn", "seq": self.ctl_seq[mid], "t_us": t_us,
                                   "dur_us": max(1, int((end - start) * TICK_NS / 1000)), "addr": addr,
                                   "wlen": 0 if nack else len(wbytes), "rlen": 0 if nack else len(rdata),
                                   "b0": wbytes[0] if wbytes else -1, "result": "addr_nack" if nack else "ok",
                                   "attempts": 1 + lost, "arb_lost": lost}))
        out.append(self._stats(mid))
        return out

    def hello(self) -> bytes:
        return json.dumps({"t": "hello", "proto": 1, "fw": "PRESENTATION sequence (SIMULATED)", "board": "simulated",
                           "sample_hz": int(1e9 / TICK_NS), "tick_ns": TICK_NS,
                           "caps": ["edges", "start_stop", "seq", "stats"]}).encode()

    # ------------------------------------------------------------------ the queue
    def peek(self):
        mid, addr, op, data, what = SCRIPT[self.index]
        return dict(master=mid, addr=addr, op=op, what=what)

    # ------------------------------------------------------------------ SIMULATED sensor registers
    @staticmethod
    def seed_sensors(sensors, meta=None):
        """Seed engine.sensors (analysis.sensor_decoder.SensorDecoder) with the full SIMULATED RTC / BMP280 register
        files, so Sensor Monitoring decodes time, temperature and pressure although every scripted read carries only 2
        bytes.  meta: the transfer about to be ingested - a READ gets the target's register pointer it starts at."""
        sensors.regs.setdefault(RTC, {}).update(RTC_REGS)
        sensors.regs.setdefault(BMP, {}).update(BMP_REGS)
        if meta is not None and meta["op"] == "R" and (meta["master"], meta["addr"]) in READ_FROM:
            sensors.pointer[meta["addr"]] = READ_FROM[(meta["master"], meta["addr"])]

    @staticmethod
    def decode_sensors(sensors, t_ns, txn=None):
        """The values Sensor Monitoring shows, computed from the seeded register files (SIMULATED calibration words)."""
        from analysis.sensor_decoder import Reading, bcd, bmp280_compensate
        r = sensors.regs.get(BMP, {})
        if not sensors.bmp.calib:
            sensors.bmp.calib, sensors.bmp.calib_simulated = dict(CALIB), True
        adc_t = (r[0xFA] << 12) | (r[0xFB] << 4) | (r[0xFC] >> 4)
        adc_p = (r[0xF7] << 12) | (r[0xF8] << 4) | (r[0xF9] >> 4)
        temp, press = bmp280_compensate(sensors.bmp.calib, adc_t, adc_p)
        raw = " ".join(f"{r[k]:02X}" for k in range(0xF7, 0xFD))
        sensors.bmp.temperature = Reading(t_ns, temp, raw)
        sensors.bmp.pressure = Reading(t_ns, press, raw)
        sensors.bmp.reason = ""
        sensors.bmp.history.append((t_ns, temp, press))
        rt = sensors.regs.get(RTC, {})
        s, m, h, wd, d, mo, y = (rt[i] for i in range(7))
        if sensors.rtc.time is None or sensors.rtc.time.t_ns < t_ns:
            sensors.rtc.time = Reading(t_ns, (bcd(h & 0x3F), bcd(m & 0x7F), bcd(s & 0x7F), wd, bcd(d & 0x3F),
                                              bcd(mo & 0x1F), 2000 + bcd(y)), " ".join(f"{rt[i]:02X}" for i in range(7)))
            sensors.rtc.reason = ""
        if txn is not None and txn.address == BMP and not getattr(txn, "semantic", ""):
            txn.semantic = f"Read BMP280: {temp:.2f} °C / {press:.2f} hPa (SIMULATED registers)"

    def next_transfer(self):
        """Synthesise the next transfer of the script.  Returns (analyzer record lines, meta)."""
        mid, addr, op, data, what = SCRIPT[self.index]
        # every transfer draws its own jitter stream: consecutive packets never share cloned timestamps
        rng = random.Random(f"{self.seed}/{self.cycle}/{self.index}")
        self.syn.rng = rng
        wbytes = list(data) if op == "W" else []
        rdata = list(data) if op == "R" else []
        nack = self.inject_nack
        self.inject_nack = False                          # one-shot
        arbitration = None
        if self.index == 0:
            # both masters saw an idle bus and STARTed together; the address bytes are identical (0x68 W), the first
            # data byte differs: Master 1 sends 0x00, Master 2 0x0E -> at data bit 3 Master 2 sends 1 but reads 0 (the
            # wired-AND bus) and stops driving: Master 2 lost arbitration and waits for the STOP
            other = SCRIPT[6][3][0]
            k = next(b for b in range(7, -1, -1) if ((wbytes[0] >> b) & 1) != ((other >> b) & 1))
            arbitration = dict(winner=1, loser=2, byte="data byte 1", bit=k,
                               text=(f"Master 1 and Master 2 (Pico 3) both STARTed on the idle bus. Both sent address "
                                     f"0x68 + W; at data bit {k} Master 2 sent 1 but SDA read 0, so Master 2 lost "
                                     f"arbitration and waits until the STOP (it is not a NACK)."))
        s = self.syn
        lines = []
        if not self.started:
            self.started = True
            s.t = 1000
            lines += self._edges([(0, 3)])
        mu, sd = BENCH_GAP_US["default"]                  # measured bus-idle gap between two transfers on the bench
        s.t += int(max(0.75 * mu, rng.gauss(mu, sd)) * 1000 / TICK_NS)
        s.out = []
        if nack:                                          # the target does not answer its address: START, ADDR, NACK, STOP
            s.start()
            s.byte(addr << 1, ack=False)
            s.stop()
        elif op == "R":                                   # plain read: target -> master, master ACKs then NACKs
            s.start()
            s.byte((addr << 1) | 1)
            for i, v in enumerate(rdata):
                s.byte(v, ack=i < len(rdata) - 1)
            s.stop()
        else:
            DemoGenerator._write(s, addr, wbytes)
        s.wait(10)
        edges = list(s.out)
        lines += self._edges(s.out)
        kind = "READ (reception: target -> master)" if op == "R" else "WRITE (transmission: master -> target)"
        notes = [f"PRESENTATION (SIMULATED, bench timing): {NAMES[addr]} {kind} by "
                 f"{'Master 1 (Pico 1)' if mid == 1 else 'Master 2 (Pico 3)'}: {what}"]
        if nack:
            notes.append("PRESENTATION: NACK injected on request: the target did not acknowledge its address, the "
                         "master ends the transfer with STOP")
        if arbitration:
            notes.append("PRESENTATION: arbitration - " + arbitration["text"])
        meta = dict(master=mid, addr=addr, op=op, write=wbytes, read=rdata, data=wbytes + rdata, what=what, nack=nack,
                    arbitration=arbitration, cycle=self.cycle, index=self.index, notes=notes,
                    controller=self._controller_records(mid, addr, op, wbytes, rdata, nack, arbitration, edges))
        self.released.append(meta)
        self.index += 1
        if self.index >= len(SCRIPT):
            self.index = 0
            self.cycle += 1
        return lines, meta

    def _edges(self, edges):
        if not edges:
            return []
        out, prev, e = [], edges[0][0], []
        for tk, st in edges:
            e.append([tk - prev, st])
            prev = tk
        out.append(json.dumps({"t": "edges", "seq": self.seq, "t0": edges[0][0], "e": e},
                              separators=(",", ":")).encode())
        self.seq += 1
        return out
