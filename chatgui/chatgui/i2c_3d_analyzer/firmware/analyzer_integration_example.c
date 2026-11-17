/*
 * analyzer_integration_example.c
 *
 * HOW TO CONNECT YOUR EXISTING RP2040 ANALYZER FIRMWARE TO THE GUI.
 * This is a template, not a drop-in replacement: keep your own PIO program and
 * DMA set-up and copy the marked parts into your main loop.
 *
 * Assumed existing design (as described for this project):
 *   - PIO state machine samples GP2 (SDA) and GP3 (SCL) with "in pins, 2" at a
 *     fixed rate SAMPLE_HZ and pushes 32-bit words (16 samples x 2 bits) to a DMA
 *     ring buffer.
 *   - The CPU walks the ring and finds transitions.
 * The only change needed: report every transition with its SAMPLE INDEX as the
 * timestamp, through proto_edge(), instead of printing free text.
 */
#include "pico/stdlib.h"
#include "i2c_proto_emit.h"

#define SAMPLE_HZ 10000000u            /* must match your PIO clock divider */

/* ---- these come from YOUR existing capture code --------------------------- */
extern volatile uint32_t capture_ring[];      /* DMA ring of packed samples           */
extern uint32_t capture_ring_words;           /* ring size in 32-bit words           */
extern uint32_t dma_write_index(void);        /* word index the DMA will write next  */
extern uint32_t dma_overruns(void);           /* how many times DMA lapped the reader */
extern void capture_start(void);
extern void capture_stop(void);
/* --------------------------------------------------------------------------- */

int main(void) {
    stdio_init_all();
    while (!stdio_usb_connected()) sleep_ms(10);
    proto_init(SAMPLE_HZ);

    uint32_t rd = 0;                  /* reader position in the ring (words)    */
    uint64_t sample_index = 0;        /* absolute sample counter = timestamp     */
    uint32_t seen_overruns = 0;
    absolute_time_t next_stat = make_timeout_time_ms(500);
    bool first = true;

    for (;;) {
        switch (proto_poll_command()) {
            case PROTO_CMD_START:
                capture_start();
                rd = dma_write_index();
                seen_overruns = dma_overruns();
                first = true;
                break;
            case PROTO_CMD_STOP:
                capture_stop();
                break;
            default:
                break;
        }
        if (!proto_capturing()) { tight_loop_contents(); continue; }

        uint32_t wr = dma_write_index();
        uint32_t ov = dma_overruns();
        if (ov != seen_overruns) {                       /* reader was lapped: data lost */
            proto_event(PROTO_EVT_OVERFLOW, sample_index, ov - seen_overruns);
            seen_overruns = ov;
            rd = wr;
            first = true;
        }
        while (rd != wr) {
            uint32_t w = capture_ring[rd];
            for (int i = 0; i < 16; i++) {
                /* bit order depends on your PIO shift direction - adjust if SDA/SCL look swapped */
                uint32_t s = (w >> (i * 2)) & 3u;
                bool sda = s & 1u;                        /* GP2 */
                bool scl = (s >> 1) & 1u;                 /* GP3 */
                if (first) { proto_capture_begin(sample_index, sda, scl); first = false; }
                else proto_edge(sample_index, sda, scl);  /* ignores non-changes itself */
                sample_index++;
            }
            rd = (rd + 1) % capture_ring_words;
        }
        if (time_reached(next_stat)) {
            uint32_t used = (wr + capture_ring_words - rd) % capture_ring_words;
            proto_stat(sample_index, used * 100u / capture_ring_words, 0, seen_overruns);
            next_stat = make_timeout_time_ms(500);
        } else {
            proto_flush();
        }
    }
}
