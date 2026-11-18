"""PACKET PROOF: every piece of evidence for ONE captured packet, built only from captured data.

For a chosen packet this module collects, from the raw SDA/SCL edges still in memory (engine.edges):
  * the edge window itself (timestamps from the analyzer's sample clock)
  * an INDEPENDENT decode of those edges (a fresh I2CDecoder), compared byte by byte with the packet as stored (on the
    RP2040 analyzer that is the firmware's own decode on the Pico, i.e. two decoders on two different CPUs)
  * a bit table: every SCL rising edge, the SDA level sampled there, the bit's role (A6..A0, R/W, D7..D0, ACK) and which
    side drives SDA for it (controller or target)
  * START / repeated START / STOP: SDA edge while SCL is HIGH, checked on the samples
  * timing measured on the edges (f_SCL, t_HIGH, t_LOW, t_HD;STA, t_SU;STA, t_SU;STO, t_BUF) against the NXP UM10204
    limits for the mode the clock is in, judged with the capture resolution (+-1 sample) - digital levels only, never an
    analog rise-time claim
  * ACK / NACK classification (an expected end-of-read NACK vs a fault), as everywhere else in the program
  * who drove it: the master's own transfer report (controller telemetry) matched to this packet, or UNKNOWN
  * what it means: register / command / BCD / ADC decode for the bench devices
  * capture integrity in that window (lost records, overflows, edge-stream gaps, decode disagreements)
Nothing is interpolated or invented: what is not in the capture is reported as UNAVAILABLE.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from analysis.i2c_decoder import I2CDecoder
from analysis.playback import _nack_note

# NXP UM10204 rev. 7, table 10 (minimum times in ns; f_max in Hz)
UM10204 = {
    "Standard-mode": dict(f_max=100_000, t_low=4700, t_high=4000, t_hd_sta=4000, t_su_sta=4700, t_su_sto=4000,
                          t_buf=4700),
    "Fast-mode": dict(f_max=400_000, t_low=1300, t_high=600, t_hd_sta=600, t_su_sta=600, t_su_sto=600, t_buf=1300),
    "Fast-mode Plus": dict(f_max=1_000_000, t_low=500, t_high=260, t_hd_sta=260, t_su_sta=260, t_su_sto=260,
                           t_buf=500),
}
PARAM_NAMES = dict(t_low="t_LOW (SCL low)", t_high="t_HIGH (SCL high)", t_hd_sta="t_HD;STA (START hold)",
                   t_su_sta="t_SU;STA (repeated START setup)", t_su_sto="t_SU;STO (STOP setup)",
                   t_buf="t_BUF (bus free before START)")
WINDOW_PAD_NS = 60_000            # edges kept around the packet (idle level before START, after STOP)

BMP_REGS = {0x88: "calibration dig_T1..dig_P9 (0x88-0x9F)", 0xD0: "chip ID", 0xE0: "soft reset", 0xF3: "status",
            0xE1: "BME280 humidity calibration (0xE1-0xE7)", 0xF2: "ctrl_hum (BME280)", 0xFD: "hum_msb (BME280)",
            0xFE: "hum_lsb (BME280)",
            0xF4: "ctrl_meas", 0xF5: "config", 0xF7: "press_msb", 0xF8: "press_lsb", 0xF9: "press_xlsb",
            0xFA: "temp_msb", 0xFB: "temp_lsb", 0xFC: "temp_xlsb"}
RTC_REGS = {0x00: "seconds", 0x01: "minutes", 0x02: "hours", 0x03: "day of week", 0x04: "date", 0x05: "month",
            0x06: "year", 0x07: "alarm 1 seconds", 0x0E: "control", 0x0F: "control/status", 0x10: "aging offset",
            0x11: "temperature MSB", 0x12: "temperature LSB"}
OLED_CMDS = {0xAE: "display OFF", 0xAF: "display ON", 0xA6: "normal (not inverted) display", 0xA7: "inverted display",
             0xA4: "display follows RAM", 0xA5: "entire display ON", 0x20: "memory addressing mode",
             0x21: "column address range", 0x22: "page address range", 0x81: "contrast", 0x8D: "charge pump",
             0xA8: "multiplex ratio", 0xD3: "display offset", 0xD5: "clock divide", 0xD9: "pre-charge period",
             0xDA: "COM pins", 0xDB: "VCOMH deselect", 0xA0: "segment remap off", 0xA1: "segment remap",
             0xC0: "COM scan normal", 0xC8: "COM scan remapped", 0x2E: "scroll off", 0x40: "display start line 0"}


@dataclass
class Check:
    name: str
    ok: bool | None            # True pass, False fail, None = cannot be judged from the capture
    detail: str


@dataclass
class Proof:
    txn: object
    source: str
    samples: list = field(default_factory=list)       # (t_ns, state) raw edges of the window (state bit0 SDA, bit1 SCL)
    raw_available: bool = False
    raw_reason: str = ""
    raw_txn: object = None                            # independent decode of the window
    agree: bool | None = None
    agree_detail: str = ""
    bits: list = field(default_factory=list)          # dicts: t, sda, role, byte_index, byte, driver, kind
    conditions: list = field(default_factory=list)    # dicts: kind, t, ok, detail
    timing: dict = field(default_factory=dict)
    spec_mode: str = ""
    spec: list = field(default_factory=list)          # Check per UM10204 parameter
    acks: list = field(default_factory=list)          # dicts: byte, value, ack, driver, meaning
    master: dict = field(default_factory=dict)
    meaning: list = field(default_factory=list)       # lines of plain text
    integrity: list = field(default_factory=list)     # Check
    checks: list = field(default_factory=list)        # overall Check list (the verdict)
    resolution_ns: float = 0.0

    @property
    def verdict(self):
        if any(c.ok is False for c in self.checks):
            return "FAIL"
        if all(c.ok is True for c in self.checks):
            return "PROVEN"
        return "PARTIAL"


# ------------------------------------------------------------------ helpers
def _signature(t):
    return [(f.start_kind, f.addr, f.rw, list(f.data), list(f.acks), f.addr_ack) for f in t.frames]


def _state_at(samples, times, t):
    """Line state in effect at time t (the last sample at or before t)."""
    i = bisect.bisect_right(times, t) - 1
    return samples[i][1] if i >= 0 else None


def _transitions(samples):
    """(t, line, new_level) for every SDA / SCL change in the window."""
    out = []
    prev = None
    for t, s in samples:
        if prev is not None:
            if (s ^ prev) & 2:
                out.append((t, "SCL", (s >> 1) & 1))
            if (s ^ prev) & 1:
                out.append((t, "SDA", s & 1))
        prev = s
    return out


def _judge(name, value, limit, res):
    """UM10204 minimum time vs a measured value known to +-res: PASS / FAIL only when the resolution cannot flip it."""
    if value is None:
        return Check(PARAM_NAMES[name], None, "not in the capture window")
    txt = f"measured {value / 1000:.3f} µs (±{res / 1000:.3f}) vs minimum {limit / 1000:.3f} µs"
    if value - res >= limit:
        return Check(PARAM_NAMES[name], True, txt)
    if value + res < limit:
        return Check(PARAM_NAMES[name], False, txt + " - below the minimum")
    return Check(PARAM_NAMES[name], None, txt + " - within one sample of the limit (cannot be decided at this "
                                                "sample rate)")


# ------------------------------------------------------------------ the proof
def build_proof(engine, txn) -> Proof:
    p = Proof(txn=txn, source=("SIMULATED (demo / presentation)" if getattr(txn, "demo", False) else
                               {"replay": "REPLAY of a saved capture", "live": "LIVE hardware capture"}.get(
                                   getattr(txn, "source", ""), getattr(txn, "source", "") or "unknown")))
    res = float(engine.tick_ns or 0.0)
    p.resolution_ns = res
    t_end = txn.t_end if txn.t_end is not None else txn.t_start
    w0, w1 = txn.t_start - WINDOW_PAD_NS, t_end + WINDOW_PAD_NS
    if not engine.edges:
        p.raw_reason = "no raw SDA/SCL edges in memory (this source sends decoded segments only)"
    elif engine.edges[0][0] > txn.t_start - 1_000:
        p.raw_reason = ("the raw edges of this packet were already overwritten in the rolling edge memory (it keeps "
                        "the most recent edges only) - choose a recent packet, or Pause display and prove it at once")
    elif any(w0 <= g <= w1 for g in engine.edge_gaps):
        p.raw_reason = "the capture reported a data-loss gap inside this packet's window"
    else:
        ts, ss = engine.edges_between(w0, w1)
        allv = [(float(t), int(s)) for t, s in zip(ts, ss)]
        # clip to the window: the level in effect at w0 starts it, and when the stream has an edge after w1 the last
        # level is known to hold until w1 (an idle bus can be quiet for tens of ms on either side of a packet)
        inside = [(t, s) for t, s in allv if w0 <= t <= w1]
        before = [(t, s) for t, s in allv if t < w0]
        if before:
            inside.insert(0, (w0, before[-1][1]))
        if inside and any(t > w1 for t, _s in allv):
            inside.append((w1, inside[-1][1]))
        p.samples = inside
        p.raw_available = len(p.samples) > 4 and p.samples[-1][0] >= t_end
        if not p.raw_available:
            p.raw_reason = "the raw edge stream has not reached the end of this packet yet"
    _master(p, engine, txn)
    _meaning(p, engine, txn)
    _integrity(p, engine, txn, w0, w1)
    if p.raw_available:
        _independent_decode(p, txn)
    if p.raw_txn is not None:
        _bits(p)
        _conditions(p)
        _timing(p, txn)
    _acks(p)
    _verdict(p)
    return p


def _independent_decode(p, txn):
    dec = I2CDecoder()
    done = dec.feed(p.samples)
    tol = max(2 * p.resolution_ns, 2_000)
    cand = [t for t in done if abs(t.t_start - txn.t_start) <= tol]
    if not cand:
        p.agree, p.agree_detail = False, "no transaction decoded from the raw edges at this packet's START"
        return
    raw = min(cand, key=lambda t: abs(t.t_start - txn.t_start))
    p.raw_txn = raw
    a, b = _signature(raw), _signature(txn)
    if a == b:
        nbytes = sum(len(f[3]) for f in a) + len(a)
        p.agree, p.agree_detail = True, (f"identical: {len(a)} frame(s), {nbytes} byte(s) incl. address, every ACK/NACK "
                                         f"and START kind equal")
    else:
        diffs = []
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                diffs.append(f"frame {i + 1}: raw {x} vs stored {y}")
        if len(a) != len(b):
            diffs.append(f"{len(a)} raw frame(s) vs {len(b)} stored")
        p.agree, p.agree_detail = False, "; ".join(diffs[:4])


def _bits(p):
    raw = p.raw_txn
    times = [t for t, _s in p.samples]
    frames = raw.frames
    for bi, b in enumerate(raw.bytes):
        f = frames[b.frame]
        rw = f.rw
        if b.kind == "addr":
            roles = [f"A{6 - k}" for k in range(7)] + ["R/W"]
        elif b.kind == "addr10":
            roles = [f"X{7 - k}" for k in range(8)]
        else:
            roles = [f"D{7 - k}" for k in range(8)]
        data_driver = "target" if (b.kind == "data" and rw == "R") else "controller"
        for k, t in enumerate(b.bit_times):
            st = _state_at(p.samples, times, t)
            p.bits.append(dict(t=t, sda=(st & 1) if st is not None else None, decoded=(b.value >> (7 - k)) & 1,
                               role=roles[k], byte_index=bi, byte=b.value, kind=b.kind, driver=data_driver,
                               frame=b.frame))
        st = _state_at(p.samples, times, b.t_ack)
        ack_driver = "controller" if (b.kind == "data" and rw == "R") else "target"
        p.bits.append(dict(t=b.t_ack, sda=(st & 1) if st is not None else None, decoded=0 if b.ack else 1,
                           role="ACK" if b.ack else "NACK", byte_index=bi, byte=b.value, kind="ack", driver=ack_driver,
                           frame=b.frame))


def _conditions(p):
    trans = _transitions(p.samples)
    times = [t for t, _s in p.samples]
    for t, kind in p.raw_txn.events:
        st_before = _state_at(p.samples, times, t - 0.5)
        st_after = _state_at(p.samples, times, t)
        scl_high = st_after is not None and (st_after >> 1) & 1 == 1 and st_before is not None and (st_before >> 1) & 1
        want = 0 if kind in ("S", "Sr") else 1
        sda_ok = st_after is not None and (st_after & 1) == want and st_before is not None and (st_before & 1) != want
        name = {"S": "START", "Sr": "repeated START", "P": "STOP"}[kind]
        edge = "falls" if want == 0 else "rises"
        p.conditions.append(dict(kind=name, t=t, ok=bool(scl_high and sda_ok),
                                 detail=f"SDA {edge} {'1→0' if want == 0 else '0→1'} while SCL = 1 (SCL stays HIGH "
                                        f"across the edge)" if scl_high and sda_ok else
                                 "SDA / SCL levels around the edge do not form this condition"))
    p._trans = trans


def _timing(p, txn):
    raw = p.raw_txn
    trans = p._trans
    rises = [t for t, ln, v in trans if ln == "SCL" and v == 1 and raw.t_start <= t <= (raw.t_end or t)]
    falls = [t for t, ln, v in trans if ln == "SCL" and v == 0 and raw.t_start <= t <= (raw.t_end or t)]
    highs, lows = [], []
    for r in rises:
        j = bisect.bisect_right(falls, r)
        if j < len(falls):
            highs.append(falls[j] - r)
    for f in falls:
        j = bisect.bisect_right(rises, f)
        if j < len(rises):
            lows.append(rises[j] - f)
    periods = sorted(b - a for a, b in zip(rises, rises[1:]))      # the median ignores the gaps around a repeated START
    tm = {}
    if periods:
        med = periods[len(periods) // 2]
        tm["f_scl_hz"] = 1e9 / med
        tm["period_median_ns"] = med
        tm["cycles"] = len(periods)
    if highs:
        tm["t_high_min"], tm["t_high_mean"] = min(highs), sum(highs) / len(highs)
    if lows:
        tm["t_low_min"], tm["t_low_mean"] = min(lows), sum(lows) / len(lows)
    # conditions: START hold = SDA fall (START) -> next SCL fall; Sr setup = SCL rise -> SDA fall; STOP setup = SCL rise
    # -> SDA rise
    hd, su_sta, su_sto = [], [], []
    for t, kind in raw.events:
        if kind in ("S", "Sr"):
            j = bisect.bisect_right(falls, t)
            if j < len(falls):
                hd.append(falls[j] - t)
        if kind == "Sr":
            prior = [r for r in rises if r < t]
            if prior:
                su_sta.append(t - prior[-1])
        if kind == "P":
            prior = [r for r in rises if r < t]
            if prior:
                su_sto.append(t - prior[-1])
    tm["t_hd_sta"] = min(hd) if hd else None
    tm["t_su_sta"] = min(su_sta) if su_sta else None
    tm["t_su_sto"] = min(su_sto) if su_sto else None
    # bus free before START: the last STOP (SDA rise with SCL high) before this START inside the window, else the
    # analyzer's own measurement for this packet
    prev_stop = [t for t, ln, v in trans if ln == "SDA" and v == 1 and t < raw.t_start]
    times = [t for t, _s in p.samples]
    prev_stop = [t for t in prev_stop if ((_state_at(p.samples, times, t) or 0) >> 1) & 1]
    if prev_stop:
        tm["t_buf"], tm["t_buf_source"] = raw.t_start - prev_stop[-1], "raw edges"
    elif txn.timing.get("bus_free_ns") is not None:
        tm["t_buf"], tm["t_buf_source"] = txn.timing["bus_free_ns"], "analyzer firmware measurement"
    else:
        tm["t_buf"], tm["t_buf_source"] = None, ""
    p.timing = tm
    f = tm.get("f_scl_hz")
    if not f:
        return
    mode = next((m for m, lim in UM10204.items() if f <= lim["f_max"] * 1.02), "Fast-mode Plus")
    p.spec_mode = mode
    lim = UM10204[mode]
    res = p.resolution_ns
    p.spec.append(Check(f"f_SCL ≤ {lim['f_max'] / 1e3:g} kHz ({mode})", f <= lim["f_max"] * 1.02,
                        f"measured {f / 1e3:.2f} kHz (median of {tm.get('cycles', 0)} SCL periods)"))
    for key, val in (("t_low", tm.get("t_low_min")), ("t_high", tm.get("t_high_min")), ("t_hd_sta", tm.get("t_hd_sta")),
                     ("t_su_sta", tm.get("t_su_sta")), ("t_su_sto", tm.get("t_su_sto")), ("t_buf", tm.get("t_buf"))):
        if key == "t_su_sta" and val is None and not any(k == "Sr" for _t, k in raw.events):
            continue                                       # no repeated START in this packet
        p.spec.append(_judge(key, val, lim[key], res))


def _acks(p):
    t = p.raw_txn or p.txn
    for fi, f in enumerate(t.frames):
        if f.addr is not None:
            p.acks.append(dict(byte=f"address 0x{f.addr:02X} + {'R' if f.rw == 'R' else 'W'}", ack=f.addr_ack,
                               driver="target", meaning=("target present, acknowledged its address" if f.addr_ack else
                                                         _nack_note(t, fi, "addr", 0))))
        for j, (b, ak) in enumerate(zip(f.data, f.acks)):
            drv = "controller" if f.rw == "R" else "target"
            meaning = ("byte received" if ak else _nack_note(t, fi, "data", j))
            p.acks.append(dict(byte=f"data {j + 1}/{len(f.data)} 0x{b:02X}", ack=ak, driver=drv, meaning=meaning))


def _master(p, engine, txn):
    mid = getattr(txn, "master_id", None)
    method = getattr(txn, "master_method", "")
    tracker = engine.masters
    m = dict(id=mid, method=method, name=None, report=None)
    if mid is None:
        m["name"] = "UNKNOWN"
        m["why"] = tracker.explain_unattributed(txn)
    else:
        s = tracker.state.get(mid)
        m["name"] = s.label if s else f"Master {mid}"
        if method == "telemetry" and s is not None:
            mt = next((x for x in reversed(s.txns) if x.packet is txn), None)
            if mt is not None:
                exp = mt.t_us * 1000.0 + (s.clock.offset_ns or 0.0)
                m["report"] = dict(seq=mt.seq, t_us=mt.t_us, addr=mt.addr, wlen=mt.wlen, rlen=mt.rlen,
                                   b0=mt.b0, result=mt.result, attempts=mt.attempts, arb_lost=mt.arb_lost,
                                   dt_ns=txn.t_start - exp, clock=s.clock.source)
        m["why"] = {"telemetry": "the master's own transfer report matches this packet (same address, bytes written, "
                                 "bytes read, first byte, at the same instant on the aligned clock)",
                    "presentation": "scripted review sequence (SIMULATED): released for this master",
                    "demo": "simulated demo stream (SIMULATED)",
                    "ownership": "configured ownership rule only (not validated by a report)"}.get(method, method)
    p.master = m


def _meaning(p, engine, txn):
    out = []
    addr = txn.address
    name, conf = engine.registry.name(addr) if addr is not None else ("—", "")
    out.append(f"Target 0x{addr:02X}: {name} ({conf.lower()})" if addr is not None else "No address byte captured")
    if txn.semantic:
        out.append(f"Decoded from these bytes: {txn.semantic}")
    w = next((f for f in txn.frames if f.rw == "W" and f.data), None)
    r = next((f for f in txn.frames if f.rw == "R" and f.data), None)
    if addr in (0x76, 0x77):
        if w:
            reg = w.data[0]
            out.append(f"Register pointer 0x{reg:02X} = {BMP_REGS.get(reg, 'BMP280 register')}")
            for k, v in enumerate(w.data[1:]):
                out.append(f"  write 0x{reg + k:02X} ({BMP_REGS.get(reg + k, '')}) = 0x{v:02X}")
            if r and reg == 0xD0 and r.data:
                cid = r.data[0]
                out.append(f"  chip ID read 0x{cid:02X} → " + {
                    0x60: "BME280 ✓ (temperature, pressure AND humidity)",
                    0x58: "BMP280 ✓ (temperature and pressure; no humidity sensor - a genuine BME280 reads 0x60)"}.get(
                    cid, "neither BME280 (0x60) nor BMP280 (0x58)"))
            if r and reg == 0xF7 and len(r.data) >= 6:
                d = r.data
                adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
                adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
                out.append(f"  raw ADC: pressure 0x{adc_p:05X} = {adc_p}, temperature 0x{adc_t:05X} = {adc_t}")
                cal = engine.sensors.bmp.calib
                if cal:
                    from analysis.sensor_decoder import bmp280_compensate
                    tc, ph = bmp280_compensate(cal, adc_t, adc_p)
                    src = "SIMULATED standard words" if engine.sensors.bmp.calib_simulated else "captured 0x88-0x9F words"
                    out.append(f"  compensated (Bosch datasheet formula, calibration = {src}): {tc:.2f} °C, "
                               f"{ph:.2f} hPa")
                else:
                    out.append("  compensation needs the calibration read (0x88-0x9F), not captured yet")
    elif addr == 0x68:
        if w:
            reg = w.data[0]
            out.append(f"Register pointer 0x{reg:02X} = {RTC_REGS.get(reg, 'DS3231 register')}")
            for k, v in enumerate(w.data[1:]):
                out.append(f"  write 0x{reg + k:02X} ({RTC_REGS.get(reg + k, '')}) = 0x{v:02X}")
            if r:
                for k, v in enumerate(r.data[:19]):
                    rg = reg + k
                    if rg <= 0x06:
                        bcd = (v >> 4 & 0x7) * 10 + (v & 0xF) if rg != 0x02 else (v >> 4 & 0x3) * 10 + (v & 0xF)
                        out.append(f"  read 0x{rg:02X} {RTC_REGS[rg]} = 0x{v:02X} → BCD {bcd}")
                    elif rg in RTC_REGS:
                        extra = " (OSF = 1: oscillator stopped!)" if rg == 0x0F and v & 0x80 else ""
                        out.append(f"  read 0x{rg:02X} {RTC_REGS[rg]} = 0x{v:02X}{extra}")
    elif addr in (0x3C, 0x3D) and w:
        ctrl = w.data[0]
        if ctrl == 0x00:
            out.append("Control byte 0x00: Co = 0, D/C# = 0 → the following bytes are COMMANDS")
            for v in w.data[1:9]:
                out.append(f"  command 0x{v:02X} = {OLED_CMDS.get(v, 'argument / other command')}")
        elif ctrl == 0x40:
            out.append(f"Control byte 0x40: D/C# = 1 → {len(w.data) - 1} byte(s) of display RAM data (each byte = 8 "
                       "vertical pixels of one column)")
        elif ctrl & 0x80:
            out.extend(oled_text_write_meaning(w.data))
        else:
            out.append(f"Control byte 0x{ctrl:02X}")
        out.append("SSD1306 on I2C is write-only: the controller only ever writes it")
    p.meaning = out


def _glyph_table():
    from sources.demo_source import FONT
    table = {}
    for ch, hx in FONT.items():
        table.setdefault(tuple(int(hx[i:i + 2], 16) for i in range(0, 10, 2)), ch)
    for ch, cols in ((":", (0, 0, 0x36, 0, 0)), (".", (0, 0, 0x60, 0, 0)), ("-", (0, 0, 0x08, 0, 0)),
                     ("I", (0x41, 0x41, 0x7F, 0x41, 0x41))):       # the controller firmware's own glyph() variants
        table[cols] = ch
    return table


def oled_text_write_meaning(data):
    """Controller 2.6-live OLED step: [0x80, cmd] pairs (Co = 1: one command byte each) set the addressing mode and a
    column / page window, then 0x40 and the display bytes of whole text lines (5x7 font, 6 columns per character).
    Returns the explanation lines, including the text read back from the pixel columns."""
    out, cmds, i = [], [], 0
    while i + 1 < len(data) and data[i] & 0x80:
        cmds.append(data[i + 1])
        i += 2
    out.append(f"Control bytes 0x80 (Co = 1, D/C# = 0) ×{len(cmds)}: each is followed by ONE command byte")
    cols = pages = None
    k = 0
    while k < len(cmds):
        c = cmds[k]
        if c == 0x20 and k + 1 < len(cmds):
            out.append(f"  0x20 0x{cmds[k + 1]:02X}: memory addressing mode = "
                       f"{['horizontal', 'vertical', 'page'][cmds[k + 1] & 3] if cmds[k + 1] & 3 < 3 else 'invalid'}")
            k += 2
        elif c in (0x21, 0x22) and k + 2 < len(cmds):
            a, b = cmds[k + 1], cmds[k + 2]
            out.append(f"  0x{c:02X} {a} {b}: {'column' if c == 0x21 else 'page'} window {a}..{b}")
            if c == 0x21:
                cols = (a, b)
            else:
                pages = (a, b)
            k += 3
        else:
            out.append(f"  command 0x{c:02X} = {OLED_CMDS.get(c, 'other command')}")
            k += 1
    if i < len(data) and data[i] == 0x40:
        payload = data[i + 1:]
        out.append(f"Control byte 0x40: D/C# = 1 → {len(payload)} byte(s) of display RAM data")
        if cols and pages:
            width = cols[1] - cols[0] + 1
            if width % 6 == 0 and len(payload) % width == 0:
                table = _glyph_table()
                for n in range(len(payload) // width):
                    row = payload[n * width:(n + 1) * width]
                    text = "".join(table.get(tuple(row[c:c + 5]), "?") for c in range(0, width, 6)).rstrip()
                    out.append(f"  page {pages[0] + n}: text \"{text}\" (read back from the {width} pixel columns)")
    return out


def _integrity(p, engine, txn, w0, w1):
    ch = []
    ch.append(Check("Packet complete (START … STOP)", txn.status == "ok", f"status {txn.status}"
                    + (f" · {'; '.join(txn.notes[:2])}" if txn.notes else "")))
    gaps = [g for g in engine.edge_gaps if w0 <= g <= w1]
    ch.append(Check("No capture gap in this packet's window", not gaps,
                    f"{len(gaps)} gap(s)" if gaps else "no lost records / stream gaps reported around it"))
    ch.append(Check("Analyzer buffer: no overflow / dropped samples (whole capture)",
                    not engine.fw_stats.get("overflows") and not engine.fw_stats.get("dropped"),
                    f"overflows {engine.fw_stats.get('overflows', 0)} · dropped {engine.fw_stats.get('dropped', 0)} · "
                    f"lost serial records {engine.lost_records}"))
    if txn.id in engine.decode_disagreements:
        ch.append(Check("Firmware decode agrees with the raw edges", False, "logged as a decode disagreement"))
    p.integrity = ch


def _verdict(p):
    c = []
    if p.raw_available and p.raw_txn is not None:
        c.append(Check("Raw SDA/SCL edges captured with timestamps", True,
                       f"{len(p.samples)} samples, resolution ±{p.resolution_ns:g} ns"))
        c.append(Check("Independent decode of the raw edges == stored packet", p.agree, p.agree_detail))
        mism = [b for b in p.bits if b["sda"] is not None and b["sda"] != b["decoded"]]
        c.append(Check("Every bit: SDA sampled on the SCL rising edge == decoded bit", not mism,
                       f"{len(p.bits)} clock(s) checked" + (f", {len(mism)} mismatch(es)" if mism else "")))
        c.append(Check("START / STOP: SDA edges while SCL is HIGH", all(x["ok"] for x in p.conditions)
                       if p.conditions else None, f"{len(p.conditions)} condition(s)"))
        judged = [s for s in p.spec if s.ok is not None]
        c.append(Check(f"Timing within UM10204 {p.spec_mode or ''}".strip(),
                       (all(s.ok for s in judged) if judged else None),
                       f"{sum(1 for s in judged if s.ok)}/{len(p.spec)} parameters proven"
                       + (f", {sum(1 for s in p.spec if s.ok is None)} not decidable" if any(s.ok is None for s in p.spec)
                          else "")))
    else:
        c.append(Check("Raw SDA/SCL edges captured with timestamps", None, p.raw_reason))
    unexpected = [a for a in p.acks if a["ack"] is False and "(expected" not in a["meaning"]]
    c.append(Check("ACK / NACK: no unexpected NACK", not unexpected,
                   f"{sum(1 for a in p.acks if a['ack'])} ACK, {sum(1 for a in p.acks if a['ack'] is False)} NACK"
                   + (" (end-of-read NACK = expected)" if p.acks and not unexpected and
                      any(a["ack"] is False for a in p.acks) else "")))
    m = p.master
    c.append(Check("Master identified and validated by its own transfer report",
                   True if m.get("report") else (None if m.get("id") is None or m.get("method") != "telemetry" else
                                                 True),
                   f"{m.get('name')}: {m.get('why', '')}"))
    c += p.integrity
    p.checks = c
