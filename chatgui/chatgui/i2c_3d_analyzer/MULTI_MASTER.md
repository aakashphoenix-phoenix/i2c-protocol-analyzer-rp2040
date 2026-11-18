# Multi-master bench: 2 masters · 3 slaves · 1 passive analyzer

| Board | Role | Firmware (.uf2) | Pins | USB |
|---|---|---|---|---|
| Pico 1 (Pico H) | **Master 1**: RTC + BMP280 reads, owns the OLED | `Master1_PicoH_COM32_I2C.uf2` (target `I2C`, `MASTER_ID=1`) | GP4 SDA, GP5 SCL, 3V3 → +3.3 V rail, GND | COM32 |
| Pico 3 (Pico W) | **Master 2**: RTC + BMP280 reads, staggered, never drives the OLED | `Master2_PicoW_COM34_I2C_master2.uf2` (target `I2C_master2`, `MASTER_ID=2`) | GP4 SDA, GP5 SCL, **GND only** | COM34 |
| Pico 2 (Pico H) | **Passive analyzer** | `Analyzer_PicoH_COM28_packet_analyzer.uf2` | GP2 SDA tap, GP3 SCL tap, GND | COM28 |
| DS3231 0x68 · BME/BMP280 0x76 · SSD1306 0x3C | targets | — | VCC → +3.3 V rail, GND, SDA, SCL | — |

The ready-built files are in `softwareproject12/firmware_uf2_multimaster/`.

## Wiring rules

- Join the **3V3 rail to Pico 1 only**. Never connect the 3V3(OUT) pins of two Picos together. Pico 2 and Pico 3 share **GND only** with the rail.
- Use **one** set of pull-ups. The breakout modules already have them. Three modules in parallel give roughly 1.5–3 kΩ, which is fine at 100 kHz. Do not add more.
- All boards share one GND.
- Run both masters at the **same SCL rate**. During arbitration the bus clock is the wired-AND of both masters, so the slower master sets the pace.

## Flashing

1. Hold **BOOTSEL**, plug in the board, and release. An `RPI-RP2` drive appears.
2. Copy that board's `.uf2` onto the drive. The board reboots into the new firmware.
3. Do this once per board. Flashing the wrong file shows up in the GUI as a **"Master ID mismatch"** event: the firmware reports its own `master_id`.

## Building (Pico SDK 2.3.1, the same tools as the VS Code extension)

```bat
set PATH=%USERPROFILE%\.pico-sdk\cmake\v4.3.4\bin;%USERPROFILE%\.pico-sdk\ninja\v1.13.2;%USERPROFILE%\.pico-sdk\toolchain\15_2_Rel1\bin;%PATH%
set PICO_SDK_PATH=%USERPROFILE%\.pico-sdk\sdk\2.3.1

cd I2C\I2C
cmake -S . -B build_master1 -G Ninja -DPICO_BOARD=pico
ninja -C build_master1 I2C                         &:: -> build_master1\I2C.uf2          (Master 1, Pico H)
cmake -S . -B build_master2_pico_w -G Ninja -DPICO_BOARD=pico_w
ninja -C build_master2_pico_w I2C_master2          &:: -> I2C_master2.uf2                (Master 2, Pico W)

cd ..\..\packet_analyzer\packet_analyzer
cmake -S . -B build_multimaster -G Ninja -DPICO_BOARD=pico
ninja -C build_multimaster                         &:: -> packet_analyzer.uf2            (analyzer)
```

Neither master firmware uses the LED or the Pico W radio, so each build tree produces both targets. `PICO_BOARD` only selects the board defaults.

## What the master firmware does on a shared bus (`I2C.c`)

