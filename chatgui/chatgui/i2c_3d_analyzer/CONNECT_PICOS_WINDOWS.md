# Connect both Raspberry Pi Picos to the 3D Python GUI (Windows)

## 0. Safety check FIRST
You previously reported components overheating. **Unplug all USB power if anything gets hot**. Do not attach more devices until an instructor has checked the wiring, module voltages, and power rails. **Never join the two Picos' 3V3 outputs.**

## 1. What this bundle contains
- `main.py`, `gui/`, `analysis/`, `communication/`, and related folders: your 3D PyQt6 application, updated for native RP2040 analyzer JSON records.
- `firmware/native_rp2040_analyzer/packet_analyzer.c` and `capture.pio`: firmware source **for Pico 2**, the passive analyzer.
- `firmware/native_rp2040_analyzer/CMakeLists.txt`: reference full target for the official Pico SDK.
- `firmware/native_rp2040_master/I2C.c`: optional **Pico 1** master code for RTC + BMP280 + OLED and USB telemetry; don't replace your working master if you don't need to.
- `tools/check_setup.py`: offline dependency/structure checker.
- `tests/test_native_firmware.py`: GUI JSON parser and decoded transaction checks.

## 2. Wiring after safety inspection, with USB cables unplugged
| Connection | From | To |
|---|---|---|
| SDA | Pico 1 GP4 (physical pin 6) / SDA bus | Pico 2 GP2 (physical pin 4) |
| SCL | Pico 1 GP5 (physical pin 7) / SCL bus | Pico 2 GP3 (physical pin 5) |
| Ground | Pico 1 GND / shared ground | Pico 2 GND (physical pin 3) |
| Power | Each Pico's own USB cable | PC |

Both analyzer GPIOs are configured as **inputs**; do not connect output pins, 5 V, or the two 3.3 V supply outputs together. The master remains connected to DS3231 (`0x68`), BMP280 (`0x76`) and SSD1306 OLED (`0x3C`, if your OLED uses that address).

## 3. Compile analyzer Pico firmware in your EXISTING VS Code project
1. Open your existing `packet_analyzer` **C project** (the one where Pico SDK v2.3.1 and Board `pico` compiled). Do not open the Python `i2c_3d_analyzer` folder in the Pico SDK extension.
2. **Back up** the old `packet_analyzer.c`, `capture.pio`, and `CMakeLists.txt`.
3. Copy the two analyzer source files from `firmware/native_rp2040_analyzer/` into that project, **beside** the existing `CMakeLists.txt` (NOT inside `build/`).
4. Preserve the Raspberry Pi VS Code extension's generated header and SDK-import lines in your existing `CMakeLists.txt`. Replace just the executable/target-related section with the one in the supplied `CMakeLists.txt` beginning `add_executable(...)`; ensure `pico_generate_pio_header(...)` references `capture.pio`, `hardware_clocks` and `pico_multicore` are linked, USB stdio is enabled. Do not add duplicate targets.
5. Click **Configure CMake**, **Compile Project**. If successful, locate **`build/packet_analyzer.uf2`**.
6. If the physical Pico has been inspected and confirmed safe, flash this file to **Pico 2 only** using BOOTSEL → `RPI-RP2` drive. Do not upload the analyzer firmware to Pico 1.
7. While the GUI is NOT connected, briefly open VS Code Serial Monitor on the analyzer COM port (previously COM33) at 115200 and look for a JSON `hello` with `"type":"hello"`. If capturing has not been started, `"capturing":false` in status is correct. Close/stop Serial Monitor afterwards.

If you also want master-side sensor values in GUI, keep your existing master if it already prints lines such as `RTC 09:55:47 18/10/2026` and `BMP280: 30.68 C, 977.98 hPa`. This GUI supports those. To switch to JSON master telemetry, compile supplied `native_rp2040_master/I2C.c` in your existing master **I2C** project with `hardware_i2c` and USB stdio. `SET_RTC_ON_BOOT` is **0** by default. Do not flash this to Pico 2.

