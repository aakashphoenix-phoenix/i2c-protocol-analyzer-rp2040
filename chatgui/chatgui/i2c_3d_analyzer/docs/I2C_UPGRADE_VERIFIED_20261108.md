# I²C analyzer implementation and verification — 8 November 2026

Project: `F:\Raspberry_PI_Project\softwareproject12\chatgui\chatgui\i2c_3d_analyzer`

The existing application was edited in place. Architecture, navigation, hardware transport, device assets, and configured bus-speed constants were retained. No archives were opened. Original edited files are preserved in `source_backup_20261108` under this project.

## Implemented changes

- Fixed an additional waveform correctness bug: pyqtgraph's previous left-step rendering placed levels before their source timestamps. Right-step rendering now holds each recorded state until its next recorded transition, with a rendered-path regression test.
- Removed the horizontal byte/annotation bars while retaining both SCL/SDA traces, grid, START/repeated START/STOP boundaries, and protocol labels. Address labels cover the seven address-bit rises; R/W is separate at the eighth rise. ACK is green; all NACK labels are red. Binary bits appear when the viewport is sufficiently zoomed in.
- Retained captured bit-rise and byte ACK-fall timestamps. Added invalid/non-increasing timestamp and ambiguous simultaneous SDA/SCL-rise detection. Invalid captures lose decoder synchronization rather than receiving invented bits.
- Fixed deterministic demo timing and increased repeated-START/STOP setup/hold times to satisfy the Standard-mode minima relevant to these generated conditions. Demo input still uses the same decoder as raw live input.
- Added a shared playback model for selected packet inspection, synchronized waveform playhead, dashboard 3D overlay, and dedicated 3D overlay. READ data moves target-to-controller; address/R/W move controller-to-target; read ACK/NACK roles are controller-to-target. Removed the old reversal of SCL particles during READ. Overlays explicitly identify inferred roles, passive analyzer/USB transport, and sampled SCL/SDA levels when available.
- Added shared 0.25×/0.5×/1×/2× event playback, packet stepping, pause/resume, zoom buttons, and waveform refresh selection. These change presentation, not the hardware or stored capture timestamps. Event playback intentionally makes each protocol stage readable; it is not a time-scaled electrical simulation. Follow-latest inspection presents selected packets while all retained packets remain available in the table.
- Added raw-window annotations for native RP2040 firmware. Raw windows are independently decoded without double-counting packet metrics. Byte times are attached to firmware packet records only after matching their raw-decoded contents. Disagreements are logged and prevent positive communication-health claims.
- Added source, absolute start/end times, address ACK, per-data ACKs, and measured frequency columns to the packet table, plus source/timestamps/ACK classification fields in CSV and JSON exports.
- Added Bus Communication Health as the third chart after frequency and transaction rate in Performance Monitoring. The dashboard's third chart now shows the same evidence; utilization remains available as a dashboard card and in Performance Monitoring.
- Added explicit retention counts/notifications, invalid-record synchronization loss, and loss-counter handling. Incomplete packets no longer inflate completed-transaction rates. No demo fallback was added to live mode.

## Timing and interpretation

Raw-edge clock period is the difference between consecutive valid SCL rising timestamps within a frame; repeated-START boundaries are excluded. The displayed robust mean excludes periods longer than three times the median, without changing stored edges. Frequency is `1e9 / mean_period_ns`. HIGH and LOW intervals remain measured durations; long LOW intervals are separately reported with an undetermined cause. They do not prove target clock stretching. Native segment-only frequency is the firmware's reported measurement.

Packet details include observed SDA setup/hold minima, byte first-rise-to-ACK-fall durations, transaction duration and preceding STOP-to-START gap where observable. Cursors report `abs(B-A)` and its reciprocal. The waveform uses a linear relative microsecond axis while absolute timestamps are retained in the records/table/export. Raw-edge resolution comes from the handshake: the included native firmware reports 500 ns. Digital threshold samples cannot certify analog rise/fall times, voltage compliance or electrical bus ownership.

## NACK and health policy

`valid ACK opportunities = ACK + raw NACK` (only known acknowledgment results).

`raw NACK rate = 100 × raw NACK / valid ACK opportunities`.

`unexpected NACK rate = 100 × unexpected NACK / valid ACK opportunities`.

