// RP2040 I2C passive analyzer prototype: 100 kHz target, PIO+DMA ping-pong.
// SDA=GP2 SCL=GP3. No GPIO ever drives the bus. Cores split capture vs USB.
// JSON Lines v1: see ../pc_tools/PROTOCOL.md.
// NOTE: 2 Msamples/sec, 0.5us timing resolution; buffer/USB losses are reported. Capture is not guaranteed lossless.
//
// Continuous edge stream (2026-11-14): besides the decoded segments, Core 1 streams EVERY SDA/SCL transition
// as a run-length-encoded entry (dt, state) packed into one integer  v = dt << 2 | state  (bit0 SDA, bit1 SCL,
// dt = samples since the previous entry, first entry of a batch: dt 0 at "t0").  Idle bus time costs nothing:
// the level simply persists, and a lightweight heartbeat (an entry repeating the current level) is added after
// 100 ms without an edge so the host's time base keeps running.  Entries are batched (256) in a bounded queue to
// Core 0; if USB cannot keep up whole batches are dropped and the next batch carries "gap":true.  Segment
// decoding is unaffected.  Sent while capturing unless the host sends STREAM 0.  Snapshot windows remain the
// behaviour when streaming is off (older hosts, host unit tests).
//
// Multi-master bus (2026-11-14): two controllers share SDA/SCL.  A passive tap sees the
// wired-AND result only, so the LOSER of an arbitration is invisible: its bits matched the
// winner's until it stopped driving.  The winner's transaction is therefore decoded as one
// normal segment - correct, not a misclassification.  Two optional fields help diagnose
// multi-master traffic without inventing anything:
//   "mid_byte_end": the segment was cut by a START or STOP inside a byte (bit count != 0).
//                   Causes: a controller ABORT/timeout, a bus-recovery STOP, or a master
//                   starting while the bus was busy (collision).  Counted in status.
//   "bus_free_ns":  measured idle time from the previous STOP to this START (UM10204 tBUF:
//                   >= 4.7 us Standard-mode, >= 1.3 us Fast-mode); omitted when unknown.
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <inttypes.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "pico/multicore.h"
#include "pico/util/queue.h"
#include "hardware/pio.h"
#include "hardware/dma.h"
#include "hardware/irq.h"
#include "hardware/clocks.h"
#include "capture.pio.h"

#define SDA_PIN 2
#define SCL_PIN 3
#define SAMPLE_RATE 2000000u
#define WORDS 2048u
// DMA ring: with only two buffers a single slow block (a 400 kHz burst can hold ~13000 edges per 16.4 ms block) was an
// overrun, i.e. a lost 16 ms and a data-loss gap.  Four buffers let Core 1 fall behind by up to 3 blocks and catch up.
#define NBUF 4u
#define SAMPLES_PER_WORD 16u
#define BLOCK_SAMPLES (WORDS * SAMPLES_PER_WORD)
#define MAX_DATA 255       // 2026-11-17: the controllers' 2.6 OLED text write is 251 bytes (was 64: truncated)
#define EVENT_QUEUE_LEN 160 // absorbs a controller bus-scan burst (112 addresses); losses still counted.  160 x ~620 B
                            // records (MAX_DATA 255) = ~99 KB: 256 of them would not fit next to the stream queue
// Bounded, measured waveform windows keep USB traffic below continuous raw
// streaming bandwidth. Segment decoding continues between these windows.
#define WAVE_EDGES 768u
#define WAVE_SAMPLES (SAMPLE_RATE / 500u)       // 2 ms
#define WAVE_INTERVAL (SAMPLE_RATE / 4u)        // at most four windows/sec
typedef struct {
    uint64_t start_sample;
    uint16_t count;
    uint16_t dt[WAVE_EDGES];
    uint8_t state[WAVE_EDGES];
} Waveform;
static queue_t waveform_queue;
static Waveform wave;
static bool wave_active;
static uint64_t wave_last, wave_due;
static uint8_t wave_state;
static volatile uint32_t waveform_dropped = 0;

// ---- continuous RLE edge stream (see header comment)
#define STREAM_BATCH_EDGES 256u
#define STREAM_QUEUE_LEN 48
#define STREAM_HEARTBEAT (SAMPLE_RATE / 10u)    // 100 ms without an edge -> time marker (level unchanged)
typedef struct {
    uint64_t t0;                          // sample index of the first entry
    uint16_t count;
    uint8_t gap;                          // samples before this batch were NOT observed / were dropped
    uint32_t v[STREAM_BATCH_EDGES];       // dt << 2 | state
} StreamBatch;
static queue_t stream_queue;
static StreamBatch stream_batch;
static volatile bool stream_wanted = true;      // STREAM 0 / STREAM 1
static volatile bool stream_enabled = false;    // active for the current capture
static bool stream_have_state, stream_gap_pending;
static uint64_t stream_last;                    // sample index of the newest entry
static uint8_t stream_state;
static volatile uint32_t stream_entries = 0, stream_dropped_batches = 0;
static volatile uint32_t decode_max_us = 0;     // slowest decode_buffer() since START (budget = one block, 16384 us)

