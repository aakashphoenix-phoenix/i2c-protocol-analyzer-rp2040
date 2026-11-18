# I²C Packet Analyzer — requirements, where they are, and how they are proven

Bench: Raspberry Pi Pico 2 = passive analyzer (COM28, 2 MS/s, ±0.5 µs) · Pico 1 = Master 1 (COM32) · Pico 3 = Master 2
(Pico W, COM34, firmware 2.1) · DS3231 RTC 0x68 · BMP280 0x76 · SSD1306 OLED 0x3C (write-only).

Each line below: **what the requirement asks → where you see it in the GUI → the code that does it → the evidence.**
Automated tests: `python -m pytest -q` (524 tests). Live bench scripts: `tools/verify_live_packet_proof.py`,
`tools/verify_live_3d_reset.py`.

---

### 1. Both SDA and SCL are captured from real hardware with timestamps
* GUI: **Digital Waveform** (SDA/SCL traces, time axis = capture time); **Packet Proof** (raw edges of one packet).
* Code: analyzer firmware samples both lines with PIO and timestamps every change with its sample counter
  (`firmware/native_rp2040_analyzer/packet_analyzer.c`, `capture.pio`); the PC reads the port in its own process
  (`communication/serial_child.py`) and keeps every edge in a rolling ring (`analysis/capture_engine.py` `_stream_edges`,
  `edges_between`).
* Evidence: Packet Proof check "Raw SDA/SCL edges captured with timestamps" (sample count, ±resolution).

### 2. Address, direction, data, ACK/NACK, START and STOP decode correctly
* GUI: Packet Analyzer table + detail, decoder row on the Digital Waveform, 3D phase banner.
* Code: `analysis/i2c_decoder.py` — START = SDA↓ while SCL=1 (`_on_start`), STOP = SDA↑ while SCL=1 (`_on_stop`), bit =
  SDA on SCL↑, 9th bit = ACK/NACK (`_on_bit`); the RP2040 analyzer also decodes on the Pico (`_native_segment`).
* Evidence: Packet Proof re-decodes the raw edges independently and compares byte by byte with the stored packet
  (`analysis/packet_proof.py` `_independent_decode`), plus a bit table (SDA sampled at each SCL↑ = decoded bit).

### 3. Repeated START and multiple-byte reads/writes are supported
* GUI: an RTC read shows `S · 0x68 W · 00 · Sr · 0x68 R · 16 bytes · NACK · P`; Packet Proof lists the Sr condition and
  measures t_SU;STA.
* Code: `analysis/i2c_decoder.py` `_on_start` (a START inside an open transaction = `Sr`, a new frame).
* Evidence: tests `test_decoder_track.py`, `test_packet_proof.py` (RTC 1+1+1+16 bytes = 171 proven clocks).

### 4. Different slave addresses can be filtered independently
* GUI: Packet Analyzer "Device" filter + address pills; Digital Waveform trigger "Address"; 3D "Show this device's packets".
* Code: `gui/packet_analyzer.py` (`self.addr`, `set_address_filter`), `models/packet_model.py` `PacketFilter`,
  `gui/waveform_viewer.py` `TRIGGERS`.
* Evidence: live check "address filter shows only that slave".

### 5. Multiple masters can access the same bus with correct arbitration handling
* GUI: Device Activity (master × target matrix, arbitration table), 3D view (amber "ARB lost" pulse), Error & Diagnostics.
* Code: controller firmware `firmware/native_rp2040_master/I2C.c` (bus-idle check, reads IC_TX_ABRT_SOURCE before it is
  cleared so ARB_LOST ≠ NACK, back-off + retry); `analysis/multi_master.py` `_on_arb` (never counted as a NACK).
* Evidence: `test_controller_firmware_host.py` (arbitration retried, not a NACK), `test_multi_master.py`.

### 6. Master identification is marked unknown unless validated by diagnostics
* GUI: Master column "Unknown", detail "Master UNKNOWN (not validated): …", banner "Master UNKNOWN (not validated)".
* Code: `analysis/multi_master.py` (`attribution_text` → `Unknown`; a master is named only when its OWN transfer report
  matches the packet: `_match_txn`/`_link`); no configured ownership is used any more (`config.py` `owns=()`); the 3D
  view never animates an unvalidated packet as a named master (`gui/main_window.py` `_follow_next`).
* Evidence: Packet Proof section 2 shows the matching controller report (seq, time, bytes, Δt); test
  `test_unknown_master_is_labelled_unknown_never_guessed`.

### 7. Timing measurements are computed from capture timestamps
* GUI: Performance Monitoring, Digital Waveform cursors, packet detail, Packet Proof section 5.
* Code: `analysis/scl_timing.py` (`cycles`, `annotate`), `analysis/performance_calculator.py`,
  `analysis/packet_proof.py` `_timing` (f_SCL, t_HIGH, t_LOW, t_HD;STA, t_SU;STA, t_SU;STO, t_BUF vs NXP UM10204,
  judged with the ±1-sample resolution — "not decidable" when within one sample).