| Rule | How |
|---|---|
| Bus idle before START | Our controller is idle (`IC_STATUS.MST_ACTIVITY`). SDA and SCL stay HIGH for a whole idle window (Master 1: 40 µs, Master 2: 70 µs, plus 0–16 µs jitter). No START from anyone appears in that window (`IC_RAW_INTR_STAT.START_DET`). |
| Fair access | After its own STOP, each master waits 120 µs, longer than the other master's idle window. A waiting master therefore always gets the next gap (round robin). |
| Arbitration loss ≠ NACK | Transfers are register-level. `IC_TX_ABRT_SOURCE` is read **before** it is cleared, so `ARB_LOST` is separate from `ABRT_7B_ADDR_NOACK` / `ABRT_TXDATA_NOACK`. The SDK's `i2c_write_*` cannot make this distinction. |
| Back-off + retry | After an arbitration loss the master waits a board-specific, jittered back-off (150/230 µs × attempt, plus up to 200 µs) and retries, up to 6 attempts. A loss never marks a target offline and never starts bus recovery. |
| Safe recovery | The 9-clock recovery runs only when SDA or SCL has been LOW for 35 ms (SMBus tTIMEOUT) with **no SCL edges** (no other master clocking). Timeouts on a bus that is not stuck only re-initialise our own controller. |
| Staggered schedules | Master 1: BMP every 200 ms, RTC + OLED every 1 s. Master 2: BMP every 330 ms, RTC every 1.1 s, starting 97 ms later. |
| Atomic register reads | Write pointer, repeated START, read: one transaction. The other master cannot move a target's register pointer in between. |
| Shared BMP280 | Configured only if its `ctrl_meas`/`config` registers differ, so the masters never restart each other's conversions. |

## GUI (`python main.py`)

1. **Analyzer port** COM28 → **Connect** → **Start capture**.
2. **Master 1** COM32 → **Connect**. **Master 2** COM34 → **Connect**. On connect the GUI sends `HELLO`, `TXLOG 1` (per-transfer log) and `SCL?`.
3. Within a few seconds each master's clock is aligned to the analyzer's capture clock, and the event log shows **"Controller clock aligned"**. From then on every packet carries its master.

Where it shows up:

- **3D/2D network**: Master 1 on the left, **Master 2 above the rails on the right** with its own feeders and USB link, and the analyzer and the three slaves. The packet arrow starts at the master that owns the packet, or at the analyzer tap when the packet is unattributed. Amber flashes mark arbitration losses. The node inspector shows live per-master counters.
- **Packet Analyzer**: a **Master** column and a **Master** filter. The detail panel says how the packet was attributed and whether the owner won the bus after lost arbitration attempts.
- **Device Activity → Controllers (multi-master)**: per-master bus-access health (HEALTHY / CONTENDED / DEGRADED / UNKNOWN), transfers OK, address/data NACKs, arbitration lost, retries, gave up, bus-busy waits/timeouts, timeouts, recoveries, attributed bus packets and clock alignment. There is also a **master × target** matrix and the **recent arbitration events**, each with its winning bus packet (click to inspect).
- **Dashboard**: a "Masters:" line. **Bus health** keeps its NACK policy unchanged; arbitration is shown next to it, labelled "not NACKs".
- **SCL setting**: target *Master 1*, *Master 2* or *Both masters*. The GUI warns when the two report different rates.
- **Capture files** store the controller records too, so a replay reproduces the attribution and the arbitration events.

### How attribution works, and its limits

A two-wire tap sees only the wired-AND bus. It cannot tell which master drove a transaction, and it never sees an arbitration loser. So the GUI combines two sources:

- Each master reports every transfer: address, bytes written and read **on the bus**, first written byte, result and attempts.
- The analyzer reports each bus packet.