static void finish_waveform(uint64_t at) {
    if (!wave_active) return;
    if (at > wave_last && wave.count < WAVE_EDGES) {
        wave.dt[wave.count] = (uint16_t)(at - wave_last);
        wave.state[wave.count++] = wave_state;
    }
    if (!queue_try_add(&waveform_queue, &wave)) waveform_dropped++;
    wave_active = false;
}
static void observe_waveform(uint8_t pins, uint64_t at) {
    if (!wave_active) return;
    if (at >= wave.start_sample + WAVE_SAMPLES) {
        if (at == wave.start_sample + WAVE_SAMPLES) wave_state = pins;
        finish_waveform(wave.start_sample + WAVE_SAMPLES);
        return;
    }
    if (pins == wave_state) return;
    wave.dt[wave.count] = (uint16_t)(at - wave_last);
    wave.state[wave.count++] = pins;
    wave_last = at;
    wave_state = pins;
    if (wave.count == WAVE_EDGES) finish_waveform(at);
}
static void start_waveform(uint8_t pins, uint64_t at) {
    if (stream_enabled || wave_active || at < wave_due) return;   // the stream replaces the 4/s snapshot windows
    wave.start_sample = at;
    wave.count = 1;
    wave.dt[0] = 0;
    wave.state[0] = pins;
    wave_last = at;
    wave_state = pins;
    wave_active = true;
    wave_due = at + WAVE_INTERVAL;
}

// One output record per segment, possibly split by repeated START.
// start_kind: 0 START, 1 RESTART; end_kind: 0 STOP, 1 RESTART, 2 GAP.
typedef struct {
    uint8_t type; // 1=segment, 2=loss
    uint32_t sequence;
    uint64_t begin_sample, end_sample;
    uint16_t address;
    uint8_t address_bits, read, address_ack, address_known;
    uint8_t start_kind, end_kind, incomplete, truncated;
    uint8_t data_len;
    uint8_t data[MAX_DATA];
    uint8_t acks[MAX_DATA]; // 1=ACK, 0=NACK
    uint32_t period_count;
    uint64_t period_samples;
    uint64_t high_samples, low_samples;
    uint32_t high_count, low_count, low_over_50us;
    uint32_t missing_buffers;
    uint8_t mid_byte_end;     // ended by START/STOP while bits of a byte were pending
    uint8_t bus_free_known;   // previous STOP observed in the same contiguous capture
    uint64_t bus_free_samples;
} Record;

static queue_t output_queue;
static volatile bool requested_run = false; // GUI START command initiates capture
static volatile bool running = false;
static volatile bool paused_for_overrun = false;
static volatile uint32_t dma_lost_buffers = 0;
static volatile uint32_t dropped_records = 0;
static volatile uint32_t mid_byte_ends = 0;  // segments cut inside a byte (abort/recovery/collision)
static uint32_t emitted_seq = 0; // only core1 accesses this

static uint32_t sample_buffers[NBUF][WORDS];
static volatile bool ready[NBUF];
static volatile uint32_t generation[NBUF];
static volatile uint64_t buffer_sample_base[NBUF];
static volatile int processing_buffer = -1;
static volatile uint32_t next_generation = 0;
static uint32_t epoch_gen = 0;
static uint64_t epoch_sample = 0;
static uint64_t now_sample(void) { return time_us_64() * (SAMPLE_RATE / 1000000u); }
static int dma_chan;
static uint8_t writing_buffer = 0;
static PIO capture_pio = pio0;
static uint capture_sm;
static uint pio_offset;

static void enqueue_record(const Record *r) {
    if (!queue_try_add(&output_queue, r)) dropped_records++;
}
static void emit_loss(uint32_t missing, uint64_t when) {
    Record r = {0};
    r.type = 2;
    r.sequence = ++emitted_seq;
    r.begin_sample = when;
    r.missing_buffers = missing;
    enqueue_record(&r);
}

// Data for one START..STOP or START..RESTART segment.
static struct {
    bool active, previous_valid;
    uint8_t previous_pin_state;
    uint8_t bit_count, shifting;
    uint8_t stage; // 0 first address, 1 second 10bit address, 2 payload
    uint16_t prior_10bit_address;
    bool prior_10bit_valid;
    uint64_t last_clock_rise;
    bool have_last_clock_rise;
    uint64_t last_scl_rise, last_scl_fall;
    bool have_last_scl_rise, have_last_scl_fall;
    uint64_t last_stop;        // sample index of the most recent STOP (any master)
    bool have_last_stop;
    Record record;
} decoder;

