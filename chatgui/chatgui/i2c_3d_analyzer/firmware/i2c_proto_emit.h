/*
 * i2c_proto_emit.h  -  analyzer -> PC protocol v1 (JSON Lines) for the RP2040 analyzer.
 *
 * Drop i2c_proto_emit.c/.h into the analyzer firmware project, add the .c file to
 * add_executable() in CMakeLists.txt, and enable USB stdio:
 *     pico_enable_stdio_usb(<target> 1)
 *     pico_enable_stdio_uart(<target> 0)
 *
 * The GUI needs exactly these calls from your existing capture loop:
 *     proto_init(sample_hz)                 once at boot (sends hello)
 *     proto_poll_command()                  regularly - handles hello/start/stop from the PC
 *     proto_edge(tick, sda, scl)            for EVERY change of SDA or SCL (and once at capture start)
 *     proto_flush()                         when the bus is idle / every few ms
 *     proto_stat(tick, buf_pct, dropped, overflows)   ~2x per second
 *     proto_event(PROTO_EVT_OVERFLOW, tick, 0)        whenever the DMA ring overran
 * "tick" is the sample index of your PIO/DMA sampler (sample_index x sample period = time).
 */
#ifndef I2C_PROTO_EMIT_H
#define I2C_PROTO_EMIT_H

#include <stdbool.h>
#include <stdint.h>

#define PROTO_VERSION 1
#define PROTO_EDGES_PER_RECORD 96   /* keeps one line well under 2 KB */

typedef enum {
    PROTO_CMD_NONE = 0,
    PROTO_CMD_HELLO,
    PROTO_CMD_START,
    PROTO_CMD_STOP,
} proto_cmd_t;

typedef enum {
    PROTO_EVT_START,      /* optional: firmware-detected START (cross-check only)  */
    PROTO_EVT_STOP,       /* optional: firmware-detected STOP                      */
    PROTO_EVT_OVERFLOW,   /* capture ring overran - samples lost                   */
    PROTO_EVT_DROPPED,    /* n samples dropped                                     */
    PROTO_EVT_RESTART,    /* capture restarted                                     */
} proto_evt_t;

void proto_init(uint32_t sample_hz);
void proto_send_hello(void);
proto_cmd_t proto_poll_command(void);
bool proto_capturing(void);

void proto_capture_begin(uint64_t tick, bool sda, bool scl);   /* sends initial line state */
void proto_edge(uint64_t tick, bool sda, bool scl);
void proto_flush(void);
void proto_stat(uint64_t tick, unsigned buf_pct, uint32_t dropped, uint32_t overflows);
void proto_event(proto_evt_t kind, uint64_t tick, uint32_t n);
void proto_log(const char *msg);

#endif
