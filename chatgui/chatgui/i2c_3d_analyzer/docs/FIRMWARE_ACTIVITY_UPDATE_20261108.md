# Firmware, demo and Device Communication Activity — 8 Nov 2026 (second round)

Backups: `source_backup_activity_20261108/` (includes both VS Code firmware projects
under `deployed_I2C/` and `deployed_packet_analyzer/`).
No physical hardware was connected and **no RP2040 firmware was built** in this round.
There is no `arm-none-eabi-gcc`, CMake or Pico SDK on this PC. Firmware results below come from host-compiled simulations.

## Which firmware is actually flashed
`F:\Raspberry_PI_Project\softwareproject12\I2C\I2C\I2C.c` has a `build/I2C.uf2` beside it.
It differs from the GUI's older copy: BMP280 every 200 ms, RTC+OLED every 1 s, identity every 10 s, bus scan every 5 s, and bus recovery.
The analyzer project `packet_analyzer/packet_analyzer/packet_analyzer.c` was identical to the GUI copy.
After this round, each GUI copy under `firmware/` is byte-identical to its project.

## Evidence-backed NACK finding (physical reduction unverified)
The flashed controller probed every address 0x08–0x77 every 5 s, plus once at boot.
On this bench only 0x3C, 0x68 and 0x76 are configured; the HW-084 module's EEPROM, typically 0x57, may also answer.
So every scan produced about 108 address NACKs. Under the documented policy these are unexpected address NACKs, so health would be DEGRADED.
The scan is now opt-in (`SCAN_PERIOD_MS 0`). The analyzer and GUI still count every NACK that occurs.
Whether real-bus NACKs actually fell must be measured on hardware.

## Controller changes (`I2C/I2C/I2C.c` = `firmware/native_rp2040_master/I2C.c`)
1. Bus scan is opt-in (see above).
2. Bus recovery is triggered only by `PICO_ERROR_TIMEOUT` or a stuck-LOW SDA/SCL line. An address NACK (`PICO_ERROR_GENERIC`) is not a stuck bus. Before, any 6 consecutive failures, NACKs included, bit-banged 9 SCL pulses.
3. DS3231: one combined read of registers 0x00–0x0F. BCD ranges and the weekday are validated. OSF (status 0x0F bit 7) marks the time invalid. `SET_RTC_ON_BOOT` clears OSF after setting the time.
4. BMP280/BME280: chip ID 0x58 or 0x60 accepted (same calibration and T/P registers). Out-of-range results are rejected.
5. A device that failed init is retried every 2 s, not every 200 ms.
6. During an optional scan, the MPU-6050 WHO_AM_I probe is never sent to the configured DS3231 address.
7. A JSON `sensor_telemetry` line is printed on the controller's own USB port (master telemetry, never bus data).

Not changed (wiring-dependent, unverified): addresses 0x76/0x77 and 0x3C/0x3D, the internal pull-ups (already enabled by the flashed firmware), and the bus speed.

## Analyzer change (both copies identical)
Segments now also carry `start_sample`/`end_sample`, and loss records carry `t_sample`: exact 0.5 µs sample indices.
Previously only whole µs were sent (truncated). Hello advertises `sample_ts`.
The GUI uses the exact values when present (see `PROTOCOL.md`). The analyzer stays passive: no output pins, no ACK, no traffic.

## Device Communication Activity (definitions)
Defined in `analysis/device_activity.py`:
- **Frame** = an acknowledged address phase inside a complete captured transaction. A combined WRITE→READ gives 1 WRITE frame + 1 READ frame, and counts as 1 bus transaction.
- **Rate** = frames per second of capture time over a rolling 10 s window.
- **Status:** ACTIVE / NO OBSERVED ACTIVITY (verified connected, reliable window, zero frames) / PARTIAL (incomplete capture for that address; counts are lower bounds) / UNKNOWN (unreliable or not-yet-full window, or never verified connected).
- **Rankings** use only devices with exact counts. Tied devices are listed together, in ascending address order. A zero enters a "least" ranking only when it is a verified zero.

## Tests (8 Nov 2026, this PC)
`python -X utf8 -m pytest -q -p no:cacheprovider` → **119 passed**. This includes:
- `tests/test_controller_firmware_host.py` (6): the real controller C file compiled with stub SDK headers against fake DS3231/BMP280/SSD1306 targets.
- `tests/test_firmware_host.py`: the real analyzer emitter compiled on the host; exact sample timestamps; raw-window ↔ segment decode agreement.
- `tests/test_device_activity.py` (7): rankings with high, low, tied, unobserved, incomplete, demo, replayed native records, and pause/focus.

GUI checks:
- `python -X utf8 tools/verify_topology_views.py` and `--2d` → **PASS**. Dashboard, 3D network and Device Activity diagrams were checked at 1680×980 and the smallest window: fitted, no label overlap, not clipped by their cards, Reset view independent.
- OpenGL frames still fail on this PC (`GLError 1282`, the same with the original code), so 3D geometry was inspected through the 2D renderer of the same layout.