static uint64_t sample_to_us(uint64_t s) {
    return (s * 1000000ull) / SAMPLE_RATE;
}
static void finish_segment(uint64_t now, uint8_t end_kind) {
    if (!decoder.active) return;
    decoder.record.end_sample = now;
    // A STOP or gap before the first full address byte is still a valid
    // *incomplete* capture record for GUI diagnostics.
    if (!decoder.record.address_bits) decoder.record.address_bits = 7;
    decoder.record.end_kind = end_kind;
    if (decoder.bit_count || decoder.stage == 1 || !decoder.record.address_known)
        decoder.record.incomplete = 1;
    decoder.record.type = 1;
    decoder.record.sequence = ++emitted_seq;
    enqueue_record(&decoder.record);
    decoder.active = false;
}
static void new_segment(uint64_t when, uint8_t start_kind) {
    memset(&decoder.record, 0, sizeof(decoder.record));
    decoder.record.begin_sample = when;
    decoder.record.start_kind = start_kind;
    decoder.stage = 0;
    decoder.bit_count = 0;
    decoder.shifting = 0;
    decoder.have_last_clock_rise = false;
    decoder.have_last_scl_rise = false;
    decoder.have_last_scl_fall = false;
    decoder.active = true;
}

static void got_byte_and_ack(uint8_t value, bool ack) {
    Record *r = &decoder.record;
    if (decoder.stage == 0) {
        // First byte after a START is the slave address + R/W bit.
        r->read = value & 1;
        r->address_ack = ack;
        if ((value & 0xF8) == 0xF0) {
            // Reserved 11110xxR 10-bit address prefix.
            r->address_bits = 10;
            uint16_t high = (uint16_t)(value & 0x06) << 7;
            if (r->read && decoder.prior_10bit_valid &&
                (decoder.prior_10bit_address & 0x300) == high) {
                // Valid 10-bit repeated-start READ header.
                r->address = decoder.prior_10bit_address;
                r->address_known = 1;
                decoder.stage = 2;
            } else {
                r->address = high;
                decoder.stage = 1;
                if (r->read) r->incomplete = 1;
            }
        } else {
            r->address_bits = 7;
            r->address = value >> 1;
            r->address_known = 1;
            decoder.stage = 2;
        }
    } else if (decoder.stage == 1) {
        r->address |= value;
        r->address_ack = (uint8_t)(r->address_ack && ack);
        r->address_known = 1;
        decoder.stage = 2;
        if (!r->read) {
            decoder.prior_10bit_address = r->address;
            decoder.prior_10bit_valid = true;
        }
    } else {
        if (r->data_len < MAX_DATA) {
            r->data[r->data_len] = value;
            r->acks[r->data_len] = ack;
            r->data_len++;
        } else {
            r->truncated = 1;
        }
    }
}

static void on_scl_rise(uint8_t sda, uint64_t timestamp) {
    if (!decoder.active) return;
    Record *r = &decoder.record;
    if (decoder.have_last_clock_rise) {
        uint64_t delta = timestamp - decoder.last_clock_rise;
        // Ignore outliers caused by idle periods and frame boundaries.
        if (delta >= 2 && delta <= (SAMPLE_RATE / 1000u)) {
            r->period_samples += delta;
            r->period_count++;
        }
    }
    decoder.last_clock_rise = timestamp;
    decoder.have_last_clock_rise = true;
    if (decoder.bit_count < 8) {
        decoder.shifting = (uint8_t)((decoder.shifting << 1) | (sda & 1));
        decoder.bit_count++;
    } else {
        // Ninth SCL pulse = ACK(0)/NACK(1).
        got_byte_and_ack(decoder.shifting, sda == 0);
        decoder.shifting = 0;
        decoder.bit_count = 0;
    }
}

// ---- continuous edge stream (Core 1): batch, flush, heartbeat, gap
static void stream_flush(void) {
    if (!stream_batch.count) return;
    stream_batch.gap = stream_gap_pending;
    if (queue_try_add(&stream_queue, &stream_batch)) stream_gap_pending = false;
    else { stream_dropped_batches++; stream_gap_pending = true; }   // never silently: the next batch says "gap"
    stream_batch.count = 0;
}
static void stream_add(uint64_t at, uint8_t pins) {
    uint32_t dt = 0;
    if (stream_batch.count == 0) stream_batch.t0 = at;
    else dt = (uint32_t)(at - stream_last);
    stream_batch.v[stream_batch.count++] = (dt << 2) | (pins & 3u);
    stream_last = at;
    stream_state = pins;
    stream_have_state = true;
    stream_entries++;
    if (stream_batch.count == STREAM_BATCH_EDGES) stream_flush();
}
static void stream_edge(uint8_t pins, uint64_t at) { if (stream_enabled) stream_add(at, pins); }
// End of one DMA block (16.4 ms): bound the latency and keep time moving while the bus is idle.
static void stream_block_end(uint64_t at) {
    if (!stream_enabled) return;
    if (stream_have_state && at - stream_last >= STREAM_HEARTBEAT) stream_add(at, stream_state);
    stream_flush();
}
// Samples were not observed (DMA overrun, capture stop): the level is unknown until the next sample.
static void stream_gap(void) {
    if (!stream_enabled) return;
    stream_flush();
    stream_gap_pending = true;
    stream_have_state = false;
}
static void stream_reset(void) {            // new capture: fresh time base, no gap to report
    stream_batch.count = 0;
    stream_have_state = false;
    stream_gap_pending = false;
    stream_last = 0;
    stream_enabled = stream_wanted;
}

