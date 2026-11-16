# System architecture

## Purpose

A dedicated Raspberry Pi Pico observes a shared I²C bus without taking part as a master or slave. The analyzer converts raw SDA/SCL transitions into protocol and performance records consumed by a Python desktop application.

## Hardware roles

| Component | Responsibility |
| --- | --- |
| Pico (Master 1) | Polls DS3231 and BMP280, updates I²C OLED |
| Pico W (Master 2) | Makes additional RTC and BMP280 reads; provides its own USB status |
| Pico (Passive analyzer) | Monitors bus on GP2/GP3 using PIO/DMA and emits USB serial records |
| DS3231 / BMP280 / OLED | Three I²C-addressed peripherals |
| Windows GUI | Displays captured packets, timings, status, and device activity |

Both masters share a physical two-wire bus. The analyzer shares SDA/SCL/GND but should keep its capture pins configured as inputs.

## Data paths

**Observed bus path:** physical SDA/SCL → PIO sampling → DMA buffers → C decoder → bounded event queue → USB serial → Python parser → application models → GUI.

**Optional master telemetry path:** master USB CDC serial → Python master telemetry parser → sensor status and transaction-source correlation.

These paths provide different evidence. Bus packets are independent observations; master telemetry contains the master’s interpretation of what it attempted and received.

## I²C operation

- Masters transmit START, a slave address plus R/W bit, data, ACK/NACK slots, repeated START as needed, and STOP.
- Register reads commonly use WRITE of a register pointer followed by repeated START and READ.
- A NACK on the last byte of a master read is often intentional.
- Address NACKs require checking the address, capture completeness, and master firmware return codes.

## Important instrumentation limits

- The bus alone does not reveal which master initiated a transaction. Provenance needs correlated master-side telemetry or other instrumentation.
- Short-window capture, buffer loss, or output queue drops can create misleading metrics.
- 2 MS/s sampling is suitable for initial 100 kHz investigation but is not a guarantee of lossless or electrical-specification-level validation.
- Digital GPIO cannot measure analog rise time, voltage levels, or pull-up current directly.
- The GUI must not synthesize raw waveforms from decoded packet summaries and present them as measured signals.

See [Protocol and metrics](PROTOCOL_AND_METRICS.md) for interpretation.