All raw NACKs remain counted. A read-end NACK is expected only in a complete, interpretable read with an acknowledged address, preceding data ACKs, and an observed STOP or subsequent repeated START. Address NACKs are unexpected even for unconfigured addresses; scanning intent is not guessed. Write-data and other NACKs remain separate categories.

The original code explicitly had no positive-health/recovery policy. The added **application policy**, not an NXP standard, uses trailing 60 capture-second buckets: at least one complete interpretable transaction and known ACK slots are required; zero unexpected NACKs gives HEALTHY, any unexpected NACK gives DEGRADED, and unreliable/incomplete/insufficient evidence gives UNKNOWN. This policy does not change the existing 1-second performance window or the separate 3-second per-device freshness boundary. Device Activity retains separately labelled session evidence. Disconnected/stopped/stale sources cannot establish healthy hardware. Demo health remains UNKNOWN with a separate simulated evaluation. Native segment-only sources with no new transactions for 60 wall seconds also become UNKNOWN rather than indefinitely retaining positive health.

## Source files modified or added

Paths below are relative to the project directory above.

| File | Purpose |
|---|---|
| `main.py` | Load the project-local declared OpenGL dependency |
| `Launch_Visual_Demo.cmd` | Launch through the working Python interpreter |
| `analysis/i2c_decoder.py` | Captured bit/byte timing and ambiguity checks |
| `analysis/capture_engine.py` | Source metadata, native-window decoding/matching, retention and integrity |
| `analysis/communication_health.py` | Contextual NACK counts and rolling policy |
| `analysis/performance_calculator.py` | Correct read-end NACK counting and timing summaries |
| `analysis/playback.py` (new) | Shared immutable playback stages/cursor |
| `sources/demo_source.py` | Seeded edge timing and Standard-mode START/STOP timing |
| `gui/waveform_viewer.py` | Timestamp-correct square waves, labels, zoom/refresh/playhead |
| `gui/main_window.py` | Shared selection/playback and acquisition-independent controls |
| `gui/network_3d.py` | Both 3D overlays, direction, sampled levels and pause |
| `gui/dashboard.py` | Health colors/evidence and third chart |
| `gui/performance_monitor.py` | Third health graph, rates, rule and state |
| `gui/packet_analyzer.py` | Synchronized selection, timing details and NACK colors |
| `gui/error_monitor.py` | NACK coloring |
| `gui/device_activity.py` | Consistent ACK/NACK colors |
| `models/packet_model.py` | Added provenance/timing/acknowledgment columns |
| `storage/capture_storage.py` | Absolute timestamps and classification exports |
| `tests/test_communication_health.py` | Tests updated to the requested explicit policy |
| `tests/test_bundle_sanity.py` | Explicit UTF-8 source reading on Windows |
| `tests/test_upgrade.py` (new) | Focused decoding, waveform, playback, live-record and export regressions |
| `tools/verify_gui_upgrade.py` (new) | Reproducible real Qt/OpenGL rendering check using synthetic records |

PyOpenGL 3.1.10, already declared as a project requirement, was installed under `.runtime_dependencies`. The system Python lacked it and the preexisting `.venv` points to a missing `C:\Users\AAKAS\...\Python313\python.exe`. No system environment was replaced. Firmware source, pin assignments and capture sampling configuration were not changed.

## Verification results

- Initial baseline: **63 passed, 2 failed**. One failure was Windows default-text decoding; this was fixed. The other was the host C compiler failing to start.
- Expanded Python/Qt suite: **85 passed** using `python -X utf8 -m pytest -q -p no:cacheprovider --ignore=tests/test_firmware_host.py` in 17.18 seconds.
- Separate firmware-host regression: **1 failed before compilation**, because `C:\MinGW\bin\gcc.exe` cannot launch: Windows error 623, Illegal System DLL Relocation. This remains unresolved; it is not a passing firmware test. No UF2 build or flash was performed.
- Real application rendering: waveform, dashboard, network and performance pages were rendered and inspected. The **OpenGL renderer was active and its context valid**. Both 3D components use the same selected transaction. Fallback Qt views are covered by automated tests too.
- Software live-record tests exercise the ingestion path and both views using deterministic test inputs; they are not physical hardware tests.
- Regressions cover ordinary/combined writes and reads, multibyte data, address/data NACKs, final-read NACK preservation, missing records, incomplete capture, ambiguous timing, long LOW intervals, render positions, annotations/colors, cursor reciprocal, playback speeds/immutability, CSV provenance, native snapshots without double counts, selection synchronization and disconnected-source labels.