static void decode_sample(uint8_t pins, uint64_t timestamp) {
    if (!decoder.previous_valid) {
        decoder.previous_pin_state = pins;
        decoder.previous_valid = true;
        stream_edge(pins, timestamp);      // the first sample defines the level
        return;
    }
    uint8_t old = decoder.previous_pin_state;
    if (pins == old) return;
    decoder.previous_pin_state = pins;
    observe_waveform(pins, timestamp);
    stream_edge(pins, timestamp);
    bool old_sda = old & 1, old_scl = old & 2;
    bool sda = pins & 1, scl = pins & 2;

    // Valid I2C START/STOP requires SDA changing while SCL remains HIGH.
    if (old_scl && scl && (old_sda != sda)) {
        if (old_sda && !sda) {
            bool repeated = decoder.active;
            // SCL may rise for RESTART setup after the last ACK, while SDA
            // is already HIGH. That rise is not a new data clock.
            if (repeated && decoder.stage == 2 && decoder.bit_count == 1) {
                decoder.bit_count = 0;
                decoder.shifting = 0;
            }
            if (repeated && decoder.bit_count) {
                // Not a valid repeated START position: a byte was being clocked.
                decoder.record.mid_byte_end = 1;
                mid_byte_ends++;
            }
            if (repeated) finish_segment(timestamp, 1);
            else decoder.prior_10bit_valid = false;
            new_segment(timestamp, repeated ? 1 : 0);
            if (!repeated && decoder.have_last_stop && timestamp >= decoder.last_stop) {
                decoder.record.bus_free_known = 1;
                decoder.record.bus_free_samples = timestamp - decoder.last_stop;
            }
            // Previous level is known from the contiguous sampled stream.
            start_waveform(old, timestamp - 1);
            observe_waveform(pins, timestamp);
        } else if (!old_sda && sda) {
            // SCL may rise just to establish a STOP. Do not treat it as
            // the first bit of a nonexistent following byte.
            if (decoder.active && decoder.stage == 2 && decoder.bit_count == 1) {
                decoder.bit_count = 0;
                decoder.shifting = 0;
            }
            if (decoder.active && decoder.bit_count) {
                decoder.record.mid_byte_end = 1;
                mid_byte_ends++;
            }
            finish_segment(timestamp, 0);
            decoder.prior_10bit_valid = false;
            decoder.last_stop = timestamp;
            decoder.have_last_stop = true;
        }
    }
    if (decoder.active && old_scl && !scl) {
        if (decoder.have_last_scl_rise) {
            uint64_t d = timestamp - decoder.last_scl_rise;
            if (d > 0 && d <= SAMPLE_RATE/1000u) {
                decoder.record.high_samples += d;
                decoder.record.high_count++;
            }
        }
        decoder.last_scl_fall = timestamp;
        decoder.have_last_scl_fall = true;
    }
    if (!old_scl && scl) {
        if (decoder.active && decoder.have_last_scl_fall) {
            uint64_t d = timestamp - decoder.last_scl_fall;
            if (d > 0 && d <= SAMPLE_RATE/1000u) {
                decoder.record.low_samples += d;
                decoder.record.low_count++;
                if (d > SAMPLE_RATE/20000u) decoder.record.low_over_50us++;
            }
        }
        decoder.last_scl_rise = timestamp;
        decoder.have_last_scl_rise = true;
        on_scl_rise((uint8_t)sda, timestamp);
    }
}

