# Controller SCL setting, measured timing and frame display — 8 Nov 2026 (third round)

Backups: `source_backup_scl_20261108/` (includes the flashed project's `I2C.c` under `deployed_I2C/`).
No hardware was connected. No RP2040 image was built (no ARM toolchain or Pico SDK on this PC).

## Selectable controller SCL rates
| Part | Limit | Basis |
|---|---|---|
| DS3231 RTC | 400 kHz (Fast-mode) | Maxim DS3231 datasheet ("Fast (400kHz) I²C interface") |
| SSD1306 OLED | 400 kHz (I²C clock cycle ≥ 2.5 µs) | Solomon SSD1306 datasheet |
| BMP280/BME280 | 3.4 MHz | Bosch datasheet |
| RP2040 I²C block | 1 MHz (Fast-mode Plus) | Raspberry Pi RP2040 datasheet |

So only **100 kHz (Standard-mode)** and **400 kHz (Fast-mode)** are offered (UM10204 modes).

The supplied bme/rtc/oled PDFs could not be rendered here. The limits come from the manufacturers' published datasheet values and are not checked against those copies.

At 2 MS/s the analyzer gets 20 samples per cycle at 100 kHz and 5 at 400 kHz. At 400 kHz, single-cycle values are quantised to 0.5 µs, so use the packet or rolling means.

## Command path (controller only)
The flashed `I2C/I2C/I2C.c` (identical to `firmware/native_rp2040_master/I2C.c`) now reads its own USB port:

- `SCL?` replies with the current setting.
- `SCL 100000` / `SCL 400000` is applied between transactions with `i2c_set_baudrate()`.
- It replies with `{"type":"scl_config","source":"controller","result":"applied|rejected|current|boot","requested_hz":…,"applied_hz":…}`.
- `applied_hz` is the SDK divider result (399 361 Hz for 400 kHz at 125 MHz), **not** a bus measurement.
- Any other value is rejected and the rate stays unchanged.
- Bus recovery re-initialises at the selected rate.

The GUI sends the command only after **Apply to controller**, and only on the controller (master) port. The analyzer's serial worker raises an error if asked to send it.

The GUI reports PENDING → CONFIRMED / MISMATCH by comparing the median of packet measurements captured after the change with the reported rate (±10 %). It never shows the requested rate as a measurement.

Demo mode applies the setting to simulated edge timing only, labelled SIMULATED. The visual playback speed is a separate control.

## Exact calculations (`analysis/scl_timing.py`, shared by all views)
- **Cycle:** consecutive committed SCL rises inside one frame (8 bit rises + the 9th ACK rise per byte). `period = next_rise − rise`, `frequency = 1/period`. Cycles never span START, repeated START, STOP, an idle gap between transactions, or a byte without captured rises.
- **Robust packet mean:** periods > 3 × the packet median are excluded and reported as long cycles; `f = 1 / mean(remaining periods)`.
- **Rolling mean:** all accepted cycles in the last 1 s of capture time (`roll_freq_hz`).
- **Native segment without a raw window:** the firmware's per-segment average, labelled "individual cycles not captured".
- **A/B cursors:** `Δt = |B − A|`, `1/Δt`, plus the individual cycle under A with HIGH (rise → first SCL fall) and LOW (fall → next rise) taken from the raw edges.
- **Long SCL LOW:** reported separately. Its cause is always "undetermined"; clock stretching is never asserted.

## Frame display
- Waveform labels "ADDR 0x76 (7-bit)", a separate "W=0"/"R=1" on the 8th clock, and "byte 0xEC = 0x76<<1 | 0".
- Data bytes are labelled "DATA 0xNN"; binary bits appear when zoomed in.
- ACK is green and NACK red. The final read NACK is labelled "NACK (end of read)" and still counted as raw.
- Incomplete frames are marked "INCOMPLETE — last verified event".
- The packet table has new "Addr byte (8-bit)" and "SCL measurement scope" columns; the CSV export adds the same fields.
- Packet details and both 3D overlays show the measured SCL with its scope and source.
- Selecting a packet anywhere now also updates the packet details panel.
