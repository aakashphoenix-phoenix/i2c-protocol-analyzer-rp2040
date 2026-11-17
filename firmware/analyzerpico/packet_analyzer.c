// RP2040 I2C passive analyzer prototype: 100 kHz target, PIO+DMA ping-pong.
// SDA=GP2 SCL=GP3. No GPIO ever drives the bus. Cores split capture vs USB.
// JSON Lines v1: see ../pc_tools/PROTOCOL.md.
// NOTE: 2 Msamples/sec, 0.5us timing resolution; buffer/USB losses are reported. Capture is not guaranteed lossless.
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
#define SAMPLES_PER_WORD 16u
#define BLOCK_SAMPLES (WORDS * SAMPLES_PER_WORD)
#define MAX_DATA 64
#define EVENT_QUEUE_LEN 64

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
} Record;

static queue_t output_queue;
static volatile bool requested_run = false; // GUI START command initiates capture
static volatile bool running = false;
static volatile bool paused_for_overrun = false;
static volatile uint32_t dma_lost_buffers = 0;
static volatile uint32_t dropped_records = 0;
static uint32_t emitted_seq = 0; // only core1 accesses this

static uint32_t sample_buffers[2][WORDS];
static volatile bool ready[2] = {false, false};
static volatile uint32_t generation[2] = {0,0};
static volatile uint64_t buffer_sample_base[2] = {0,0};
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

static void decode_sample(uint8_t pins, uint64_t timestamp) {
    if (!decoder.previous_valid) {
        decoder.previous_pin_state = pins;
        decoder.previous_valid = true;
        return;
    }
    uint8_t old = decoder.previous_pin_state;
    if (pins == old) return;
    decoder.previous_pin_state = pins;
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
            if (repeated) finish_segment(timestamp, 1);
            else decoder.prior_10bit_valid = false;
            new_segment(timestamp, repeated ? 1 : 0);
        } else if (!old_sda && sda) {
            // SCL may rise just to establish a STOP. Do not treat it as
            // the first bit of a nonexistent following byte.
            if (decoder.active && decoder.stage == 2 && decoder.bit_count == 1) {
                decoder.bit_count = 0;
                decoder.shifting = 0;
            }
            finish_segment(timestamp, 0);
            decoder.prior_10bit_valid = false;
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
    for (uint32_t word=0; word<WORDS; word++) {
        uint32_t w = buf[word];
        for (uint32_t j=0; j<SAMPLES_PER_WORD; j++) {
            decode_sample((uint8_t)((w >> (2*j)) & 3u),
                          base + word * SAMPLES_PER_WORD + j);
        }
    }
}

static void reset_after_capture_gap(uint64_t at_sample) {
    if (decoder.active) finish_segment(at_sample, 2);
    decoder.previous_valid = false;
    decoder.active = false;
    decoder.prior_10bit_valid = false;
    decoder.have_last_clock_rise = false;
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
    uint8_t next = done ^ 1u;
    if (ready[next] || processing_buffer == next) {
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
    memset((void *)ready, 0, sizeof(ready));
    memset(&decoder, 0, sizeof(decoder));
    next_generation = 0;
    epoch_gen = 0;
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
    ready[0] = ready[1] = false;
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
        if (ready[0] && ready[1]) b = generation[0] <= generation[1] ? 0 : 1;
        else if (ready[0]) b = 0;
        else if (ready[1]) b = 1;
        if (b >= 0) {
            uint32_t g = generation[b];
            uint64_t base = buffer_sample_base[b];
            processing_buffer = b;
            ready[b] = false;
            if (g != expected) {
                reset_after_capture_gap(base);
                emit_loss(g > expected ? g-expected : 1, base);
            }
            decode_buffer(sample_buffers[b], base);
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
           "\"capabilities\":[\"segment\",\"loss\",\"status\",\"7bit\",\"10bit\",\"ack\",\"timing\"],"
           "\"waveform\":false}\n", SAMPLE_RATE);
}
static void print_record(const Record *r) {
    if (r->type == 2) {
        printf("{\"type\":\"loss\",\"seq\":%lu,\"t_us\":%llu,\"lost_buffers\":%lu}\n",
               (unsigned long)r->sequence,
               (unsigned long long)sample_to_us(r->begin_sample),
               (unsigned long)r->missing_buffers);
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
    const char *start = r->start_kind ? "RESTART" : "START";
    const char *end = r->end_kind==0 ? "STOP" : r->end_kind==1 ? "RESTART" : "GAP";
    printf("{\"type\":\"segment\",\"seq\":%lu,"
           "\"start_us\":%llu,\"end_us\":%llu,\"duration_us\":%llu,"
           "\"start\":\"%s\",\"end\":\"%s\","
           "\"address\":%u,\"address_bits\":%u,\"address_known\":%s,"
           "\"direction\":\"%s\",\"address_ack\":%s,"
           "\"scl_hz\":%lu,\"scl_high_avg_ns\":%lu,\"scl_low_avg_ns\":%lu,"
           "\"scl_low_over_50us\":%lu,\"truncated\":%s,\"incomplete\":%s,\"data\":[",
           (unsigned long)r->sequence,
           (unsigned long long)sample_to_us(r->begin_sample),
           (unsigned long long)sample_to_us(r->end_sample),
           (unsigned long long)sample_to_us(r->end_sample-r->begin_sample),
           start,end,(unsigned)r->address,(unsigned)r->address_bits,
           r->address_known?"true":"false",r->read?"read":"write",
           r->address_ack?"true":"false",hz,high_ns,low_ns,
           (unsigned long)r->low_over_50us,
           r->truncated?"true":"false",r->incomplete?"true":"false");
    for(unsigned i=0;i<r->data_len;i++) printf("%s%u",i?",":"",(unsigned)r->data[i]);
    printf("],\"acks\":[");
    for(unsigned i=0;i<r->data_len;i++) printf("%s%s",i?",":"",r->acks[i]?"true":"false");
    printf("]}\n");
}
static void print_status(void) {
    printf("{\"type\":\"status\",\"capturing\":%s,"
           "\"dma_lost_buffers\":%lu,\"dropped_records\":%lu}\n",
           running?"true":"false",(unsigned long)dma_lost_buffers,
           (unsigned long)dropped_records);
}
static void handle_command(const char *command) {
    if (!strcmp(command,"START")) requested_run = true;
    else if (!strcmp(command,"STOP")) requested_run = false;
    else if (!strcmp(command,"HELLO")) print_hello();
    else if (!strcmp(command,"STATUS")) print_status();
    else printf("{\"type\":\"error\",\"message\":\"unknown_command\"}\n");
}
int main(void) {
    stdio_init_all();
    queue_init(&output_queue, sizeof(Record), EVENT_QUEUE_LEN);
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
        uint64_t now = time_us_64();
        if (connected && now-last_status >= 1000000) {
            print_status(); last_status = now;
        }
        sleep_ms(1);
    }
}
