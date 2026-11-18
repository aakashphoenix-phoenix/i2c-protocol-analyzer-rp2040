"""Capture engine: the single pipeline every data source goes through.

serial / demo / replay  ->  protocol records  ->  CaptureEngine.ingest()
    -> sequence check -> I2CDecoder -> PerformanceCalculator
                                   -> DeviceRegistry -> SensorDecoder -> event log
The GUI only reads from this object; it never decodes anything itself.
"""
from __future__ import annotations

from itertools import accumulate
import datetime as _dt
import logging
import time
from collections import deque
from dataclasses import dataclass

import config
from analysis.device_activity import FrameLog
from analysis.device_registry import DeviceRegistry
from analysis import scl_timing
from analysis.communication_health import CommunicationEvidence, RULE
from analysis.i2c_decoder import I2CDecoder, ByteRec, Frame, Transaction
from analysis.multi_master import MultiMasterTracker
from analysis.performance_calculator import PerformanceCalculator, observed_data_timing
from analysis.sensor_decoder import SensorDecoder
from communication.master_protocol import MasterProtocolError, parse_master_line
from communication.protocol_parser import (Capabilities, ProtocolError, Record, SequenceTracker,
                                           capabilities_from_hello, parse_line)

import json as _json

STREAM_RAW_SAVE_CAP = 96 * 2 ** 20      # saved captures keep at most this much edge-stream text