## 4. Run Python GUI
Extract this whole ZIP (do not open source files directly from the ZIP). Open the `i2c_3d_analyzer` directory in VS Code, then choose **View → Terminal → New Terminal**. Run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe tools\check_setup.py
.\.venv\Scripts\python.exe main.py
```

Once Python packages are installed, the application is offline and does not require the internet. If Python is already installed in `.venv`, skip creating the environment.

## 5. Connect real hardware
1. Plug both Picos in by USB only after the electrical safety checks. Your previous COM ports were **COM32 (master)** and **COM33 (analyzer)**, but they may change.
2. Stop all existing VS Code Serial Monitor sessions (otherwise Python cannot open a COM port already in use).
3. In your GUI select **Analyzer port** = Pico 2 COM port, click **Connect**. Wait for `RP2040 segment analyzer` handshake. **Start capture remains disabled until handshake arrives.**
4. Press **Start capture**; this sends plain text `START\n` to Pico 2.
5. View **Packet Analyzer** and **Device Activity** for real `0x68`, `0x76`, and `0x3C` transactions observed by the analyzer.
6. Optional: set **Master telemetry** = Pico 1's COM port, click **Connect master** for real RTC time, BMP280 temperature, and pressure. Readings from the master are separately labelled and are *not counted as I²C packets*.
7. Use **Stop** to end capture (`STOP\n`). You can save and export a capture from the GUI.

## 6. Analyzer wire protocol
First handshake (illustrative):
```json
{"type":"hello","protocol":1,"role":"analyzer","board":"RP2040","sda_pin":2,"scl_pin":3,"sample_rate":2000000,"sample_resolution_us":0.5,"capabilities":["segment","loss","status","7bit","10bit","ack","timing","edges"],"waveform":true,"waveform_window_us":2000,"waveform_interval_us":250000}
```
Segment (illustrative RTC write):
```json
{"type":"segment","seq":1,"start_us":100000,"end_us":100290,"duration_us":290,"start":"START","end":"STOP","address":104,"address_bits":7,"address_known":true,"direction":"write","address_ack":true,"scl_hz":100000,"scl_high_avg_ns":5000,"scl_low_avg_ns":5000,"scl_low_over_50us":0,"truncated":false,"incomplete":false,"data":[0],"acks":[true]}
```
`address` is decimal in JSON (104 = hexadecimal `0x68`). One `segment` covers START..STOP or START..repeated START. The GUI assembles repeated START segments into a complete transaction. `loss` and `status` report missing capture buffers/USB records. Timestamps are approximate PIO sampling time references, not oscilloscope-calibrated measurements.

## 7. What is deliberately NOT claimed
- Updated native analyzer sends measured waveform snapshots (up to 2 ms, at most four per second). Rebuild and flash the updated analyzer; the viewer displays only the latest window and does not fill gaps.
- DMA buffers can overflow at heavy loads. The firmware pauses rather than overwriting a buffer being decoded and reports loss events. It is **not** certified lossless.
- The RP2040 cannot determine analog I²C rise time or precise analog voltages from digital inputs.
- Per-byte ACK/NACK values come from sampled digital transitions; verify them against known transactions. SCL time resolution is 0.5 us.
- 100 kHz short-cable operation is the intended initial test. 400 kHz, 10-bit address corner cases, complicated clock stretching and exact timing need additional validation.
- This ZIP supplies **source code**, not an `.uf2` file compiled for your Windows Pico SDK installation.

## 8. Troubleshooting
| Problem | Check |
|---|---|
| No COM port | USB data cable, Pico firmware, Windows Device Manager |
| Cannot open COM port | Stop the Serial Monitor on that COM port before connecting GUI |
| GUI says waiting for handshake | Is Pico 2 actually flashed with the included native JSON analyzer, not old `START at ...` firmware? |
| Start capture disabled | Wait for a valid `hello` handshake; check the COM port matches Pico 2 |
| No packets | Pico 1 running? GP2/SDA, GP3/SCL, common GND? No protocol loss? |
| Loss events | Analysis/USB couldn't keep up; evaluate load and report incomplete capture |
| Waveform unavailable | Older firmware has no edges capability. Rebuild/flash the updated analyzer; verify its hello includes edges and waveform:true |
| Sensor values empty | Connect optional master COM port; BMP calibration may not be captured by passive analyzer |
| Hardware gets very hot | **Unplug immediately; do not reconnect until an experienced person checks it** |


Verified port roles on 2026-11-07: analyzer COM28 (USB serial
50443405B828B41C), master COM32 (5044505930DCBA1C). Port numbers can change.
Only analyzer USB is required for bus graphs. After the firmware update,
restart main.py, leave Demo off, connect the analyzer, press Start capture,
select Digital Waveform and enable Follow live. A 5 ms selection displays the
whole available measured window (up to 2 ms); missing intervals are not filled.
Activity can legitimately alternate with sampled flat levels. The GUI shows
window age. No raw samples means the current bus state is unknown.
