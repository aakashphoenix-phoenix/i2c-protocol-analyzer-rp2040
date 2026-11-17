"""Sample-accurate 2 MS/s two-master I²C bus for tests and verification (no hardware).

The bus runs in TRUE continuous time (each controller at its own real SCL rate, open-drain level changes at real
instants).  The passive RP2040 analyzer sees every level change at the first 0.5 µs sample at or after it, exactly like
the PIO capture: that quantised sample stream is what this module turns into the analyzer's wire records
  * raw edge records ("edges" + "e", classic tick-based edge capture),
  * native firmware segments (one per START/Sr..Sr/STOP frame, with the firmware's per-segment average SCL),
  * the continuous packed RLE stream ("edges" + "r", stream:true), optionally with a dropped batch announced by gap:true,
and, per transaction, the controller's own JSON `txn` report on that master's COM port (Master 1 / Master 2 clocks have
independent offsets), each line with the PC arrival time a USB link would give it.

Nothing here is a measurement: it is the reference the measurements are checked against (`true_hz`, `true_bus_free_ns`)."""
from __future__ import annotations

import json
import math

TICK_NS = 500                                   # 2 MS/s analyzer sample period
OFF = {1: 5_000_123_456.0, 2: -1_999_923_000.0}  # analyzer_ns - controller_us * 1000 (independent controller clocks)