# UM10204 Table 10/11: bus free time between a STOP and the next START.
TBUF_MIN_NS = {100_000: 4_700, 400_000: 1_300}

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
        self.masters = MultiMasterTracker()      # controller telemetry + packet attribution
        self.wall_clock = time.monotonic          # PC arrival clock (tests substitute a fake)
        self.master_telemetry_by_id = {}
        self.clear()
        self.caps = Capabilities()
        self.source = "none"         # none | live | demo | replay

    # ------------------------------------------------------------- lifecycle
    def clear(self):
        self.last_transfer_timestamps = {}
        self.frame_log = FrameLog()          # per-frame activity from decoded records only
        self.scl_setting = None              # last controller SCL request/reply (never a measurement)
        self.capture_start_ns = None         # first captured sample time of this capture
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
        self.decode_agreements = 0              # packets whose raw-edge decode == firmware segment decode
        self._raw_pending = deque()             # edge-stream raw transactions not yet matched to their packet
        self.waveform_transactions = []
        # continuous edge stream (RLE): a rolling ring of every transition, decoded incrementally
        self.stream_active = False           # a stream record was received in this capture
        self.stream_now = None               # capture time (ns) of the newest stream entry (heartbeats advance it)
        self.stream_entries = 0
        self.stream_gaps = 0                 # batches announced as "data lost before this" by the firmware
        self._stream_line_lost = False       # a serial line was rejected while streaming: it may have held edges
        self.stream_raw_bytes = 0
        self._stream_decoder = I2CDecoder()
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
        self.master_telemetry_by_id = {}
        self.master_telemetry_series = {}        # master id -> deque[(monotonic, temp_c, press_hpa)]: one clean series each
        self.master_errors = 0
        self.mid_byte_cuts = 0
        self.tbuf_violations = 0
        self._clock_saved = {}
        self.masters.reset_session()
        # attribution arrives after a packet was counted (ownership at on_packet, telemetry later): the per-master
        # statistics follow the tracker's decision exactly (never a guess)
        self.masters.on_attribution = self.perf.reattribute

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
            head = ln[:40] if isinstance(ln, (bytes, str)) else b""
            if isinstance(head, str):
                head = head.encode()
            if head.startswith(b'{"type":"master_record"'):
                # Controller telemetry saved inside a capture file (replay): flush the analyzer
                # records before it so attribution sees the same order as the live session.
                self.ingest(recs)
                recs = []
                self._replay_master_record(ln)
                continue
            try:
                recs.append((ln, parse_line(ln)))
            except ProtocolError as e:
                self.transport_error(f"{e} — line dropped")
                # A rejected line may have been an edge batch: the ring would silently hold the old level across the
                # hole.  Damaged segment records ('{"type":...') cannot hold edges, everything else might.
                if self.stream_active and not head.startswith(b'{"type":'):
                    self._stream_line_lost = True
        self.ingest(recs)

    # ------------------------------------------------------------- controllers (masters)
    def ingest_master_lines(self, slot, lines, wall=None):
        """Lines from master `slot`'s own COM port.  Returns [(record dict or None, raw text)].

        JSON controller records update the multi-master tracker (and SCL / sensor telemetry);
        plain text is returned with record None so the caller can apply legacy parsing."""
        out = []
        for ln in lines:
            text = ln.decode("utf-8", "replace").strip() if isinstance(ln, bytes) else str(ln).strip()
            try:
                d = parse_master_line(text)
            except MasterProtocolError as e:
                self.master_errors += 1
                if self.master_errors == 1 or self.master_errors % 100 == 0:
                    self.log("warning", "Multi-master", "Controller record rejected",
                             f"Master {slot} port: {e} (×{self.master_errors})")
                out.append((None, ""))
                continue
            if d is not None:
                self.ingest_master_record(slot, d, wall=wall)
            out.append((d, text))
        return out

    def ingest_master_record(self, slot, d, wall=None, record=True):
        mid = d.get("master_id") if d.get("master_id") in self.masters.state else slot
        if d.get("master_id") in self.masters.state and d["master_id"] != slot and record:
            self.log("warning", "Multi-master", "Master ID mismatch",
                     f"port of Master {slot} reports master_id {d['master_id']}: check which firmware is flashed "
                     f"(I2C.uf2 = Master 1, I2C_master2.uf2 = Master 2)")
        t = d.get("type")
        if t == "scl_config":
            self.record_scl_setting(d.get("requested_hz"), d.get("applied_hz"), d.get("result"),
                                    (f"SIMULATED Master {mid} controller (demo)" if d.get("simulated") is True
                                     else f"Master {mid} controller firmware"), d.get("reason", ""), master_id=mid)
        elif t == "sensor_telemetry":
            self.update_master_telemetry(d.get("rtc"), d.get("temperature_c"), d.get("pressure_hpa"), master_id=mid)
        elif t == "rtc_set":
            self.log("info", "Session", "RTC set", f"Master {mid}: {d.get('result')} {d.get('time', '')} "
                                                   f"{d.get('reason', '')}".strip(), 0x68)
        wall = self.wall_clock() if wall is None else wall
        for sev, kind, msg, addr, t_hw in self.masters.ingest(mid, d, wall=wall):
            self.log(sev, "Multi-master", kind, msg, addr, t_hw)
        if record and self.source in ("live", "demo") and t not in ("sensor_telemetry",):
            # Saved with the capture so replay reproduces attribution and arbitration events (a demo capture keeps
            # its SIMULATED controller records, each still marked "simulated": true).
            self.raw_lines.append(_json.dumps({"type": "master_record", "slot": slot, "record": d},
                                              separators=(",", ":")).encode())
            c = self.masters.state[mid].clock
            saved = self._clock_saved.get(mid)
            if c.locked and c.offset_ns is not None and (saved is None or abs(c.offset_ns - saved) > 50_000):
                self._clock_saved[mid] = c.offset_ns
                self.raw_lines.append(_json.dumps({"type": "master_record", "slot": mid, "record": {
                    "type": "master_clock", "master_id": mid, "offset_ns": c.offset_ns}},
                    separators=(",", ":")).encode())
        self.revision += 1

    def _replay_master_record(self, ln):
        try:
            obj = _json.loads(ln)
            d = obj["record"]
            slot = int(obj.get("slot", 1))
            if not isinstance(d, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.transport_error("malformed saved controller record — line dropped")
            return
        self.raw_lines.append(ln if isinstance(ln, bytes) else ln.encode())
        self.ingest_master_record(slot, d, record=False)

    def ingest(self, items) -> None:
        """items: iterable of (raw_line_bytes_or_None, Record)."""
        new = []
        for raw, rec in items:
            if raw is not None:
                packed = rec.type == "edges" and "r" in rec.data
                if packed:
                    self.stream_raw_bytes += len(raw)
                if not packed or self.stream_raw_bytes <= STREAM_RAW_SAVE_CAP:
                    if len(self.raw_lines) == self.raw_lines.maxlen:
                        self.raw_records_evicted += 1
                    self.raw_lines.append(raw if isinstance(raw, bytes) else raw.encode())
                elif self.stream_raw_bytes - len(raw) <= STREAM_RAW_SAVE_CAP:
                    self.log("warning", "Session", "Waveform stream not saved further",
                             f"the saved capture keeps the first {STREAM_RAW_SAVE_CAP // 2 ** 20} MB of edge-stream "
                             "records; segments are always saved")
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
        elif t == "edges" and "r" in d:
            self._stream_edges(d)
            return done
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
                if self.capture_start_ns is None:
                    self.capture_start_ns = samples[0][0]
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
            ts = (d["t_sample"] * self.tick_ns) if ("t_sample" in d and self.caps.tick_ns) else d["t_us"] * 1000.0
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

    def _native_times(self, d):
        """Exact sample-index timestamps when the firmware sends them, else integer us."""
        if "start_sample" in d and self.caps.tick_ns:
            return d["start_sample"] * self.tick_ns, d["end_sample"] * self.tick_ns
        return d["start_us"] * 1000.0, d["end_us"] * 1000.0

    def _native_segment(self, d, record_gap=False):
        """Assemble STOP-delimited transactions from C firmware segments.

        A RESTART closes a *message*, not a transaction. The C firmware's
        timing fields are already measured; no digital waveform is invented.
        """
        t0, t1 = self._native_times(d)
        if self.capture_start_ns is None:
            self.capture_start_ns = t0
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
        # The same bus notices the raw-edge decoder raises (kind, severity, wording), so the Error & Diagnostics log lists
        # the NACKs of native-segment captures too (logged with the packet when it is stored; classification unchanged).
        if fr.addr is not None and fr.addr_ack is False:
            self.decoder.notices.append((t1, "error", "Address NACK", f"Error: Invalid / unacknowledged slave address "
                                         f"0x{fr.addr:02X} — no slave pulled SDA LOW at the 9th clock (Address NACK)",
                                         fr.addr))
        if fr.rw == "W" and fr.addr is not None:
            for b, ack in zip(fr.data, fr.acks):
                if ack is False:
                    self.decoder.notices.append((t1, "warning", "Data NACK",
                                                 f"0x{fr.addr:02X} refused written byte 0x{b:02X}", fr.addr))
        if d["truncated"] or d["incomplete"] or not d["address_known"]:
            if txn.status == "open":
                txn.status = "incomplete"
            txn.notes.append("Firmware reports truncated/incomplete segment")
        if d.get("scl_low_over_50us"):
            txn.notes.append(f"{d['scl_low_over_50us']} SCL LOW intervals >50us; clock stretching not proven")
        if d.get("mid_byte_end"):
            self.mid_byte_cuts += 1
            txn.notes.append("segment cut by START/STOP inside a byte: controller abort/timeout, bus recovery or "
                             "a master starting on a busy bus (multi-master collision)")
            self.log("warning", "I2C bus", "Byte cut by START/STOP",
                     f"segment {d.get('seq')} ended {d['end']} inside a byte (abort, recovery or collision)",
                     d["address"] if d["address_known"] else None, t1)
        if d["start"] == "START" and "bus_free_ns" in d:
            txn.timing["bus_free_ns"] = d["bus_free_ns"]          # measured by the analyzer firmware
        f = d.get("scl_hz", 0)
        if f:
            txn.timing.update(frequency_hz=f, period_ns=1e9/f, high_ns=d.get("scl_high_avg_ns"), low_ns=d.get("scl_low_avg_ns"))
            # keyed to its packet: a later raw-edge match replaces this firmware average (perf.retime_transaction)
            self.perf.add_firmware_frequency(f, t1, d.get("scl_high_avg_ns"), d.get("scl_low_avg_ns"), txn=txn)
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

    # ------------------------------------------------------------- continuous edge stream
    def _stream_edges(self, d):
        """Accumulate a packed RLE batch into the rolling edge ring.

        Never clears history (snapshots did) and never marks idle time as a gap: a gap is recorded ONLY when the
        firmware says samples were lost ("gap":true: DMA overrun, capture restart, USB could not keep up).
        Entries repeating the current level are idle heartbeats: they keep the time base moving."""
        tick = self.tick_ns
        r = d["r"]
        # entry i sits at t0 + sum of the dt of entries 1..i (the first entry's dt is 0): one accumulate + comprehension
        samples = [(t * tick, v & 3) for t, v in zip(accumulate((v >> 2 for v in r[1:]), initial=d["t0"]), r)]
        first = samples[0][0]
        if self.capture_start_ns is None:
            self.capture_start_ns = first
        lost_line = self._stream_line_lost
        self._stream_line_lost = False
        if d.get("gap") or lost_line:
            self.stream_gaps += 1
            self.edge_gaps.append(first)                       # real data loss: the only thing that creates a gap
            self.log("warning", "Analyzer", "Edge stream gap",
                     "edge-stream records were dropped or samples were not observed before this point; the waveform "
                     "is not drawn across it (segments are unaffected)", t_hw=first)
            cut = self._stream_decoder.lose_sync(first, "edge stream gap")
            self._add_waveform_transactions(cut)
            self._stream_decoder = I2CDecoder()
        if len(self.edges) + len(samples) > self.edges.maxlen:
            self.edges_evicted += len(self.edges) + len(samples) - self.edges.maxlen
        self.edges.extend(samples)                             # rolling ring (deque maxlen), history kept
        self.stream_active = True
        self.stream_entries += len(samples)
        self.stream_now = samples[-1][0]
        done = self._stream_decoder.feed(samples)
        done += self._stream_decoder.check_timeout(self.stream_now)    # heartbeats close an interrupted transaction
        self._add_waveform_transactions(done)
        self._match_waveform_timing()

    def _add_waveform_transactions(self, txns):
        if txns:
            self.waveform_transactions.extend(txns)
            if len(self.waveform_transactions) > 3000:
                del self.waveform_transactions[:1000]
            self._raw_pending.extend(t for t in txns if t.status == 'ok')
            while len(self._raw_pending) > 3000:
                self._raw_pending.popleft()

    def _match_waveform_timing(self):
        """Attach measured byte times only after raw/segment content agreement."""
        # Runs on every stored packet and every stream batch (~100+/s on a busy two-master bus), so each raw
        # transaction is matched ONCE: re-annotating already matched pairs starved the GUI thread and the serial
        # readers.  An unmatched raw (its segment not here yet) is retried until the bus has moved on by 5 s.
        # Every unmatched raw transaction waits in _raw_pending (a burst - the ARB test makes ~400 transfers in 1.5 s -
        # pushed raws out of the old "last 48" window unmatched, so the winning packet had no measured bit times and no
        # decoder-track boxes); its packet is found by binary search on the time-ordered packet list.
        import bisect
        latest = self.packets[-1].t_start if self.packets else None
        if self.stream_active:
            pending, self._raw_pending = self._raw_pending, deque()
        else:
            pending = [r for r in self.waveform_transactions if r.status == 'ok']
        keep = []
        for raw in pending:
            if getattr(raw, "timing_matched", False) or raw.status != 'ok':
                continue
            if latest is not None and latest - raw.t_start > 5e9:
                raw.timing_matched = True
                continue
            i = bisect.bisect_left(self.packets, raw.t_start - 1000, key=lambda t: t.t_start)
            cands = []
            while i < len(self.packets) and self.packets[i].t_start <= raw.t_start + 1000:
                cands.append(self.packets[i])
                i += 1
            if not cands:
                keep.append(raw)                                # its packet has not been decoded yet: retry later
                continue
            for txn in sorted(cands, key=lambda t: abs(t.t_start - raw.t_start)):
                def signature(t):
                    return [(f.addr, f.rw, f.data, f.acks, f.addr_ack) for f in t.frames]
                raw.timing_matched = True
                if signature(raw) != signature(txn):
                    if txn.id not in self.decode_disagreements:
                        self.decode_disagreements.add(txn.id)
                        self.log('error', 'Analyzer', 'Decode disagreement', f'Packet #{txn.id}: raw waveform and firmware segment disagree; preserved both sources', t_hw=raw.t_start)
                    continue
                if len(raw.bytes) != len(txn.bytes):
                    continue
                self.decode_agreements += 1
                txn.raw_verified = True                         # two decoders (Pico firmware + PC) agree on it
                for original, measured in zip(txn.bytes, raw.bytes):
                    original.t_first, original.t_ack = measured.t_first, measured.t_ack
                    original.bit_times, original.t_end = measured.bit_times, measured.t_end
                scl_timing.annotate(txn, resolution_ns=self.tick_ns or None)   # cycles now come from the raw window
                # the statistics follow the packet: its firmware per-segment average is REPLACED by the same captured
                # cycles (cumulative / rolling / per-second / min-max / per master), so every view shows one number
                self.perf.retime_transaction(txn, raw)
                if self.edges_available(raw.t_start, raw.t_end or raw.t_start):
                    ts, ss = self.edges_between(raw.t_start, raw.t_end or raw.t_start)
                    txn.timing.update(observed_data_timing(zip(ts, ss), self.tick_ns or None))
                self.revision += 1
                break
        if self.stream_active:
            self._raw_pending.extendleft(reversed(keep))

    def update_master_telemetry(self, rtc_iso, temperature_c, pressure_hpa, master_id=1):
        """Separate, explicitly identified master-USB data; not bus-decoded."""
        import time
        try:
            datetime_value = _dt.datetime.fromisoformat(rtc_iso)
            t, p = float(temperature_c), float(pressure_hpa)
        except (ValueError, TypeError):
            return
        if not (-40 <= t <= 85 and 300 <= p <= 1100):
            return
        self.master_telemetry = {"rtc": datetime_value, "temperature_c": t, "master_id": master_id,
                                 "pressure_hpa": p, "received_at": time.monotonic()}
        self.master_telemetry_by_id[master_id] = self.master_telemetry
        self.master_telemetry_history.append((time.monotonic(), t, p))
        self.master_telemetry_series.setdefault(master_id, deque(maxlen=1000)).append((time.monotonic(), t, p))
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
            txn.timing.update(observed_data_timing(zip(ts, ss), self.tick_ns or None))
        # The packet's measured SCL first: the tBUF limit below depends on it (it was read before any measurement existed
        # for raw-edge packets, so their tBUF check never ran), and the statistics are fed the SAME cycles afterwards.
        scl_timing.annotate(txn, simulated=self.source == "demo", resolution_ns=self.tick_ns or None)
        if ('bus_free_ns' not in txn.timing and self.packets and self.packets[-1].status == 'ok'
                and not any(self.packets[-1].t_end <= g <= txn.t_start for g in self.edge_gaps)):
            txn.timing['bus_free_ns'] = max(0, txn.t_start-self.packets[-1].t_end)
        free = txn.timing.get('bus_free_ns')
        hz = txn.timing.get('frequency_hz')
        if free is not None and hz:
            tbuf = TBUF_MIN_NS[100_000] if hz <= 110_000 else TBUF_MIN_NS[400_000]
            if free < tbuf:
                self.tbuf_violations += 1
                txn.notes.append(f"bus free time {free / 1e3:.1f} µs < tBUF {tbuf / 1e3:.1f} µs (UM10204) before this "
                                 "START: a controller did not wait for bus-free time")
        self.communication.observe(txn, self.registry.configured)
        self.frame_log.add(txn)
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
        self.masters.on_packet(txn, self.source, self.wall_clock())
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
                   simulated_state=c['status'] if self.source == 'demo' else None,
                   # Arbitration is controller-reported and never part of the NACK policy above.
                   multi_master=self.masters.summary())
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

    def _telemetry_source(self, tele):
        cfg = config.MASTERS.get(tele.get('master_id', 1), {})
        return f"{cfg.get('pico', 'Pico 1')} {cfg.get('label', 'master').lower()} USB telemetry (not bus-decoded)"

    def record_scl_setting(self, requested_hz, applied_hz, result, source, reason="", master_id=None):
        """Remember a controller (or SIMULATED demo) SCL setting change.

        Only packets that START after this capture time can confirm it on the bus."""
        self.scl_settings = getattr(self, "scl_settings", {})
        if master_id is not None:
            self.scl_settings[master_id] = dict(requested_hz=requested_hz, applied_hz=applied_hz, result=result)
        self.scl_setting = dict(requested_hz=requested_hz, applied_hz=applied_hz, result=result, source=source,
                                reason=reason, after_ns=self.hw_now if self.hw_now is not None else 0.0,
                                wall=time.monotonic())
        self.log("info", "Session", "SCL setting",
                 f"{source}: {result} requested {requested_hz} Hz, reported {applied_hz} Hz — bus effect must be measured")
        self.revision += 1

    def scl_confirmation(self, tolerance=0.10, min_packets=3):
        """Compare the requested/applied rate with frequencies MEASURED after the change.

        Returns dict(state=PENDING|CONFIRMED|MISMATCH|UNKNOWN, measured_hz, packets)."""
        st = self.scl_setting
        if st is None or st["result"] not in ("applied", "boot", "current"):
            return dict(state="UNKNOWN", measured_hz=None, packets=0)
        vals = [t.timing["frequency_hz"] for t in self.packets[-2000:]
                if t.t_start > st["after_ns"] and t.status == "ok" and t.timing.get("frequency_hz")]
        if len(vals) < min_packets:
            return dict(state="PENDING", measured_hz=None, packets=len(vals))
        import statistics
        med = statistics.median(vals)
        target = st["applied_hz"] or st["requested_hz"]
        ok = abs(med - target) <= tolerance * target
        return dict(state="CONFIRMED" if ok else "MISMATCH", measured_hz=med, packets=len(vals))

    def device_activity(self, window_s=None):
        """Per-device READ/WRITE frame activity (see analysis.device_activity)."""
        from analysis import device_activity
        key = (self.revision, self.hw_now, window_s, self.source, self.measurement_trust()[0])
        if getattr(self, "_activity_key", None) != key:
            self._activity = device_activity.compute(self, window_s or device_activity.WINDOW_S)
            self._activity_key = key
        return self._activity

    def rtc_status(self, stale_after_s=3.0):
        """Provenance-tagged DS3231 time. Never uses the PC wall clock.

        Returns dict(state=VALID|STALE|UNAVAILABLE, text, source, t_ns).
        Sources: bus-decoded DS3231 read (live/replay), clearly labelled demo
        simulation, or separately identified master USB telemetry.
        """
        r = self.sensors.rtc.time
        osf_t = self.sensors.rtc.osf_t
        if r is None and osf_t is not None:
            return dict(state='INVALID', source='bus-decoded DS3231 status 0x0F: OSF set (oscillator stopped)',
                        t_ns=osf_t, age_s=None, text='time invalid')
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
                        source=self._telemetry_source(tele),
                        text=tele['rtc'].strftime('%Y-%m-%d %H:%M:%S'))
        return dict(state='UNAVAILABLE', source='no valid DS3231 record', t_ns=None, age_s=None, text='—')
