"""USB-serial link to the analyzer Pico (background QThread + Qt signals)."""
from __future__ import annotations

import time
import logging
from collections import deque

from PyQt6.QtCore import QThread, pyqtSignal

import config
from communication.protocol_parser import LineAssembler, command

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


class SerialWorker(QThread):
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

    def stop(self):
        self._run = False

    def run(self):
        if serial is None:
            self.failed.emit("pyserial is not installed (pip install pyserial)")
            return
        try:
            self.ser = serial.Serial(self.port, config.SERIAL_BAUD, timeout=config.SERIAL_READ_TIMEOUT_S, write_timeout=0.25)
            try:
                self.ser.dtr = True     # Pico USB CDC only transmits once DTR is asserted
            except Exception:
                pass
        except Exception as e:
            self.failed.emit(f"Cannot open {self.port}: {e}")
            return
        log.info("Opened %s: USB CDC / %s baud, DTR asserted", self.port, config.SERIAL_BAUD)
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
                while self._tx:
                    payload = self._tx.popleft()
                    log.debug("%s TX %r", self.port, payload)
                    self.ser.write(payload)
                if not self._run:
                    break
                chunk = self.ser.read(self.ser.in_waiting or 1)
                if chunk:
                    self.bytes_rx += len(chunk)
                    previous = asm.overlong
                    batch.extend(asm.feed(chunk))
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
            reason = f"link lost: {e}"
            log.warning("%s %s", self.port, reason)
        finally:
            if batch:
                self.lines_ready.emit(batch)
            try:
                self.ser.close()
            except Exception:
                pass
            self.disconnected.emit(reason)
