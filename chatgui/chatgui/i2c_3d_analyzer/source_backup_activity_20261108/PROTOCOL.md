# RP2040 analyzer firmware in this bundle — active wire format

The included analyzer emits **JSON Lines** with `"type"` keys (`hello`, `segment`, `loss`, `status`).
The included GUI supports this *native segment* format and its older `"t"` raw-edge format.
The updated native analyzer also sends bounded, measured `edges` snapshots; see the native waveform extension below.
To control the included analyzer, send plain text `HELLO`, `START`, `STOP`, `STATUS`, each followed by `\n`.
The GUI automatically selects this command format after the `hello` handshake.
See [CONNECT_PICOS_WINDOWS.md](CONNECT_PICOS_WINDOWS.md) for examples and limitations.

---

## Original GUI protocol description (supported for different firmware)

# Analyzer ↔ PC protocol, version 1

The protocol is JSON Lines over USB CDC serial: one UTF-8 JSON object per line, ending in `\n`.
It is implemented in C by `firmware/i2c_proto_emit.c` and in Python by `communication/protocol_parser.py`.

## Why this format

The bench runs at 100 kHz. Typical traffic is a BMP280 poll every 100 ms, an RTC read each second and short OLED updates, which comes to about 5–15 kB/s of JSON. Pico USB full-speed CDC realistically sustains 0.5–1 MB/s, so text is well within budget and easy to debug with any serial terminal.

A bus kept 100 % busy at 100 kHz produces about 200 k edges/s, roughly 1.4 MB/s of JSON. That is above what USB full-speed can carry, so the firmware **must** report the loss rather than hide it (see `overflow`). A future binary framing (`caps: "binary"`) is reserved for that case.

## Analyzer → PC records

| `t` | Fields | Meaning |
|---|---|---|
| `hello` | `proto`, `fw`, `board`, `sample_hz`, `tick_ns`, `caps[]` | Handshake. Sent at boot and in reply to `{"cmd":"hello"}`. |
| `edges` | `seq`, `t0`, `e:[[dt,s],…]` | Line transitions. `t0` is the sample tick of the first edge. Each `dt` is ticks since the previous edge in the record (the first is `0`). `s` is the state after the edge: bit 0 = SDA, bit 1 = SCL. The **first record after `start` must contain the initial line state.** |
| `evt` | `seq`, `ts`, `kind`, `n` | `overflow` (capture ring overran), `dropped` (n samples lost), `restart`, and optionally `start`/`stop`. Firmware-detected START/STOP are used only as a cross-check; the PC decodes from edges. |
| `stat` | `seq`, `ts`, `buf_pct`, `dropped`, `overflows` | About 2× per second. Advances hardware time while the bus is idle. |
| `txn` | — | Reserved for firmware-side decoding (`caps: "decoded"`). |
| `log` | `msg` | Free text. Shown in the event log, never interpreted. |

All records that carry `seq` share **one** uint32 counter. A jump in `seq` means records were lost. The GUI then:

- marks the open transaction as `gap`,
- discards the decoder state until the next START,
- excludes the missing interval from observed time, so it is not counted as idle,
- shows *Capture health: Degraded*.

### Capabilities

| cap | Meaning |
|---|---|
| `edges` | Sends `edges` records. **Required** for decoding, the waveform view and timing metrics. |
| `start_stop` | May send `evt start/stop`. |
| `seq` | Records carry a sequence number. |
| `stats` | Sends `stat` records. |
| `lossless` | Firmware guarantees no loss. Without it, the GUI shows "Good\*". |
| `decoded`, `binary` | Reserved. |

The GUI never claims a feature the connected firmware did not announce. For example, without `edges` the waveform page says *"Waveform unavailable — raw capture data required."*

## PC → analyzer commands

```
{"cmd":"hello"}   {"cmd":"start"}   {"cmd":"stop"}
```

## Example session

```
→ {"cmd":"hello"}
← {"t":"hello","proto":1,"fw":"pico-i2c-analyzer 1.0","board":"RP2040","sample_hz":10000000,"tick_ns":100.0000,"caps":["edges","start_stop","seq","stats"]}
→ {"cmd":"start"}
← {"t":"edges","seq":0,"t0":1000,"e":[[0,3],[25,2],[50,0],[25,1],[25,3]]}
← {"t":"stat","seq":1,"ts":5000,"buf_pct":3,"dropped":0,"overflows":0}
```

## Validation in the parser

Records are rejected and counted as *Serial framing errors* when any of these hold:

- the line is not UTF-8,
- it is not a JSON object,
- it has an unknown `t`,
- an integer field is not an integer or is out of range,
- a state is not 0–3,
- the line is longer than 64 KB.

Partial reads are reassembled by `LineAssembler`. A garbage burst with no newline is discarded and the stream resynchronises at the next line.


## Native measured waveform extension (2026-11-07)

Native `segment` and `loss` records and plain HELLO/START/STOP/STATUS commands are unchanged.
The hello now includes `edges`, `waveform:true`, `waveform_window_us:2000` and
`waveform_interval_us:250000`. The latter is a minimum interval between windows;
windows normally begin at a START. A buffer-aligned raw window is also sent at capture start and after about 1.25 s without a new window, so inactive/stuck inputs are sampled honestly.

Each independent window is one complete JSON line:

```json
{"t":"edges","snapshot":true,"t0":200000,"e":[[0,3],[1,2],[10,0],[10,2],[10,3],[3969,3]]}
```

This illustrative record uses actual capture ticks (500 ns per tick in this firmware).
`t0` is absolute; deltas accumulate within this line. Initial and final sampled
levels bound the measured interval. A window holds at most 768 state entries and
can end before 2 ms when full. It has no `seq`, so it does not alter the existing
segment/loss sequence. Two waveform queue slots bound memory; `waveform_dropped`
in status counts unsent windows separately from lost transaction records.

When `segment` is present, Python uses segments for packets/metrics and snapshots
only for drawing. It retains the latest window and never bridges the intervals
between snapshots or synthesizes edges from decoded bytes. Legacy edge-only
firmware/demo still runs through the existing edge decoder. Older segment-only
firmware remains compatible for packet/metric charts, without a waveform.
Continuous full-bus raw streaming is not provided by this extension.


### Capture integrity verification (2026-11-07)

The transaction queue now has 256 entries to absorb the master's 112-address
scan burst. The sample-word decoder uses a two-bit transition mask to visit
actual changed sample positions; the PIO words, 2 MS/s clock, GPIO states and
absolute sample indices are unchanged. Constant words produce no invented
edges. Unit tests compare every emitted raw state/tick against the original
packed samples, including random cross-word changes and constant HIGH/LOW.

On the connected COM28 analyzer, the final ten-second real capture reported
zero DMA lost buffers, zero dropped USB records and zero dropped waveform
windows. This is an observed test result, not a general lossless guarantee.
The GUI retains loss reporting, labels snapshot age, clears old source graphics
on a session change, and describes absent samples as unknown rather than idle.