Same signature at the same instant (after mapping the master's µs clock onto the analyzer clock) means same transaction. Methods: **telemetry** (matched), **owner** (configured: only Master 1 drives the OLED) and **demo**. Otherwise the packet is **unattributed**: the GUI never guesses which master drove it.

The analyzer firmware adds two diagnostics to segments:

- `mid_byte_end`: a START or STOP inside a byte. Causes: an abort, a recovery STOP, or a master starting on a busy bus.
- `bus_free_ns`: the measured idle time since the previous STOP. The GUI flags it when it is shorter than UM10204 tBUF (4.7 µs at 100 kHz).

### Verified vs not verified

- Verified on the host: the firmware's multi-master logic runs in `tests/test_controller_firmware_host.py`, including arbitration injection, another master's START, a stuck SDA line and another master clocking. Both masters and the analyzer build with the Pico SDK. Attribution, arbitration and health logic are covered in `tests/test_multi_master.py` with a simulated two-clock bench.
- **Not verified here**: the real RP2040 I2C peripheral under true simultaneous STARTs, and electrical behaviour on your breadboard. Check them on the bench with the analyzer.


## One-click connection (2026-11-14)

**Connect all** (header, row 2) opens Master 1, Master 2 and the analyzer, **masters first**, then starts the capture as soon as the analyzer handshake arrives. Pressing it again disconnects everything. Each master has its own red **NOT CONNECTED** / green `COMxx · fw 2.0` badge in row 3, and a warning banner appears if you capture with a master missing.

Ports are pre-selected by **USB serial number** (`config.py`: `ANALYZER_SERIAL`, `MASTERS[n]["serial"]`), then by the COM hint. Windows renumbers COM ports when a board is re-flashed or moved to another socket (the Pico W went COM33 → COM34), and the serial number survives that.

### Why a packet can show "Not attributed"

The packet's detail panel and the HUD now say *why*:

| Reason shown | What to do |
|---|---|
| no master COM port is connected | press **Connect all** |
| captured before the masters' telemetry started | connect the masters **before** starting the capture (Connect all does) |
| Master N is not connected | connect it |
| aligning Master N to the analyzer clock | wait a few seconds |
| no master reported this transfer | a log line was dropped, or another device drove the bus |

## Bench validation of arbitration (real boards)

Measured on the real bench with a stress build (`cmake -DMM_STRESS=ON`; **bench only, never flash for normal use**) in which both masters hammer the bus with no idle window:

- Different targets (BMP280 0x76 vs RTC 0x68): Master 1 lost arbitration thousands of times in 25 s, each correctly reported as `ARB_LOST` (never as a NACK) and retried successfully (0 gave up), while both masters kept ~340 and ~374 transfers/s. No stalls, no bus recovery.
- **Identical simultaneous transactions never arbitrate.** If both masters send the same address and data at the same instant, they merge into one bus transaction: the analyzer sees one packet while both masters report success. Normal schedules make this rare; the GUI then attributes the packet to one of them.
- **A master that boots next to a saturated bus can lose its first transfers** (idle bus, empty FIFO, no completion). The firmware now resets its controller and retries immediately instead of failing three transfers; `master_stats.last_timeout` records time, address, `IC_STATUS` and line levels of the last such event.


## Reliable master connection (2026-11-14, later)

- **Confirmed handshake.** Right after DTR the Pico's USB CDC can drop the first bytes, so a master port no longer relies on one-shot commands: `HELLO` and `TXLOG 1` are re-sent every 0.6 s until the controller's own reply arrives (`"type":"hello","role":"controller"`, `"type":"txlog","enabled":true`, or a `master_stats` line with `"txlog":true`); `SCL?` follows once HELLO is confirmed. A master that never answers within 10 s is reported in the event log and as a toast (the port stays open for older firmware), and a write that times out is kept and retried instead of dropping the link.
- **Self-healing transfer log.** If a connected master reports `"txlog":false` (for example after it rebooted) the GUI sends `TXLOG 1` again, and the master's health says "transfer log is OFF" instead of silently showing no packets.
- **Firmware (`I2C.c`): the target address is now set before the bus-idle check.** `IC_TAR` can only change while the I2C controller is disabled, and a disabled controller neither tracks the bus nor can raise `START_DET`. It used to be changed after `wait_bus_idle()`, leaving a few-microsecond window in which the other master could start unseen. **Both masters must be re-flashed to get this** (`Master1_PicoH_COM32_I2C.uf2`, `Master2_PicoW_COM34_I2C_master2.uf2`); the analyzer firmware is unchanged.

## 2D topology layout

- The Master 2 USB cable leaves the board on its connector (left) side, rises in the gap between the analyzer and Master 2, runs in a lane at least 9 px above every caption and comes down the PC's left side. It never passes behind a caption.
- Labels are placed against the real widget bounds with boards and wires as obstacles; the font is chosen per size (10 pt down to 8 pt; 7 pt only if nothing else fits). The legend wraps onto a second row, and the two HUD rows elide with "..." instead of widening the window.


## Graph stability and "Active communication" (2026-11-14, graphs)

- **Active communication** banner above the graphs (Dashboard, Digital Waveform, Performance): `LIVE · Master 1 (Pico H) → DS3231 RTC (0x68) · READ · #145`, or `Unattributed ↔ BMP280 (0x76) · WRITE` when no master telemetry matched. One elided line; the full text is the tooltip.
- **One clock for every rolling graph:** seconds relative to the latest completed capture second (`analysis/series.py`). Partial seconds and missing (NaN) values are skipped, so lines are continuous and a half-observed second never shows as a spike. Curves are redrawn only when a new second completes, and the time axis grows in 10 s steps instead of rescaling on every update.
- **Live waveform (continuous stream, firmware with `edge_stream`):** oscilloscope behaviour. Trigger **Auto** holds the last complete START-triggered sweep (no redraw, no flicker) and replaces it only with the next complete one, free-running as a rolling time base after 1.5 s without a trigger; **START** holds until the next sweep; **Address** triggers only on the selected device; **Roll** always scrolls. Idle is drawn as flat HIGH, and only a real data-loss gap is marked (GAP). Needs the rebuilt `Analyzer_PicoH_COM28_packet_analyzer.uf2` flashed (see PROTOCOL.md).
- **Network playback (3D/2D diagram):** one protocol stage (START · ADDR · R/W · DATA · ACK/NACK · STOP) is one step: **0.40 s** for a pinned packet at 1×, and following live traffic the packet takes 6 s but never less than **0.20 s per stage** (a 43-stage packet 8.6 s; it was 65 ms per stage, so every dot died after ~8 % of its wire). A packet is played to the end of its **STOP** before the next one is selected (live: the newest packet when the current one finishes, so a busy bus is *sampled*, not queued), at 0.25×/0.5×/1×/2× alike, and a GUI stall never skips a stage. Every stage sends one dot along the **whole** SDA wire in its true direction, crossing it in 80 % of the stage: START/Repeated START/STOP, ADDR and R/W master → target; DATA write master → target, DATA read target → master; ACK/NACK from whoever answers (target for address and written data, master for read data), green for ACK and red for NACK (the ✕ marks the target's tap). Dots are never cleared between stages of one packet, only on another packet or a new capture. The badge reads e.g. `0x76 · ADDR 0x76 W=0 ▶`, `DATA 0x51 ◀`, `ACK ◀`, `NACK (end) ▶` (normal end of read), `NACK (unexp.) ◀`.
- **Playback smoothness (verified in the real GUI, 2026-11-14):** the 2D diagram repainted wires, scaled board photos and ~25 captions every frame (~27 ms on a 125 % display, against the 33 ms the dot timer has), so dots ticked at ~12 fps and the whole GUI thread starved. The static scene is now two cached pixmaps (rebuilt only on a size / device-list change), the stage path is cached per stage, the badge slot is searched once per 48 px width bucket (it used to run on every stage), hidden diagram tabs are not refreshed, and a dot never moves more than 0.1 s of travel per tick (a stalled GUI makes dots lag, not teleport). Dots in flight when the next packet starts are NOT dropped (only a new capture clears them), and neither is the dot of a stage when controller telemetry attributes the packet mid-stage (the path and badge move to the master's wire, the dot finishes). Verified on live hardware (COM28/32/34, 2D renderer): 2 runs, 10 complete packets of 23/39/43 stages from both masters, every stage shown, 246 dots, all arrived, 0 cut, each packet fully played to STOP before the next. On a hardware-like feed: paint 18 → 7.9 ms, dot tick median 47 ms, dot travel 0.20–0.23 s (target 0.21 s), 4 packets × 23 stages = 92 dots all arrived, none cut. Demo mode is limited by its own simulated-edge decoding on the GUI thread (`performance_calculator.frame_timing`), not by the animation.
- **Decoder track (Digital Waveform, `Decoder track` checkbox, on by default):** a Saleae/PulseView-style row above SCL/SDA with coloured rounded boxes on the exact recorded clock timestamps: `S` / `Sr` amber, `P` gray, `ADDR 0x76 W|R` and `DATA 0xF7` **blue for a write frame, cyan for a read frame**, `ACK` green, `NACK` red (hover for the reason: normal end of read, unexpected, address not acknowledged; a data box also tells you the direction). Text shortens with the pixel width (`DATA 0xF7` → `F7` → a plain coloured bar), boxes are clipped so none can overlap, and the A/B cursor letters sit in the free band between SCL and the track. Bit digits stay between SDA and SCL. Unticking the box returns the classic text rows above SCL and the old plot height.
- **Time base follows the bus clock (`Window = Auto (SCL)`, the default):** the window is 100 / f(kHz) ms, snapped to 50, 100, 250, 500 µs or 1, 2, 5 ms: **100 kHz → 1 ms, 400 kHz → 250 µs**, so a byte is always ~125 px wide and every SCL pulse, SDA bit, address, data and ACK label stays readable. The rate is the median of the recently *measured* packets, else the SCL rate applied to the controllers, else 100 kHz, and the view re-scales by itself when you press Apply. A long packet (e.g. a 20-byte OLED write at 400 kHz) keeps this scale and shows its first window; drag to pan along the rest or press *Fit packet*. Any fixed window (50 µs … 100 ms) overrides Auto; *Zoom +/−*, wheel zoom and pan work as before.
- **Labels by pixels per byte (one style per packet):** ≥ 200 px verbose · ≥ 90 px `ADDR 0x76`, `W=0` / `R=1`, raw byte `0xEC`, `DATA 0xF7`, `ACK` / `NACK (end)` (end of read) / `NACK unexp.` · ≥ 55 px short · ≥ 26 px hex only · below that only the S/P markers. Bit digits (compact font, centred on each SCL pulse) appear once a bit is 10 px wide. Traces with more than 220 vertices are drawn as 1 px hairlines (Qt strokes wide pens at ~0.25 ms per vertex).
- **Live waveform (older segment analyzer):** the window on screen is replaced only by a newer snapshot that decoded at least one transaction, after it has been shown for 0.8 s. Idle windows and the gaps between bursts never blank it. Annotation items are reused (no delete/recreate), labels are laid out in pixels with a real collision test, and the detail thins out as you zoom out (full text, then short text, then markers only).
- **Bars** (Performance and Device Activity) are persistent items updated in place.
- **Sensor trends** use ONE source (bus-decoded history, else one master's telemetry), shown in the graph title. The source and the big values are sticky until it has been silent for 15 s, so Master 1 and Master 2 readings are never interleaved.


## Turn taking (2026-11-17, firmware 2.2-turns, default build)

`I2C.c` now builds with `SCHEDULE_TURNS 1` by default:

    Master 1: RTC 0x68 -> BMP280 0x76 -> OLED 0x3C   ·   4 s idle bus   ·   Master 2: RTC -> BMP280 -> OLED   ·   4 s idle   ·   ...

- There is no extra wire: each master watches the bus (`START_DET` + SDA/SCL levels). After its own turn it waits until it has
  seen the other master's turn followed by 4 s (`TURN_GAP_MS`) of idle bus.
- Master 1 takes the first turn after power-up (after 4.5 s of idle bus). If the other master stays silent for 13.5 s the
  turn is taken anyway and reported as `{"type":"peer_absent"}`.
- Nothing touches the bus between turns: sensor/display init, the 10 s BMP280 identity check and the `TIME` command all run
  inside the master's own turn. Both masters draw a full OLED frame titled `MASTER n TURN k`.
- Records: `{"type":"turn_start"}`, `{"type":"turn_end"}` (duration, transfers, rtc/bmp/oled result), `peer_absent`.
- The previous free-running schedule is still available: `cmake ... -DSCHEDULE_TURNS=0` (or `#define SCHEDULE_TURNS 0`).
  The `MM_STRESS` bench build keeps its hammer loop.

Images: `firmware_uf2_multimaster/Master1_PicoH_COM32_I2C_turns.uf2` and `Master2_PicoW_COM34_I2C_master2_turns.uf2`
(built in `I2C/I2C/out_turns_master1` and `out_turns_master2_pico_w`). Measured on the bench: see
`work/measurements/MM_TURNS_20261117.md` (16 alternating turns in 70 s, gaps 4.0003–4.0014 s, 0 unexpected NACKs).
Demo mode simulates this schedule (`DemoGenerator(masters=2, schedule="turns")`, labelled SIMULATED).
