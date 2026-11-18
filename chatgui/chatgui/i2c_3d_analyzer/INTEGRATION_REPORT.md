# Integration report

## Source basis

- User's `i2c_3d_analyzer.zip` graphical Python application.
- Existing project-compatible `type: hello|segment|loss|status` adapter and RP2040 analyzer prototype C firmware, incorporated into the GUI.
- Optional Pico 1 master firmware for DS3231 (`0x68`), BMP280 (`0x76`), SSD1306 (`0x3C`).

## Improvements in this package

1. Analyzer starts in standby and waits for host `START` after protocol handshake.
2. GUI Start Capture is disabled until a valid firmware handshake arrives.
3. DMA overrun guard prevents a busy buffer from being overwritten while the decoder consumes it. A capture interruption is reported as `loss` and the sampler resumes with a new time origin.
4. Device segment timestamps use the Pico's monotonic clock when (re)starting the sampler. Gap durations are not silently labelled bus idle.
5. Incomplete segments before an address byte are retained as incomplete instead of being rejected by the GUI parser.
6. Both Pico firmware source trees and CMake build targets are included with explicit Windows instructions.
7. The original GUI 3D pages, charts, history and demo mode remain intact; no pretend raw-waveform records are generated.

## Offline checks performed in this environment

- `python -m compileall -q .` completed.
- `pytest -q` → 40 passed (Python parser, decoder, performance, capture round-trip, native firmware protocol integration, and source contracts).
- The tests did **not** render/launch the PyQt6 GUI; the PyQt6 package is unavailable in this environment. In `tests/conftest.py`, Qt threading signals are minimally stubbed **only for running non-GUI Python unit tests**.
- No RP2040 hardware, Pico SDK cross-compiler or physical USB serial device was available for compiling, flashing or confirming the C firmware. These steps **must** be tested on your Windows/Pico setup.

## Known limitations

- Analyzer firmware is a **100 kHz-target prototype**, not a certified lossless analyzer. Data can be lost if CPU or USB processing cannot keep up; reported gaps and dropped records must be checked.
- No raw edge samples or digital waveform transport with the supplied native C firmware. The waveform page correctly remains unavailable in hardware mode.
- No hardware-validated 400 kHz operation, high-speed I²C modes, SMBus/PMBus extension, or analog voltage/rise-time measurement.
- The optional master USB connection reports real sensor values from the master; they are **not evidence of observed bus packets**.
- Source files are supplied rather than precompiled `.uf2` firmware.
