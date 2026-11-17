"""Reproduce/verify: inspecting devices must never freeze the 3D diagram or stop capture.

Drives the REAL MainWindow with analyzer-format (native segment) records fed through
the same MainWindow._on_lines path the serial worker uses, at the bench's real
traffic mix (from the 2026-11-09 hardware capture): 0x76 every 200 ms, 0x68 every 1 s,
0x3C framebuffer writes.  These are REPLAYED records, not live hardware.

Each step performs a real user action, then lets traffic flow for 2 s and records:
capture flags, packets received, whether the diagram moved to new packets, pinned
state, and which code path called select_transaction().

    python -X utf8 tools/repro_inspect_freeze.py                # replayed records
    python -X utf8 tools/repro_inspect_freeze.py --port COM28    # LIVE analyzer (GUI must not hold the port)
"""
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
from PyQt6.QtCore import QItemSelectionModel, QTimer   # noqa: E402
from PyQt6.QtWidgets import QApplication            # noqa: E402

HELLO = {"type": "hello", "protocol": 1, "role": "analyzer", "board": "RP2040", "sample_rate": 2000000,
         "sample_resolution_us": 0.5, "capabilities": ["segment", "loss", "status", "7bit", "10bit", "ack",
                                                         "timing", "edges", "sample_ts"], "waveform": False}


