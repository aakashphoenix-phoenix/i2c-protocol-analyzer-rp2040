# I²C Protocol Analyzer & Real-Time Performance Monitoring System

A hardware-based I²C monitoring and experimentation project using **Raspberry Pi Pico / Pico W (RP2040)** controllers and a **Python desktop GUI**.

> **Repository publication status:** Project documentation is available. The original project archive (proposed **v1.0**) and upgraded source files (proposed **v2.0**) are being reviewed and organized for publication. No historical release should be treated as published until its actual source has been committed and tagged.

## What does this project do?

I²C (Inter-Integrated Circuit) allows controllers to communicate with several peripherals using two shared signal lines: **SDA** (data) and **SCL** (clock). This project examines what happens on that bus rather than merely reading sensors. A separate RP2040 observes the lines without intentionally driving them, decodes bus activity, and reports data to an interactive Python application.

The extended system uses **two masters**, **three slave peripherals**, and **one passive analyzer**:

| Hardware | Role | Interface |
| --- | --- | --- |
| Raspberry Pi Pico (RP2040) | Master 1: RTC/BMP280 reads and OLED updates | I²C0, GP4 SDA, GP5 SCL |
| Raspberry Pi Pico W (RP2040) | Master 2: additional RTC and BMP280 reads | I²C0, GP4 SDA, GP5 SCL |
| Raspberry Pi Pico (RP2040) | Passive protocol analyzer | GP2 observes SDA; GP3 observes SCL |
| DS3231 RTC module | Slave: time and date | 7-bit address **0x68** |
| BMP280 module | Slave: temperature and pressure | Observed address **0x76**, chip ID **0x58** |
| SSD1306-type I²C OLED | Slave: display | Configured address **0x3C** |
| Windows laptop | GUI and serial monitoring | Separate USB connections to Pico boards |

**All three slave addresses above were observed in the existing analyzer GUI.** Addresses describe bus endpoints; address detection alone is not proof of a particular sensor model. The actual BMP280 chip ID was checked separately.

## System architecture

```text
       Pico 1 (Master 1)       Pico W (Master 2)
                |                    |
                +---- SDA + SCL -----+
                           |
           +---------------+----------------+
           |               |                |
        DS3231 RTC       BMP280         I²C OLED
           |               |                |
           +---------------+----------------+
                           |
               Pico 2 (passive analyzer)
                  PIO + DMA capture
                           |
                     USB JSON records
                           |
                 Python desktop GUI
             Packet view · 3D bus · Metrics
```

The analyzer is **in parallel**, not between the masters and slaves. All devices share a compatible **3.3 V logic** bus and common ground. Do not connect separately USB-powered Pico boards' 3V3 outputs together.

## Main capabilities

**Demonstrated on the evolving hardware/software setup:**
- Single-master operation and addressing of multiple slave devices.
- Independent Pico W reads from the DS3231 and BMP280.
- Passive monitoring of START/STOP, slave address, read/write direction, data bytes, ACK/NACK, and timing.
- Live Python GUI with decoded transaction stream, detected addresses, measured SCL frequency, transaction rate, utilization, and 3D bus overview.
- Real-time reporting of capture-health issues such as missed output records.

**Under development / requires additional validation:**
- Reliable multi-master arbitration handling, provenance of each master transaction, and arbitration-loss diagnosis.
- Minimizing dropped records and investigating unexpected address NACK reports.
- Faithful raw SDA/SCL waveform streaming and synchronized edge-based playback.
- Controlled fault-injection UI and automated recovery experiments.
- Versioned, reproducible firmware and GUI source releases.

> The presence of a GUI control or displayed metric does not imply complete capture reliability. Missing records and incomplete transactions must be reported explicitly.

## Real bench observations

In prior live captures, the analyzer reported clock rates near **94–95 kHz** when the master was configured near **100 kHz**. The desktop application displayed traffic at **0x3C, 0x68, and 0x76**. Separate Pico W USB telemetry showed successful DS3231 time reads and BMP280 chip-ID **0x58** reads.

Some runs also reported hundreds of **lost records** and **address NACKs**. These are **open diagnostic issues**, not validated fault-injection results. A final read-byte NACK can be intentional and normal.

See [Testing and validation](docs/TESTING.md) for measurement cautions.

## Getting started

1. Read [Hardware and wiring](docs/HARDWARE.md) before connecting boards.
2. Read [Build and run guide](docs/GETTING_STARTED.md) for the Pico SDK and Windows Python environment.
3. Read [Data format and interpretation](docs/PROTOCOL_AND_METRICS.md).
4. Review [Architecture](docs/ARCHITECTURE.md) and [Version history](CHANGELOG.md).

**Source release note:** The historical source snapshot from the supplied original-project ZIP is awaiting archive inspection. Until the actual `firmware/` and `software/` trees are committed, this repository is documentation for the project—not yet a ready-to-build software release.

## Technology

- **Embedded:** C11, Raspberry Pi Pico SDK 2.3.1, RP2040 PIO, DMA, USB CDC serial.
- **Desktop:** Python 3, PyQt6, PySerial, PyQtGraph, 3D visualization through OpenGL-capable libraries.
- **Workflow:** VS Code + Raspberry Pi Pico extension, CMake, BOOTSEL UF2 flashing, GitHub.

## Roadmap and versions

- **Original version (v1.0 planned):** Preserve the actual first-project source exactly as submitted and document its verified feature set.
- **Extended version (v2.0 planned):** Track the dual-master setup, analyzer, 3D GUI, and real sensor telemetry as a separate source snapshot.
- **Future:** Capture-reliability improvements, 3D protocol-phase visualization, fault-injection tests, and arbitration validation.

No `v1.0` or `v2.0` tag should be created until the corresponding source snapshot is complete. See [CHANGELOG.md](CHANGELOG.md).

## License and contributors

The license and contributor details will be added after confirming ownership, team approvals, and publication permissions. Until then, **no open-source license has been granted** by this repository.

This is an educational engineering project, not a certified protocol analyzer or electrical test instrument.