### 8. Expected final-read NACKs are distinguished from faults
* GUI: ACK/NACK column, Health card ("end-of-read"), Packet Proof section 6.
* Code: `analysis/communication_health.py` (`expected_read_nacks` vs `unexpected_nacks`), `analysis/playback.py`
  `_nack_note`.
* Evidence: Packet Proof "ACK / NACK: no unexpected NACK … (end-of-read NACK = expected)".

### 9. Buffer overflow and lost samples are detectable
* GUI: Error & Diagnostics "Analyzer problems" card, Health = UNKNOWN when the capture lost data, waveform never drawn
  across a gap.
* Code: record sequence numbers (`communication/protocol_parser.py` `SequenceTracker`) → `lost_records` + "Dropped
  records"; firmware `overflows`/`dropped`; edge-stream `gap` → "Edge stream gap" (`analysis/capture_engine.py`);
  `measurement_trust`.
* Fixed 2026-11-16: the PC itself was losing data (Windows usbser keeps only ~25 kB; the analyzer sends 1 Hz bursts) —
  now read by a separate process (`communication/serial_child.py`): 0 lost records in 330 s live.

### 10. Zoom, pan, trigger, search and export work on captured data
* GUI: Digital Waveform zoom ± / wheel / drag pan / triggers Auto·START·Address·Roll / A-B cursors / **Export view
  (VCD / CSV)**; Packet Analyzer search + "Export visible CSV"; Packet Proof "Export edges VCD / CSV".
* Code: `gui/waveform_viewer.py` (`zoom_in_view`, `export_view`), `storage/signal_export.py` (`write_vcd`,
  `write_edges_csv` — the captured edges exactly, no resampling), `gui/packet_analyzer.py` (`search`, `_csv`).
* Evidence: live check "Waveform Export view VCD holds the captured packet (re-decoded from the file)".

### 11. Saved sessions can be replayed without fabricating signals
* GUI: Capture History → open / replay (labelled REPLAY).
* Code: `storage/capture_storage.py` (`load_capture`: the recorded analyzer lines are fed through the same parser and
  decoder; "nothing measured is altered on replay").

### 12. Results are compared with known transactions or a reference logic analyzer
* GUI: Packet Analyzer → **✔ Cross-check** window; **🔬 Packet Proof** for one packet.
* Code: `analysis/cross_check.py`:
  * known transactions — each master's own transfer log vs the bus (`known_transactions`);
  * two independent decoders — Pico firmware vs PC raw-edge decode (`decoder_agreement`);
  * known device content — chip ID 0xD0 at 0x76 = 0x60 (BME280) or 0x58 (BMP280), DS3231 BCD (`known_content`);
  * reference logic analyzer — import sigrok-cli / PulseView annotations or a Saleae Logic CSV
    (`parse_sigrok_text`, `parse_saleae_csv`) and align it transaction by transaction (`compare_with_reference`).
* Note: no external logic analyzer / sigrok is installed on this PC; the importers are tested with files in those tools'
  documented formats. Export a window as VCD, decode it in PulseView, export the annotations and import them here.

---

## Live bench results (2026-11-17 17:44, `tools/verify_live_packet_proof.py`, Real Hardware Mode, 2 MS/s analyzer)

Controller firmware on the bench at this run: "2.3-turns" (Master 1 and Master 2 take turns; both draw OLED frames).
Proof reports: `work/verification/proofs/proof_M*_*.html` (+ `.png` screenshot, `.vcd` raw edges of the packet).

| Master → target | Packet | Bytes on the bus | Verdict | Clocks checked | Meaning |
|---|---|---|---|---|---|
| Master 1 → BMP280 | #588 WRITE→READ | `W: F7 \| R: 67 9A 00 88 4C 00` | PROVEN | 81 | 32.66 °C / 974.05 hPa |
| Master 1 → RTC | #733 WRITE→READ | `W: 00 \| R: 23 44 17 03 17 11 26 …` | PROVEN | 171 | 17:44:23 |
| Master 1 → OLED | #804 WRITE | `W: 40 00 00 …` (17 bytes) | PROVEN | 162 | display data, 16 bytes |
| Master 2 → BMP280 | #806 WRITE→READ | `W: F7 \| R: 67 9A 00 88 4B 00` | PROVEN | 81 | 32.65 °C / 974.04 hPa |
| Master 2 → RTC | #805 WRITE→READ | `W: 00 \| R: 28 44 17 03 17 11 26 …` | PROVEN | 171 | 17:44:28 |
| Master 2 → OLED | #1024 WRITE | `W: 40 00 00 …` (17 bytes) | PROVEN | 162 | display data, 16 bytes |

* Known transactions: Master 1 reported 584 transfers → 584 found on the bus; Master 2 reported 512 → 512 found;
  0 missing; 0 bus packets without a validated master.
* Two decoders: 1,095 packets decoded identically by the Pico firmware and by the PC from the raw edges, 0 different.
* Known content: chip ID 0xD0 at 0x76 read **0x58 in 8/8 reads = a BMP280** (a genuine BME280 reads 0x60 and also
  measures humidity; modules sold as "BME280" often carry a BMP280); DS3231 time valid BCD in 15/15 reads.
