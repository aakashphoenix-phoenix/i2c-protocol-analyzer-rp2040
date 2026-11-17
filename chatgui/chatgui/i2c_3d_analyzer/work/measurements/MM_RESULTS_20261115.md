# Multi-master live baseline, 2026-11-15 (Phase 1 + Phase 2)

Bench: analyzer COM28 (Pico H), Master 1 COM32 (Pico H, fw `2.0-multimaster`, MASTER_ID 1),
Master 2 COM34 (Pico W, fw `2.0-multimaster`, MASTER_ID 2). Both masters reported `scl_config`
requested 100000 Hz / applied 100000 Hz. Uptime at capture: about 45 min (master `t_us` ≈ 2.70e9).

All counts below are directly measured. Nothing was filtered: every raw NACK is counted.

## Files

| File | What it holds |
|---|---|
| `mm_baseline_before.jsonl` | raw analyzer lines, 30 s live (run 2, **primary**) |
| `mm_baseline_before_report.txt` | `tools/nack_phase_report.py` output for run 2 |
| `mm_before_com32.log`, `mm_before_com34.log` | raw master USB logs, 40 s, captured at the same time (HELLO, TXLOG 1, SCL?, STATS sent; TXLOG 0 restored at the end) |
| `mm_before_summary.json` | `tools/mm_bench_summary.py` output for run 2: phases, NACK categories, integrity, master deltas, per-master attribution |
| `*_run1.*` | an earlier identical run. Its master logs have **no** `txn`/`scl_config` lines: the logger's commands were never sent (my logger bug, fixed for run 2). Its analyzer data agrees with run 2. |

Reproduce the summary: `py -3.12 -X utf8 tools/mm_bench_summary.py --analyzer work/measurements/mm_baseline_before.jsonl --master work/measurements/mm_before_com32.log --master work/measurements/mm_before_com34.log --out work/measurements/mm_before_summary.json`

## Analyzer COM28 (run 2, 30 s)

records 6020 (2 hello · 32 status · 2731 segment · 3255 edge batches) · rejected 0 · lost 0 · packets **2419**
(all status `ok`, 0 without STOP) · 80.6 packets/s · 312 RESTART segments (one per read transaction).

Integrity: `dma_lost_buffers` 0 · `dropped_records` 0 · `waveform_dropped` 0 · `mid_byte_ends` 0 ·
`stream_dropped` 0 · `loss` records 0 · edge batches with `gap` 0 · engine overflows 0 ·
`decode_max_us` 7713 (block budget 16 384 µs). SCL measured 94.6 kHz median (92.8–94.7 kHz).
`bus_free_ns` min 46.5 µs, median 440 µs, 0 below UM10204 tBUF 4.7 µs.

| Addr | Txns | ACK | NACK | Phase breakdown |
|---|---|---|---|---|
| 0x3C OLED | 2107 | 35136 | **0** | addr-W ACK 2107 · write[0] 2107 · write[1] 2107 · write[>=2] 28815 |
| 0x68 RTC | 58 | 1044 | **58** | addr-W/addr-R ACK 58/58 · write[0] 58 · read[not final] ACK 870 · `read[final]:NACK(controller end-of-read, normal)` 58 |
| 0x76 BMP280 | 254 | 2110 | **254** | addr-W/addr-R ACK 254/254 · write[0] 254 · read[not final] ACK 1348 · `read[final]:NACK(controller end-of-read, normal)` 254 |
| ALL | 2419 | 38290 | **312** | raw NACK rate 0.81 % of 38602 ACK slots |

NACK categories (all addresses): unexpected addr-W/addr-R **0** · write-data **0** · premature read
`read[not final]` **0** · unverified read-end (`STOP not captured`) **0** · `mid_byte_end` **0** ·
normal controller end-of-read `read[final]` **312**. No other address appeared on the bus (no scan,
no `NACK_TEST_ADDR` probe, no redetect).

Run 1 (30 s, same bench): 2410 packets, 0 lost/overflow/gap/mid_byte_end, 310 NACKs, all `read[final]`
followed by STOP (0x68: 57, 0x76: 253), 0 unexpected.

## Masters (run 2, 40 s window: last − first `master_stats`)

| | Master 1 COM32 | Master 2 COM34 |
|---|---|---|
| txn_ok | +3048 | +166 |
| addr_nack / data_nack | 0 / 0 | 0 / 0 |
| arb_lost / retries / gave_up | 0 / 0 / 0 | 0 / 0 / 0 |
| busy_waits / busy_timeouts | 18 / 0 | 15 / 0 |
| timeouts / other_aborts / recoveries / recovery_skipped / controller_resets | 0 each | 0 each |
| cumulative since boot | txn_ok 205923, every error counter 0 | txn_ok 11188, arb_lost 1 (retries 1, gave_up 0), every other error counter 0 |
| txn lines (TXLOG) | 3046, all `ok`, 0 multi-attempt | 164, all `ok`, 0 multi-attempt |
| signatures | 0x3C w17 2560 · 0x3C w2 240 · 0x68 w1r16 40 · 0x76 w1r6 198 · w1r1 4 · w1r24 4 | 0x68 w1r16 36 · 0x76 w1r6 120 · w1r1 4 · w1r24 4 |

