# Connect the real RP2040 analyzer and master to the Python GUI (Windows)

## Which firmware works with this updated GUI?

**Compatible (recommended):** The RP2040 `packet_analyzer.c` + `capture.pio` from the included `firmware/native_rp2040_analyzer/` folder (also supplied separately in `I2C_Master_Analyzer_Firmware_Bundle.zip`). This sends JSON lines:

```json
{"type":"hello","protocol":1,"role":"analyzer","board":"RP2040","sample_rate":2000000,"sample_resolution_us":0.5,"capabilities":["segment","loss","status","7bit","10bit","ack","timing","edges"],"waveform":true,"waveform_window_us":2000,"waveform_interval_us":250000}
{"type":"segment","seq":10,"start_us":1234,"end_us":1890,"duration_us":656,"start":"START","end":"STOP","address":104,"address_bits":7,"address_known":true,"direction":"read","address_ack":true,"scl_hz":100100,"scl_high_avg_ns":5000,"scl_low_avg_ns":5000,"scl_low_over_50us":0,"truncated":false,"incomplete":false,"data":[19,22],"acks":[true,false]}
```

**Also compatible:** The earlier GUI-specific edge protocol using `{"t":"hello",...}` / `{"t":"edges",...}`.

**NOT compatible:** The older test firmware printing `I2C CAPTURE RESULTS`, `START at ... us`, and transition counts. Those text summaries contain no complete packet records; replace the analyzer firmware before expecting packets, addresses and graphs in the GUI. The connection itself may work, but a handshake warning will appear.

## A. Make sure the two Pico boards have their *different* firmware

- **Pico 1** master: your already working RTC (`0x68`) + BMP280 (`0x76`) + OLED (`0x3C`) C firmware. Keep it; do not overwrite it with analyzer firmware.
- **Pico 2** analyzer: compile the compatible `packet_analyzer.c` and `capture.pio` files and flash `packet_analyzer.uf2` **to the second board only**. Use the CMake reference beside these files; preserve the Pico extension's SDK setup section. The actual sampling/decoder version is a prototype and must be validated on your hardware.

Flash `.uf2` through BOOTSEL / `RPI-RP2`, as you did previously. It is a binary file, not editable text.

## B. Wire ONLY when the hardware has been checked safe

You reported overheating earlier. Ask an experienced instructor to check wiring and affected components before powering them again. With USB power disconnected, verify Pico 2 **GP2 (physical pin 4)** is connected to the shared **SDA**, Pico 2 **GP3 (pin 5)** to shared **SCL**, Pico 2 **GND (pin 3)** to shared **GND**. Both Pico boards can have their own USB cables. **Do not connect their 3V3 outputs together.** Analyzer inputs must stay high impedance.

## C. Start the native Python GUI

1. Unzip the UPDATED GUI bundle and open its `i2c_3d_analyzer` folder in VS Code.
2. Open **View → Terminal**.
3. If your `.venv` from the previous GUI still exists, it can be reused; otherwise use the following Windows commands:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

After dependencies are installed, the GUI runs without internet.

## D. Connect the analyzer

1. **STOP/CLOSE VS Code Serial Monitor** (also PuTTY/Arduino serial terminal) for **both** relevant COM ports before opening them in the GUI. Only one application can open a COM port at a time.
2. Connect your two USB cables. Previously your analyzer appeared as **COM33** and master as **COM32**; they can change.
3. In the GUI select **Analyzer port → COM33 (or the current analyzer port)**.
4. Click **Connect**. The upper header should display **RP2040 segment analyzer · RP2040 · 2 MS/s · protocol v1** when the handshake arrives.
5. Click **Start capture**. The analyzer responds to plain text `START`/`STOP` commands (the GUI now sends the right format automatically).
6. Click **Packet Analyzer** or **Live Dashboard**. Watch actual packets and device activity; transaction records normally contain addresses `0x68`, `0x76`, and `0x3C`.
7. If you initially see no packets, wait for the master to perform a transaction. The new analyzer is *supposed* to capture continuously but may report losses. Do not equate missing packets with guaranteed inactivity.
8. Click **Performance Monitoring** for firmware-reported SCL clock and transaction statistics. **Digital Waveform** displays the latest measured snapshot after rebuilding and flashing the updated analyzer. Older segment-only firmware still reports waveform unavailable.

## E. Optional: also show the true RTC + temperature + pressure in Sensor Monitoring

Under **Master telemetry (optional)** choose the other COM port (**COM32**, if that is the master), then **Connect master**. The GUI accepts either the supplied master JSON `sensor_telemetry` record or your *existing* master's text lines such as:

```text
RTC 09:55:47 18/10/2026
BMP280: 30.68 C, 977.98 hPa
```

The Sensor Monitoring page marks these **Master USB**, not analyzer-decoded values, and does not invent humidity. You can use both USB connections concurrently. These master readings are **never** counted as captured I²C packets.

## F. Troubleshooting

| Symptom | Check |
|---|---|
| Failed to open COM33 | Stop VS Code's Serial Monitor and any other program using the COM port; refresh ports in GUI |
| Handshake warning | You've likely flashed the **old** console transition test, not the native JSON analyzer; inspect USB output in a serial terminal *with the GUI disconnected* |
| An analyzer repeatedly says `Analyzer Pico is working!` | That's just the earlier USB smoke-test firmware — flash real analyzer JSON firmware |
| Connection works but no packets | Confirm analyzer emits JSON `segment` records, has GP2↔SDA / GP3↔SCL / common GND, and Pico 1 is transmitting |
| 3D activity but sensor values unavailable | Passively decoded packets may not contain BMP280 calibration; connect the optional **master** COM port for real measurements |
| Waveform says unavailable | Check HELLO: old firmware lacks edges and reports waveform:false. Rebuild/flash the measured-window firmware and restart the GUI |
| Missing/incorrect measurements | Check all loss counters and raw records; the short hardware verification does not guarantee lossless or oscilloscope-calibrated measurements |
| GUI won't launch | Run from `i2c_3d_analyzer` (folder with `main.py`); check pip installation error output |
| Code 43 or overheating | Unplug and have hardware and USB wiring inspected before reconnecting |

## G. Quality/validation notes

The updated application was verified against the physical COM28 analyzer on 2026-11-07. Raw GPIO snapshots, actual serial bytes, parsing and plotted state/time arrays were checked. The final ten-second capture reported zero DMA/USB/waveform drops. These are bounded digital snapshots at 2 MS/s, not an analog scope or a continuous lossless recording. Restart the GUI to load the corrected source; a process opened before the edits retains its old Python code.


Verified port roles on 2026-11-07: analyzer COM28 (USB serial
50443405B828B41C), master COM32 (5044505930DCBA1C). Port numbers can change.
Only analyzer USB is required for bus graphs. After the firmware update,
restart main.py, leave Demo off, connect the analyzer, press Start capture,
select Digital Waveform and enable Follow live. A 5 ms selection displays the
whole available measured window (up to 2 ms); missing intervals are not filled.
Activity can legitimately alternate with sampled flat levels. The GUI shows
window age. No raw samples means the current bus state is unknown.
