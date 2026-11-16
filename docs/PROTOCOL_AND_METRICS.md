# Protocol decoding and performance measurements

## I²C message anatomy

A typical transaction consists of:

\`\`\`text
START → (7-bit address + R/W) → ACK/NACK → data bytes and ACK slots → STOP
\`\`\`

Register reads may be:

\`\`\`text
START → address+W → ACK → register → ACK
REPEATED START → address+R → ACK → bytes → final NACK → STOP
\`\`\`

ACK is the ninth SCL pulse after each eight-bit byte. A receiver pulls SDA low to ACK. The master normally NACKs the last byte of a read.

## Example device associations

| 7-bit address | Configured name | Typical operation |
| --- | --- | --- |
| 0x68 | DS3231 RTC | Register-pointer WRITE then READ |
| 0x76 | BMP280 | Register-pointer WRITE then READ; chip ID at 0xD0 is 0x58 |
| 0x3C | I²C OLED | Display control/data WRITEs |

An address is not a cryptographic device identity. Unknown or unexpected addresses should remain visible, not automatically reassigned to a known module.

## Analyzer data

The evolving analyzer firmware reports line-delimited structured records over USB CDC (for example status, decoded segments, capture-loss reports and a firmware handshake). Exact field names and sample schema must be documented against the **committed firmware source** before clients rely on them.

The desktop UI should accept only compatible protocol versions and display an explicit unsupported-firmware warning when needed.

## Metrics

- **SCL frequency (Hz):** measured clock periods over captured bus activity.
- **Transaction rate:** completed transactions divided by the chosen time window.
- **Bus utilization:** the measured interval during which the bus was active, divided by *valid observed capture time*.
- **ACK/NACK:** distinguish address acknowledgements, data acknowledgements, and deliberate final-read NACKs.
- **Capture health:** record sequence gaps, output queue drops, DMA losses and malformed serial frames separately.

If samples or messages are missing, aggregate statistics should indicate that limitation. Configured \`100 kHz\` speed must not be confused with observed \`~94–95 kHz\` frequency.

## Realism in the 3D GUI

A high-quality visualization can animate START, address/R/W, ACK, bytes, repeated START and STOP *when supported by real decoded events*. Highlighting a device based on address is useful but does **not** prove which master sent that packet. Raw waveform displays require timestamped edge/sample data, not reconstructed decorative pulses.

## Diagnostics still to validate

- High reported address-NACK counts versus master-side outcomes.
- Output record loss during OLED activity.
- Multi-master arbitration behavior and fault recovery.
- Accurate event ordering and synchronization between bus analyzer and multiple master USB streams.