Attribution (analyzer ↔ txn telemetry, clock fit M1 −28.6 ppm, M2 +13.6 ppm): **2419/2419 packets
attributed** (M1 2295, M2 124), 0 unattributed, 0 master transfers missing on the bus.
Per master on the bus: M1 0x68 31 / 0x76 157 read-end NACKs; M2 0x68 27 / 0x76 97 read-end NACKs; all normal.

## Root cause of every observed NACK

All 312 (run 2) and 310 (run 1) NACKs are the controller's mandatory NACK on the 9th clock after the
last byte of a READ, each followed by a captured STOP (UM10204 §3.1.6). Each read transaction has
exactly one. There are no target faults, no write NACKs, no address NACKs, no analyzer drops and no
arbitration-related cuts in the window. So there is no wiring or device fault to report.

Read transactions per 30 s: 0x76 `w1 r6` 242 (data) + `w1 r1` 6 (chip ID 0xD0) + `w1 r24` 6
(calibration) · 0x68 `w1 r16` 58 (time + status in ONE transaction, so OSF costs no extra NACK).

## Phase 2: firmware review against the trace

- `I2C.c`: `SCAN_PERIOD_MS 0`, `NACK_TEST_ADDR 0x00`, no redetect fired (all devices OK): no
  absent-address probes. Retries 0, arb_lost 0 in the window. `bus_free` ≥ 46.5 µs, 0 `mid_byte_end`:
  no START collision. `BUS_IDLE_US`/`POST_TXN_HOLDOFF_US`/`hal_set_target` show no defect in this trace.
- `packet_analyzer.c` / `capture.pio`: 0 DMA overruns, 0 queue drops, 0 stream gaps, worst decode
  7.7 ms against a 16.4 ms block with a 4-buffer ring. No missing STOPs or splits.
- **No firmware edit, no build, no flash.** The `after` capture was not needed: nothing changed.

### Measured redundancy (not a defect; a schedule choice, left for the user)

The BMP280 is configured with `config` 0xA0 (t_sb 1000 ms) while Master 1 polls it every 200 ms and
Master 2 every 330 ms. On the bus, **213 of 242** `0x76 w1 r6` reads (88 %) returned data byte-identical
to the previous read; the value changed every ~0.92–1.0 s. Each read still costs one mandatory
end-of-read NACK. Matching the poll to the sensor rate (or the sensor rate to the poll) would remove
~7 redundant reads/s, but it changes the documented 200/330 ms multi-master schedule and needs
both masters reflashed, so it was not applied without approval. The 6 chip-ID `w1 r1` reads per
30 s are the documented identity/hot-swap check that the GUI decodes; they are not redundant.

## Phase 3 inputs (hardware-grounded)

- Bus: 100 kHz requested, 94.6 kHz measured; 80.6 packets/s total.
- Per 30 s: 0x3C 2107 writes (1921 × w17 data, 186 × w2 command) · 0x68 58 × w1r16 · 0x76 242 × w1r6,
  6 × w1r1, 6 × w1r24.
- Per master per second: M1 ≈ 70 OLED writes, 1 RTC read, 5 BMP reads (+ 2 identity reads / 10 s);
  M2 ≈ 0.9 RTC read, 3 BMP reads (+ 2 identity reads / 10 s).
- Transfer durations (master `dur_us` median): 0x3C w17 1732 µs, 0x68 1853 µs, 0x76 r6 894 µs, r24 max 2620 µs.
- Expected NACKs: exactly 1 per read transaction, `read[final]`, followed by STOP; 0 otherwise.
- Arbitration: 0 in 40 s; 1 since boot on M2 (≈45 min), retried and won.
- busy_waits ≈ 0.45/s (M1) and 0.38/s (M2); busy_timeouts 0.

`py -3.12 -m pytest -q`: `464 passed in 388.43s (0:06:28)`.

## Phase 3/4 (same day): hardware-grounded demo bench and cross-view verification

- `mm_baseline_before.i2ccap`: the real 30 s capture plus both controller logs merged by `tools/build_bench_capture.py`
  (records unchanged; controller records only ordered by time; meta `source: live`, `simulated: false`). Open it in
  the GUI (Capture History / Open capture) to replay the real bench.
- Demo mode now runs `DemoGenerator(masters=2)`: two SIMULATED controllers with the I2C.c schedules, the measured
  94.6 kHz SCL timing, idle windows, gaps and clock drift, and SIMULATED controller telemetry (`"simulated": true`).
  `DemoGenerator()` with no `masters` argument is the unchanged single-controller stream.
- GUI offscreen check (real replay vs a 32 s live Demo-mode run vs that demo capture saved and reopened): every view
  agreed with `CaptureEngine` (173 checks passed, 0 failed). Real replay: 2419 packets, ACK 38290, NACK 312 (all
  read-end), unexpected 0, Master 1 2295 / Master 2 124 attributed by telemetry. Simulation: 2605 packets, ACK 40936,
  NACK 337 (all read-end), unexpected 0, 0.817 %, Master 1 2469 / Master 2 136, all labelled SIMULATED.