* Waveform "Export view" VCD re-decoded from the file contains the packet on screen; address filter shows only 0x76.
* Timing on every packet: f_SCL = 95.24 kHz (Standard-mode ≤ 100 kHz ✔), SCL LOW ≥ 6.0 µs (≥ 4.7 ✔).
  **SCL HIGH minimum measures 4.0 µs (mean ≈ 4.4 µs) against the 4.0 µs minimum: within one sample (±0.5 µs) of the
  limit, so it is reported "not decidable" at 2 MS/s** (likewise some START setup / hold times). A faster reference
  analyzer is needed to judge those; nothing measured fails.

## New: Packet Proof (justify one packet)
Packet Analyzer → select a packet → **🔬 Packet Proof** (or the newest packet when none is selected). Window: SDA/SCL of
that packet with every sampled bit labelled, then 1 verdict · 2 which master (its own report) · 3 bit-by-bit table
(SDA at every SCL↑, role A6..A0 / R/W / D7..D0 / ACK, who drives SDA) · 4 START/STOP · 5 timing vs UM10204 · 6 ACK/NACK ·
7 meaning (register / BCD / ADC → °C, hPa / OLED command) · 8 capture integrity · 9 method. **Save proof report (HTML)**
writes a self-contained file with the waveform picture. Raw edges are kept for the most recent seconds only: prove a
recent packet (the window offers "Prove the newest packet of this device instead" otherwise).
Code: `analysis/packet_proof.py`, `gui/proof_views.py`, tests `tests/test_packet_proof.py`.


## Guided flow (controller firmware 2.5-turns → 2.6-live, verified live 2026-11-17 with `tools/verify_live_guided.py`: 19/19)
Toolbar → **▶ Guided flow**: the GUI drives both masters on the real bus (firmware CONDUCT mode), one transfer at a
time — Master 1 RTC 0x68 → BMP280 0x76 → OLED 0x3C, Master 2 RTC → BMP280 → OLED — and plays each packet completely,
every byte with its own ACK/NACK stage, before sending the next.  With **Inject errors**, every cycle ends with an
invalid address (0x2A, 0x45, 0x13, 0x5B rotating, Master 1 / Master 2 alternating: a real Address NACK) or an
arbitration between different slaves (0x76 vs 0x68, 0x3C vs 0x76, 0x68 vs 0x3C: a real arb_lost, measured 30/30 on the
bench).  Code: `gui/guided_flow.py`, firmware `I2C.c` (`run_step`, `run_arb_test`, `wait_bus_sync`), tests
`tests/test_guided_flow.py`, `tests/test_controller_firmware_host.py`.

### Live readings + presentation-mode controls (controller firmware 2.6-live, 2026-11-17)
* **OLED live readings** — every OLED step is ONE write: `[0x80, cmd]` pairs (Co = 1) set horizontal addressing and a
  window (columns 0..77; Master 1 pages 0-2, Master 2 pages 4-6), then `0x40` and three text lines: `M1 12:34:56`,
  `T 25.31 C`, `P 1008.25 HPA` — the time from that master's newest RTC read and the temperature / pressure from its
  newest BMP280 read.  Still write-only (no OLED read anywhere).  In the guided flow this 251-byte write plays its first
  8 bytes byte by byte (each ACK), the rest as one condensed stage + its ACK (a NACK is never condensed away).
  Packet Proof → Meaning lists the window commands and reads the three text lines back from the pixel columns; the
  Sensor page mirrors the OLED RAM rebuilt from the captured bytes ("CAPTURED").
* **DO CAL** — the GUI asks Master 1 to re-read the BMP280 chip ID (0xD0) and calibration (0x88-0x9F) at capture start,
  when a master connects during a capture, and when the guided flow starts, until the calibration has been captured:
  temperature / pressure are then decoded from the captured bus bytes (Sensor page, trends), not only from the masters'
  USB telemetry.  CONDUCT steps also report `sensor_telemetry` after a BMP280 read.
* **Presentation-mode controls on real hardware** — in the guided flow, **⏸ Pause** + **Step ▸ next phase** walks one
  real packet phase by phase (Phase n/6 banner) and, after STOP, sends the next real command; **NACK next transfer**
  makes the master due next address a device that is not on the bus (0x2A / 0x45 / 0x13 / 0x5B): a real Address NACK.
  An arbitration winner carries the phase-1 arbitration picture (the losing master outlined amber) from the REAL
  collision.  The pill reads "GUIDED · REAL HARDWARE".
* **Laptop screen** — the toolbar rows wrap (FlowLayout), so the window fits 1536 x 816 in both themes.
* **Analyzer** — `packet_analyzer.c` MAX_DATA 64 → 255 (EVENT_QUEUE_LEN 256 → 160 to keep RAM ≈ 205 KB): a segment
  now carries the whole 251-byte OLED write (before: cut at 64, so its attribution and the raw-vs-segment check
  failed).  GUI parser limit raised to match (`communication/protocol_parser.py` MAX_SEGMENT_DATA).
* Verified live 2026-11-17 in the Presentation theme at 1536 x 790: `tools/verify_live_guided.py` 26/26.
