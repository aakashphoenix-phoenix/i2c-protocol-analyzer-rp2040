"""Analyzer serial reader in its OWN process:  python -m communication.serial_child PORT BAUD RX_QUEUE_BYTES

Why (measured on the live bench, 2026-11-16): the analyzer streams ~54 kB/s, but not evenly - once a second Master 1
refreshes the OLED (~70 back-to-back writes, ~130 ms of bus) and the analyzer reports those segments as they happen,
at ~350 kB/s.  Windows' usbser driver queues only ~25 kB, so the port must be read within ~70 ms during every burst,
or the Pico's USB stdio drops characters: a corrupted line, a lost record and a capture marked unreliable.  Inside the
GUI process the reader thread shares the interpreter lock with the GUI thread (painting, garbage collection,
screenshots), so it is sometimes late.  This process does nothing but read the port into an unbounded in-memory queue;
a separate thread forwards the bytes to the GUI, so a busy GUI only delays delivery, it never loses data.

Wire format on stdout, one frame each:  1 byte kind + 4 byte little-endian length + payload
    O  port opened             (payload: port name)
    E  could not open the port (payload: error text)              -> the process exits
    D  received bytes
    H  heartbeat, every HEARTBEAT_S without data (lets the parent service its command queue / handshake)
    C  link closed             (payload: reason)                  -> the process exits
stdin: raw bytes to transmit (whole command lines).  EOF on stdin = stop: close the port and exit.
Only the standard library and pyserial are imported (fast start, small heap).
"""
from __future__ import annotations

import struct
import sys
import threading
import time
from collections import deque

HEARTBEAT_S = 0.05
MAX_BUFFER_BYTES = 256 * 1024 * 1024        # far beyond any GUI stall; beyond it the oldest bytes are dropped


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("<I", len(payload)) + payload


def read_frame(stream):
    """(kind, payload) from a file-like stream; None at EOF."""
    head = _read_exact(stream, 5)
    if head is None:
        return None
    n = struct.unpack("<I", head[1:])[0]
    payload = _read_exact(stream, n) if n else b""
    if payload is None:
        return None
    return head[:1], payload


def _read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def main(argv):
    port = argv[1]
    baud = int(argv[2]) if len(argv) > 2 else 115200
    rx_queue = int(argv[3]) if len(argv) > 3 else 0
    out = sys.stdout.buffer
    try:
        import serial
        ser = serial.serial_for_url(port, baud, timeout=0.01, write_timeout=0.25)
        try:
            ser.dtr = True                  # Pico USB CDC only transmits once DTR is asserted
        except Exception:
            pass
        if rx_queue:
            try:
                ser.set_buffer_size(rx_size=rx_queue, tx_size=4096)
            except Exception:
                pass
    except Exception as e:
        out.write(frame(b"E", f"Cannot open {port}: {e}".encode("utf-8", "replace")))
        out.flush()
        return 1
    out.write(frame(b"O", port.encode()))
    out.flush()

    buf: deque = deque()
    size = [0]
    cond = threading.Condition()
    state = dict(run=True, reason="disconnected by user")

    def reader():
        try:
            while state["run"]:
                data = ser.read(ser.in_waiting or 1)
                if data:
                    with cond:
                        buf.append(data)
                        size[0] += len(data)
                        while size[0] > MAX_BUFFER_BYTES:
                            size[0] -= len(buf.popleft())
                        cond.notify()
        except Exception as e:              # USB unplugged, device reset, ...
            state["reason"] = f"link lost: {e}"
        finally:
            state["run"] = False
            with cond:
                cond.notify()

    def commands():
        stdin = sys.stdin.buffer
        try:
            while state["run"]:
                line = stdin.readline()
                if not line:                # parent closed stdin: stop
                    break
                for _attempt in range(8):   # CDC not ready yet (just after DTR): retry, the parent re-sends anyway
                    try:
                        ser.write(line)
                        break
                    except Exception as e:
                        if type(e).__name__ != "SerialTimeoutException":
                            raise
                        time.sleep(0.05)
        except Exception as e:
            state["reason"] = f"link lost: {e}"
        finally:
            state["run"] = False
            with cond:
                cond.notify()

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=commands, daemon=True).start()
    try:
        while True:
            with cond:
                if not buf and state["run"]:
                    cond.wait(HEARTBEAT_S)
                chunks = list(buf)
                buf.clear()
                size[0] = 0
                running = state["run"]
            if chunks:
                out.write(frame(b"D", b"".join(chunks)))
            elif running:
                out.write(frame(b"H"))
            out.flush()
            if not running:
                break
    except (BrokenPipeError, OSError):      # the GUI went away
        state["run"] = False
    try:
        ser.close()
    except Exception:
        pass
    try:
        out.write(frame(b"C", state["reason"].encode("utf-8", "replace")))
        out.flush()
    except (BrokenPipeError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
