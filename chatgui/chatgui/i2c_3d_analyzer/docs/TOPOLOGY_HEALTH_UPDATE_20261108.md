# Topology diagrams, NACK classification and bus health — 8 Nov 2026

Backup of every edited source file: `source_backup_topology_20261108/`.
Hardware was **not** available. Nothing here is a physical hardware verification.

## Verified connection sources

| Item | Value | Source |
|---|---|---|
| Controller | Raspberry Pi Pico (RP2040), `i2c0`, **SDA GP4, SCL GP5**, 100 kHz, drives SCL for READ and WRITE | `firmware/native_rp2040_master/I2C.c` |
| Targets | DS3231 **0x68**, BMP280 **0x76**, SSD1306 **0x3C** | `I2C.c`, `config.py` |
| Analyzer | Second RP2040 Pico, PIO inputs **SDA GP2, SCL GP3**, pulls disabled, never drives, no address, no ACK | `firmware/native_rp2040_analyzer/packet_analyzer.c` |
| Analyzer → PC | USB CDC, JSON-lines records | `packet_analyzer.c` (`stdio_usb`) |
| Controller → PC | USB CDC (power + optional telemetry, separate COM port) | `I2C.c` printf, `CMakeLists.txt` (`pico_enable_stdio_usb`) |
| Module headers (photos) | HW-084 DS3231: GND VCC SDA SCL SQW 32K; GY-BME/PM280: VCC GND SCL SDA CSB SDO; 0.96" OLED: 4-pin | component photos (silkscreen only) |

Jumper-wire positions in the photos were **not** used as evidence.

### Not verifiable from firmware/documentation (marked "unverified" in the GUI)
- **Common GND** between controller, analyzer and targets (required by I²C; not provable from firmware).
- **Target VCC** (3.3 V from Pico 1 3V3 recommended). If any module is powered from 5 V, its on-board pull-ups pull SDA/SCL to 5 V — not acceptable for RP2040 GPIO.
- **Pull-ups**: firmware enables none on the controller; the modules carry their own (HW-084 4.7 kΩ network, GY board 10 kΩ). Parallel value unmeasured.
- **BMP/BME280 address**: 0x76 assumes SDO tied low on the GY-BME/PM280 board; 0x77 if high.
- **Chip identity**: board silkscreen says BME/PM280. Firmware `bmp_init()` accepts only chip-ID 0x58 (BMP280); a BME280 (0x60) would make init fail silently (no NACK, but no sensor traffic).
- **OLED address**: 0x3C assumed (0x3D possible by resistor strap).
- HW-084 also carries an AT24C32 EEPROM (not accessed by firmware) and a charge path (R4/D2) intended for a rechargeable cell — check the coin cell type.

## NACK findings

Evidence-backed causes of the high **displayed** NACK count (fixed):
1. The dashboard showed every raw NACK in red. In the old screenshot all 34 NACKs were normal end-of-read NACKs (controller NACKs the last byte of each READ, UM10204 §3.1.6). The raw count is kept; the card is now amber unless unexpected NACKs exist, and its text separates end-of-read / unexpected / address / write.
2. Demo traffic did not match the firmware: BMP280 reads at 10 Hz (firmware: once per ~1 s loop, so 10× the end-of-read NACKs) and an injected NACKed address probe to 0x2A every 7 s that the firmware never sends. The demo now replays the firmware sequence exactly. The fault probe is opt-in only (`DemoGenerator(inject_absent_probe=True)`).
3. Classification: a READ observed from a repeated START (native capture beginning mid-transaction) had its normal final NACK counted as **unexpected**. Now it is expected when the following STOP/Sr was captured. A final READ NACK whose STOP/Sr was not captured is counted as raw and reported as **unverified**, not as unexpected.

Not changed: raw NACKs are never removed, no ACK is invented, raw timestamps are untouched, and health thresholds are unchanged. Firmware sources were reviewed and **not modified**. The controller's transaction sequence (combined write/Sr/read, final NACK) is correct. Real-bus unexpected NACKs can still come from the unverified items above. They have **not** been measured, and no reduction is claimed.

## Health policy (application policy, unchanged thresholds)
`slots = ACK + raw NACK` (unknown slots excluded) · raw rate = 100·raw/slots · unexpected rate = 100·unexpected/slots, over a rolling 60 s of capture time.
HEALTHY: reliable fresh live capture, ≥1 complete transaction, known slots, zero unexpected NACKs.
DEGRADED: ≥1 unexpected NACK in the window. This now takes precedence over incomplete records.
UNKNOWN: disconnected or unreliable capture, or insufficient evidence.
In demo mode the verified state stays UNKNOWN. The policy result is shown separately as "SIMULATED policy: …".

## Diagrams
Both the dashboard box and the 3D network page use one shared schematic layout:
- Pico 1 controller, the three targets on SDA/SCL/GND rails, and Pico 2 tapping SDA/SCL/GND with dashed (passive) lines.
- A USB line from Pico 2 to the PC, and a dashed optional USB line from Pico 1 to the PC.
- Configured targets are drawn as "Not yet observed" until traffic proves them.

Arrows and labels come only from playback stages of decoded records:
- Blue = controller → target; cyan = target → controller.
- Green ACK; red NACK marked with ✕ and no arrow (nobody pulls SDA low).
- SCL arrows always come from the controller.

Fit-to-box: the camera distance and centre (or the 2D scale) are solved so every board, wire end and label is inside the view with no label overlap. Label text shortens in small boxes. The user's rotation and zoom are kept until **Reset view**, which refits that diagram only.

The HUD shows:
- capture/simulation clock;
- last READ and last WRITE timestamps;
- RTC time with provenance — bus-decoded, SIMULATED demo record, or master telemetry; VALID, STALE or UNAVAILABLE; never the PC clock;
- transaction stage;
- health counts and rates.

## Tests and commands (run 8 Nov 2026)
- `python -X utf8 -m pytest -q -p no:cacheprovider` → **106 passed** (includes `tests/test_firmware_host.py`, which compiled and passed this time).
- `python -X utf8 tools/verify_topology_views.py` (real Qt/OpenGL window, demo records) → fit/overlap/Reset view **PASS** at 1680×980 and the smallest window.
- `python -X utf8 tools/verify_topology_views.py --2d` → **PASS**. Screenshots are in `work/verification/topology_*`.
- **Limitation:** in this session every OpenGL frame fails with `GLError 1282 at glClearColor`, so no 3D geometry is drawn. The unmodified original code fails identically here. The 3D geometry was therefore **not visually verified**; the identical layout was inspected through the 2D renderer. The GL paint error is now logged once instead of raising every frame.

Launch: `python -X utf8 main.py --demo` (demo) · `python -X utf8 main.py` (hardware; select the analyzer COM port → Connect → Start capture).