class BusSynth:
    def __init__(self, hz=100_000, t0_ns=10_000_000_000.0, high_frac=0.45):     # 10 s: every controller t_us >= 0
        self.hz = hz
        self.high_frac = high_frac
        self.t = float(t0_ns)                    # true time of the last SCL edge / position on the bus
        self.changes = [(self.t - 50_000.0, 3)]  # (true ns, state) state = SCL << 1 | SDA; idle HIGH/HIGH
        self.state = 3
        self.frames = []                         # finished frames (one native segment each)
        self.frame = None
        self.txns = []                           # finished transactions (controller reports)
        self.txn = None
        self.last_stop = None                    # true time of the previous STOP (t_BUF reference)
        self.mseq = {1: 0, 2: 0}

    # ------------------------------------------------------------------ level changes
    def _set(self, t, scl=None, sda=None):
        s = self.state
        if scl is not None:
            s = (s & 1) | (scl << 1)
        if sda is not None:
            s = (s & 2) | sda
        if s != self.state:
            self.changes.append((t, s))
            self.state = s

    @property
    def _period(self):
        return 1e9 / self.txn["hz"]

    @property
    def _low(self):
        return self._period * (1 - self.high_frac)

    @property
    def _high(self):
        return self._period * self.high_frac

    @property
    def _cond(self):                             # tSU;STA = tHD;STA = tSU;STO: half a period (>= UM10204 minimum)
        return self._period * 0.5

    def idle(self, ns):
        self.t += ns

    # ------------------------------------------------------------------ protocol
    def start(self, master, hz=None, bus_free_ns=None):
        """START (from idle).  bus_free_ns: STOP -> START distance, default 2 periods (>= tBUF)."""
        assert self.txn is None and self.state == 3
        self.txn = dict(master=master, hz=hz or self.hz, frames=[], t_start=None, t_end=None)
        if self.last_stop is not None and bus_free_ns is not None:
            self.t = self.last_stop + bus_free_ns          # exactly this STOP -> START distance (overrides any idle)
        elif self.last_stop is not None:
            self.t = max(self.t, self.last_stop + 2 * self._period)
        self.txn["t_start"] = self.t
        self._set(self.t, sda=0)
        self._open_frame("START", self.t)
        self.t += self._cond
        self._set(self.t, scl=0)

    def restart(self):
        self.t += self._low * 0.5
        self._set(self.t, sda=1)
        self.t += self._low * 0.5
        self._set(self.t, scl=1)
        self.t += self._cond
        self._close_frame("RESTART", self.t)
        self._set(self.t, sda=0)
        self._open_frame("RESTART", self.t)
        self.t += self._cond
        self._set(self.t, scl=0)

    def stop(self):
        self.t += self._low * 0.4
        self._set(self.t, sda=0)
        self.t += self._low * 0.6
        self._set(self.t, scl=1)
        self.t += self._cond
        self._set(self.t, sda=1)
        self._close_frame("STOP", self.t)
        self.last_stop = self.t
        tx, self.txn = self.txn, None
        tx["t_end"] = self.t
        self.txns.append(tx)

    def bit(self, b, stretch_ns=0.0):
        fall = self.t
        self._set(fall + self._low * 0.4, sda=b)
        rise = fall + self._low + stretch_ns
        self._set(rise, scl=1)
        self.t = rise + self._high
        self._set(self.t, scl=0)
        self.frame["rises"].append(rise)

    def byte(self, v, ack=True, stretch_ns=0.0):
        for i in range(8):
            self.bit((v >> (7 - i)) & 1, stretch_ns if i == 0 else 0.0)
        self.bit(0 if ack else 1)
        f = self.frame
        if f["addr"] is None:
            f["addr"], f["dir"], f["addr_ack"] = v >> 1, ("read" if v & 1 else "write"), ack
        else:
            f["data"].append(v)
            f["acks"].append(ack)

    def _open_frame(self, kind, t):
        self.frame = dict(kind0=kind, t0=t, addr=None, dir=None, addr_ack=None, data=[], acks=[], rises=[],
                          master=self.txn["master"], hz=self.txn["hz"])

    def _close_frame(self, kind, t):
        f, self.frame = self.frame, None
        f["kind1"], f["t1"] = kind, t
        self.frames.append(f)
        self.txn["frames"].append(f)

    # ------------------------------------------------------------------ convenience transactions
    def write(self, master, addr, data, hz=None, nack_addr=False, nack_at=None, bus_free_ns=None, stretch_ns=0.0):
        self.start(master, hz, bus_free_ns)
        self.byte(addr << 1, ack=not nack_addr)
        if not nack_addr:
            for i, d in enumerate(data):
                self.byte(d, ack=(i != nack_at), stretch_ns=stretch_ns if i == 0 else 0.0)
                if i == nack_at:
                    break
        self.stop()

    def write_read(self, master, addr, reg, n, hz=None, values=None, stretch_ns=0.0):
        self.start(master, hz)
        self.byte(addr << 1)
        self.byte(reg)
        self.restart()
        self.byte(addr << 1 | 1)
        values = values or [(0x30 + 7 * k) & 0xFF for k in range(n)]
        for k, v in enumerate(values):
            self.byte(v, ack=k < n - 1, stretch_ns=stretch_ns if k == 0 else 0.0)
        self.stop()

    # ------------------------------------------------------------------ the analyzer's view (0.5 µs samples)
    @staticmethod
    def sample(t):
        return int(math.ceil(t / TICK_NS - 1e-9))

    def samples(self):
        """[(sample index, state)] of every visible level change (two changes inside one 0.5 µs tick merge)."""
        out = []
        for t, s in self.changes:
            n = self.sample(t)
            if out and out[-1][0] == n:
                out[-1] = (n, s)
            else:
                out.append((n, s))
        return out

    def true_hz(self, master=None):
        """{transaction index: true SCL Hz} reference."""
        return [tx["hz"] for tx in self.txns if master is None or tx["master"] == master]

    def true_bus_free_ns(self):
        """Quantised STOP -> next START distance per transaction (None for the first)."""
        out, prev = [], None
        for tx in self.txns:
            out.append(None if prev is None else (self.sample(tx["t_start"]) - self.sample(prev)) * TICK_NS)
            prev = tx["t_end"]
        return out

    # ------------------------------------------------------------------ wire records
    @staticmethod
    def raw_hello():
        return json.dumps({"t": "hello", "proto": 1, "fw": "bus_synth raw", "sample_hz": 2_000_000, "tick_ns": TICK_NS,
                           "caps": ["edges", "seq"]})

    @staticmethod
    def native_hello():
        return json.dumps({"type": "hello", "protocol": 1, "role": "analyzer", "board": "RP2040", "sample_rate": 2_000_000,
                           "sample_resolution_us": 0.5, "waveform": False,
                           "capabilities": ["segment", "loss", "status", "7bit", "ack", "timing"]})

    def raw_records(self, chunk=40):
        """Classic edge records ("e": [[dt, state]] in ticks), each with the PC arrival time of its last sample."""
        smp = self.samples()
        out, seq = [], 0
        for i in range(0, len(smp), chunk):
            c = smp[i:i + chunk]
            prev = c[0][0]
            e = []
            for n, s in c:
                e.append([n - prev, s])
                prev = n
            out.append((c[-1][0] * TICK_NS / 1e9 + 0.002, "an",
                        json.dumps({"t": "edges", "seq": seq, "t0": c[0][0], "e": e})))
            seq += 1
        return out

    def _firmware_scl(self, f):
        """What the analyzer firmware puts in a segment: mean of the frame's captured periods <= 1 ms, HIGH/LOW means."""
        rises = [self.sample(r) * TICK_NS for r in f["rises"]]
        periods = [b - a for a, b in zip(rises, rises[1:]) if 0 < b - a <= 1_000_000]
        if not periods:
            return 0, 0, 0
        mean = sum(periods) / len(periods)
        return round(1e9 / mean), round(mean * self.high_frac), round(mean * (1 - self.high_frac))

    def segment_records(self, seq0=1):
        out, prev_stop = [], None
        for k, f in enumerate(self.frames):
            n0, n1 = self.sample(f["t0"]), self.sample(f["t1"])
            hz, hi, lo = self._firmware_scl(f)
            d = dict(type="segment", seq=seq0 + k, start_us=int(n0 * TICK_NS // 1000), end_us=int(n1 * TICK_NS // 1000),
                     duration_us=int(n1 * TICK_NS // 1000) - int(n0 * TICK_NS // 1000), start_sample=n0, end_sample=n1,
                     start=f["kind0"], end=f["kind1"], address=f["addr"], address_bits=7, address_known=True,
                     direction=f["dir"], address_ack=f["addr_ack"], scl_hz=hz, scl_high_avg_ns=hi, scl_low_avg_ns=lo,
                     scl_low_over_50us=0, truncated=False, incomplete=False, data=f["data"], acks=f["acks"],
                     mid_byte_end=False)
            if f["kind0"] == "START" and prev_stop is not None:
                d["bus_free_ns"] = (n0 - prev_stop) * TICK_NS
            if f["kind1"] == "STOP":
                prev_stop = n1
            out.append((n1 * TICK_NS / 1e9 + 0.001, "an", json.dumps(d)))
        return out

    def stream_records(self, batch_ns=1_500_000, drop_batch=None):
        """Packed RLE stream batches; drop_batch=k loses batch k and announces it with gap:true on the next one."""
        smp = self.samples()
        span = int(batch_ns // TICK_NS)
        batches, cur, edge = [], [], (smp[0][0] // span + 1) * span
        for n, s in smp:
            while n >= edge:
                if cur:
                    batches.append(cur)
                cur, edge = [], edge + span
            cur.append((n, s))
        if cur:
            batches.append(cur)
        out, gap = [], False
        for k, b in enumerate(batches):
            if k == drop_batch:
                gap = True
                continue
            r = [b[0][1]] + [((n - p) << 2) | s for (p, _), (n, s) in zip(b, b[1:])]
            rec = {"t": "edges", "stream": True, "t0": b[0][0], "r": r}
            if gap:
                rec["gap"], gap = True, False
            out.append((b[-1][0] * TICK_NS / 1e9 + 0.003, "an", json.dumps(rec)))
        return out

    def master_records(self, jitter=None):
        """Each controller's own `txn` report on its COM port (independent clock, own USB delay)."""
        out = []
        for i, tx in enumerate(self.txns):
            m = tx["master"]
            fr = tx["frames"]
            addr = fr[0]["addr"]
            w = [b for f in fr if f["dir"] == "write" for b in f["data"]]
            r = [b for f in fr if f["dir"] == "read" for b in f["data"]]
            if fr[0]["addr_ack"] is False:
                result = "addr_nack"
            elif any(a is False for f in fr if f["dir"] == "write" for a in f["acks"]):
                result = "data_nack"
            else:
                result = "ok"
            self.mseq[m] += 1
            t_us = int(round((self.sample(tx["t_start"]) * TICK_NS - OFF[m]) / 1000))
            rec = dict(type="txn", source="controller", master_id=m, seq=self.mseq[m], t_us=t_us,
                       dur_us=int((tx["t_end"] - tx["t_start"]) // 1000), addr=addr, wlen=len(w), rlen=len(r),
                       b0=w[0] if w else -1, result=result, attempts=1, arb_lost=0)
            wall = (t_us * 1000 + OFF[m]) / 1e9 + 0.003 + (jitter(i) if jitter else 0.0)
            out.append((wall, m, json.dumps(rec)))
        return out


TRUE_HZ_100K = {1: 99_600, 2: 99_206}            # the two controllers' real Standard-mode clocks (dividers differ)
TRUE_HZ_400K = {1: 396_825, 2: 398_406}           # ... and Fast-mode clocks (2.520 µs / 2.510 µs periods)
STRETCH_NS = 300_000                              # one target clock stretch (> 3x the median cycle)
SHORT_BUF_NS = 2_000                              # one STOP -> START closer than tBUF: 4.7 µs in Standard-mode ...
SHORT_BUF_FM_NS = 1_000                           # ... and 1.3 µs in Fast-mode (2 µs would be legal at 400 kHz)


def standard_traffic(b, true_hz, rounds=16, spacing_ns=40_000_000, short_buf_ns=None):
    """Two masters sharing the bus: Master 1 writes the OLED (0x3C), Master 2 reads the RTC (0x68, write + Sr + read)
    and the BMP280 (0x76).  Round 5: the BMP280 stretches SCL before its first read byte; round 7: Master 1 starts
    only SHORT_BUF_NS after the previous STOP; round 9: Master 1 probes an absent 0x77 (address NACK); round 11: the
    OLED refuses a written byte (data NACK).  First written bytes vary so every controller report is distinguishable."""
    if short_buf_ns is None:
        short_buf_ns = SHORT_BUF_NS if true_hz[1] < 200_000 else SHORT_BUF_FM_NS
    for k in range(rounds):
        b.write(1, 0x3C, [k, 0x40] + [(3 * k + i) & 0xFF for i in range(5)], hz=true_hz[1],
                bus_free_ns=short_buf_ns if k == 7 else None)
        b.idle(spacing_ns)
        b.write_read(2, 0x68, k, 7, hz=true_hz[2])
        b.idle(spacing_ns)
        b.write_read(2, 0x76, 0xF0 + (k % 8), 6, hz=true_hz[2], stretch_ns=STRETCH_NS if k == 5 else 0.0)
        b.idle(spacing_ns)
        if k == 9:
            b.write(1, 0x77, [], hz=true_hz[1], nack_addr=True)
            b.idle(spacing_ns)
        if k == 11:
            b.write(1, 0x3C, [0x80 + k, 0x01, 0x02], hz=true_hz[1], nack_at=1)
            b.idle(spacing_ns)
    return b


def feed(engine, records, ports=(1, 2)):
    """Deliver (wall, kind, line) records in PC arrival order; each stream stays FIFO."""
    for wall, kind, line in sorted(records, key=lambda r: r[0]):
        engine.wall_clock = lambda w=wall: w
        if kind == "an":
            engine.ingest_lines([line.encode()])
        elif kind in ports:
            engine.ingest_master_lines(kind, [line.encode()])
