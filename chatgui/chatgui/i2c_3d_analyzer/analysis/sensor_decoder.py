"""Interprets decoded I²C traffic for the known devices on this bench.

Only values that can be computed from bytes actually observed on the bus are
produced. Anything missing (for example BMP280 calibration, which the master
reads once at boot) is reported as a reason instead of a value.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class Reading:
    t_ns: float
    value: object
    raw: str = ""


@dataclass
class BMP280State:
    calib: dict = field(default_factory=dict)
    calib_simulated: bool = False   # the standard SIMULATED calibration (demo only), not words captured on this bus
    temperature: Reading | None = None
    pressure: Reading | None = None
    reason: str = "Awaiting valid sensor data"
    history: list = field(default_factory=list)    # (t_ns, temp_c, press_hpa)


@dataclass
class DS3231State:
    time: Reading | None = None     # value = (h, m, s, weekday, day, month, year)
    reason: str = "Awaiting valid RTC read (registers 0x00–0x06)"
    reads: int = 0
    osf_t: float | None = None      # capture time of a read whose status OSF bit was set


@dataclass
class OLEDState:
    writes: int = 0
    cmd_bytes: int = 0
    data_bytes: int = 0
    nacks: int = 0
    last_t: float | None = None
    display_on: bool | None = None
    fb: bytearray = field(default_factory=lambda: bytearray(128 * 8))
    written: bytearray = field(default_factory=lambda: bytearray(128 * 8))
    init_seen: bool = False
    seg_remap: bool = True
    com_flip: bool = True
    inverted: bool = False
    mode: int = 2                   # 0 horiz, 1 vert, 2 page
    col0: int = 0
    col1: int = 127
    page0: int = 0
    page1: int = 7
    col: int = 0
    page: int = 0
    pending: list = field(default_factory=list)  # [cmd, args_needed, args]
    frame_counter: int = 0

    @property
    def coverage(self) -> float:
        return sum(self.written) / len(self.written)


CMD_ARGS = {0x20: 1, 0x21: 2, 0x22: 2, 0x81: 1, 0x8D: 1, 0xA8: 1, 0xD3: 1, 0xD5: 1,
            0xD9: 1, 0xDA: 1, 0xDB: 1, 0xA3: 2, 0x26: 6, 0x27: 6, 0x29: 5, 0x2A: 5}


def bcd(v: int) -> int:
    return (v >> 4) * 10 + (v & 0x0F)


def bmp280_compensate(calib: dict, adc_t: int, adc_p: int):
    """Bosch BMP280 datasheet floating-point compensation. Returns (°C, hPa or None)."""
    T1, T2, T3 = calib["T1"], calib["T2"], calib["T3"]
    var1 = (adc_t / 16384.0 - T1 / 1024.0) * T2
    var2 = ((adc_t / 131072.0 - T1 / 8192.0) ** 2) * T3
    t_fine = var1 + var2
    temp = t_fine / 5120.0
    if adc_p is None:
        return temp, None
    P = [None] + [calib[f"P{i}"] for i in range(1, 10)]
    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * P[6] / 32768.0
    var2 = var2 + var1 * P[5] * 2.0
    var2 = var2 / 4.0 + P[4] * 65536.0
    var1 = (P[3] * var1 * var1 / 524288.0 + P[2] * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * P[1]
    if var1 == 0:
        return temp, None
    p = 1048576.0 - adc_p
    p = (p - var2 / 4096.0) * 6250.0 / var1
    var1 = P[9] * p * p / 2147483648.0
    var2 = p * P[8] / 32768.0
    p = p + (var1 + var2 + P[7]) / 16.0
    return temp, p / 100.0


def parse_bmp280_calib(regs: dict):
    if not all((0x88 + i) in regs for i in range(24)):
        return None
    raw = bytes(regs[0x88 + i] for i in range(24))
    vals = struct.unpack("<HhhHhhhhhhhh", raw)
    keys = ["T1", "T2", "T3", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
    c = dict(zip(keys, vals))
    if c["T1"] == 0 or c["P1"] == 0:
        return None
    return c


class SensorDecoder:
    REGISTER_DEVICES = {0x68, 0x76, 0x77}

    def __init__(self, registry, oled_addr=0x3C):
        self.registry = registry
        self.oled_addr = oled_addr
        self.regs: dict[int, dict[int, int]] = {}
        self.pointer: dict[int, int] = {}
        self.bmp = BMP280State()
        self.rtc = DS3231State()
        self.oled = OLEDState()
        # set by presentation mode only: its scripted BMP280 reads skip the 0x88-0x9F calibration read, so the
        # simulated sensor's own (standard) words decode them, labelled SIMULATED.  Never set for demo or live.
        self.simulated_calib = getattr(self, "simulated_calib", False)

    def clear(self):
        self.__init__(self.registry, self.oled_addr)

    # -------------------------------------------------------------- dispatch
    def observe(self, txn) -> list:
        notices = []
        txn.semantic = ""
        if txn.status not in ("ok",):
            return notices
        oled_before = (self.oled.cmd_bytes, self.oled.data_bytes, self.oled.frame_counter)
        for f in txn.frames:
            if f.addr == self.oled_addr:
                self._oled(f, txn)
        if any(f.addr == self.oled_addr for f in txn.frames):
            txn.semantic = self._oled_semantic(oled_before)
        addr = txn.address
        if addr not in self.REGISTER_DEVICES:
            return notices
        touched = self._register_transfer(addr, txn)
        for reg, val in touched.get("read", {}).items():
            name = self.registry.confirm(addr, reg, val)
            if name:
                notices.append((txn.t_start, "info", "Device confirmed",
                                f"0x{addr:02X} identified as {name} (reg 0x{reg:02X} = 0x{val:02X})", addr))
        if addr in (0x76, 0x77):
            prev = self.bmp.temperature
            self._bmp280(addr, touched, txn)
            if self.bmp.temperature is not prev:
                self.bmp.reason = ""
            t, p = self.bmp.temperature, self.bmp.pressure
            if t is not None and t is not prev and t.t_ns == txn.t_start:
                txn.semantic = f"Read BMP280: {t.value:.2f} °C" + (
                    f" / {p.value:.2f} hPa" if p is not None and p.t_ns == txn.t_start else "")
        elif addr == 0x68:
            prev = self.rtc.time
            self._ds3231(touched, txn)
            r = self.rtc.time
            if r is not None and r is not prev and r.t_ns == txn.t_start:
                h, m, s = r.value[:3]
                txn.semantic = f"Read RTC: {h:02d}:{m:02d}:{s:02d}"
        return notices

    def _oled_semantic(self, before):
        """Text for the SSD1306 bytes the packet just wrote ('' when the address was NACKed / nothing was written)."""
        o = self.oled
        cmds, data = o.cmd_bytes - before[0], o.data_bytes - before[1]
        if data and o.frame_counter != before[2]:
            return f"OLED Frame Write: {data} data bytes"
        if data:
            return f"OLED Data Write: {data} bytes" + (f" + {cmds} cmd" if cmds else "")
        if cmds:
            return f"OLED Cmd Write: {cmds} byte" + ("s" if cmds != 1 else "")
        return ""

    def _register_transfer(self, addr, txn):
        regs = self.regs.setdefault(addr, {})
        touched = {"read": {}, "write": {}}
        for f in txn.frames:
            if f.addr != addr or f.addr_ack is False:
                continue
            if f.rw == "W" and f.data:
                ptr = f.data[0]
                for i, b in enumerate(f.data[1:]):
                    regs[(ptr + i) & 0xFF] = b
                    touched["write"][(ptr + i) & 0xFF] = b
                self.pointer[addr] = (ptr + len(f.data) - 1) & 0xFF
            elif f.rw == "R" and addr in self.pointer:
                ptr = self.pointer[addr]
                for i, b in enumerate(f.data):
                    regs[(ptr + i) & 0xFF] = b
                    touched["read"][(ptr + i) & 0xFF] = b
                self.pointer[addr] = (ptr + len(f.data)) & 0xFF
        return touched

    # -------------------------------------------------------------- BMP280
    def _bmp280(self, addr, touched, txn):
        regs = self.regs[addr]
        rd = touched["read"]
        if not self.bmp.calib or self.bmp.calib_simulated:
            c = parse_bmp280_calib(regs)
            if c:                                           # captured words always win over the simulated set
                self.bmp.calib, self.bmp.calib_simulated = c, False
        has_t = all(r in rd for r in (0xFA, 0xFB, 0xFC))
        has_p = all(r in rd for r in (0xF7, 0xF8, 0xF9))
        if not has_t:
            return
        if not self.bmp.calib and self.simulated_calib and getattr(txn, "demo", False):
            # SIMULATED presentation records only: their raw ADC values were generated with the standard
            # simulated calibration words (sources.demo_source.CALIB), so those are the words that decode them.  A live
            # capture never takes this path: it waits for the real 0x88-0x9F read.
            from sources.demo_source import CALIB
            self.bmp.calib, self.bmp.calib_simulated = dict(CALIB), True
        if not self.bmp.calib:
            self.bmp.reason = ("Raw data seen, but calibration (0x88–0x9F) was not captured. "
                               "Start capture, then reset the master so it re-reads calibration.")
            return
        adc_t = (rd[0xFA] << 12) | (rd[0xFB] << 4) | (rd[0xFC] >> 4)
        adc_p = ((rd[0xF7] << 12) | (rd[0xF8] << 4) | (rd[0xF9] >> 4)) if has_p else None
        if adc_t == 0x80000 or adc_p == 0x80000:
            self.bmp.reason = "Sensor returned reset value 0x80000 (measurement skipped)"
            return
        temp, press = bmp280_compensate(self.bmp.calib, adc_t, adc_p)
        if not -40 <= temp <= 85 or (press is not None and not 300 <= press <= 1100):
            self.bmp.reason = "Decoded value outside sensor range — ignored"
            return
        raw = " ".join(f"{rd[r]:02X}" for r in range(0xF7, 0xFD) if r in rd)
        self.bmp.temperature = Reading(txn.t_start, temp, raw)
        if press is not None:
            self.bmp.pressure = Reading(txn.t_start, press, raw)
        self.bmp.history.append((txn.t_start, temp, press))
        if len(self.bmp.history) > 3000:
            del self.bmp.history[:1000]

    # -------------------------------------------------------------- DS3231
    def _ds3231(self, touched, txn):
        """Decode the time when a READ touched the time registers 0x00..0x06 and all seven are known - read in this
        transfer, or accumulated from earlier reads / the master's own time-set write of those registers."""
        rd = dict(touched["read"])
        if not any(r in rd for r in range(7)):
            return
        regs = self.regs.get(0x68, {})
        if not all(r in rd or r in regs for r in range(7)):
            return
        for r in range(7):
            rd.setdefault(r, regs[r])
        if 0x0F in rd and rd[0x0F] & 0x80:
            # DS3231 status register: OSF=1 means the oscillator stopped and the
            # time registers are not trustworthy until the time is set again.
            self.rtc.time = None
            self.rtc.osf_t = txn.t_start
            self.rtc.reason = "DS3231 oscillator-stop flag (OSF) set in status 0x0F — time invalid until set"
            return
        s, m, h, wd, d, mo, y = (rd[i] for i in range(7))
        sec, mn = bcd(s & 0x7F), bcd(m & 0x7F)
        if h & 0x40:   # 12 h mode
            hr = bcd(h & 0x1F) % 12 + (12 if h & 0x20 else 0)
        else:
            hr = bcd(h & 0x3F)
        day, month, year = bcd(d & 0x3F), bcd(mo & 0x1F), 2000 + bcd(y) + (100 if mo & 0x80 else 0)
        if not (sec < 60 and mn < 60 and hr < 24 and 1 <= wd <= 7 and 1 <= day <= 31 and 1 <= month <= 12):
            self.rtc.reason = "RTC bytes failed BCD range check — ignored"
            return
        self.rtc.reads += 1
        self.rtc.time = Reading(txn.t_start, (hr, mn, sec, wd, day, month, year),
                                " ".join(f"{rd[i]:02X}" for i in range(7)))

    # -------------------------------------------------------------- SSD1306
    def _oled(self, f, txn):
        o = self.oled
        o.last_t = txn.t_start
        if f.addr_ack is False:
            o.nacks += 1
            return
        if f.rw != "W":
            return
        o.writes += 1
        data = f.data
        i = 0
        while i < len(data):
            ctrl = data[i]
            i += 1
            co, dc = ctrl & 0x80, ctrl & 0x40
            chunk = data[i:i + 1] if co else data[i:]
            i += len(chunk)
            for b in chunk:
                if dc:
                    o.data_bytes += 1
                    self._oled_data(b)
                else:
                    o.cmd_bytes += 1
                    self._oled_cmd(b)

    def _oled_cmd(self, b):
        o = self.oled
        if o.pending:
            o.pending[2].append(b)
            if len(o.pending[2]) >= o.pending[1]:
                cmd, _, a = o.pending
                o.pending = []
                if cmd == 0x20:
                    o.mode = a[0] & 3
                elif cmd == 0x21:
                    o.col0, o.col1 = a[0] & 127, a[1] & 127
                    o.col = o.col0
                elif cmd == 0x22:
                    o.page0, o.page1 = a[0] & 7, a[1] & 7
                    o.page = o.page0
            return
        if b in CMD_ARGS:
            o.pending = [b, CMD_ARGS[b], []]
            if b == 0xA8 or b == 0xD5:
                o.init_seen = True
        elif b in (0xAE, 0xAF):
            o.display_on = b == 0xAF
        elif b in (0xA0, 0xA1):
            o.seg_remap = b == 0xA1
        elif b in (0xC0, 0xC8):
            o.com_flip = b == 0xC8
        elif b in (0xA6, 0xA7):
            o.inverted = b == 0xA7
        elif 0xB0 <= b <= 0xB7:
            o.page = b & 7
        elif b <= 0x0F:
            o.col = (o.col & 0xF0) | b
        elif 0x10 <= b <= 0x1F:
            o.col = (o.col & 0x0F) | ((b & 0x0F) << 4)

    def _oled_data(self, b):
        o = self.oled
        if o.col > 127 or o.page > 7:
            return
        idx = o.page * 128 + o.col
        o.fb[idx] = b
        o.written[idx] = 1
        if o.mode == 0:
            o.col += 1
            if o.col > o.col1:
                o.col = o.col0
                o.page += 1
                if o.page > o.page1:
                    o.page = o.page0
                    o.frame_counter += 1
        elif o.mode == 1:
            o.page += 1
            if o.page > o.page1:
                o.page = o.page0
                o.col += 1
                if o.col > o.col1:
                    o.col = o.col0
        else:
            o.col = min(o.col + 1, 128)

    def oled_pixels(self):
        """128x64 list of rows (0/1), oriented as seen on a module initialised with A1/C8."""
        o = self.oled
        rows = []
        for y in range(64):
            page, bit = divmod(y, 8)
            row = [(o.fb[page * 128 + x] >> bit) & 1 for x in range(128)]
            if not o.seg_remap:
                row.reverse()
            if o.inverted:
                row = [1 - v for v in row]
            rows.append(row)
        if not o.com_flip:
            rows.reverse()
        return rows
