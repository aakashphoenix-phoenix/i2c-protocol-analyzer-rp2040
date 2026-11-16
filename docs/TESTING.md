# Testing and validation

This page distinguishes **observed results** from features that still need controlled validation.

## Previously observed bench behavior

- A Pico acting as master communicated with DS3231 (0x68), BMP280 (0x76), and OLED (0x3C).
- The passive analyzer reached the Python GUI and populated a live decoded transaction table.
- The GUI displayed SCL frequency around 94–95 kHz in sessions using nominal 100 kHz bus configuration.
- A separate Pico W acting as Master 2 reported successful DS3231 RTC and BMP280 chip-ID reads over its USB serial stream.
- Some GUI sessions reported **lost records** and **address NACKs**; these have not been fully diagnosed.

These are observations from development sessions, not formal calibration or conformance certification.

## Suggested reproducible test matrix

| Test | Setup | Pass/record criteria |
| --- | --- | --- |
| Single-master three-slave operation | Master 1 only | Expected addresses, correct sensor values, OLED updating |
| Analyzer protocol decoding | Master 1 + passive Pico | Address/RW/data/ACK checked against known transactions |
| RTC repeated START | Poll DS3231 registers | Detect paired address W/R and final read NACK |
| BMP280 chip-ID check | Read 0xD0 | 0x58 returned and address acknowledged |
| Second-master independent operation | Pico W only | Successful RTC/BMP280 reads from its own telemetry |
| Shared-bus dual-master operation | Both masters with recovery logic | Consistent behavior and error counters, not presumed contention |
| Loss analysis | Compare low-traffic and high-traffic cases | Record dropped counts, DMA overruns, framing issues |
| Address-NACK validation | Compare master return codes and analyzer | Separate genuine NACKs from decoder artifacts |
| Error simulation | Controlled, non-destructive protocol test | Intentional events clearly labeled |
| GUI export | Save capture | Timestamps, metadata and loss caveats preserved |

## Benchmark record format

For each experiment record:
- Date, firmware commit SHA, GUI commit SHA, board variant, wiring/pull-up configuration.
- Nominal versus measured SCL speed.
- Duration of **valid** observation.
- Number of complete/incomplete transactions.
- Per-address read/write and ACK/NACK counts.
- Queue drops, DMA buffer loss, framing errors.
- Whether demo data, playback data, or actual live data was used.

Do not cite unverified or simulated numbers as real measurements.

## Data privacy and safety

Before publishing screenshots and captures, review them for local file paths, names, serial numbers and other identifying details. Recheck the low-voltage circuit before every wiring change and disconnect power if modules become unusually hot.
