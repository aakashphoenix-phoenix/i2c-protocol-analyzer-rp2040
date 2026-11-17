/*
 * i2c_proto_emit.c  -  see i2c_proto_emit.h and PROTOCOL.md
 *
 * Output goes through stdio (USB CDC on the Pico). All records that carry "seq"
 * share one counter so the PC can detect lost records.
 */
#include "i2c_proto_emit.h"

#include <stdio.h>
#include <string.h>

#ifdef PROTO_HOST_TEST            /* lets the file compile/run on a PC for testing */
#include <stdlib.h>
#define PICO_ERROR_TIMEOUT (-1)
static int getchar_timeout_us(unsigned us) { (void)us; return PICO_ERROR_TIMEOUT; }
#else
#include "pico/stdlib.h"
#endif

#ifndef PROTO_FW_NAME
#define PROTO_FW_NAME "pico-i2c-analyzer 1.0"
#endif

static uint32_t s_sample_hz;
static uint32_t s_seq;
static bool s_capturing;

static uint64_t s_t0;                 /* tick of first edge in the pending record */
static uint64_t s_prev;               /* tick of previous edge in pending record   */
static uint8_t s_state = 0xFF;        /* last emitted line state                    */
static uint32_t s_dt[PROTO_EDGES_PER_RECORD];
static uint8_t s_st[PROTO_EDGES_PER_RECORD];
static unsigned s_n;

static char s_cmd[48];
static unsigned s_cmd_len;

void proto_init(uint32_t sample_hz) {
    s_sample_hz = sample_hz;
    s_seq = 0;
    s_n = 0;
    s_capturing = false;
    proto_send_hello();
}

void proto_send_hello(void) {
    /* tick_ns is sent as an integer when exact, which it is for 10/12.5/20/25/50/100 MS/s */
    double tick_ns = 1e9 / (double)s_sample_hz;
    printf("{\"t\":\"hello\",\"proto\":%d,\"fw\":\"%s\",\"board\":\"RP2040\",\"sample_hz\":%lu,"
           "\"tick_ns\":%.4f,\"caps\":[\"edges\",\"start_stop\",\"seq\",\"stats\"]}\n",
           PROTO_VERSION, PROTO_FW_NAME, (unsigned long)s_sample_hz, tick_ns);
    fflush(stdout);
}

bool proto_capturing(void) { return s_capturing; }

/* Very small parser for {"cmd":"xxx"} lines - no heap, no JSON library needed. */
proto_cmd_t proto_poll_command(void) {
    int c;
    while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
        if (c == '\n' || c == '\r') {
            s_cmd[s_cmd_len] = 0;
            s_cmd_len = 0;
            if (strstr(s_cmd, "\"hello\"")) { proto_send_hello(); return PROTO_CMD_HELLO; }
            if (strstr(s_cmd, "\"start\"")) { s_capturing = true; s_seq = 0; s_n = 0; s_state = 0xFF; return PROTO_CMD_START; }
            if (strstr(s_cmd, "\"stop\""))  { proto_flush(); s_capturing = false; return PROTO_CMD_STOP; }
        } else if (s_cmd_len < sizeof(s_cmd) - 1) {
            s_cmd[s_cmd_len++] = (char)c;
        }
    }
    return PROTO_CMD_NONE;
}

void proto_flush(void) {
    if (s_n == 0) return;
    printf("{\"t\":\"edges\",\"seq\":%lu,\"t0\":%llu,\"e\":[", (unsigned long)s_seq++, (unsigned long long)s_t0);
    for (unsigned i = 0; i < s_n; i++) {
        printf(i ? ",[%lu,%u]" : "[%lu,%u]", (unsigned long)s_dt[i], (unsigned)s_st[i]);
    }
    printf("]}\n");
    s_n = 0;
}

static void push_edge(uint64_t tick, uint8_t st) {
    if (s_n == 0) {
        s_t0 = tick;
        s_prev = tick;
    }
    uint64_t dt = tick - s_prev;
    if (dt > 0xFFFFFFFFull) {          /* very long idle gap: start a new record */
        proto_flush();
        s_t0 = tick;
        dt = 0;
    }
    s_dt[s_n] = (uint32_t)dt;
    s_st[s_n] = st;
    s_prev = tick;
    if (++s_n == PROTO_EDGES_PER_RECORD) proto_flush();
}

void proto_capture_begin(uint64_t tick, bool sda, bool scl) {
    s_state = (uint8_t)((scl ? 2 : 0) | (sda ? 1 : 0));
    push_edge(tick, s_state);          /* the PC decoder needs the initial line state */
}

void proto_edge(uint64_t tick, bool sda, bool scl) {
    if (!s_capturing) return;
    uint8_t st = (uint8_t)((scl ? 2 : 0) | (sda ? 1 : 0));
    if (st == s_state) return;         /* only real transitions */
    s_state = st;
    push_edge(tick, st);
}

void proto_stat(uint64_t tick, unsigned buf_pct, uint32_t dropped, uint32_t overflows) {
    if (!s_capturing) return;
    proto_flush();
    printf("{\"t\":\"stat\",\"seq\":%lu,\"ts\":%llu,\"buf_pct\":%u,\"dropped\":%lu,\"overflows\":%lu}\n",
           (unsigned long)s_seq++, (unsigned long long)tick, buf_pct, (unsigned long)dropped, (unsigned long)overflows);
}

void proto_event(proto_evt_t kind, uint64_t tick, uint32_t n) {
    static const char *names[] = {"start", "stop", "overflow", "dropped", "restart"};
    if (!s_capturing) return;
    proto_flush();
    printf("{\"t\":\"evt\",\"seq\":%lu,\"ts\":%llu,\"kind\":\"%s\",\"n\":%lu}\n",
           (unsigned long)s_seq++, (unsigned long long)tick, names[kind], (unsigned long)n);
    if (kind == PROTO_EVT_OVERFLOW || kind == PROTO_EVT_DROPPED) s_state = 0xFF;  /* resend state after a gap */
}

void proto_log(const char *msg) {
    printf("{\"t\":\"log\",\"msg\":\"");
    for (const char *p = msg; *p; p++) {
        if (*p == '"' || *p == '\\') putchar('\\');
        if ((unsigned char)*p >= 0x20) putchar(*p);
    }
    printf("\"}\n");
}

#ifdef PROTO_HOST_TEST
int main(void) {
    proto_init(10000000);
    s_capturing = true;
    proto_capture_begin(1000, 1, 1);
    proto_edge(1025, 0, 1);   /* START */
    proto_edge(1075, 0, 0);
    proto_edge(1100, 1, 0);
    proto_edge(1125, 1, 1);
    proto_flush();
    proto_stat(5000, 3, 0, 0);
    proto_event(PROTO_EVT_OVERFLOW, 6000, 0);
    proto_log("host \"test\" ok");
    return 0;
}
#endif