// Right-shift PIO IN autopush means the earliest two-bit sample lies in bits 1:0
// of each completed 32-bit RX word (16 samples in chronological order).
static void decode_buffer(const uint32_t *buf, uint64_t base) {
    // When no START-triggered window has arrived for >1 s, report an actual
    // raw window anyway. Constant levels here are sampled idle/stuck levels,
    // never invented activity or an inference from absent packets.
    if (!wave_active && (wave_due == 0 || base >= wave_due + SAMPLE_RATE))
        start_waveform((uint8_t)(buf[0] & 3u), base);
    for (uint32_t word=0; word<WORDS; word++) {
        uint32_t w = buf[word];
        // Each two-bit field compares this sample with its predecessor;
        // bit zero compares against the final observed state of the last word.
        // Coalesce each nonzero field into its low bit, then visit only those
        // actual sample positions. This preserves every transition and tick.
        uint32_t diff = w ^ ((w << 2) | decoder.previous_pin_state);
        uint32_t changed = (diff | (diff >> 1)) & 0x55555555u;
        if (!decoder.previous_valid) changed |= 1u;
        while (changed) {
            uint32_t shift = (uint32_t)__builtin_ctz(changed);
            decode_sample((uint8_t)((w >> shift) & 3u),
                          base + word * SAMPLES_PER_WORD + shift / 2u);
            changed &= changed - 1u;
        }
    }
    // Close a window even if the bus was idle for the remainder of this block.
    observe_waveform(decoder.previous_pin_state, base + BLOCK_SAMPLES - 1);
    stream_block_end(base + BLOCK_SAMPLES - 1);
}

static void reset_after_capture_gap(uint64_t at_sample) {
    wave_active = false; // never extend a waveform across missing samples
    stream_gap();
    if (decoder.active) finish_segment(at_sample, 2);
    decoder.previous_valid = false;
    decoder.active = false;
    decoder.prior_10bit_valid = false;
    decoder.have_last_clock_rise = false;
    decoder.have_last_stop = false;   // idle time across a gap was not observed
}

// IRQ is installed on CORE 1; quickly start DMA into the other RAM buffer.
static void __isr dma_handler(void) {
    dma_hw->ints0 = 1u << dma_chan;
    if (!running) return;
    uint8_t done = writing_buffer;
    uint32_t gen = next_generation++;
    generation[done] = gen;
    buffer_sample_base[done] = epoch_sample + (uint64_t)(gen - epoch_gen) * BLOCK_SAMPLES;
    ready[done] = true;
    uint8_t next = (uint8_t)((done + 1u) % NBUF);
    if (ready[next] || processing_buffer == (int)next) {
        // Stop rather than silently overwrite samples the decoder has not
        // finished using. The core-1 loop drains both buffers, emits a loss
        // event, then reinitializes PIO/DMA and resumes sampling.
        pio_sm_set_enabled(capture_pio, capture_sm, false);
        dma_lost_buffers++;
        paused_for_overrun = true;
        running = false;
        return;
    }
    writing_buffer = next;
    dma_channel_set_write_addr(dma_chan, sample_buffers[next], false);
    dma_channel_set_trans_count(dma_chan, WORDS, true);
}

static void hardware_init(void) {
    // High-impedance PIO inputs; no internal pullups on analyzer.
    pio_offset = pio_add_program(capture_pio, &i2c_capture_program);
    capture_sm = pio_claim_unused_sm(capture_pio, true);
    pio_gpio_init(capture_pio, SDA_PIN);
    pio_gpio_init(capture_pio, SCL_PIN);
    gpio_disable_pulls(SDA_PIN);
    gpio_disable_pulls(SCL_PIN);
    pio_sm_config cfg = i2c_capture_program_get_default_config(pio_offset);
    sm_config_set_in_pins(&cfg, SDA_PIN);
    sm_config_set_in_shift(&cfg, true, true, 32);
    sm_config_set_fifo_join(&cfg, PIO_FIFO_JOIN_RX);
    sm_config_set_clkdiv(&cfg, (float)clock_get_hz(clk_sys) / (2.0f * SAMPLE_RATE));
    pio_sm_init(capture_pio, capture_sm, pio_offset, &cfg);
    pio_sm_set_consecutive_pindirs(capture_pio, capture_sm, SDA_PIN, 2, false);

    dma_chan = dma_claim_unused_channel(true);
    dma_channel_config dc = dma_channel_get_default_config(dma_chan);
    channel_config_set_transfer_data_size(&dc, DMA_SIZE_32);
    channel_config_set_read_increment(&dc, false);
    channel_config_set_write_increment(&dc, true);
    channel_config_set_dreq(&dc, pio_get_dreq(capture_pio, capture_sm, false));
    dma_channel_configure(dma_chan, &dc,
                          sample_buffers[0], &capture_pio->rxf[capture_sm], WORDS, false);
    dma_channel_set_irq0_enabled(dma_chan, true);
    irq_set_exclusive_handler(DMA_IRQ_0, dma_handler);
    irq_set_enabled(DMA_IRQ_0, true);
}

