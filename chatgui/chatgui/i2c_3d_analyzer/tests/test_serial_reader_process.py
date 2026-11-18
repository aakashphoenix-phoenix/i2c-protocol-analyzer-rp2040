"""The analyzer port's separate reader process (communication/serial_child.py), driven for real through pyserial's
loop:// port (every byte written comes back as received data).

Why it exists (measured on the live bench, 2026-11-16): the analyzer sends ~54 kB/s in 1 Hz bursts of ~350 kB/s and
Windows' usbser queues only ~25 kB, so a reader that is ~70 ms late during a burst loses data.  In its own process the
reader is never held up by the GUI; the bytes wait in the child's memory instead."""
import os
import subprocess
import sys
import time
from pathlib import Path

from communication.serial_child import read_frame
from tests.test_hardware_pipeline import pump, qt_app      # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def child(port="loop://"):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / ".runtime_dependencies")]))
    return subprocess.Popen([sys.executable, "-u", "-m", "communication.serial_child", port, "115200", "0"], cwd=ROOT,
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=0)


def test_child_opens_echoes_heartbeats_and_stops_on_stdin_eof():
    p = child()
    assert read_frame(p.stdout) == (b"O", b"loop://")
    p.stdin.write(b"HELLO\n"); p.stdin.flush()
    got, kinds = b"", set()
    t0 = time.monotonic()
    while b"HELLO\n" not in got and time.monotonic() - t0 < 5:
        k, data = read_frame(p.stdout)
        kinds.add(k)
        got += data if k == b"D" else b""
    assert got == b"HELLO\n"
    k, _ = read_frame(p.stdout)
    assert k == b"H"                                            # idle: heartbeats keep the parent's loop serviced
    p.stdin.close()                                             # EOF = stop
    frames = []
    while (f := read_frame(p.stdout)) is not None:
        frames.append(f)
    assert frames[-1][0] == b"C" and p.wait(5) == 0


def test_child_reports_a_port_that_cannot_be_opened():
    p = child("COM_DOES_NOT_EXIST_99")
    k, msg = read_frame(p.stdout)
    assert k == b"E" and b"Cannot open COM_DOES_NOT_EXIST_99" in msg
    assert p.wait(5) == 1


def test_child_keeps_reading_while_the_gui_side_does_not_read():
    """The parent stops reading for a while (a GUI stall); every byte is still delivered, intact and in order."""
    p = child()
    assert read_frame(p.stdout)[0] == b"O"
    lines = [f'{{"type":"segment","seq":{i},"pad":"{"x" * 180}"}}\n'.encode() for i in range(6000)]   # ~1.2 MB
    want = b"".join(lines)
    for i in range(0, len(lines), 200):
        p.stdin.write(b"".join(lines[i:i + 200])); p.stdin.flush()
    time.sleep(1.5)                                             # nobody reads stdout: the pipe is full long before
    got = bytearray()
    t0 = time.monotonic()
    while len(got) < len(want) and time.monotonic() - t0 < 20:
        k, data = read_frame(p.stdout)
        if k == b"D":
            got += data
    p.stdin.close()
    p.wait(5)
    assert bytes(got) == want


def test_serial_worker_reads_the_analyzer_through_the_reader_process(qt_app, monkeypatch):
    from communication.serial_manager import SerialWorker
    monkeypatch.setattr(SerialWorker, "USE_READER_PROCESS", True)
    w = SerialWorker("loop://")
    assert w._use_reader_process()
    seen, events = [], []
    w.lines_ready.connect(seen.extend)
    w.connected.connect(lambda p: events.append(("connected", p)))
    w.disconnected.connect(lambda r: events.append(("disconnected", r)))
    w.start()
    try:
        pump(qt_app, lambda: any(ln == b"HELLO" for ln in seen))       # the worker's own HELLO probe, looped back
        assert ("connected", "loop://") in events and w.ser.kind == "reader process"
        proc = w.ser.proc
    finally:
        w.stop()
        assert w.wait(5000)
    pump(qt_app, lambda: any(e[0] == "disconnected" for e in events))
    assert ("disconnected", "disconnected by user") in events
    assert proc.wait(5) == 0                                      # the child closed the port and exited


def test_controller_ports_stay_in_the_worker_thread():
    from communication.serial_manager import SerialWorker
    w = SerialWorker("COM32", telemetry=True)
    w.USE_READER_PROCESS = True
    assert not w._use_reader_process()
