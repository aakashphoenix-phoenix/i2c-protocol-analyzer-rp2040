"""USB-serial link to the analyzer Pico (background QThread + Qt signals)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque

from PyQt6.QtCore import QThread, pyqtSignal

import config
from communication.protocol_parser import LineAssembler, command
from communication.serial_child import read_frame

try:
    import serial
    import serial.tools.list_ports
except ImportError:          # pragma: no cover - reported in the GUI
    serial = None

log = logging.getLogger(__name__)

RPI_VID = 0x2E8A


def list_ports():
    """[(device, description, is_pico)] sorted with Raspberry Pi boards first."""
    if serial is None:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        is_pico = p.vid == RPI_VID
        desc = p.description or p.device
        if p.vid is not None:
            desc += f"  [{p.vid:04X}:{p.pid:04X}]"
        out.append((p.device, desc, is_pico))
    out.sort(key=lambda x: (not x[2], x[0]))
    return out


def _is_controller_hello(line: bytes) -> bool:
    return b'"type":"hello"' in line and b'"role":"controller"' in line


def _is_txlog_on(line: bytes) -> bool:
    # the reply to "TXLOG 1", or any master_stats line (printed every second) that says the log is on
    return ((b'"type":"txlog"' in line and b'"enabled":true' in line)
            or (b'"type":"master_stats"' in line and b'"txlog":true' in line))


def _is_scl_reply(line: bytes) -> bool:
    return b'"type":"scl_config"' in line


class Handshake:
    """Confirm-and-retry command sequence for a CONTROLLER (master) port.

    Right after DTR is asserted the Pico's USB CDC can drop the first bytes, so a command fired once may
    be lost without any error.  Each step is re-sent every `interval` seconds until its confirming reply
    arrives (or `timeout` expires):  HELLO -> controller hello;  TXLOG 1 -> {"type":"txlog","enabled":true}
    (or a master_stats line with "txlog":true);  SCL? only after HELLO was confirmed (the controller is
    demonstrably listening by then).  All commands are idempotent."""

    def __init__(self, interval=0.6, timeout=10.0, steps=None):
        self.interval, self.timeout = interval, timeout
        self.steps = steps or [("HELLO", _is_controller_hello), ("TXLOG 1", _is_txlog_on)]
        self.after_hello = [("SCL?", _is_scl_reply)]
        self.confirmed = set()
        self.t0 = None
        self._last = {}
        self.attempts = {}

    def _pending(self):
        todo = [st for st in self.steps if st[0] not in self.confirmed]
        if "HELLO" in self.confirmed or not any(st[0] == "HELLO" for st in self.steps):
            todo += [st for st in self.after_hello if st[0] not in self.confirmed]
        return todo

    def due(self, now):
        """Commands (str) to send now: every unconfirmed step whose retry interval elapsed."""
        if self.t0 is None:
            self.t0 = now
        out = []
        if self.expired(now):
            return out
        for cmd, _pred in self._pending():
            if now - self._last.get(cmd, -1e9) >= self.interval:
                self._last[cmd] = now
                self.attempts[cmd] = self.attempts.get(cmd, 0) + 1
                out.append(cmd)
        return out

    def feed(self, lines):
        for ln in lines:
            for cmd, pred in self.steps + self.after_hello:
                if cmd not in self.confirmed and pred(ln):
                    self.confirmed.add(cmd)

    @property
    def done(self):
        return not self._pending()

    def expired(self, now):
        return self.t0 is not None and not self.done and now - self.t0 > self.timeout

    def missing(self):
        return [cmd for cmd, _ in self._pending()]


def port_serials():
    """{device: USB serial number} for every port that has one (stable across COM renumbering)."""
    if serial is None:
        return {}
    return {p.device: p.serial_number for p in serial.tools.list_ports.comports() if p.serial_number}


RX_QUEUE_BYTES = 4 * 1024 * 1024   # Windows driver receive queue for every Pico USB port (see SerialWorker.run)

class SerialWorker(QThread):
    handshake_ok = pyqtSignal(list)     # confirmed command names
    handshake_failed = pyqtSignal(list) # commands never confirmed within the timeout (port stays open)
    lines_ready = pyqtSignal(list)      # list[bytes], batched ~every 30 ms
    connected = pyqtSignal(str)
    disconnected = pyqtSignal(str)      # reason
    failed = pyqtSignal(str)

    def __init__(self, port: str, parent=None, telemetry: bool = False):
        super().__init__(parent)
        self.port = port
        self._run = True
        self._tx = deque()
        self.telemetry = telemetry
        self.native_commands = False
        self.handshake_received = False
        self.ser = None
        self.bytes_rx = 0
        self.handshake = None            # Handshake (controller ports): retried until confirmed
        self._hs_reported = False

    def send(self, cmd: str):
        if self.telemetry:
            return
        if self.native_commands:
            self._tx.append((cmd.upper() + "\n").encode("ascii"))
        elif cmd == "hello":
            # The native RP2040 emits its own hello at connection; it also
            # understands HELLO, unlike the earlier GUI edge protocol.
            self._tx.append(b"HELLO\n")
        else:
            self._tx.append(command(cmd))

    def send_controller(self, line: str):
        """Raw command to the CONTROLLER Pico's own USB port (telemetry worker only).

        The passive analyzer worker never accepts SCL commands."""
        if not self.telemetry:
            raise RuntimeError("controller commands go to the controller (master) port only")
        self._tx.append((line.strip() + "\n").encode("ascii"))

    def require_handshake(self, handshake=None):
        """Controller ports: send HELLO / TXLOG 1 (/ SCL?) and keep re-sending until each is confirmed."""
        if not self.telemetry:
            raise RuntimeError("only controller (master) ports have a handshake")
        self.handshake = handshake or Handshake()

    def stop(self):
        self._run = False

    # The analyzer port is read by a separate process (communication/serial_child.py): see its docstring for the
    # measured reason.  Controller (master) ports carry little data and stay in this thread.  A frozen build (no
    # python executable to run the child with) and tests that patch serial.Serial use the in-thread reader.
    USE_READER_PROCESS = True

    def _use_reader_process(self):
        return (self.USE_READER_PROCESS and not self.telemetry and serial is not None
                and not getattr(sys, "frozen", False))

    def run(self):
        if serial is None:
            self.failed.emit("pyserial is not installed (pip install pyserial)")
            return
        src = _ChildPort(self.port) if self._use_reader_process() else _ThreadPort(self.port)
        err = src.open()
        if err is not None:
            self.failed.emit(err)
            return
        self.ser = src
        log.info("Opened %s: USB CDC / %s baud, DTR asserted (%s)", self.port, config.SERIAL_BAUD, src.kind)
        self.connected.emit(self.port)
        if not self.telemetry:
            self.send("hello")
        asm = LineAssembler()
        batch: list[bytes] = []
        last_emit = time.monotonic()
        last_probe = last_emit
        last_log = last_emit
        reason = "disconnected by user"
        try:
            while self._run or self._tx:
                now = time.monotonic()
                if self._run and not self.telemetry and not self.handshake_received and now - last_probe >= 0.5:
                    # Probe both supported protocols; tolerate Pico boot delay.
                    self._tx.extend((b"HELLO\n", command("hello")))
                    last_probe = now
                if self.handshake is not None and self._run:
                    for cmd in self.handshake.due(now):
                        self._tx.append((cmd + "\n").encode("ascii"))
                    if self.handshake.done and not self._hs_reported:
                        self._hs_reported = True
                        self.handshake_ok.emit(sorted(self.handshake.confirmed))
                    elif self.handshake.expired(now) and not self._hs_reported:
                        self._hs_reported = True
                        self.handshake_failed.emit(self.handshake.missing())
                while self._tx:
                    payload = self._tx.popleft()
                    log.debug("%s TX %r", self.port, payload)
                    if not src.write(payload):
                        # CDC not ready yet (just after DTR): keep the command, the handshake re-sends anyway
                        self._tx.appendleft(payload)
                        break
                if not self._run:
                    break
                chunk = src.read()
                if chunk:
                    self.bytes_rx += len(chunk)
                    previous = asm.overlong
                    new_lines = asm.feed(chunk)
                    batch.extend(new_lines)
                    if self.handshake is not None and new_lines:
                        self.handshake.feed(new_lines)
                    if asm.overlong != previous:
                        log.warning("%s: discarded overlong serial frame (%s total)", self.port, asm.overlong)
                now = time.monotonic()
                if now - last_log >= 2:
                    log.info("%s RX bytes=%s, framing discards=%s", self.port, self.bytes_rx, asm.overlong)
                    last_log = now
                if batch and (now - last_emit > 0.03 or len(batch) > 2000):
                    self.lines_ready.emit(batch)
                    batch = []
                    last_emit = now
        except Exception as e:      # USB unplugged, device reset, ...
            reason = e.reason if isinstance(e, _LinkLost) else f"link lost: {e}"
            log.warning("%s %s", self.port, reason)
        finally:
            if batch:
                self.lines_ready.emit(batch)
            src.close()
            self.disconnected.emit(reason)


class _LinkLost(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _ThreadPort:
    """The port read in the worker thread itself (controller ports; fallback for the analyzer)."""
    kind = "in-thread reader"

    def __init__(self, port):
        self.port = port
        self.s = None

    def open(self):
        try:
            self.s = serial.Serial(self.port, config.SERIAL_BAUD, timeout=config.SERIAL_READ_TIMEOUT_S,
                                   write_timeout=0.25)
            try:
                self.s.dtr = True     # Pico USB CDC only transmits once DTR is asserted
            except Exception:
                pass
            try:
                # Windows: ask for a large driver receive queue (SetupComm).  NOTE (measured 2026-11-16 on the
                # bench): usbser.sys accepts the call but still queues only ~25 kB (~0.5 s of the analyzer stream,
                # far less during its 1 Hz bursts); that is why the analyzer port is read by serial_child.py.
                self.s.set_buffer_size(rx_size=RX_QUEUE_BYTES, tx_size=4096)
            except Exception:           # not Windows / not supported by the driver: unchanged behaviour
                pass
        except Exception as e:
            return f"Cannot open {self.port}: {e}"
        return None

    def read(self):
        return self.s.read(self.s.in_waiting or 1)

    def write(self, payload):
        try:
            self.s.write(payload)
            return True
        except serial.SerialTimeoutException:
            return False

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


class _ChildPort:
    """The port read by communication/serial_child.py in its own process; bytes arrive through a pipe."""
    kind = "reader process"
    OPEN_TIMEOUT_S = 10.0

    def __init__(self, port):
        self.port = port
        self.proc = None

    def open(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([root, os.path.join(root, ".runtime_dependencies"),
                                             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "communication.serial_child", self.port, str(config.SERIAL_BAUD),
                 str(RX_QUEUE_BYTES)], cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            return f"Cannot start the serial reader for {self.port}: {e}"
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("f", read_frame(self.proc.stdout)), daemon=True)
        t.start()
        t.join(self.OPEN_TIMEOUT_S)
        f = got.get("f")
        if f is not None and f[0] == b"O":
            return None
        self.close()
        if f is not None and f[0] == b"E":
            return f[1].decode("utf-8", "replace")
        return f"Cannot open {self.port}: the serial reader process did not start"

    def read(self):
        """Received bytes (b'' for a heartbeat: the child sends one every 50 ms without data)."""
        f = read_frame(self.proc.stdout)
        if f is None:
            raise _LinkLost("link lost: serial reader process ended")
        kind, payload = f
        if kind == b"D":
            return payload
        if kind == b"C":
            raise _LinkLost(payload.decode("utf-8", "replace") or "link lost")
        return b""

    def write(self, payload):
        try:
            self.proc.stdin.write(payload if payload.endswith(b"\n") else payload + b"\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise _LinkLost(f"link lost: serial reader process ({e})")
        return True

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()          # EOF = stop: the child closes the port and exits
        except Exception:
            pass
        try:
            self.proc.wait(2.0)
        except Exception:
            self.proc.kill()
        try:
            self.proc.stdout.close()
        except Exception:
            pass
