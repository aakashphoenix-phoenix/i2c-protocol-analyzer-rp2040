# Getting started on Windows

> **Source archive status:** Detailed source-specific build instructions will be completed when the historical (v1.0) and upgraded (v2.0) source trees are published. The instructions below describe the established toolchain and workflow, not a claim that this documentation-only repository currently builds.

## Tools

- Windows PC and USB data cables.
- VS Code and the Raspberry Pi Pico extension.
- Pico SDK 2.3.1 with CMake, Ninja and ARM compiler provided through the extension setup.
- Python 3 and project-specific dependencies (PyQt6, PySerial, PyQtGraph, NumPy and 3D dependencies when used).

## Firmware layout planned for publication

\`\`\`text
firmware/
  master1_pico/       # Pico C firmware for RTC/BMP280/OLED
  master2_pico_w/     # Pico W C firmware for RTC/BMP280
  packet_analyzer/    # Pico C firmware + PIO program
software/
  i2c_3d_analyzer/    # Python GUI package, parsers, models, tests
\`\`\`

Use each project’s own \`CMakeLists.txt\` and \`pico_sdk_import.cmake\`. Do not merge multiple projects into a single CMake file. Do not place \`.c\` or \`.pio\` source files inside the generated \`build/\` folder.

## Build a Pico firmware project

1. Open the *firmware project folder* in VS Code.
2. Select board \`pico\` for original Picos, or \`pico_w\` for the second master.
3. Configure CMake.
4. Compile Project.
5. Locate the generated \`.uf2\` within the actual build output folder.
6. With the target Pico disconnected from the bus, hold BOOTSEL while connecting USB.
7. Copy the corresponding \`.uf2\` to the \`RPI-RP2\` drive.
8. Check USB serial output with an appropriate terminal.

Only flash the board intended for each firmware.

## Run the Python application (once source is published)

From the GUI project folder in PowerShell:

\`\`\`powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
\`\`\`

Use \`python main.py --demo\` only when the installed GUI includes the explicitly labeled demonstration-data mode. Demo data must never be presented as real hardware data.

Connect to the **analyzer** serial port and wait for a compatible firmware handshake before starting capture. If Windows reports "Access is denied", close other serial monitors using the same COM port. COM numbers are assigned dynamically and should not be hard-coded.

Master USB telemetry, when supported by the GUI, may be connected on a separate COM port. The analyzer cannot directly determine the originating master from SDA/SCL alone.

## Troubleshooting

| Symptom | First check |
| --- | --- |
| CMake cannot find source | Match CMake source filename to the actual \`.c\` file |
| SDK import error | Confirm SDK installation and project folder |
| No \`RPI-RP2\` | USB data cable and BOOTSEL procedure |
| No serial port | USB CDC firmware, correct board, cable, Device Manager |
| COM port access denied | Another application has opened it |
| Handshake not recognized | Analyzer firmware/GUI protocol compatibility |
| No packets | Actual master activity, wiring, START command, analyzer status |
| Degraded capture | Lost record/sequence diagnostics, USB queue throughput |

Never disable capture-health warnings merely to make the dashboard show "Healthy".