class Bus:
    """Native analyzer records with the bench's schedule; t in microseconds."""

    def __init__(self):
        self.seq, self.t, self.rtc_s = 1, 1_000_000, 0

    def seg(self, start, end, kind0, kind1, addr, rw, data, acks):
        d = dict(type="segment", seq=self.seq, start_us=start, end_us=end, duration_us=end - start,
                 start_sample=start * 2, end_sample=end * 2, start=kind0, end=kind1, address=addr,
                 address_bits=7, address_known=True, direction=rw, address_ack=True, scl_hz=100000,
                 scl_high_avg_ns=4000, scl_low_avg_ns=6000, scl_low_over_50us=0, truncated=False,
                 incomplete=False, data=data, acks=acks)
        self.seq += 1
        return json.dumps(d).encode()

    def read(self, addr, reg, values, t):
        return [self.seg(t, t + 250, "START", "RESTART", addr, "write", [reg], [True]),
                self.seg(t + 250, t + 250 + 90 * len(values), "RESTART", "STOP", addr, "read", values,
                         [True] * (len(values) - 1) + [False])]

    def until(self, t_end_us):
        out = []
        while self.t < t_end_us:
            t = self.t
            out += self.read(0x76, 0xF7, [0x65, 0x5D, 0x40, 0x7E, 0xF4, (t // 1000) & 0xFF], t)
            if (t // 200_000) % 5 == 0:
                s = self.rtc_s; self.rtc_s += 1
                out += self.read(0x68, 0x00, [s % 60 // 10 << 4 | s % 10, 0x22, 0x10, 2, 9, 0x11, 0x26] + [0] * 7 + [0x1C, 0x08], t + 2000)
                for k in range(8):
                    out.append(self.seg(t + 4000 + k * 2000, t + 5700 + k * 2000, "START", "STOP", 0x3C, "write",
                                        [0x40] + [(t // 1000 + k) & 0xFF] * 16, [True] * 17))
            self.t += 200_000
        return out


def run(port=None):
    app = QApplication.instance() or QApplication([])
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    w.resize(1680, 980); w.show()
    # State as after "Connect" + "Start capture" on a live analyzer (no serial port opened here).
    w.worker = type("FakeWorker", (), {"send": lambda self, c: None, "isRunning": lambda self: True,
                                        "port": "REPLAY", "stop": lambda self: None,
                                        "wait": lambda self, t=0: True})()
    w.capturing = True
    w.engine.set_source("live"); w.engine.stream_connected = True; w.engine.capture_active = True
    bus = Bus()
    sim = {"us": 1_000_000, "pending": b""}
    ser = None
    if port:
        import serial
        ser = serial.Serial(port, 115200, timeout=0)
        ser.dtr = True
        import time as _t; _t.sleep(0.3)
        ser.write(b"HELLO\n"); _t.sleep(0.2); ser.write(b"START\n")
        print(f"LIVE hardware: reading analyzer records from {port}", flush=True)
    else:
        w._on_lines([json.dumps(HELLO).encode()])
    callers = []
    orig = w.select_transaction

    def spy(txn):
        stack = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_stack()[:-1]
                 if "i2c_3d_analyzer" in f.filename and "repro_inspect" not in f.filename]
        callers.append((txn.id, " <- ".join(reversed(stack[-3:]))))
        return orig(txn)
    w.select_transaction = spy

    def feed():                                   # 50 ms of bus traffic every 50 ms of wall time
        if ser is not None:                       # real analyzer bytes, same path as SerialWorker
            sim["pending"] += ser.read(ser.in_waiting or 0)
            *lines, sim["pending"] = sim["pending"].split(b"\n")
            lines = [ln.strip() for ln in lines if ln.strip()]
            if lines:
                w._on_lines(lines)
            return
        sim["us"] += 50_000
        w._on_lines(bus.until(sim["us"]))
    feeder = QTimer(); feeder.timeout.connect(feed); feeder.start(50)

    results = []

    def observe(label, then):
        before = dict(pk=len(w.engine.packets), shown=w.playback.txn.id if w.playback.txn else None)
        shown = set()
        check = {"samples": 0, "mismatch": [], "nonmonotonic_capture": 0}
        import re
        by_id = lambda i: next((t for t in reversed(w.engine.packets) if t.id == i), None)

        def sample():
            shown.add(w.playback.txn.id if w.playback.txn else None)
            text = w.p_net.net.transaction_overlay.text()
            m = re.search(r"Packet #(\d+) · 0x([0-9A-F]{2}) \(7-bit\).*?byte (0x[0-9A-F]{2}|—)", text)
            st = w.playback.current
            if not m or st is None:
                return
            check["samples"] += 1
            t = by_id(int(m.group(1)))
            problems = []
            if t is None:
                problems.append("packet id not in capture")
            else:
                if int(m.group(2), 16) not in {f.addr for f in t.frames}:
                    problems.append("address")
                wire = {b.value for b in t.bytes}
                if m.group(3) != "—" and int(m.group(3), 16) not in wire:
                    problems.append("byte not in packet")
                if st.ack is True and ">ACK<" not in text:
                    problems.append("ACK not shown")
                if st.ack is False and ">NACK<" not in text:
                    problems.append("NACK not shown")
                if st.ack is not None and st.byte is not None:
                    actual = [b.ack for b in t.bytes if b.value == st.byte]
                    if st.ack not in actual:
                        problems.append("ack/nack differs from capture")
            if problems:
                check["mismatch"].append((m.group(1), problems))
        probe = QTimer()
        probe.timeout.connect(sample)
        probe.start(100)

        def done():
            probe.stop()
            focus = getattr(w, "device_filter", None)
            new_pk = len(w.engine.packets) - before["pk"]
            new_focus_pk = [t.id for t in w.engine.packets[-new_pk:]
                            if focus is None or any(f.addr == focus for f in t.frames)] if new_pk else []
            moved = {s for s in shown if s is not None and s > (before["shown"] or 0)}
            stage_txn = w.playback.txn
            results.append(dict(step=label, capture_active=w.engine.capture_active, capturing=w.capturing,
                                packets_in_2s=new_pk, focus=None if focus is None else f"0x{focus:02X}",
                                diagram_showed_new_packets=len(moved), pinned=w.playback.pinned,
                                shown_now=stage_txn.id if stage_txn else None,
                                shown_addr=f"0x{stage_txn.address:02X}" if stage_txn and stage_txn.address is not None else None,
                                newest_focus_packet=new_focus_pk[-1] if new_focus_pk else None,
                                overlay_dash_eq_net=w.p_dash.net.transaction_overlay.text() == w.p_net.net.transaction_overlay.text(),
                                samples=check["samples"], mismatches=len(check["mismatch"]),
                                mismatch_detail=check["mismatch"][:3],
                                lag_packets=(sum(1 for t in w.engine.packets if t.id > (stage_txn.id if stage_txn else 0)
                                                 and (focus is None or any(f.addr == focus for f in t.frames))))))
            then()
        QTimer.singleShot(2000, done)

    def click_packet_row():
        # The user selects a packet row in Packet Analyzer (legitimate deliberate pin).
        pk = w.p_pk
        pk.table.selectionModel().select(pk.proxy.index(max(0, pk.proxy.rowCount() - 3), 0),
                                         QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)

    steps = [("baseline (no inspection)", lambda: None)]
    for rnd in (1, 2):
        for a in (0x68, 0x76, 0x3C):
            steps.append((f"round {rnd}: inspect 0x{a:02X} (3D node click)", lambda a=a: w.p_net.net.node_clicked.emit(a)))
    steps += [("select a packet row (Packet Analyzer)", click_packet_row),
              ("then inspect 0x68 (3D node click)", lambda: w.p_net.net.node_clicked.emit(0x68)),
              ("then inspect 0x76 (Device Activity row)", lambda: w.focus_device(0x76)),
              ("then inspect 0x3C (3D node click)", lambda: w.p_net.net.node_clicked.emit(0x3C)),
              ("Pause display", lambda: w._pause_display(True)),
              ("Resume display", lambda: w._pause_display(False))]
    it = iter(steps)

    def next_step():
        try:
            label, act = next(it)
        except StopIteration:
            feeder.stop()
            if ser is not None:
                ser.write(b"STOP\n"); ser.close()
            print(f"{'step':<44}{'capture':>8}{'pkts/2s':>8}  {'focus':>6}{'new shown':>10}{'pinned':>7}{'shown':>7}{'addr':>6}"
                  f"{'checks':>7}{'bad':>5}{'behind':>7}")
            for r in results:
                print(f"{r['step']:<44}{str(r['capture_active']):>8}{r['packets_in_2s']:>8}  {str(r['focus']):>6}"
                      f"{r['diagram_showed_new_packets']:>10}{str(r['pinned']):>7}{str(r['shown_now']):>7}{str(r['shown_addr']):>6}"
                      f"{r['samples']:>7}{r['mismatches']:>5}{r['lag_packets']:>7}")
                if r["mismatch_detail"]:
                    print("      mismatches:", r["mismatch_detail"])
            print("\nselect_transaction() callers (txn id <- call path):")
            for c in callers:
                print("  ", c)
            print("\npackets retained:", len(w.engine.packets), "· lost records:", w.engine.lost_records,
                  "· rejected:", w.engine.transport_errors)
            app.quit()
            return
        w.nav.setCurrentRow(1)
        act()
        observe(label, next_step)

    QTimer.singleShot(500, next_step)
    app.exec()
    return results, callers, w


if __name__ == "__main__":
    port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None
    run(port)
