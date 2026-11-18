"""Record the CONTROLLER Pico's own USB log (not the analyzer) and query its SCL setting.

    python -X utf8 tools/read_controller_log.py COM32 12

Old controller firmware never reads its USB input: the 'SCL?' query then gets no
reply (and may not even be accepted), which identifies that firmware. The upgraded
firmware answers with a {"type":"scl_config",...} line.
"""
import sys
import time

import serial

port, secs = sys.argv[1], float(sys.argv[2])
s = serial.Serial(port, 115200, timeout=0.2, write_timeout=0.5)
s.dtr = False; time.sleep(0.3); s.dtr = True        # Pico stdio_usb only sends while DTR is asserted
t0, sent, pending = time.time(), False, b""
while time.time() - t0 < secs:
    if not sent and time.time() - t0 > 1.0:
        sent = True
        try:
            s.write(b"SCL?\n")
        except serial.SerialTimeoutException:
            print("# SCL? query not accepted by the controller (its USB input is not being read)", flush=True)
    pending += s.read(s.in_waiting or 1)
    *lines, pending = pending.split(b"\n")
    for ln in lines:
        print(ln.decode("utf-8", "replace").rstrip("\r"), flush=True)
s.close()