static void begin_capture(void) {
    wave_active = false;
    wave_due = 0;
    stream_reset();
    memset((void *)ready, 0, sizeof(ready));
    memset(&decoder, 0, sizeof(decoder));
    next_generation = 0;
    epoch_gen = 0;
    decode_max_us = 0;
    epoch_sample = now_sample();
    writing_buffer = 0;
    processing_buffer = -1;
    paused_for_overrun = false;
    dma_hw->ints0 = 1u << dma_chan;
    pio_sm_set_enabled(capture_pio, capture_sm, false);
    pio_sm_clear_fifos(capture_pio, capture_sm);
    pio_sm_restart(capture_pio, capture_sm);
    dma_channel_set_write_addr(dma_chan, sample_buffers[0], false);
    dma_channel_set_trans_count(dma_chan, WORDS, true);
    running = true;
    pio_sm_set_enabled(capture_pio, capture_sm, true);
}
static void end_capture(void) {
    // Stop PIO before aborting DMA (capture is not continuous across STOP).
    pio_sm_set_enabled(capture_pio, capture_sm, false);
    running = false;
    paused_for_overrun = false;
    dma_channel_abort(dma_chan);
    for (unsigned u = 0; u < NBUF; u++) ready[u] = false;
    reset_after_capture_gap(now_sample());
}

static void core1_entry(void) {
    hardware_init();
    uint32_t expected = 0;
    while (true) {
        if (!requested_run && (running || paused_for_overrun)) end_capture();
        if (requested_run && !running && !paused_for_overrun) {
            begin_capture();
            expected = 0;
        }
        if (!running && !paused_for_overrun) {
            tight_loop_contents();
            continue;
        }
        // Consume oldest completed buffer first. DMA will never be armed to
        // overwrite a ready or currently-processing buffer.
        int b = -1;
        for (unsigned u = 0; u < NBUF; u++)
            if (ready[u] && (b < 0 || generation[u] < generation[b])) b = (int)u;
        if (b >= 0) {
            uint32_t g = generation[b];
            uint64_t base = buffer_sample_base[b];
            processing_buffer = b;
            ready[b] = false;
            if (g != expected) {
                reset_after_capture_gap(base);
                emit_loss(g > expected ? g-expected : 1, base);
            }
            uint64_t t_start = time_us_64();
            decode_buffer(sample_buffers[b], base);
            uint32_t spent = (uint32_t)(time_us_64() - t_start);
            if (spent > decode_max_us) decode_max_us = spent;
            processing_buffer = -1;
            expected = g + 1;
            continue;
        }
        if (paused_for_overrun) {
            // Both completed buffers have been decoded. Resume from a new
            // hardware time origin; the elapsed unobserved interval is *not*
            // silently counted as idle bus time.
            uint64_t t = now_sample();
            reset_after_capture_gap(t);
            emit_loss(1, t);
            pio_sm_set_enabled(capture_pio, capture_sm, false);
            pio_sm_clear_fifos(capture_pio, capture_sm);
            pio_sm_restart(capture_pio, capture_sm);
            writing_buffer = 0;
            epoch_gen = next_generation;
            epoch_sample = now_sample();
            dma_channel_set_write_addr(dma_chan, sample_buffers[0], false);
            dma_channel_set_trans_count(dma_chan, WORDS, true);
            paused_for_overrun = false;
            running = true;
            pio_sm_set_enabled(capture_pio, capture_sm, true);
        } else {
            tight_loop_contents();
        }
    }
}

