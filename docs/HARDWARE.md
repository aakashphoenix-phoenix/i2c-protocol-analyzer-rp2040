# Hardware and connections

## Known hardware

- Original Raspberry Pi Pico (RP2040) — Master 1
- Original Raspberry Pi Pico (RP2040) — passive analyzer
- Raspberry Pi Pico W (RP2040) — Master 2
- DS3231 real-time-clock module — address 0x68
- BMP280 pressure/temperature module — observed address 0x76, chip ID 0x58
- I²C OLED (SSD1306-type) — address 0x3C
- USB data cables, jumper wires, breadboard as needed

## I²C bus wiring

| Board | SDA | SCL | GND |
| --- | --- | --- | --- |
| Pico Master 1 | GP4, physical pin 6 | GP5, physical pin 7 | Common ground |
| Pico W Master 2 | GP4, physical pin 6 | GP5, physical pin 7 | Common ground |
| Pico Analyzer | GP2, physical pin 4 (input) | GP3, physical pin 5 (input) | GP3 **is not ground**: use physical pin 3 GND |

For the analyzer, **physical pin 3 is GND** and physical pin 5 is **GP3/SCL**. Do not confuse GPIO numbers with physical pin numbers.

Connect all SDA points together and all SCL points together. Connect module GND and Pico GND references together. Device VCC pins should be powered from one suitable, verified module supply; power arrangement and pull-ups must be checked for the actual breakout modules.

## Electrical precautions

- Power everything off before rewiring.
- Use 3.3 V-compatible logic. RP2040 GPIOs are not 5 V tolerant.
- Do not connect the 3V3 OUT rails of separately USB-powered Pico boards together.
- The analyzer must **not** drive SDA or SCL.
- Confirm the effective pull-up resistance: breakout modules may each include resistors, so parallel pull-ups can become stronger than intended.
- Check pin labeling and module voltage requirements before applying power.
- If a board or module becomes hot, disconnect power immediately and inspect the circuit.

## Initial bring-up sequence

1. Flash and serial-test each Pico independently, disconnected from SDA/SCL.
2. Verify Master 1 communicates with the RTC/BMP280/OLED by itself.
3. Add the analyzer as a passive observer; verify valid address captures and protocol data.
4. Connect Pico W to the shared bus only after its own firmware and serial commands work.
5. Test Pico W as sole active master before attempting simultaneous traffic.
6. For actual multi-master contention experiments, ensure **both** masters handle arbitration loss, timeouts and retries correctly.

## Measurement notes

The measured SCL frequency may differ from the configured target. Capture-health warnings and lost records mean a session is not fully reconstructable. Those warnings should remain visible in published measurements.
