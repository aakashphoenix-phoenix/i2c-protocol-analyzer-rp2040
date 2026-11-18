"""Set the DS3231 through the CONTROLLER Pico's USB port (never the analyzer).

    python -X utf8 tools/set_rtc.py COM32            # PC local time, aligned to a second boundary
    python -X utf8 tools/set_rtc.py COM32 2026-11-09T10:15:30

The controller firmware validates the value, writes DS3231 registers 0x00-0x06
between bus transactions and clears the oscillator-stop flag (OSF). Expected
error versus the PC clock: under about 0.2 s (one main-loop pass).
"""
import datetime as dt
import sys
import time

import serial

port = sys.argv[1]
s = serial.Serial(port, 115200, timeout=0.1, write_timeout=1.0)
s.dtr = False; time.sleep(0.3); s.dtr = True
time.sleep(0.5)
s.reset_input_buffer()
if len(sys.argv) > 2:
    stamp = sys.argv[2]
else:
    now = time.time()
    time.sleep(1.0 - (now % 1.0))                     # wait for the next whole second
    stamp = dt.datetime.now().replace(microsecond=0).isoformat()
s.write(f"TIME {stamp}\n".encode("ascii"))
print(f"sent: TIME {stamp}  (PC local time)")
t0, pending = time.time(), b""
while time.time() - t0 < 5.0:
    pending += s.read(s.in_waiting or 1)
    *lines, pending = pending.split(b"\n")
    for ln in lines:
        text = ln.decode("utf-8", "replace").strip()
        if text.startswith('{"type":"rtc_set"') or text.startswith("RTC"):
            print(f"{dt.datetime.now():%H:%M:%S.%f}"[:-3] + "  " + text, flush=True)
s.close()