static void print_hello(void) {
    printf("{\"type\":\"hello\",\"protocol\":1,\"role\":\"analyzer\","
           "\"board\":\"RP2040\",\"sda_pin\":2,\"scl_pin\":3,"
           "\"sample_rate\":%u,\"sample_resolution_us\":0.5,"
           "\"capabilities\":[\"segment\",\"loss\",\"status\",\"7bit\",\"10bit\",\"ack\",\"timing\",\"edges\",\"sample_ts\","
           "\"mid_byte_end\",\"bus_free\",\"edge_stream\",\"compact_segment\"],"
           "\"waveform\":true,\"waveform_window_us\":2000,\"waveform_interval_us\":250000}\n", SAMPLE_RATE);
}
// Every record is rendered into one buffer and written with ONE fputs.  The previous per-field printf calls
// (about 20 per segment plus 3 per data byte) each became a separate USB CDC write + flush, which kept Core 0
// busy enough that the edge stream could only leave at ~48 batches/s and the rest was dropped.
static void print_record(const Record *r) {
    static char out[2816];                        // longest segment: 255 data bytes ~= 1.7 KB
    char *p = out, *const end = out + sizeof out - 2;
#define EMIT(...) do { int n_ = snprintf(p, (size_t)(end - p), __VA_ARGS__); if (n_ > 0) p += (n_ < end - p ? n_ : end - p); } while (0)
    if (r->type == 2) {
        EMIT("{\"type\":\"loss\",\"seq\":%lu,\"t_us\":%llu,\"t_sample\":%llu,\"lost_buffers\":%lu}\n",
             (unsigned long)r->sequence,
             (unsigned long long)sample_to_us(r->begin_sample),
             (unsigned long long)r->begin_sample,
             (unsigned long)r->missing_buffers);
        fputs(out, stdout);
        return;
    }
    unsigned long hz = r->period_samples && r->period_count
        ? (unsigned long)(((uint64_t)SAMPLE_RATE * r->period_count) / r->period_samples)
        : 0u;
    unsigned long high_ns = r->high_count
        ? (unsigned long)((r->high_samples * 1000000000ull) /
                          ((uint64_t)SAMPLE_RATE * r->high_count)) : 0u;
    unsigned long low_ns = r->low_count
        ? (unsigned long)((r->low_samples * 1000000000ull) /
                          ((uint64_t)SAMPLE_RATE * r->low_count)) : 0u;
    // Compact form ("compact_segment" capability): fields that are derivable (start_us/end_us/duration_us from the
    // sample indices at 2 MS/s) or at their default (START, STOP, 7-bit, address known, write, address ACK, flags false)
    // are omitted and the host parser restores them; acks is one '1'/'0' character per byte instead of true/false.
    // A busy two-master bench at 400 kHz emits ~90 segments/s: at ~530 bytes each they filled the USB link (~79 KB/s)
    // and the edge stream was dropped.  This form is about half the size.
    EMIT("{\"type\":\"segment\",\"seq\":%lu,\"start_sample\":%llu,\"end_sample\":%llu,\"address\":%u",
         (unsigned long)r->sequence,
         (unsigned long long)r->begin_sample,(unsigned long long)r->end_sample,
         (unsigned)r->address);
    if (r->start_kind) EMIT(",\"start\":\"RESTART\"");
    if (r->end_kind == 1) EMIT(",\"end\":\"RESTART\"");
    else if (r->end_kind != 0) EMIT(",\"end\":\"GAP\"");
    if (r->address_bits != 7) EMIT(",\"address_bits\":%u", (unsigned)r->address_bits);
    if (!r->address_known) EMIT(",\"address_known\":false");
    if (r->read) EMIT(",\"direction\":\"read\"");
    if (!r->address_ack) EMIT(",\"address_ack\":false");
    EMIT(",\"scl_hz\":%lu,\"scl_high_avg_ns\":%lu,\"scl_low_avg_ns\":%lu", hz, high_ns, low_ns);
    if (r->low_over_50us) EMIT(",\"scl_low_over_50us\":%lu", (unsigned long)r->low_over_50us);
    if (r->truncated) EMIT(",\"truncated\":true");
    if (r->incomplete) EMIT(",\"incomplete\":true");
    EMIT(",\"data\":[");
    for(unsigned i=0;i<r->data_len;i++) EMIT("%s%u",i?",":"",(unsigned)r->data[i]);
    EMIT("],\"acks\":\"");
    for(unsigned i=0;i<r->data_len;i++) EMIT("%c",r->acks[i]?'1':'0');
    EMIT("\"");
    if (r->mid_byte_end) EMIT(",\"mid_byte_end\":true");
    if (r->bus_free_known)
        EMIT(",\"bus_free_ns\":%llu",
             (unsigned long long)((r->bus_free_samples * 1000000000ull) / SAMPLE_RATE));
    EMIT("}\n");
#undef EMIT
    *p = 0;
    fputs(out, stdout);
}
static void print_waveform(const Waveform *w) {
    // Existing edge schema; snapshot=true marks a complete isolated window.
    // No seq: the existing segment/loss sequence remains unchanged.
    printf("{\"t\":\"edges\",\"snapshot\":true,\"t0\":%llu,\"e\":[",
           (unsigned long long)w->start_sample);
    for (unsigned i=0; i<w->count; i++)
        printf("%s[%u,%u]", i?",":"", (unsigned)w->dt[i], (unsigned)w->state[i]);
    printf("]}\n");
}
// One stream batch as a single line:
//   {"t":"edges","stream":true,"t0":N,"s":S,["gap":true,]"r":"<text>"}
// The first entry is (t0, level S).  Every later entry is ONE character (two when the gap is long) because a
// typical I2C edge is 5-12 ticks after the previous one: the number v is written in base 45, least significant
// digit first, from 90 printable symbols (no '"' and no '\'); a symbol >= 45 means "more digits follow".
//   v = 2*dt + (SCL toggled ? 1 : 0)            exactly one line changed (SDA = 0, SCL = 1)
//   '~' then v = 2*dt + (nothing changed ? 1 : 0)   both lines changed together (0) / heartbeat, level unchanged (1)
// ~1.1 bytes per edge instead of ~3 as decimal text: the USB CDC link carries ~64 KB/s through stdio, and the bus
// produces 20k+ edges/s on a busy two-master bench, so the old format dropped about half of the batches.
static char *put_sym(char *p, unsigned k) {
    unsigned c = 35u + k;                 // '#' .. '}' skipping the backslash
    if (c >= 92u) c++;
    *p++ = (char)c;
    return p;
}
static char *put_num(char *p, uint32_t v) {
    while (v >= 45u) { p = put_sym(p, 45u + v % 45u); v /= 45u; }
    return put_sym(p, v);
}
static void print_stream(const StreamBatch *b) {
    static char line[STREAM_BATCH_EDGES * 9u + 160u];
    int n = snprintf(line, sizeof line, "{\"t\":\"edges\",\"stream\":true,\"t0\":%llu,\"s\":%u,%s\"r\":\"",
                     (unsigned long long)b->t0, (unsigned)(b->v[0] & 3u), b->gap ? "\"gap\":true," : "");
    char *p = line + n;
    for (unsigned i = 1; i < b->count; i++) {
        uint32_t dt = b->v[i] >> 2;
        unsigned x = (b->v[i] ^ b->v[i - 1]) & 3u;            // lines that changed since the previous entry
        if (x == 1u || x == 2u) {
            p = put_num(p, dt * 2u + (x == 2u ? 1u : 0u));
        } else {
            *p++ = '~';
            p = put_num(p, dt * 2u + (x == 0u ? 1u : 0u));
        }
    }
    *p++ = '"'; *p++ = '}'; *p++ = '\n'; *p = 0;
    fputs(line, stdout);
}
static void print_status(void) {
    printf("{\"type\":\"status\",\"capturing\":%s,"
           "\"dma_lost_buffers\":%lu,\"dropped_records\":%lu,\"waveform_dropped\":%lu,\"mid_byte_ends\":%lu,"
           "\"stream\":%s,\"stream_entries\":%lu,\"stream_dropped\":%lu,\"decode_max_us\":%lu}\n",
           running?"true":"false",(unsigned long)dma_lost_buffers,
           (unsigned long)dropped_records, (unsigned long)waveform_dropped, (unsigned long)mid_byte_ends,
           stream_enabled?"true":"false",(unsigned long)stream_entries,(unsigned long)stream_dropped_batches,(unsigned long)decode_max_us);
}
static void handle_command(const char *command) {
    if (!strcmp(command,"START")) requested_run = true;
    else if (!strcmp(command,"STOP")) requested_run = false;
    else if (!strcmp(command,"HELLO")) print_hello();
    else if (!strcmp(command,"STATUS")) print_status();
    else if (!strcmp(command,"STREAM 1")) stream_wanted = true;     // takes effect at the next START
    else if (!strcmp(command,"STREAM 0")) stream_wanted = false;
    else printf("{\"type\":\"error\",\"message\":\"unknown_command\"}\n");
}
int main(void) {
    stdio_init_all();
    queue_init(&output_queue, sizeof(Record), EVENT_QUEUE_LEN);
    queue_init(&waveform_queue, sizeof(Waveform), 2);
    queue_init(&stream_queue, sizeof(StreamBatch), STREAM_QUEUE_LEN);
    multicore_launch_core1(core1_entry);
    sleep_ms(2000);
    bool last_connected = false;
    uint64_t last_status = 0;
    char command[24]; unsigned ci = 0;
    while (true) {
        bool connected = stdio_usb_connected();
        if (connected && !last_connected) { print_hello(); print_status(); }
        last_connected = connected;
        // Only core 0 interacts with USB; no USB I/O from capture core or IRQ.
        int ch;
        while ((ch = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
            if (ch=='\n' || ch=='\r') {
                if (ci) { command[ci]=0; handle_command(command); ci=0; }
            } else if (ch >= 32 && ch < 127 && ci < sizeof(command)-1) command[ci++]=(char)ch;
        }
        Record r;
        unsigned sent=0;
        while (sent < 10 && queue_try_remove(&output_queue, &r)) {
            sent++;
            if (connected) print_record(&r);
            else dropped_records++;
        }
        static Waveform w; // keep >2 KB snapshot off the Pico main stack
        if (queue_try_remove(&waveform_queue, &w) && connected) print_waveform(&w);
        // Edge stream: lowest priority (segments above are always printed first); a few batches per pass.
        static StreamBatch sb;                 // off the stack
        unsigned streamed = 0;
        while (streamed < 8 && queue_try_remove(&stream_queue, &sb)) {
            streamed++;
            if (connected) print_stream(&sb);
        }
        uint64_t now = time_us_64();
        if (connected && now-last_status >= 1000000) {
            print_status(); last_status = now;
        }
        if (!sent && !streamed) sleep_ms(1);   // nothing to print: yield; otherwise keep draining
    }
}
