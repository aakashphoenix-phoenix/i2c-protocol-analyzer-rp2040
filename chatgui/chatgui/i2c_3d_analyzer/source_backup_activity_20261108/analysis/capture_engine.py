"""Capture engine: the single pipeline every data source goes through.

serial / demo / replay  ->  protocol records  ->  CaptureEngine.ingest()
    -> sequence check -> I2CDecoder -> PerformanceCalculator
                                   -> DeviceRegistry -> SensorDecoder -> event log
The GUI only reads from this object; it never decodes anything itself.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from collections import deque
from dataclasses import dataclass

import config
from analysis.device_registry import DeviceRegistry
from analysis.communication_health import CommunicationEvidence, RULE
from analysis.i2c_decoder import I2CDecoder, ByteRec, Frame, Transaction
from analysis.performance_calculator import PerformanceCalculator, observed_data_timing
from analysis.sensor_decoder import SensorDecoder
from communication.protocol_parser import (Capabilities, ProtocolError, Record, SequenceTracker,
                                           capabilities_from_hello, parse_line)

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}


@dataclass
class Event:
    t_hw: float | None
    wall: _dt.datetime
    severity: str        # info | warning | error
    category: str        # I2C bus | Analyzer | Transport | Topology | Session
    kind: str
    message: str
    addr: int | None = None


class CaptureEngine:
    def __init__(self):
        self.registry = DeviceRegistry()
        self.sensors = SensorDecoder(self.registry, config.OLED_ADDRESS_DEFAULT)
        self.listeners = []          # callables(new_txns) for animations
        self.clear()
        self.caps = Capabilities()
        self.source = "none"         # none | live | demo | replay

    # ------------------------------------------------------------- lifecycle
    def clear(self):
        self.last_transfer_timestamps = {}
        self.communication = CommunicationEvidence()
        self.stream_connected = False
        self.capture_active = False
        self.last_record_wall = None
        self.decoder = I2CDecoder()
        self.perf = PerformanceCalculator()
        self.seq = SequenceTracker()
        self.registry.clear()
        self.sensors.clear()
        self.packets: list = []
        self.packets_dropped_from_memory = 0
        self.edges = deque(maxlen=config.MAX_EDGES_IN_MEMORY)
        self.edge_gaps: list = []                     # hardware times where a gap occurred
        self.events = deque(maxlen=config.MAX_EVENTS_IN_MEMORY)
        self.raw_lines = deque(maxlen=2_000_000)
        self.fw_stats = {"buf_pct": None, "dropped": 0, "overflows": 0}
        self.fw_start_count = 0
        self.fw_stop_count = 0
        self.transport_errors = 0
        self.records_received = 0
        self.decode_disagreements = set()
        self.waveform_transactions = []
        self.edges_evicted = 0
        self.raw_records_evicted = 0
        self.waveform_snapshots = 0
        self.waveform_received_at = None
        self.lost_records = 0
        self.pending_activity: list = []
        self.hw_now: float | None = None
        self.capture_wall_start: _dt.datetime | None = None
        self.revision = 0
        self.native_pending = None
        self.native_counter_baseline = None
        self.master_telemetry = None
        self.master_telemetry_history = deque(maxlen=1000)

    def set_source(self, source: str):
        self.source = source

    def set_caps(self, caps: Capabilities):
        self.caps = caps

    def set_oled_address(self, addr: int):
        self.registry.set_oled_address(addr)
        self.sensors.oled_addr = addr

    @property
    def tick_ns(self) -> float:
        return self.caps.tick_ns or 100.0

    # ------------------------------------------------------------- logging
    def log(self, severity, category, kind, message, addr=None, t_hw=None):
        self.events.append(Event(t_hw, _dt.datetime.now(), severity, category, kind, message, addr))
        self.revision += 1

    def transport_error(self, message: str):
        """Malformed line. Bursts (e.g. old firmware printing plain text) are coalesced."""
        self.transport_errors += 1
        for txn in self.decoder.lose_sync(self.hw_now, "invalid serial record") + self._native_gap(self.hw_now, "invalid serial record"):
            self._store(txn)
        if self.hw_now is not None:
            self.edge_gaps.append(self.hw_now)
        if self.transport_errors == 1 or self.transport_errors % 100 == 0:
            logging.getLogger(__name__).warning("Rejected serial record #%s: %s", self.transport_errors, message)
        now = _dt.datetime.now()
        last = self.events[-1] if self.events else None
        if last and last.kind == "Serial framing error" and (now - last.wall).total_seconds() < 1.0:
            n = getattr(last, "count", 1) + 1
            last.count = n
            last.message = f"{message} (×{n} in burst)"
            self.revision += 1
            return
        self.log("error", "Transport", "Serial framing error", message)

    # ------------------------------------------------------------- ingest
    def ingest_lines(self, lines) -> None:
        recs = []
        for ln in lines:
            try:
                recs.append((ln, parse_line(ln)))
            except ProtocolError as e:
                self.transport_error(f"{e} — line dropped")
        self.ingest(recs)

    def ingest(self, items) -> None:
        """items: iterable of (raw_line_bytes_or_None, Record)."""
        new = []
        for raw, rec in items:
            if raw is not None:
                if len(self.raw_lines) == self.raw_lines.maxlen:
                    self.raw_records_evicted += 1
                self.raw_lines.append(raw if isinstance(raw, bytes) else raw.encode())
            new.extend(self._one(rec))
        for txn in new:
            self._store(txn)
        if new:
            self.pending_activity.extend(new)
            if len(self.pending_activity) > config.MAX_PACKETS_IN_MEMORY:
                self.pending_activity = self.pending_activity[-config.MAX_PACKETS_IN_MEMORY:]
                self.log("warning", "Session", "Display backlog", "Visual activity backlog exceeded retention; consult retained packet records")
            for fn in self.listeners:
                fn(new)
        self.revision += 1

    def _one(self, rec: Record):
        lost = self.seq.check(rec.seq)
        done = []
        if lost:
            self.lost_records += lost
            self.log("error", "Analyzer", "Dropped records",
                     f"{lost} record(s) missing before seq {rec.seq} — data in that interval was not observed",
                     t_hw=self.hw_now)
            done += self.decoder.lose_sync(self.hw_now, f"{lost} records lost")
            done += self._native_gap(self.hw_now, f"{lost} serial records lost")
            if self.hw_now is not None:
                self.edge_gaps.append(self.hw_now)
        self.records_received += 1
        self.last_record_wall = time.monotonic()
        t = rec.type
        d = rec.data
        if t == "hello":
            self.caps = capabilities_from_hello(rec)
            self.seq.reset()
            self.native_pending = None
            self.log("info", "Session", "Analyzer handshake",
                     f"{self.caps.firmware} · protocol v{self.caps.proto} · "
                     f"{self.caps.sample_hz / 1e6:g} MS/s · caps: {', '.join(sorted(self.caps.caps)) or 'none'}")
        elif t == "edges":
            tick = self.tick_ns
            tk = d["t0"]
            samples = []
            for dt, s in d["e"]:
                tk += dt
                samples.append((tk * tick, s))
            if samples and self.caps.has("segment"):
                # Native segments remain authoritative for transactions/metrics.
                # These optional raw windows must never decode transactions twice
                # or connect sampled windows across unobserved intervals.
                if d.get("snapshot"):
                    self.edges.clear()
                    self.waveform_snapshots += 1
                    self.waveform_received_at = time.monotonic()
                self.edges.extend(samples)
                wd = I2CDecoder()
                self.waveform_transactions = wd.feed(samples)
                if wd.txn is not None:
                    wd.txn.t_end = samples[-1][0]
                    wd.txn.status = "incomplete"
                    self.waveform_transactions.append(wd.txn)
                self._match_waveform_timing()
                return done
            if samples:
                self.perf.advance(samples[0][0], gap=bool(lost))
                self.edges_evicted += max(0, len(self.edges) + len(samples) - self.edges.maxlen)
                self.edges.extend(samples)
                done += self.decoder.feed(samples)
                self.hw_now = samples[-1][0]
                self.perf.advance(self.hw_now)
        elif t == "evt":
            ts = d.get("ts", 0) * self.tick_ns
            k = d["kind"]
            if k == "start":
                self.fw_start_count += 1
            elif k == "stop":
                self.fw_stop_count += 1
            elif k == "overflow":
                self.fw_stats["overflows"] += 1
                self.log("error", "Analyzer", "Buffer overflow",
                         "analyzer capture buffer overflowed — samples were lost", t_hw=ts)
                done += self.decoder.lose_sync(ts, "analyzer buffer overflow")
                self.edge_gaps.append(ts)
            elif k == "dropped":
                n = d.get("n", 0)
                self.fw_stats["dropped"] += n
                self.log("error", "Analyzer", "Dropped samples", f"analyzer reports {n} dropped samples", t_hw=ts)
                done += self.decoder.lose_sync(ts, "analyzer dropped samples")
                self.edge_gaps.append(ts)
            elif k == "restart":
                self.log("warning", "Analyzer", "Capture interruption", "analyzer restarted its capture", t_hw=ts)
                done += self.decoder.lose_sync(ts, "capture restarted")
        elif t == "stat":
            for k in ("buf_pct", "dropped", "overflows"):
                if k in d:
                    self.fw_stats[k] = d[k]
            if "ts" in d:
                ts = d["ts"] * self.tick_ns
                if self.hw_now is None or ts > self.hw_now:
                    self.perf.advance(ts, gap=bool(lost))
                    self.hw_now = ts
        elif t == "segment":
            done += self._native_segment(d, record_gap=bool(lost))
        elif t == "loss":
            ts = d["t_us"] * 1000.0
            n = d["lost_buffers"]
            self.fw_stats["overflows"] += n
            self.log("error", "Analyzer", "Capture gap", f"{n} DMA buffers lost", t_hw=ts)
            done += self._native_gap(ts, f"{n} DMA buffers lost")
            self.perf.advance(ts, gap=True)
            self.edge_gaps.append(ts)
            self.hw_now = ts
        elif t == "native_status":
            # C firmware sends cumulative counters. Do not add them repeatedly.
            dmas, drops = d["dma_lost_buffers"], d["dropped_records"]
            if self.native_counter_baseline is None:
                self.native_counter_baseline = (dmas, drops)
            base_dma, base_drop = self.native_counter_baseline
            # Counters in current firmware are lifetime cumulative; subtract
            # baseline instead of attributing pre-session losses to this capture.
            observed_dma = max(0, dmas - base_dma)
            observed_drop = max(0, drops - base_drop)
            if observed_dma > self.fw_stats["overflows"] or observed_drop > self.fw_stats["dropped"]:
                self.log("warning", "Analyzer", "Capture health",
                         f"DMA lost buffers: {observed_dma}, dropped USB records: {observed_drop}")
            if observed_dma > self.fw_stats["overflows"] or observed_drop > self.fw_stats["dropped"]:
                done += self._native_gap(self.hw_now, "cumulative hardware loss counter increased")
            self.fw_stats["overflows"] = max(observed_dma, self.fw_stats["overflows"])
            self.fw_stats["dropped"] = max(observed_drop, self.fw_stats["dropped"])
            self.fw_stats["capturing"] = d["capturing"]
            self.fw_stats["waveform_dropped"] = d.get("waveform_dropped", 0)
        elif t == "log":
            self.log("info", "Analyzer", "Firmware message", d.get("msg", ""))
        if self.hw_now is not None:
            done += self.decoder.check_timeout(self.hw_now)
        return done

    def _native_gap(self, t, reason):
        """Preserve partial native captures explicitly instead of faking STOP."""
        txn = self.native_pending
        if txn is None:
            return []
        txn.status = "gap"
        txn.notes.append("capture gap: " + reason)
        txn.t_end = max(t or txn.t_start, txn.t_start)
        self.native_pending = None
        return [txn]

    def _native_segment(self, d, record_gap=False):
        """Assemble STOP-delimited transactions from C firmware segments.

        A RESTART closes a *message*, not a transaction. The C firmware's
        timing fields are already measured; no digital waveform is invented.
        """
        t0 = d["start_us"] * 1000.0
        t1 = d["end_us"] * 1000.0
        done = []
        if d["start"] == "START" and self.native_pending is not None:
            done += self._native_gap(t0, "unexpected START before STOP")
        elif d["start"] == "RESTART" and self.native_pending is None:
            # The first segment of the observed capture begins mid-transaction.
            self.log("warning", "I2C bus", "Partial transaction", "RESTART observed without preceding START", t_hw=t0)
        if self.native_pending is None:
            txn = Transaction(id=self.decoder.next_id, t_start=t0)
            self.decoder.next_id += 1
            if d["start"] == "RESTART":
                txn.status = "incomplete"
                txn.notes.append("Capture began at a repeated START")
            self.native_pending = txn
        else:
            txn = self.native_pending
        kind = "S" if d["start"] == "START" else "Sr"
        txn.events.append((t0, kind))
        fr = Frame(start_kind=kind, t_start=t0,
                   addr=d["address"] if d["address_known"] else None,
                   ten_bit=(d["address_bits"] == 10),
                   rw="R" if d["direction"] == "read" else "W",
                   addr_ack=d["address_ack"] if d["address_known"] else None,
                   data=list(d["data"]), acks=list(d["acks"]))
        txn.frames.append(fr)
        # The firmware provides segment-level timing only, so these are
        # approximate placeholders for table ACK counts, not clock timestamps.
        if fr.addr is not None and fr.addr_ack is not None:
            txn.bytes.append(ByteRec(((fr.addr << 1) | (fr.rw == "R")) if not fr.ten_bit else (fr.addr & 255), fr.addr_ack, "addr", t0, t0, len(txn.frames)-1))
        for b, ack in zip(fr.data, fr.acks):
            txn.bytes.append(ByteRec(b, ack, "data", t0, t1, len(txn.frames)-1))
        if d["truncated"] or d["incomplete"] or not d["address_known"]:
            if txn.status == "open":
                txn.status = "incomplete"
            txn.notes.append("Firmware reports truncated/incomplete segment")
        if d.get("scl_low_over_50us"):
            txn.notes.append(f"{d['scl_low_over_50us']} SCL LOW intervals >50us; clock stretching not proven")
        f = d.get("scl_hz", 0)
        if f:
            txn.timing.update(frequency_hz=f, period_ns=1e9/f, high_ns=d.get("scl_high_avg_ns"), low_ns=d.get("scl_low_avg_ns"))
            self.perf.add_firmware_frequency(f, t1, d.get("scl_high_avg_ns"), d.get("scl_low_avg_ns"))
        self.perf.advance(t0, gap=record_gap)
        self.hw_now = max(self.hw_now or 0, t1)
        if d["end"] == "STOP":
            txn.events.append((t1, "P"))
            txn.t_end = t1
            if txn.status == "open":
                txn.status = "ok"
            done.append(txn)
            self.native_pending = None
        elif d["end"] == "GAP":
            done += self._native_gap(t1, "firmware reported GAP")
            self.perf.advance(t1, gap=True)
            self.edge_gaps.append(t1)
        # RESTART deliberately leaves the transaction open until next segment.
        self.perf.advance(t1)
        return done

    def _match_waveform_timing(self):
        """Attach measured byte times only after raw/segment content agreement."""
        for raw in self.waveform_transactions:
            for txn in reversed(self.packets[-400:]):
                if abs(txn.t_start-raw.t_start) > 1000:
                    continue
                def signature(t):
                    return [(f.addr, f.rw, f.data, f.acks, f.addr_ack) for f in t.frames]
                if raw.status != 'ok':
                    continue
                if signature(raw) != signature(txn):
                    if txn.id not in self.decode_disagreements:
                        self.decode_disagreements.add(txn.id)
                        self.log('error', 'Analyzer', 'Decode disagreement', f'Packet #{txn.id}: raw waveform and firmware segment disagree; preserved both sources', t_hw=raw.t_start)
                    continue
                if len(raw.bytes) != len(txn.bytes):
                    continue
                for original, measured in zip(txn.bytes, raw.bytes):
                    original.t_first, original.t_ack = measured.t_first, measured.t_ack
                    original.bit_times, original.t_end = measured.bit_times, measured.t_end
                break

    def update_master_telemetry(self, rtc_iso, temperature_c, pressure_hpa):
        """Separate, explicitly identified master-USB data; not bus-decoded."""
        import time
        try:
            datetime_value = _dt.datetime.fromisoformat(rtc_iso)
            t, p = float(temperature_c), float(pressure_hpa)
        except (ValueError, TypeError):
            return
        if not (-40 <= t <= 85 and 300 <= p <= 1100):
            return
        self.master_telemetry = {"rtc": datetime_value, "temperature_c": t,
                                 "pressure_hpa": p, "received_at": time.monotonic()}
        self.master_telemetry_history.append((time.monotonic(), t, p))
        self.revision += 1

    def _store(self, txn):
        txn.demo = self.source == "demo"
        txn.source = self.source
        for fi, frame in enumerate(txn.frames):
            if not frame.data or frame.rw not in ('R', 'W'):
                continue
            data_bytes = [b for b in txn.bytes if b.frame == fi and b.kind == 'data']
            last_byte = data_bytes[-1] if data_bytes else None
            exact = bool(last_byte and last_byte.bit_times)
            timestamp = last_byte.t_first if exact else frame.t_start
            self.last_transfer_timestamps['READ' if frame.rw == 'R' else 'WRITE'] = (timestamp, exact, txn.id)
        if not self.caps.has('segment') and self.edges_available(txn.t_start, txn.t_end or txn.t_start):
            ts, ss = self.edges_between(txn.t_start, txn.t_end or txn.t_start)
            txn.timing.update(observed_data_timing(zip(ts, ss)))
        if self.packets and self.packets[-1].status == 'ok' and not any(self.packets[-1].t_end <= g <= txn.t_start for g in self.edge_gaps):
            txn.timing['bus_free_ns'] = max(0, txn.t_start-self.packets[-1].t_end)
        self.communication.observe(txn, self.registry.configured)
        self.perf.add_transaction(txn)
        txn.rises = txn.falls = None          # timing consumed, free memory
        notices = list(self.decoder.notices)
        self.decoder.notices.clear()
        notices += self.registry.observe(txn)
        notices += self.sensors.observe(txn)
        for t, sev, kind, msg, addr in notices:
            cat = "Topology" if kind in ("New address", "Device confirmed") else (
                "Analyzer" if kind == "Decoder sync loss" else "I2C bus")
            self.log(sev, cat, kind, msg, addr, t)
        self.packets.append(txn)
        if self.caps.has("segment"):
            self._match_waveform_timing()
        if len(self.packets) > config.MAX_PACKETS_IN_MEMORY:
            cut = config.MAX_PACKETS_IN_MEMORY // 10
            del self.packets[:cut]
            self.packets_dropped_from_memory += cut
            self.log("warning", "Session", "Retention limit", f"{cut} oldest packet records evicted from memory; save captures regularly")

    def take_activity(self):
        a, self.pending_activity = self.pending_activity, []
        return a

    # ------------------------------------------------------------- queries
    def edges_between(self, t0, t1):
        """(times_ns, states) for edges in [t0, t1], plus the state before t0."""
        import bisect
        import itertools

        import numpy as np
        ed = self.edges
        if not ed or t1 < ed[0][0]:
            return np.empty(0), np.empty(0, dtype=int)
        n = len(ed)
        i0 = max(0, bisect.bisect_left(ed, (t0,)) - 1)
        i1 = min(n, bisect.bisect_right(ed, (t1, 9)) + 1)
        if i0 > n // 2:      # walk from the right end: cheaper for the live window
            seg = list(itertools.islice(reversed(ed), n - i1, n - i0))[::-1]
        else:
            seg = list(itertools.islice(ed, i0, i1))
        if not seg:
            return np.empty(0), np.empty(0, dtype=int)
        arr = np.asarray(seg, dtype=float)
        return arr[:, 0], arr[:, 1].astype(int)

    def edges_available(self, t0, t1) -> bool:
        return bool(self.edges) and self.edges[0][0] <= t0 and (not self.caps.has("segment") or t1 <= self.edges[-1][0]) and not any(t0 <= g <= t1 for g in self.edge_gaps)

    def health(self) -> tuple[str, str]:
        """(status word, detail) for the capture-health card."""
        if self.source == "none":
            return "Idle", "not capturing"
        problems = self.lost_records + self.fw_stats["overflows"] + (self.fw_stats["dropped"] or 0)
        if self.transport_errors or problems:
            return "Degraded", (f"{self.lost_records} lost records · {self.fw_stats['overflows']} overflows · "
                                f"{self.transport_errors} framing errors")
        if not self.caps.has("lossless"):
            return "Good*", "no loss detected · firmware does not guarantee lossless capture"
        return "Good", "no loss detected"

    def measurement_trust(self):
        if self.decode_disagreements:
            return False, "Raw waveform/segment decode disagreement; inspect diagnostics"
        if self.source != "live":
            return False, "Not live hardware (demo/replay/unverified)"
        if not self.stream_connected or not self.capture_active:
            return False, "Analyzer disconnected or capture stopped"
        if self.fw_stats.get("capturing") is False:
            return False, "Analyzer reports capture stopped"
        if self.last_record_wall is None or time.monotonic()-self.last_record_wall >= 3:
            return False, "No fresh analyzer records"
        if self.transport_errors or self.lost_records or self.fw_stats["overflows"] or self.fw_stats["dropped"] or not self.decoder.synced and self.decoder.last_t is not None:
            return False, "Capture unreliable; device communication cannot be verified"
        return True, "Fresh live capture; no capture loss detected"

    def device_health(self, addr):
        trusted, reason = self.measurement_trust()
        return self.communication.device(addr, trusted, reason)

    def communication_health(self):
        h = self.health_summary()
        return h['state'], h['detail']

    def health_summary(self):
        """One shared health record for the dashboard card, both health graphs and 3D views.

        state is the verified bus state (HEALTHY / DEGRADED / UNKNOWN) and is only
        evaluated for trustworthy live hardware capture.  In demo mode the same
        policy runs on simulated records and is returned separately as
        simulated_state; it is never reported as verified health.
        """
        from analysis.communication_health import WINDOW_LABEL
        trusted, reason = self.measurement_trust()
        c = dict(self.communication.snapshot(self.hw_now))
        out = dict(counts=c, window=WINDOW_LABEL, rule=RULE, simulated=self.source == 'demo',
                   simulated_state=c['status'] if self.source == 'demo' else None)
        if not trusted:
            sim = f"; SIMULATED policy result: {c['status']} (not verified hardware)" if self.source == 'demo' else ''
            out.update(state="UNKNOWN", reason=reason, detail=reason + sim)
            return out
        if self.communication.last_transaction_wall is None or time.monotonic()-self.communication.last_transaction_wall > 60:
            out.update(state="UNKNOWN", reason="No completed transaction evidence within the last 60 seconds")
            out['detail'] = out['reason']
            return out
        detail = (f"60 s: unexpected {c['unexpected_nacks']}/{c['opportunities']} ACK slots; "
                  f"raw NACK {c['raw_nacks']}; expected read-end {c['expected_read_nacks']}; "
                  f"incomplete {c['incomplete']}. {RULE}")
        out.update(state=c['status'], reason='Fresh live capture', detail=detail)
        return out

    def rtc_status(self, stale_after_s=3.0):
        """Provenance-tagged DS3231 time. Never uses the PC wall clock.

        Returns dict(state=VALID|STALE|UNAVAILABLE, text, source, t_ns).
        Sources: bus-decoded DS3231 read (live/replay), clearly labelled demo
        simulation, or separately identified master USB telemetry.
        """
        r = self.sensors.rtc.time
        if r is not None:
            hr, mn, sec, _wd, day, month, year = r.value
            age = None if self.hw_now is None else max(0.0, (self.hw_now - r.t_ns) / 1e9)
            src = ('SIMULATED demo DS3231 record (decoded)' if self.source == 'demo' else
                   'bus-decoded DS3231 read (replay)' if self.source == 'replay' else
                   'bus-decoded DS3231 read 0x68 reg 00–06')
            state = 'STALE' if age is not None and age > stale_after_s else 'VALID'
            return dict(state=state, source=src, t_ns=r.t_ns, age_s=age,
                        text=f"{year:04d}-{month:02d}-{day:02d} {hr:02d}:{mn:02d}:{sec:02d}")
        tele = self.master_telemetry
        if tele is not None and self.source != 'demo':
            age = max(0.0, time.monotonic() - tele['received_at'])
            return dict(state='STALE' if age > stale_after_s else 'VALID', t_ns=None, age_s=age,
                        source='Pico 1 master USB telemetry (not bus-decoded)',
                        text=tele['rtc'].strftime('%Y-%m-%d %H:%M:%S'))
        return dict(state='UNAVAILABLE', source='no valid DS3231 record', t_ns=None, age_s=None, text='—')
