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

### Continuous RLE edge stream (`edge_stream`, 2026-11-14)

Firmware advertising the `edge_stream` capability sends the bus as a run-length-encoded
edge stream instead of isolated snapshots (snapshots are then disabled; `STREAM 0` /
`STREAM 1` before START select the old/new mode, default is streaming):

```json
{"t":"edges","stream":true,"t0":523390,"s":3,"gap":true,"r":"%A7#kB"}
```

* `t0` is the absolute sample index (500 ns ticks) of the first entry and `s` its level
  (bit 0 SDA, bit 1 SCL); `gap` is present only when samples were really lost since the
  previous batch (queue overflow, DMA overrun, capture restart). A batch holds at most 256 entries.
* `r` is a string, one entry per edge after the first, normally ONE character (about 1.1 bytes
  per edge; decimal text was about 3). Each entry is a number written in base 45, least
  significant digit first, from the 90 symbols `#`..`}` (the backslash is skipped); a symbol
  of value >= 45 means more digits follow. `v = 2*dt + (SCL toggled ? 1 : 0)` when exactly one
  line changed (`dt` = ticks since the previous entry); `~` escapes the rare kinds:
  `~` + `v = 2*dt + 1` is an idle heartbeat (level unchanged), `v = 2*dt + 0` both lines changed
  at once. The parser (`decode_stream_text`) expands this into the packed list
  `[dt << 2 | state, ...]` (first entry dt 0), which is also still accepted directly as `r`.
* After 100 ms with no edge the firmware emits a heartbeat so idle time is covered (flat
  line) without ever being reported as a gap.
* `status` adds `stream`, `stream_entries` and `stream_dropped` (whole batches dropped;
  the next batch carries `gap:true`).

Link budget (measured on the two-master bench, ~24k edges/s): the SDK's USB CDC TX FIFO is a
single 64-byte packet, which limits the whole link to ~78 KB/s. With decimal text the
stream was dropped ~10 times per second; with the dense text plus a 256-byte FIFO
(`CFG_TUD_CDC_TX_BUFSIZE=256` in `CMakeLists.txt`) it ran with 0 dropped batches over
25 s. Larger FIFOs (512, 4096) were faster but occasionally lost ~64-byte chunks mid-line on
this SDK/host pair: re-run a line validation (every line through `parse_line`) before
raising it. Records are rendered into one buffer and written with a single `fputs`.

Python keeps the entries in a bounded rolling ring (`CaptureEngine.edges`, never cleared
by a new batch), decodes transactions from it for the waveform, and records only real
`gap` batches in `edge_gaps`, plus a serial line rejected while streaming that may have
held edges (a damaged `{"type":...` segment record cannot, so it is not a gap). Segment/loss
records are unchanged.


### Compact segment records and 400 kHz headroom (`compact_segment`, 2026-11-14)

At 400 kHz a busy two-master bench emits ~90 segment records/s, ~530 bytes each: together with the edge stream
that is ~75 KB/s against a USB CDC link that carries ~78 KB/s, so the stream was dropped ~10 times per second
(2470 "data-loss gaps" in one session). Firmware advertising `compact_segment` therefore omits what the host can
restore, and `parse_line` expands it back so every consumer sees the identical record:

* `start_us`, `end_us`, `duration_us` are derived from `start_sample` / `end_sample` (2 ticks per us).
* Defaults are omitted: `start:"START"`, `end:"STOP"`, `address_bits:7`, `address_known:true`,
  `direction:"write"`, `address_ack:true`, `truncated`/`incomplete`/`mid_byte_end:false`, `scl_low_over_50us:0`.
* `acks` is one `1`/`0` character per data byte (`"11110"`) instead of `[true,true,...]`.

About half the size (a 24-byte read: ~530 -> ~250 bytes); full records are still accepted. Measured on the bench at
400 kHz: 53 KB/s total, 0 dropped stream batches over 30 s.

Core 1 (capture/decode) changes made for 400 kHz, where one 16.4 ms block can hold ~13000 edges:
* the DMA ring has 4 buffers (`NBUF`) instead of 2, so one slow block is no longer an overrun (a lost 16 ms);
* the whole program runs from SRAM (`pico_set_binary_type(... copy_to_ram)`): decoding from flash shared the XIP cache
  with Core 0 printing;
* `status` reports `decode_max_us` (slowest block decode since START; the budget is 16384 us). At 400 kHz it peaks at
  ~21 ms, i.e. a block can take longer than real time and the ring absorbs it; `dma_lost_buffers` stays 0.

Host side: a reader that stalls for 100 ms or more (a busy GUI thread) loses data regardless of firmware: a plain
Python reader that sleeps 100-250 ms every 0.5 s sees corrupt lines (a 64-byte USB packet missing at a line
boundary) and device `gap:true` flags, a reader that never stalls sees none. The engine turns a rejected line
(while streaming) into a real gap marker rather than a silent hole.

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

## Exact sample timestamps (`sample_ts`, 2026-11-08)

Analyzer firmware advertising the `sample_ts` capability adds exact 0.5 µs sample
indices to its records; the existing fields are unchanged for older GUIs:

| Record | Added field(s) | Meaning |
|---|---|---|
| `segment` | `start_sample`, `end_sample` | analyzer sample index of the START/Sr and of the STOP/Sr/GAP. `start_us`/`end_us` remain and are these values ÷ 2, truncated to whole µs. |
| `loss` | `t_sample` | sample index where the DMA loss was detected (`t_us` kept). |

The GUI uses `sample × tick_ns` (500 ns) when present and otherwise falls back to
`*_us × 1000`. The validator rejects `end_sample < start_sample`. Compatibility is
covered by `tests/test_firmware_host.py` (real C emitter compiled on the host) and
`tests/test_native_firmware.py` (records without the new fields).

## Multi-master additions (2026-11-14)

**Analyzer `segment` (optional fields):** `mid_byte_end` (bool) marks a segment cut by a START or STOP inside a byte. `bus_free_ns` (int) is the measured STOP→START idle time, present only when the previous STOP was captured. `status` gains `mid_byte_ends`. Hello capabilities gain `mid_byte_end` and `bus_free`.

**Controller ports (one per master, never the analyzer port):** JSON lines with `"source":"controller"` and `"master_id"` 1/2:
`hello` (role `controller`), `txn` (per transfer after `TXLOG 1`: `seq`, `t_us`, `dur_us`, `addr`, `wlen`/`rlen` = bytes on the bus, `b0`, `result` = ok | addr_nack | data_nack | arb_lost | timeout | abort_other | bus_busy, `attempts`, `arb_lost`), `arb_lost` (per lost attempt: `addr`, `attempt`, `byte`, `abort_source`, `backoff_us`, `retry`), `master_stats` (cumulative counters, 1 Hz), `bus_recovery`, `controller_reset`, `txlog`, `scl_config`, `rtc_set`, `sensor_telemetry`.
Commands: `HELLO`, `STATS`, `TXLOG 0|1`, `SCL?`, `SCL 100000|400000`, `TIME YYYY-MM-DDTHH:MM:SS`.
The analyzer parser rejects controller records with a "connect this COM port as a master" error, and the controller parser rejects analyzer records the same way. See [MULTI_MASTER.md](MULTI_MASTER.md).