## Launch

PowerShell:

```powershell
Set-Location 'F:\Raspberry_PI_Project\softwareproject12\chatgui\chatgui\i2c_3d_analyzer'
python -X utf8 main.py --demo
```

For hardware mode, use `python -X utf8 main.py` (omit `--demo`). The repaired `Launch_Visual_Demo.cmd` is an alternative demo launcher. Select a row in Packet Analyzer, then use Digital Waveform or 3D I²C Network; shared packet/speed/pause controls are in the header. Wheel/drag zoom and pan remain available. Zoom closer to inspect individual bits.

## Later physical verification

1. Build the existing analyzer target using the Pico SDK/VS Code project, from `firmware/native_rp2040_analyzer/packet_analyzer.c`, `capture.pio`, and its CMake target. A standalone build requires an existing `PICO_SDK_PATH`. This session did not build a UF2.
2. Hold BOOTSEL while connecting the **analyzer Pico**, then copy the successfully built `packet_analyzer.uf2` to its RPI-RP2 drive. Keep the controller firmware on the controller Pico.
3. Source-verified assignments: controller SDA **GP4**, SCL **GP5**; passive analyzer SDA **GP2**, SCL **GP3**. Connect both to the corresponding shared target bus lines, plus common GND. Check compatible 3.3 V bus pull-ups and do not apply 5 V to RP2040 GPIO. Do not join separate 3V3 regulator outputs.
4. Launch hardware mode, select the analyzer's actual current COM port, Connect, wait for the handshake, then Start capture. Port numbers are not assumed. The optional master telemetry port is separate and is not used to synthesize bus captures.
5. Confirm genuine incoming records and hardware provenance. Select a captured transaction. A packet outside the retained raw windows must say raw waveform unavailable rather than showing a fabricated trace.
6. Set A/B on consecutive SCL rising edges within a byte: check `f = 1/Δt`, and compare to measured packet frequency. Inspect the ninth SCL pulse: SDA LOW must show green ACK; SDA HIGH red NACK. Check seven address bits, separate R/W, and eight data bits.
7. Exercise write, read with final controller NACK, combined repeated START, and an intentionally absent address. Confirm the raw NACK count includes the final read NACK but only unexpected failures degrade the valid live health window.
8. Pause/resume and change visual speed while checking that capture counts continue. Disconnect the analyzer: no demo substitution and no healthy hardware claim are permitted. Compare exported timestamps/bytes/ACKs with the capture.

## Remaining limitations

- Physical bus, wiring, electrical timing and firmware build/flash are **unverified**.
- Existing firmware transmits intermittent measured windows (up to 2 ms, at most about four per second, with bounded edge capacity), not continuous full-bus waveform data. Many packet rows therefore cannot have a retained raw waveform. Missing windows are never filled from packet bytes.
- Native segment-only records lack individual bit timestamps. Their playback explicitly shows unavailable byte timestamps; precise timing is attached only when a matching raw window exists.
- Event-stage animation is explanatory playback. It does not claim to replay every physical SCL transition in 3D; the raw waveform remains the authoritative view of individual clocks.
- Retention is bounded. Old packets/raw records can be evicted; save/export promptly. Display selection is not a promise to animate every packet at slower playback speeds.
- The host GCC failure remains an environment blocker for the C decoder regression. The Python/Qt results do not substitute for that test or real RP2040 validation.

Reference: user-provided NXP UM10204 Revision 7.0 (1 October 2021), §§3.1.3–3.1.6, 3.1.9–3.1.11, Figures 4–6 and 9–13, Table 11/Figure 38. Existing screenshots were used as visual references, not evidence of hardware correctness.


## Timestamp display follow-up

Both the dashboard and dedicated 3D view now continuously display the source capture clock as HH:MM:SS.ffffff, plus the last READ and WRITE data-event timestamps and packet IDs. The selected event retains its own absolute timestamp. The capture clock updates with incoming records even when visual playback is paused; last-transfer timestamps remain visible during idle periods. They reset on a new capture. Segment-only byte timestamps remain unavailable and are explicitly labelled with the known frame-start time instead. No computer wall-clock time is substituted for bus timestamps.

Changed: analysis/playback.py, analysis/capture_engine.py, gui/network_3d.py; added tests/test_timestamps.py. Follow-up validation: 23 focused timestamp/upgrade tests passed.
