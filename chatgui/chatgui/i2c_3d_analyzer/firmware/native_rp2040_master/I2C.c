/*
 * I2C.c - multi-master I2C controller firmware for the analyzer bench (RP2040).
 *
 * ONE source file builds BOTH controllers; MASTER_ID selects the role:
 *   MASTER_ID 1  Master 1 · Raspberry Pi Pico H   (CMake target I2C)
 *                RTC + BMP280 reads, owns the SSD1306 OLED, may set the RTC
 *   MASTER_ID 2  Master 2 · Raspberry Pi Pico W   (CMake target I2C_master2)
 *                RTC + BMP280 reads on its own, staggered schedule; never draws on the OLED, but
 *                writes one SSD1306 command transfer [0x00, 0xA6] per display cycle (OLED_CMD_WRITES)
 *
 *   Shared bus: SDA GP4, SCL GP5, i2c0 @ 100 kHz on both masters.
 *   Targets:    DS3231 RTC 0x68 · BMP280/BME280 0x76 · SSD1306 OLED 0x3C
 *
 * Multi-master rules implemented here (2026-11-14):
 *   1. Bus-idle check before every START: our own controller idle (IC_STATUS.MST_ACTIVITY),
 *      SDA and SCL HIGH for a whole idle window, and no START seen on the bus during that
 *      window (IC_RAW_INTR_STAT.START_DET is set by ANY master's START).  After our own
 *      STOP each master also waits a hold-off; because the hold-off is longer than the
 *      other master's idle window, a waiting master always gets the next gap (round robin).
 *   2. Register-level transfers.  The Pico SDK's i2c_write/read functions clear the abort
 *      reason before returning, so an arbitration loss looks exactly like a NACK.  Here
 *      IC_TX_ABRT_SOURCE is read BEFORE it is cleared, so ARB_LOST is told apart from
 *      ABRT_7B_ADDR_NOACK (address NACK) and ABRT_TXDATA_NOACK (data NACK).
 *   3. Arbitration loss -> jittered, board-specific back-off and retry.  A lost
 *      arbitration never marks a target offline and never triggers bus recovery.
 *   4. 9-clock bus recovery only when SDA or SCL has been LOW for STUCK_LOW_US (SMBus
 *      tTIMEOUT, 35 ms) with NO SCL edges, i.e. no other master is clocking.  Transfer
 *      timeouts on a bus that is not stuck only re-initialise our own controller.
 *   5. Staggered schedules: Master 2 uses different periods (330 ms / 1100 ms) and a
 *      97 ms start offset, so the two masters do not poll on the same millisecond grid.
 *   6. A combined "write register, repeated START, read" is ONE bus transaction, so the
 *      other master can never move a target's register pointer between the two halves.
 *   7. (2026-11-17, SCHEDULE_TURNS, default) strict turn taking: Master 1 RTC -> BMP280 -> OLED, 4 s idle bus,
 *      Master 2 RTC -> BMP280 -> OLED, 4 s idle bus, ...  Hand-over by watching the bus; see SCHEDULE_TURNS.
 *      Records: {"type":"turn_start"} {"type":"turn_end"} {"type":"peer_absent"}.
 *
 * USB (each master has its own COM port; JSON lines carry "master_id"):
 *   {"type":"hello","role":"controller",...}       at boot, on USB connect, on "HELLO"
 *   {"type":"master_stats",...}                      once per second
 *   {"type":"txn",...}                               every transfer, only after "TXLOG 1"
 *   {"type":"arb_lost",...} {"type":"bus_recovery",...} {"type":"controller_reset",...}
 *   {"type":"sensor_telemetry",...} {"type":"scl_config",...} {"type":"rtc_set",...}
 *   Commands: HELLO · STATS · TXLOG 0|1 · SCL? · SCL 100000|400000 · TIME YYYY-MM-DDTHH:MM:SS
 *
 * Earlier behaviour kept: BMP280 chip-ID + calibration re-read every 10 s, RTC time + status
 * in one combined read with OSF check, BMP280/BME280 IDs 0x58/0x60, absent devices
 * re-detected every 2 s, opt-in bus scan, SCL 100/400 kHz and TIME commands.
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>

#include "pico/stdlib.h"
#include "hardware/i2c.h"
#ifndef I2C_HOST_SIM
#include "hardware/gpio.h"
#include "pico/rand.h"
#include "pico/stdio_usb.h"
#endif

/* ------------------------------------------------------------------ schedule */
/* SCHEDULE_TURNS 1 (default, 2026-11-17): strict turn taking.
 *     Master 1 turn: RTC 0x68 -> BMP280 0x76 -> OLED 0x3C, one after another
 *     bus idle TURN_GAP_MS (4 s)
 *     Master 2 turn: RTC 0x68 -> BMP280 0x76 -> OLED 0x3C
 *     bus idle TURN_GAP_MS ... and so on.
 *   There is no wire between the masters: each one WATCHES the bus (START_DET + SDA/SCL levels).  After its
 *   own turn a master waits until it has seen the other master's activity followed by TURN_GAP_MS of idle bus.
 *   Master 1 owns the first turn after power-up (it waits TURN_GAP_MS + TURN_TAKEOVER_EXTRA_MS of idle, longer
 *   than a normal gap, so a Master 1 reboot never collides with a running Master 2).  If the other master stays
 *   silent for PEER_TIMEOUT_MS (3 turn cycles) the turn is taken anyway and reported as "peer_absent".
 *   Nothing touches the bus outside a master's own turn: init, identity check, re-detection, TIME and bus scan
 *   all run inside the turn.  Every multi-master safety rule below still applies to each transfer.
 * SCHEDULE_TURNS 0: the earlier free-running schedule (BMP 200/330 ms, RTC 1/1.1 s, Master 2 never draws). */
#ifndef SCHEDULE_TURNS
#define SCHEDULE_TURNS 1
#endif
#if SCHEDULE_TURNS && !defined(MM_STRESS)
#define TURNS_ACTIVE 1              /* the MM_STRESS bench build keeps its back-to-back hammer loop */
#else
#define TURNS_ACTIVE 0
#endif
#define TURN_GAP_MS 4000             /* bus idle between the end of one turn and the start of the next */
#define TURN_TAKEOVER_EXTRA_MS 500   /* first turn after boot waits TURN_GAP_MS + this */
#define TURN_MAX_MS 500              /* generous bound of one turn (RTC + BMP + a full OLED frame ~ 0.2 s) */
#define PEER_TIMEOUT_MS (3 * (TURN_GAP_MS + TURN_MAX_MS))
#define TURN_POLL_US 100             /* bus observation period while waiting for a turn */

/* ------------------------------------------------------------------ role */
#ifndef MASTER_ID
#define MASTER_ID 1
#endif
#if MASTER_ID == 1
#define MASTER_NAME "Master 1"
#define DRIVES_OLED 1
#define BMP_PERIOD_MS 200
#define RTC_PERIOD_MS 1000           /* RTC read + OLED refresh */
#define SCHEDULE_OFFSET_MS 0
#define BUS_IDLE_US 40               /* shorter idle window = this master takes a free gap first */
#define BACKOFF_BASE_US 150
#elif MASTER_ID == 2
#define MASTER_NAME "Master 2"
#if TURNS_ACTIVE
#define DRIVES_OLED 1                /* turns never overlap, so Master 2 draws its own full frame in its turn */
#else
#define DRIVES_OLED 0                /* the OLED framebuffer belongs to Master 1: interleaved frames would garble it */
#define OLED_CMD_WRITES 1            /* ...but Master 2 also WRITES to the OLED: one command transfer [0x00, 0xA6]
                                      * (normal display, already set by Master 1's init) per RTC_PERIOD_MS.  A single
                                      * command between Master 1's data chunks never moves the GDDRAM pointer, so the
                                      * picture is unaffected; the SSD1306 is write-only on I2C, so this is a WRITE. */
#endif
#define BMP_PERIOD_MS 330
#define RTC_PERIOD_MS 1100
#define SCHEDULE_OFFSET_MS 97        /* prime offset: never on Master 1's millisecond grid */
#define BUS_IDLE_US 70
#define BACKOFF_BASE_US 230
#else
#error "MASTER_ID must be 1 (Pico H) or 2 (Pico W)"
#endif
#ifndef OLED_CMD_WRITES
#define OLED_CMD_WRITES 0
#endif

#ifdef MM_STRESS
/* BENCH STRESS BUILD ONLY (cmake -DMM_STRESS=ON): no idle window, no hold-off and a
 * back-to-back read loop on BOTH masters, so both start the moment the bus frees and arbitrate
 * on every transaction.  Proves ARB_LOST detection/retry on the real RP2040; never flash it
 * for normal use. */
#undef BUS_IDLE_US
#define BUS_IDLE_US 0
#endif

#if defined(RASPBERRYPI_PICO_W)
#define BOARD_NAME "pico_w"
#elif defined(RASPBERRYPI_PICO)
#define BOARD_NAME "pico"
#else
#define BOARD_NAME "rp2040"
#endif
#if TURNS_ACTIVE
#define FW_VERSION "2.6-live"
#else
#define FW_VERSION "2.1-multimaster"
#endif

#define BUS i2c0
#define SDA_PIN 4
#define SCL_PIN 5
#define I2C_HZ 100000                /* boot default: Standard-mode */
#define RTC_ADDR 0x68
#define BMP_ADDR 0x76
#define OLED_ADDR 0x3C

#define SET_RTC_ON_BOOT 0            /* 1 = Master 1 writes the time below once at boot */
#define IDENTITY_PERIOD_MS 10000     /* BMP280 chip ID + calibration */
#define SCAN_PERIOD_MS 0             /* opt-in: e.g. 5000. Each scan NACKs every absent address */
#define REDETECT_PERIOD_MS 2000      /* retry interval for a device that failed init */
#define STATS_PERIOD_MS 1000
#define NACK_TEST_ADDR 0x00          /* e.g. 0x2A: probe an empty address every 10 s (test 5) */

/* multi-master timing */
#ifdef MM_STRESS
#define POST_TXN_HOLDOFF_US 0
#else
#define POST_TXN_HOLDOFF_US 120      /* after OUR STOP; > the other master's idle window */
#endif
#define IDLE_JITTER_US 16
#define BUS_BUSY_TIMEOUT_US 30000    /* give up one transfer if the bus never goes idle */
#define MAX_ATTEMPTS 6               /* first try + 5 retries after arbitration loss */
#define BACKOFF_JITTER_US 200
#define STUCK_LOW_US 35000           /* SMBus tTIMEOUT */
#define STUCK_CHECK_PERIOD_MS 250
#define TIMEOUTS_BEFORE_RECOVERY 3

#define OLED_WIDTH 128
#define OLED_HEIGHT 64
#if DRIVES_OLED
static uint8_t screen[OLED_WIDTH * OLED_HEIGHT / 8];
#endif

/* Transfer results.  Values are part of the host-simulation interface (tests). */
enum { XR_OK = 0, XR_ADDR_NACK = 1, XR_DATA_NACK = 2, XR_ARB_LOST = 3, XR_TIMEOUT = 4,
       XR_OTHER = 5, XR_BUS_BUSY = 6 };
static const char *const XR_NAME[] = {"ok", "addr_nack", "data_nack", "arb_lost", "timeout",
                                      "abort_other", "bus_busy"};

/* ------------------------------------------------------------------ hardware layer */
#ifdef I2C_HOST_SIM
/* Supplied by tests/test_controller_firmware_host.py (no RP2040 peripheral on the host). */
void hal_start_det_clear(void);
bool hal_start_det_seen(void);
bool hal_controller_busy(void);
int hal_transfer(uint8_t addr, const uint8_t *w, size_t wlen, uint8_t *r, size_t rlen,
                 size_t *wdone, size_t *rdone, uint32_t timeout_us, uint32_t *abort_source);
uint32_t hal_random(void);
uint32_t hal_status(void);
void hal_set_target(uint8_t addr);
#else
static inline i2c_hw_t *hw(void) { return i2c_get_hw(BUS); }

/* START_DET is raised by a START from ANY master on the bus (RP2040 datasheet, IC_RAW_INTR_STAT). */
static void hal_start_det_clear(void) { (void)hw()->clr_start_det; }
static bool hal_start_det_seen(void) { return (hw()->raw_intr_stat & I2C_IC_RAW_INTR_STAT_START_DET_BITS) != 0; }
static bool hal_controller_busy(void) { return (hw()->status & I2C_IC_STATUS_MST_ACTIVITY_BITS) != 0; }
static uint32_t hal_random(void) { return get_rand_32(); }
static uint32_t hal_status(void) { return hw()->status; }

static int classify_abort(uint32_t src) {
    if (src & I2C_IC_TX_ABRT_SOURCE_ARB_LOST_BITS) return XR_ARB_LOST;
    if (src & (I2C_IC_TX_ABRT_SOURCE_ABRT_7B_ADDR_NOACK_BITS | I2C_IC_TX_ABRT_SOURCE_ABRT_10ADDR1_NOACK_BITS |
               I2C_IC_TX_ABRT_SOURCE_ABRT_10ADDR2_NOACK_BITS)) return XR_ADDR_NACK;
    if (src & I2C_IC_TX_ABRT_SOURCE_ABRT_TXDATA_NOACK_BITS) return XR_DATA_NACK;
    if (src & I2C_IC_TX_ABRT_SOURCE_ABRT_USER_ABRT_BITS) return XR_TIMEOUT;
    return XR_OTHER;
}

/* Read the abort reason FIRST: reading IC_CLR_TX_ABRT clears IC_TX_ABRT_SOURCE too. */
static bool take_abort(uint32_t *src) {
    i2c_hw_t *h = hw();
    if (!(h->raw_intr_stat & I2C_IC_RAW_INTR_STAT_TX_ABRT_BITS)) return false;
    *src = h->tx_abrt_source;
    (void)h->clr_tx_abrt;
    return true;
}

static void wait_us_or(uint32_t limit_us, const volatile uint32_t *reg, uint32_t bits, bool want_set) {
    uint64_t t0 = time_us_64();
    while ((((*reg) & bits) != 0) != want_set && time_us_64() - t0 < limit_us) tight_loop_contents();
}

static int finish_abort(uint32_t src) {
    i2c_hw_t *h = hw();
    int res = classify_abort(src);
    if (res != XR_ARB_LOST) {
        /* NACK/user aborts end with OUR automatic STOP.  After ARB_LOST the winner owns
         * the bus and will send the STOP: do not wait for its whole transfer. */
        wait_us_or(2000, &h->raw_intr_stat, I2C_IC_RAW_INTR_STAT_STOP_DET_BITS, true);
        (void)h->clr_stop_det;
    }
    wait_us_or(2000, &h->status, I2C_IC_STATUS_MST_ACTIVITY_BITS, false);
    return res;
}

/* Timeout: ask the controller to abort (it sends STOP and flushes its FIFOs). */
static int timeout_abort(void) {
    i2c_hw_t *h = hw();
    hw_set_bits(&h->enable, I2C_IC_ENABLE_ABORT_BITS);
    wait_us_or(2000, &h->enable, I2C_IC_ENABLE_ABORT_BITS, false);
    (void)h->clr_tx_abrt;
    return XR_TIMEOUT;
}

/* IC_TAR can only change while the controller is disabled, and a disabled controller does not track the
 * bus (it also cannot raise START_DET).  So the target address is set BEFORE wait_bus_idle(): the idle
 * window is then observed by an enabled controller, and nothing happens between "bus is idle" and our
 * START.  (Doing it afterwards left a few-microsecond gap in which another master could start unseen.) */
static void hal_set_target(uint8_t addr) {
    i2c_hw_t *h = hw();
    if ((h->tar & I2C_IC_TAR_IC_TAR_BITS) != addr) {
        h->enable = 0;
        h->tar = addr;
        h->enable = 1;
    }
}

/* One bus transaction: [write wlen bytes] [repeated START, read rlen bytes] STOP. */
static int hal_transfer(uint8_t addr, const uint8_t *w, size_t wlen, uint8_t *r, size_t rlen,
                        size_t *wdone, size_t *rdone, uint32_t timeout_us, uint32_t *abort_source) {
    i2c_hw_t *h = hw();
    *wdone = *rdone = 0;
    *abort_source = 0;
    hal_set_target(addr);           /* normally a no-op: xfer() already set it BEFORE the idle check */
    (void)h->clr_tx_abrt;
    (void)h->clr_stop_det;                  /* other masters' STOPs set STOP_DET as well */
    uint64_t deadline = time_us_64() + timeout_us;
    uint32_t src;
    for (size_t i = 0; i < wlen; i++) {
        bool last = (i + 1 == wlen) && rlen == 0;
        h->data_cmd = (last ? I2C_IC_DATA_CMD_STOP_BITS : 0) | w[i];
        /* TX_EMPTY_CTRL (set by i2c_init): TX_EMPTY = FIFO empty AND this byte + ACK done */
        while (!(h->raw_intr_stat & I2C_IC_RAW_INTR_STAT_TX_EMPTY_BITS))
            if (time_us_64() > deadline) return timeout_abort();
        if (take_abort(&src)) { *abort_source = src; return finish_abort(src); }
        *wdone = i + 1;
    }
    for (size_t j = 0; j < rlen; j++) {
        while (!i2c_get_write_available(BUS))
            if (time_us_64() > deadline) return timeout_abort();
        h->data_cmd = I2C_IC_DATA_CMD_CMD_BITS |
                      ((j == 0 && wlen) ? I2C_IC_DATA_CMD_RESTART_BITS : 0) |
                      ((j + 1 == rlen) ? I2C_IC_DATA_CMD_STOP_BITS : 0);
        while (!i2c_get_read_available(BUS)) {
            if (take_abort(&src)) { *abort_source = src; return finish_abort(src); }
            if (time_us_64() > deadline) return timeout_abort();
        }
        r[j] = (uint8_t)h->data_cmd;
        *rdone = j + 1;
    }
    /* Wait for OUR STOP so the bus is really free before anyone may start again. */
    while (!(h->raw_intr_stat & I2C_IC_RAW_INTR_STAT_STOP_DET_BITS)) {
        if (take_abort(&src)) { *abort_source = src; return finish_abort(src); }
        if (time_us_64() > deadline) return timeout_abort();
    }
    (void)h->clr_stop_det;
    return XR_OK;
}
#endif

/* ------------------------------------------------------------------ statistics */
static struct {
    uint32_t txn_ok, addr_nack, data_nack, arb_lost, retries, gave_up, busy_waits, busy_timeouts,
             timeouts, other_aborts, recoveries, recovery_skipped, controller_resets;
} stats;
static uint32_t txn_seq;
/* Diagnostics of the most recent transfer timeout, reported in every master_stats line. */
static struct { uint64_t t_us; unsigned addr, wlen, rlen, status, sda, scl, attempt; } last_to;
static bool txlog;                       /* per-transfer JSON lines ("TXLOG 1" from the GUI) */
static int consecutive_timeouts;
static uint64_t last_own_end_us;

/* Outcome of the most recent logical transfer (after retries). */
static struct {
    int result;
    unsigned attempts, arb_losses;
} last;

static void print_stats(void) {
    printf("{\"type\":\"master_stats\",\"source\":\"controller\",\"master_id\":%d,\"t_us\":%llu,"
           "\"txn_ok\":%lu,\"addr_nack\":%lu,\"data_nack\":%lu,\"arb_lost\":%lu,\"retries\":%lu,"
           "\"gave_up\":%lu,\"busy_waits\":%lu,\"busy_timeouts\":%lu,\"timeouts\":%lu,\"other_aborts\":%lu,"
           "\"recoveries\":%lu,\"recovery_skipped\":%lu,\"controller_resets\":%lu,\"txlog\":%s,"
           "\"last_timeout\":{\"t_us\":%llu,\"addr\":%u,\"wlen\":%u,\"rlen\":%u,\"attempt\":%u,"
           "\"ic_status\":%u,\"sda\":%u,\"scl\":%u}}\n",
           MASTER_ID, (unsigned long long)time_us_64(),
           (unsigned long)stats.txn_ok, (unsigned long)stats.addr_nack, (unsigned long)stats.data_nack,
           (unsigned long)stats.arb_lost, (unsigned long)stats.retries, (unsigned long)stats.gave_up,
           (unsigned long)stats.busy_waits, (unsigned long)stats.busy_timeouts, (unsigned long)stats.timeouts,
           (unsigned long)stats.other_aborts, (unsigned long)stats.recoveries,
           (unsigned long)stats.recovery_skipped, (unsigned long)stats.controller_resets,
           txlog ? "true" : "false", (unsigned long long)last_to.t_us, last_to.addr, last_to.wlen, last_to.rlen,
           last_to.attempt, last_to.status, last_to.sda, last_to.scl);
}

/* ------------------------------------------------------------------ SCL setting */
/* Selectable controller SCL rates.  Limits from component documentation:
 *   DS3231  - Fast-mode I2C, 400 kHz maximum
 *   SSD1306 - I2C clock cycle >= 2.5 us, i.e. 400 kHz maximum
 *   BMP280/BME280 - up to 3.4 MHz (not the limiting part)
 *   RP2040 I2C - up to 1 MHz (Fast-mode Plus), not usable with DS3231/SSD1306
 * so Standard-mode 100 kHz and Fast-mode 400 kHz (UM10204) are offered.  With two
 * masters the bus runs at the slower master's rate during arbitration (clock sync). */
static const unsigned SCL_SUPPORTED[] = {100000, 400000};
static unsigned scl_requested = I2C_HZ;
static unsigned scl_applied;               /* rate returned by the SDK divider */

static void report_scl(const char *result, unsigned requested, const char *reason) {
    printf("{\"type\":\"scl_config\",\"source\":\"controller\",\"master_id\":%d,\"result\":\"%s\","
           "\"requested_hz\":%u,\"applied_hz\":%u,\"supported_hz\":[%u,%u],\"reason\":\"%s\"}\n",
           MASTER_ID, result, requested, scl_applied, SCL_SUPPORTED[0], SCL_SUPPORTED[1], reason);
}

/* SEQ mode (live review command "SEQ ON [gap_ms]", see poll_commands): reported by HELLO */
static bool seq_mode;
static uint32_t seq_gap_ms = 100;
/* CONDUCT mode (2026-11-17, "CONDUCT ON"): this master starts NOTHING by itself; the GUI drives every transfer with
 * "DO RTC" / "DO BME" / "DO OLED" / "PROBE <addr>" / "ARB [addr]", one at a time, so the review shows Master 1 RTC ->
 * BMP280 -> OLED, Master 2 RTC -> BMP280 -> OLED, ... with each packet played completely before the next one. */
static bool conduct_mode;
static char pending_step;                   /* 'R' RTC, 'B' BMP280, 'O' OLED, 0 none */
static uint8_t arb_target;                  /* ARB <addr>: the target of this master's arbitration transfers */
/* DO CAL (2.6): re-read the BMP280 chip ID (0xD0) and calibration (0x88..0x9F) on request, so a GUI that started
 * capturing after this master booted still sees the calibration words on the bus and decodes temperature / pressure. */
static bool pending_cal;
#define OLED_LINE_CHARS 13                  /* "P 1008.25 HPA": 13 characters x 6 columns = 78 columns */

static void print_hello(void) {
#if TURNS_ACTIVE
    printf("{\"type\":\"hello\",\"role\":\"controller\",\"source\":\"controller\",\"master_id\":%d,"
           "\"name\":\"%s\",\"board\":\"%s\",\"fw\":\"I2C multi-master controller\",\"fw_version\":\"%s\","
           "\"sda_pin\":%d,\"scl_pin\":%d,\"scl_hz\":%u,\"drives_oled\":%s,\"oled_cmd_writes\":false,"
           "\"schedule\":\"turns\",\"turn_gap_ms\":%d,\"turn_takeover_ms\":%d,\"peer_timeout_ms\":%d,"
           "\"turn_order\":[%d,%d,%d],\"first_turn_owner\":1,\"identity_ms\":%d,\"seq\":%s,\"seq_gap_ms\":%lu,"
           "\"conduct\":%s,\"turn_transfers\":{\"rtc\":\"W 00 + Sr R 7\",\"bmp\":\"W F7 + Sr R 6\","
           "\"oled\":\"W 80 cmds + 40 + 3 text lines (time, temperature, pressure)\",\"cal\":\"W D0 + Sr R 1, W 88 + Sr R 24\"},"
           "\"oled_layout\":{\"master1_pages\":[0,2],\"master2_pages\":[4,6],\"columns\":[0,%d]},"
           "\"review_cmds\":[\"SEQ\",\"PROBE\",\"ARB\",\"CONDUCT\",\"DO\",\"DO CAL\"],"
           "\"idle_window_us\":%d,\"holdoff_us\":%d,\"max_attempts\":%d,\"txlog\":%s}\n",
           MASTER_ID, MASTER_NAME, BOARD_NAME, FW_VERSION, SDA_PIN, SCL_PIN, scl_applied,
           DRIVES_OLED ? "true" : "false", TURN_GAP_MS, TURN_GAP_MS + TURN_TAKEOVER_EXTRA_MS, PEER_TIMEOUT_MS,
           RTC_ADDR, BMP_ADDR, OLED_ADDR, 0, seq_mode ? "true" : "false", (unsigned long)seq_gap_ms,
           conduct_mode ? "true" : "false", OLED_LINE_CHARS * 6 - 1, BUS_IDLE_US, POST_TXN_HOLDOFF_US, MAX_ATTEMPTS,
           txlog ? "true" : "false");
#else
    printf("{\"type\":\"hello\",\"role\":\"controller\",\"source\":\"controller\",\"master_id\":%d,"
           "\"name\":\"%s\",\"board\":\"%s\",\"fw\":\"I2C multi-master controller\",\"fw_version\":\"%s\","
           "\"sda_pin\":%d,\"scl_pin\":%d,\"scl_hz\":%u,\"drives_oled\":%s,\"oled_cmd_writes\":%s,"
           "\"schedule\":\"free\",\"review_cmds\":[\"PROBE\",\"ARB\"],"
           "\"periods_ms\":{\"bmp\":%d,\"rtc\":%d,\"identity\":%d},\"offset_ms\":%d,"
           "\"idle_window_us\":%d,\"holdoff_us\":%d,\"max_attempts\":%d,\"txlog\":%s}\n",
           MASTER_ID, MASTER_NAME, BOARD_NAME, FW_VERSION, SDA_PIN, SCL_PIN, scl_applied,
           DRIVES_OLED ? "true" : "false", OLED_CMD_WRITES ? "true" : "false", BMP_PERIOD_MS, RTC_PERIOD_MS,
           IDENTITY_PERIOD_MS,
           SCHEDULE_OFFSET_MS, BUS_IDLE_US, POST_TXN_HOLDOFF_US, MAX_ATTEMPTS, txlog ? "true" : "false");
#endif
}

/* ------------------------------------------------------------------ bus access */
static bool lines_high(void) { return gpio_get(SDA_PIN) && gpio_get(SCL_PIN); }

/* "ARB" command (live review): while set, every transfer starts with a synchronised START (wait_bus_sync): both
 * masters start ARB_SYNC_US after the same STOP edge, so a real wired-AND arbitration happens on the bus. */
static bool arb_test_mode;

/* ARB test: a synchronised START.  Both masters measure idle time from the SAME bus event - the end of the last
 * transfer on the bus (the waiting master sees its STOP edge on the GPIOs within ~1 us; the master that made it uses
 * its own end time) - and start exactly ARB_SYNC_US after it.  Their STARTs then coincide within a few microseconds and
 * the wired-AND bus arbitrates for real: Master 1 (0x76 -> 1110110 0) loses to Master 2 (0x68 -> 1101000 0) at bit 5
 * of the address byte.  (2026-11-17: the random 0..16 us jitter alone never produced a collision in 60 + 60 transfers:
 * the master that had just finished was always slower to restart, so the two simply alternated.)  ARB_SYNC_US is longer
 * than the software path after a transfer (report_txn), so that master is back in this loop before the deadline. */
#define ARB_SYNC_US 400
/* The RP2040 controller does not START once it has seen another master's START, so a real collision needs both STARTs
 * within well under a microsecond - finer than the 1 us timer.  Each synchronised attempt therefore collides only with
 * some probability (measured live: ~20-80 % per burst of 8, depending on the transfer types), so a conducted ARB makes
 * up to ARB_CONDUCT_MAX attempts with a 0..2 us random jitter, and the GUI sends "ARB STOP" as soon as the loser has
 * reported its loss (the winner cannot see the collision). */
#define ARB_CONDUCT_MAX 60
static int32_t arb_sweep_us;
static bool arb_stop;
static bool wait_bus_sync(void) {
    uint64_t give_up = time_us_64() + BUS_BUSY_TIMEOUT_US, ref = last_own_end_us;
    hal_start_det_clear();
    for (;;) {
        uint64_t now = time_us_64();
        if (!lines_high() || hal_controller_busy() || hal_start_det_seen()) {
            hal_start_det_clear();
            ref = now;                                   /* someone is on the bus: measure from its end */
        } else if ((int64_t)(now - ref) >= (int64_t)ARB_SYNC_US + arb_sweep_us) {
            return true;
        }
        if (now >= give_up) {
            stats.busy_timeouts++;
            return false;
        }
        busy_wait_us(1);                                 /* ~1 us polling resolution */
    }
}

/* True when the bus is free: hold-off after our own STOP elapsed, our controller idle,
 * both lines HIGH for the whole idle window and no START from anyone in that window. */
static bool wait_bus_idle(void) {
    if (arb_test_mode) return wait_bus_sync();
    uint64_t now = time_us_64(), ready = last_own_end_us + POST_TXN_HOLDOFF_US;
    if (last_own_end_us && now < ready) busy_wait_us(ready - now);
    uint64_t give_up = time_us_64() + BUS_BUSY_TIMEOUT_US;
    bool waited = false;
    for (;;) {
        uint32_t window = BUS_IDLE_US + hal_random() % (IDLE_JITTER_US + 1);
        hal_start_det_clear();
        uint64_t t0 = time_us_64();
        bool idle = !hal_controller_busy();
        while (idle && time_us_64() - t0 < window) {
            if (!lines_high()) idle = false;
            else busy_wait_us(2);
        }
        if (idle && !hal_start_det_seen()) {
            if (waited) stats.busy_waits++;
            return true;
        }
        waited = true;
        if (time_us_64() >= give_up) {
            stats.busy_timeouts++;
            return false;
        }
        busy_wait_us(5);
    }
}

static uint32_t xfer_timeout_us(size_t bytes) {
    /* 9 clocks per byte + address: ~90 us/byte at 100 kHz, generous for clock stretching, PLUS
     * 10 ms for the OTHER master's transfer: the controller holds our START until it sees that
     * master's STOP, and the longest transfer on this bench (OLED chunk / RTC burst) is ~2 ms.
     * Measured on the bench with both masters saturating the bus: a 5 ms budget timed out
     * (3 in 22k transfers) purely because we were waiting for the bus, not because it was stuck. */
    return 15000u + 400u * (uint32_t)bytes;
}

static void report_txn(uint8_t addr, int b0, size_t wbus, size_t rbus, uint64_t t_us, uint32_t dur_us) {
    if (!txlog) return;
    printf("{\"type\":\"txn\",\"source\":\"controller\",\"master_id\":%d,\"seq\":%lu,\"t_us\":%llu,"
           "\"dur_us\":%lu,\"addr\":%u,\"wlen\":%u,\"rlen\":%u,\"b0\":%d,\"result\":\"%s\","
           "\"attempts\":%u,\"arb_lost\":%u}\n",
           MASTER_ID, (unsigned long)txn_seq, (unsigned long long)t_us, (unsigned long)dur_us, addr,
           (unsigned)wbus, (unsigned)rbus, b0, XR_NAME[last.result], last.attempts, last.arb_losses);
}

static void controller_reset(const char *reason);

/* Logical transfer with multi-master handling.  wlen and/or rlen may be 0 (not both). */
static bool xfer(uint8_t addr, const uint8_t *w, size_t wlen, uint8_t *r, size_t rlen) {
    int res = XR_BUS_BUSY;
    unsigned attempts = 0, arb = 0;
    size_t wbus = 0, rbus = 0;
    uint64_t t_us = 0;
    uint32_t dur_us = 0;
    txn_seq++;
    for (unsigned attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
        attempts = attempt;
        hal_set_target(addr);               /* before the idle check, see hal_set_target() */
        if (!wait_bus_idle()) {
            res = XR_BUS_BUSY;
            break;
        }
        size_t wd = 0, rd = 0;
        uint32_t src = 0;
        t_us = time_us_64();
        res = hal_transfer(addr, w, wlen, r, rlen, &wd, &rd, xfer_timeout_us(wlen + rlen), &src);
        last_own_end_us = time_us_64();
        if (res == XR_TIMEOUT) {
            last_to.t_us = last_own_end_us; last_to.addr = addr; last_to.wlen = (unsigned)wlen;
            last_to.rlen = (unsigned)rlen; last_to.attempt = attempt; last_to.status = hal_status();
            last_to.sda = gpio_get(SDA_PIN); last_to.scl = gpio_get(SCL_PIN);
        }
        dur_us = (uint32_t)(last_own_end_us - t_us);
        /* bytes actually clocked on the bus (the NACKed data byte was clocked too) */
        wbus = res == XR_ADDR_NACK ? 0 : res == XR_DATA_NACK ? wd + 1 : wd;
        rbus = rd;
        if (res == XR_TIMEOUT && attempt < MAX_ATTEMPTS && lines_high()) {
            /* Bus idle but our transfer never completed: after i2c_init() on a bus that is already
             * busy (boot next to a saturating master) the controller can lose its first transfers.
             * Re-initialise it and retry now instead of failing 3 transfers and resetting later. */
            stats.timeouts++;
            controller_reset("transfer timeout on an idle bus: controller re-init, transfer retried");
            continue;
        }
        if (res != XR_ARB_LOST) break;
        /* Lost arbitration: another master won this START.  Not a target failure. */
        arb++;
        stats.arb_lost++;
        uint32_t backoff = BACKOFF_BASE_US * attempt + hal_random() % BACKOFF_JITTER_US;
        printf("{\"type\":\"arb_lost\",\"source\":\"controller\",\"master_id\":%d,\"t_us\":%llu,"
               "\"addr\":%u,\"attempt\":%u,\"byte\":%u,\"abort_source\":%lu,\"backoff_us\":%lu,\"retry\":%s}\n",
               MASTER_ID, (unsigned long long)t_us, addr, attempt, (unsigned)wd, (unsigned long)src,
               (unsigned long)backoff, attempt < MAX_ATTEMPTS ? "true" : "false");
        if (attempt < MAX_ATTEMPTS) busy_wait_us(backoff);
    }
    if (arb) stats.retries += attempts - 1;
    switch (res) {
    case XR_OK: stats.txn_ok++; break;
    case XR_ADDR_NACK: stats.addr_nack++; break;
    case XR_DATA_NACK: stats.data_nack++; break;
    case XR_ARB_LOST: stats.gave_up++; break;
    case XR_TIMEOUT: stats.timeouts++; break;
    case XR_OTHER: stats.other_aborts++; break;
    default: break;                       /* XR_BUS_BUSY counted in wait_bus_idle() */
    }
    if (res == XR_TIMEOUT) consecutive_timeouts++;
    else if (res != XR_BUS_BUSY && res != XR_ARB_LOST) consecutive_timeouts = 0;
    last.result = res;
    last.attempts = attempts;
    last.arb_losses = arb;
    report_txn(addr, wlen ? w[0] : -1, wbus, rbus, t_us, dur_us);
    return res == XR_OK;
}

/* The last failure came from another master (arbitration / busy bus), not from the target. */
static bool contention_only(void) { return last.result == XR_ARB_LOST || last.result == XR_BUS_BUSY; }

static bool write_reg(uint8_t addr, uint8_t reg, uint8_t value) {
    uint8_t b[2] = {reg, value};
    return xfer(addr, b, 2, NULL, 0);
}

/* write register pointer, repeated START, read -> ONE transaction (S ... Sr ... P) */
static bool read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t len) {
    return xfer(addr, &reg, 1, buf, len);
}

static void bus_init(void) {
    scl_applied = i2c_init(BUS, scl_requested);     /* recovery keeps the selected rate */
    gpio_set_function(SDA_PIN, GPIO_FUNC_I2C);
    gpio_set_function(SCL_PIN, GPIO_FUNC_I2C);
    /* module pull-ups do the job; internal ones are weak but harmless */
    gpio_pull_up(SDA_PIN);
    gpio_pull_up(SCL_PIN);
}

/* A line LOW for the whole STUCK_LOW_US window with no SCL edges = stuck.  Any SCL
 * activity means another master is clocking: that is traffic, never a stuck bus. */
static bool line_stuck_low(void) {
    if (lines_high()) return false;
    uint64_t t0 = time_us_64();
    bool sda_low = true, scl_low = true, scl_prev = gpio_get(SCL_PIN);
    unsigned edges = 0;
    while (time_us_64() - t0 < STUCK_LOW_US) {
        bool sda = gpio_get(SDA_PIN), scl = gpio_get(SCL_PIN);
        if (sda) sda_low = false;
        if (scl) scl_low = false;
        if (scl != scl_prev) {
            scl_prev = scl;
            if (++edges >= 2) return false;
        }
        if (!sda_low && !scl_low) return false;
        busy_wait_us(1);
    }
    return true;
}

static void controller_reset(const char *reason) {
    stats.controller_resets++;
    printf("{\"type\":\"controller_reset\",\"source\":\"controller\",\"master_id\":%d,\"t_us\":%llu,"
           "\"reason\":\"%s\"}\n", MASTER_ID, (unsigned long long)time_us_64(), reason);
    i2c_deinit(BUS);
    bus_init();
    consecutive_timeouts = 0;
}

/* A slave reset mid-byte can hold SDA low forever: clock it out, then send STOP. */
static void bus_recover(const char *reason) {
    stats.recoveries++;
    printf("{\"type\":\"bus_recovery\",\"source\":\"controller\",\"master_id\":%d,\"t_us\":%llu,"
           "\"reason\":\"%s\"}\n", MASTER_ID, (unsigned long long)time_us_64(), reason);
    printf("I2C bus recovery\n");
    i2c_deinit(BUS);
    gpio_init(SCL_PIN);
    gpio_init(SDA_PIN);
    gpio_set_dir(SDA_PIN, GPIO_IN);
    gpio_set_dir(SCL_PIN, GPIO_IN);
    for (int i = 0; i < 9 && !gpio_get(SDA_PIN); i++) {
        if (!gpio_get(SCL_PIN)) break;     /* someone else holds SCL: do not fight it */
        gpio_put(SCL_PIN, 0);
        gpio_set_dir(SCL_PIN, GPIO_OUT);   /* drive low (open-drain style) */
        sleep_us(5);
        gpio_set_dir(SCL_PIN, GPIO_IN);    /* release: pull-up takes it high */
        sleep_us(5);
    }
    gpio_put(SDA_PIN, 0);                  /* STOP: SDA low -> high while SCL high */
    gpio_set_dir(SDA_PIN, GPIO_OUT);
    sleep_us(5);
    gpio_set_dir(SDA_PIN, GPIO_IN);
    sleep_us(5);
    bus_init();
    consecutive_timeouts = 0;
}

static void check_bus(void) {
    if (!lines_high() && line_stuck_low()) {
        bus_recover("SDA or SCL LOW for 35 ms with no SCL edges");
    } else if (consecutive_timeouts >= TIMEOUTS_BEFORE_RECOVERY) {
        stats.recovery_skipped++;
        controller_reset("transfer timeouts but bus not stuck: controller re-init only, no clock pulses");
    }
}

/* ------------------------------------------------------------------ DS3231 */
static uint8_t bcd(uint8_t n) { return ((n / 10) << 4) | (n % 10); }
static uint8_t decimal(uint8_t n) { return (n >> 4) * 10 + (n & 15); }

static bool leap(int y) { return (y % 4 == 0 && y % 100 != 0) || y % 400 == 0; }

static bool valid_date_time(int y, int mo, int d, int h, int mi, int s) {
    static const int mdays[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (y < 2000 || y > 2099 || mo < 1 || mo > 12 || h < 0 || h > 23 || mi < 0 || mi > 59 ||
        s < 0 || s > 59 || d < 1) return false;
    return d <= mdays[mo - 1] + (mo == 2 && leap(y));
}

/* Day of week, 0 = Sunday (Sakamoto). DS3231 register 0x03 is 1..7; this firmware
 * keeps its existing convention 1 = Sunday. */
static int weekday_1_sunday(int y, int m, int d) {
    static const int t[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    if (m < 3) y -= 1;
    return (y + y / 4 - y / 100 + y / 400 + t[m - 1] + d) % 7 + 1;
}

/* Write 0x00..0x06 in 24-hour mode (century bit 0 for 2000-2099), then clear OSF. */
static bool rtc_write_time(int y, int mo, int d, int h, int mi, int s) {
    uint8_t b[8] = {0x00, bcd((uint8_t)s), bcd((uint8_t)mi), bcd((uint8_t)h),
                    (uint8_t)weekday_1_sunday(y, mo, d), bcd((uint8_t)d), bcd((uint8_t)mo),
                    bcd((uint8_t)(y % 100))};
    if (!xfer(RTC_ADDR, b, 8, NULL, 0)) return false;
    /* time is valid again: clear OSF (status bit 7), keep the other status bits */
    uint8_t status;
    if (!read_regs(RTC_ADDR, 0x0F, &status, 1)) return false;
    return write_reg(RTC_ADDR, 0x0F, status & 0x7F);
}

#if SET_RTC_ON_BOOT && MASTER_ID == 1
static bool set_rtc(void) { return rtc_write_time(2026, 10, 18, 8, 22, 0); }
#endif

#if TURNS_ACTIVE
/* TIME command received between turns: applied at the start of this master's next turn. */
static struct { bool set; int y, mo, d, h, mi, s; } pending_time;
#endif

typedef struct { int hour, minute, second, day, month, year; bool osf; } RTC_Data;

/* One combined read of registers 0x00..0x0F: time/date (0x00..0x06) and the
 * status register (0x0F) whose OSF bit says the oscillator stopped, i.e. the
 * time is not trustworthy until it is set again. */
static bool __attribute__((unused)) read_rtc(RTC_Data *t) {   /* free-running build */
    uint8_t b[16];
    if (!read_regs(RTC_ADDR, 0, b, 16)) return false;
    t->osf = (b[0x0F] & 0x80) != 0;
    t->second = decimal(b[0] & 0x7F);
    t->minute = decimal(b[1] & 0x7F);
    if (b[2] & 0x40) {
        t->hour = decimal(b[2] & 0x1F) % 12;
        if (b[2] & 0x20) t->hour += 12;
    } else {
        t->hour = decimal(b[2] & 0x3F);
    }
    t->day = decimal(b[4] & 0x3F);
    t->month = decimal(b[5] & 0x1F);
    t->year = 2000 + decimal(b[6]) + ((b[5] & 0x80) ? 100 : 0);
    int weekday = b[3] & 0x07;
    return !t->osf && t->second <= 59 && t->minute <= 59 && t->hour <= 23 &&
           weekday >= 1 && weekday <= 7 && t->day >= 1 && t->day <= 31 &&
           t->month >= 1 && t->month <= 12;
}

/* ------------------------------------------------------------------ BMP280 */
typedef struct {
    uint16_t T1, P1;
    int16_t T2, T3, P2, P3, P4, P5, P6, P7, P8, P9;
} Calibration;
static Calibration cal;

static uint16_t u16(const uint8_t *b) { return (uint16_t)b[0] | ((uint16_t)b[1] << 8); }
static int16_t s16(const uint8_t *b) { return (int16_t)u16(b); }

/* Chip ID (0xD0) + calibration (0x88..0x9F). The analyzer GUI uses exactly these
 * bytes to confirm the device and to compute temperature/pressure itself. */
static bool bmp_chip_id_ok(void) {
    uint8_t id;
    /* 0x58 = BMP280, 0x60 = BME280: identical calibration/T/P register map */
    return read_regs(BMP_ADDR, 0xD0, &id, 1) && (id == 0x58 || id == 0x60);
}

static bool bmp_calibration(void) {
    uint8_t b[24];
    if (!read_regs(BMP_ADDR, 0x88, b, 24)) return false;
    cal.T1 = u16(b);      cal.T2 = s16(b + 2);  cal.T3 = s16(b + 4);
    cal.P1 = u16(b + 6);  cal.P2 = s16(b + 8);  cal.P3 = s16(b + 10);
    cal.P4 = s16(b + 12); cal.P5 = s16(b + 14); cal.P6 = s16(b + 16);
    cal.P7 = s16(b + 18); cal.P8 = s16(b + 20); cal.P9 = s16(b + 22);
    return cal.P1 != 0;
}

static bool bmp_identity(void) { return bmp_chip_id_ok() && bmp_calibration(); }

static bool bmp_init(void) {
    if (!bmp_identity()) return false;
    /* Both masters share this sensor: configure it only if it is not configured yet
     * (normal mode, temperature x1, pressure x1, standby 1000 ms).  Rewriting ctrl_meas
     * while the other master is mid-measurement would restart its conversion. */
    uint8_t ctrl[2];
    if (!read_regs(BMP_ADDR, 0xF4, ctrl, 2)) return false;
    if (ctrl[0] == 0x27 && ctrl[1] == 0xA0) return true;
    if (!write_reg(BMP_ADDR, 0xF5, 0xA0)) return false;
    if (!write_reg(BMP_ADDR, 0xF4, 0x27)) return false;
    sleep_ms(10);
    return true;
}

static bool bmp_read(float *temp, float *press) {
    uint8_t b[6];
    if (!read_regs(BMP_ADDR, 0xF7, b, 6)) return false;
    int32_t adcP = ((int32_t)b[0] << 12) | ((int32_t)b[1] << 4) | (b[2] >> 4);
    int32_t adcT = ((int32_t)b[3] << 12) | ((int32_t)b[4] << 4) | (b[5] >> 4);
    if (adcP == 0x80000 || adcT == 0x80000) return false;

    double v1 = ((double)adcT / 16384.0 - cal.T1 / 1024.0) * cal.T2;
    double v2 = ((double)adcT / 131072.0 - cal.T1 / 8192.0);
    v2 = v2 * v2 * cal.T3;
    double fine = v1 + v2;
    double temperature = fine / 5120.0;

    v1 = fine / 2.0 - 64000.0;
    v2 = v1 * v1 * cal.P6 / 32768.0;
    v2 += v1 * cal.P5 * 2.0;
    v2 = v2 / 4.0 + cal.P4 * 65536.0;
    v1 = (cal.P3 * v1 * v1 / 524288.0 + cal.P2 * v1) / 524288.0;
    v1 = (1.0 + v1 / 32768.0) * cal.P1;
    if (v1 == 0.0) return false;
    double p = 1048576.0 - adcP;
    p = (p - v2 / 4096.0) * 6250.0 / v1;
    v1 = cal.P9 * p * p / 2147483648.0;
    v2 = p * cal.P8 / 32768.0;
    p += (v1 + v2 + cal.P7) / 16.0;

    if (temperature < -40.0 || temperature > 85.0 || p < 30000.0 || p > 110000.0) return false;
    *temp = (float)temperature;
    *press = (float)(p / 100.0);
    return true;
}

/* ------------------------------------------------------------------ OLED SSD1306 (Master 1) */
#if DRIVES_OLED
static bool oled_cmd(uint8_t command) {
    uint8_t b[2] = {0x00, command};
    return xfer(OLED_ADDR, b, 2, NULL, 0);
}

static bool oled_init(void) {
    if (!oled_cmd(0xAE)) return false;          /* display off (also a presence probe) */
    const uint8_t init[] = {
        0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40, 0x8D, 0x14,
        0x20, 0x00,                             /* horizontal addressing */
        0xA1, 0xC8, 0xDA, 0x12, 0x81, 0x7F, 0xD9, 0xF1, 0xDB, 0x30,
        0xA4, 0xA6, 0x2E, 0xAF
    };
    for (size_t i = 0; i < sizeof(init); i++)
        if (!oled_cmd(init[i])) return false;
    return true;
}

static void glyph(char c, uint8_t g[5]) {
    static const uint8_t digits[10][5] = {
        {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},{0x42,0x61,0x51,0x49,0x46},
        {0x21,0x41,0x45,0x4B,0x31},{0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},
        {0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},{0x36,0x49,0x49,0x49,0x36},
        {0x06,0x49,0x49,0x29,0x1E}
    };
    static const struct { char c; uint8_t g[5]; } letters[] = {
        {'A',{0x7E,0x11,0x11,0x11,0x7E}},{'B',{0x7F,0x49,0x49,0x49,0x36}},{'C',{0x3E,0x41,0x41,0x41,0x22}},
        {'D',{0x7F,0x41,0x41,0x22,0x1C}},{'E',{0x7F,0x49,0x49,0x49,0x41}},{'H',{0x7F,0x08,0x08,0x08,0x7F}},
        {'I',{0x41,0x41,0x7F,0x41,0x41}},{'M',{0x7F,0x02,0x0C,0x02,0x7F}},{'N',{0x7F,0x04,0x08,0x10,0x7F}},
        {'O',{0x3E,0x41,0x41,0x41,0x3E}},{'P',{0x7F,0x09,0x09,0x09,0x06}},{'R',{0x7F,0x09,0x19,0x29,0x46}},
        {'S',{0x46,0x49,0x49,0x49,0x31}},{'T',{0x01,0x01,0x7F,0x01,0x01}},{'U',{0x3F,0x40,0x40,0x40,0x3F}},
        {'X',{0x63,0x14,0x08,0x14,0x63}},{'/',{0x20,0x10,0x08,0x04,0x02}},
    };
    memset(g, 0, 5);
    if (c >= '0' && c <= '9') { memcpy(g, digits[c - '0'], 5); return; }
    if (c == ':') { g[2] = 0x36; return; }
    if (c == '.') { g[2] = 0x60; return; }
    if (c == '-') { g[2] = 0x08; return; }
    for (size_t i = 0; i < sizeof(letters) / sizeof(letters[0]); i++)
        if (letters[i].c == c) { memcpy(g, letters[i].g, 5); return; }
}

static void oled_text(int x, int y, const char *s) {
    if (y < 0 || y > 56) return;
    int page = y / 8;
    while (*s && x <= 122) {
        uint8_t g[5];
        glyph(*s++, g);
        for (int i = 0; i < 5; i++) screen[page * 128 + x + i] = g[i];
        x += 6;
    }
}

static bool oled_show(void) {
    const uint8_t cmds[] = {0x21, 0, 127, 0x22, 0, 7};
    for (int i = 0; i < 6; i++)
        if (!oled_cmd(cmds[i])) return false;
    for (int i = 0; i < 1024; i += 16) {             /* 64 writes of 0x40 + 16 data bytes */
        uint8_t b[17];
        b[0] = 0x40;
        memcpy(&b[1], &screen[i], 16);
        if (!xfer(OLED_ADDR, b, 17, NULL, 0)) return false;
    }
    return true;
}

/* Render one frame (title, RTC, BMP280) and send it: window commands + 64 data writes. */
static void oled_render(const char *title, const RTC_Data *rtc, bool rtc_ok, bool reading_ok, float temp,
                        float pressure) {
    char line[32];
    memset(screen, 0, sizeof(screen));
    oled_text(0, 0, title);
    if (rtc_ok) {
        snprintf(line, sizeof(line), "TIME %02d:%02d:%02d", rtc->hour, rtc->minute, rtc->second);
        oled_text(0, 16, line);
        snprintf(line, sizeof(line), "DATE %02d/%02d/%04d", rtc->day, rtc->month, rtc->year);
        oled_text(0, 26, line);
    } else if (rtc->osf) {
        /* DS3231 answered but its oscillator stopped (power lost, no/flat CR2032):
         * the time is invalid until set with the TIME command. Not a wiring fault. */
        oled_text(0, 16, "RTC NOT SET");
        oled_text(0, 26, "SEND TIME CMD");
    } else {
        oled_text(0, 16, "RTC ERROR");
    }
    if (reading_ok) {
        snprintf(line, sizeof(line), "TEMP %.2f C", temp);
        oled_text(0, 40, line);
        snprintf(line, sizeof(line), "PRESS %.2f HPA", pressure);
        oled_text(0, 50, line);
    } else {
        oled_text(0, 40, "BMP ERROR");
    }
}

static bool __attribute__((unused)) oled_draw(const char *title, const RTC_Data *rtc, bool rtc_ok, bool reading_ok, float temp,
                      float pressure) {
    oled_render(title, rtc, rtc_ok, reading_ok, temp, pressure);
    return oled_show();
}
#endif

/* ------------------------------------------------------------------ bus scan */
static bool present[128];

static bool reserved_addr(uint8_t a) { return (a & 0x78) == 0 || (a & 0x78) == 0x78; }

static void bus_scan(void) {
    printf("Bus scan:");
    for (uint8_t a = 0x08; a < 0x78; a++) {
        if (reserved_addr(a)) continue;
        uint8_t rx;
        bool ack = xfer(a, NULL, 0, &rx, 1);
        if (ack) printf(" 0x%02X", a);
        if (ack && !present[a]) {
            /* newly found: read a known ID register so the GUI can name it */
            uint8_t id;
            if (a == 0x76 || a == 0x77) read_regs(a, 0xD0, &id, 1);        /* BMP280/BME280/BMP180 */
            /* MPU-6050 WHO_AM_I, but never on the configured DS3231 address */
            else if ((a == 0x68 || a == 0x69) && a != RTC_ADDR) read_regs(a, 0x75, &id, 1);
        }
        if (!ack && present[a]) printf(" [0x%02X removed]", a);
        present[a] = ack;
    }
    printf("\n");
}

/* ------------------------------------------------------------------ live review commands (2026-11-17) */
/* SEQ ON [gap_ms] / SEQ OFF  spaced sequential turns: one transfer per target with gap_ms of idle bus between them
 *                            (turn-taking build only; between turns 2 x gap_ms so a waiting master never mistakes an
 *                            in-turn gap for the end of the other master's turn)
 * PROBE <hex addr>          one real write of one byte to that address (an absent address -> a real address NACK);
 *                            run at the start of this master's next turn (immediately in the free-running build)
 * ARB                       one arbitration test: back-to-back reads (Master 1 -> 0x76, Master 2 -> 0x68) with the idle
 *                            window and hold-off skipped until this master has lost arbitration once or ARB_TEST_MAX
 *                            transfers were made.  Send it to BOTH masters: their STARTs coincide on the wired-AND bus. */
#define SEQ_GAP_MIN_MS 5
#define SEQ_GAP_MAX_MS 5000
#define ARB_TEST_MAX 400
#define ARB_WINDOW_US 1500000         /* hammer this long (or until this master lost once): the two ARB commands arrive
                                       * over two USB ports a few ms apart, and a master may first finish its turn
                                       * (a normal turn ~0.2 s; in SEQ mode the ARB starts within one gap) */
static struct { bool set; uint8_t addr; } pending_probe;
static bool arb_request;

static void report_seq(void) {
    printf("{\"type\":\"seq\",\"source\":\"controller\",\"master_id\":%d,\"enabled\":%s,\"gap_ms\":%lu}\n",
           MASTER_ID, seq_mode ? "true" : "false", (unsigned long)seq_gap_ms);
}

static void run_probe(void) {
    uint8_t a = pending_probe.addr, dummy = 0;
    pending_probe.set = false;
    uint64_t t0 = time_us_64();
    bool ok = xfer(a, &dummy, 1, NULL, 0);
    printf("{\"type\":\"probe\",\"source\":\"controller\",\"master_id\":%d,\"state\":\"done\",\"addr\":%u,"
           "\"t_us\":%llu,\"result\":\"%s\"}\n", MASTER_ID, a, (unsigned long long)t0, XR_NAME[last.result]);
    (void)ok;
}

/* One arbitration transfer to `target`: the RTC / BMP280 data read of a normal turn, a harmless command to the OLED
 * (0xA6 = normal display; the SSD1306 is write-only), else a one-byte write. */
static void arb_transfer(uint8_t target) {
    uint8_t buf[8];
    if (target == RTC_ADDR) {
        (void)read_regs(RTC_ADDR, 0x00, buf, 7);
    } else if (target == BMP_ADDR) {
        (void)read_regs(BMP_ADDR, 0xF7, buf, 6);
    } else if (target == OLED_ADDR) {
        const uint8_t cmd[2] = {0x00, 0xA6};
        (void)xfer(OLED_ADDR, cmd, 2, NULL, 0);
    } else {
        uint8_t dummy = 0;
        (void)xfer(target, &dummy, 1, NULL, 0);
    }
}

static void poll_commands(void);

static void run_arb_test(void) {
    /* default (plain "ARB"): Master 1 -> BMP280 0x76, Master 2 -> RTC 0x68; "ARB <addr>" picks the target, so the
     * review can show arbitration between different slaves (the lower address wins the wired-AND bus) */
    uint8_t target = arb_target ? arb_target : (MASTER_ID == 1 ? BMP_ADDR : RTC_ADDR);
    uint32_t lost0 = stats.arb_lost, gave0 = stats.gave_up;
    uint64_t t0 = time_us_64();
    unsigned n = 0, cap = conduct_mode ? ARB_CONDUCT_MAX : ARB_TEST_MAX;
    arb_request = false;
    /* The two ARB commands arrive over two USB ports a few ms apart: with a short burst one master could finish before
     * the other starts.  So Master 1 waits 40 ms before its first transfer and Master 2 waits (up to 300 ms) until it
     * sees a transfer on the bus; from then on both start ARB_SYNC_US after the same STOP edge and collide. */
    if (MASTER_ID == 1) {
        busy_wait_us(40000);
    } else {
        uint64_t until = time_us_64() + 300000;
        hal_start_det_clear();
        while (time_us_64() < until && !hal_start_det_seen() && lines_high()) busy_wait_us(2);
    }
    arb_test_mode = true;
    arb_stop = false;
    while (n < cap && stats.arb_lost == lost0 && time_us_64() - t0 < ARB_WINDOW_US && !arb_stop) {
        arb_sweep_us = (int32_t)(hal_random() % 3);
        arb_transfer(target);
        poll_commands();                          /* "ARB STOP" from the GUI once the other master reported its loss */
        n++;
    }
    arb_test_mode = false;
    last_own_end_us = time_us_64();
    printf("{\"type\":\"arb_test\",\"source\":\"controller\",\"master_id\":%d,\"target\":%u,\"t_us\":%llu,"
           "\"transfers\":%u,\"arb_lost\":%lu,\"gave_up\":%lu,\"result\":\"%s\"}\n", MASTER_ID, target,
           (unsigned long long)t0, n, (unsigned long)(stats.arb_lost - lost0), (unsigned long)(stats.gave_up - gave0),
           stats.arb_lost != lost0 ? "lost_arbitration" : "no_loss_observed");
}

/* ------------------------------------------------------------------ USB commands */
/* Called only between bus transactions, so a rate change never splits a transfer. */
static void poll_commands(void) {
    static char line[32];
    static unsigned n;
    int c;
    while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
        if (c != '\n' && c != '\r') {
            if (n < sizeof(line) - 1) line[n++] = (char)c;
            continue;
        }
        if (!n) continue;
        line[n] = 0;
        n = 0;
        if (!strcmp(line, "SCL?")) {
            report_scl("current", scl_requested, "");
        } else if (!strcmp(line, "HELLO")) {
            print_hello();
        } else if (!strcmp(line, "STATS")) {
            print_stats();
        } else if (!strcmp(line, "TXLOG 1") || !strcmp(line, "TXLOG 0")) {
            txlog = line[6] == '1';
            printf("{\"type\":\"txlog\",\"source\":\"controller\",\"master_id\":%d,\"enabled\":%s}\n",
                   MASTER_ID, txlog ? "true" : "false");
        } else if (!strncmp(line, "TIME ", 5)) {
            int y, mo, d, h, mi, sec;
            char extra;
            bool ok = sscanf(line + 5, "%4d-%2d-%2dT%2d:%2d:%2d%c", &y, &mo, &d, &h, &mi, &sec, &extra) == 6 &&
                      valid_date_time(y, mo, d, h, mi, sec);
            if (!ok) {
                printf("{\"type\":\"rtc_set\",\"source\":\"controller\",\"master_id\":%d,\"result\":\"rejected\","
                       "\"reason\":\"expected TIME YYYY-MM-DDTHH:MM:SS, years 2000-2099\"}\n", MASTER_ID);
            } else {
#if TURNS_ACTIVE
                /* never touch the bus outside our turn: written at the start of this master's next turn */
                pending_time.set = true;
                pending_time.y = y; pending_time.mo = mo; pending_time.d = d;
                pending_time.h = h; pending_time.mi = mi; pending_time.s = sec;
                printf("{\"type\":\"rtc_set\",\"source\":\"controller\",\"master_id\":%d,\"result\":\"queued\","
                       "\"time\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"reason\":\"written in this master's next turn\"}\n",
                       MASTER_ID, y, mo, d, h, mi, sec);
#else
                bool written = rtc_write_time(y, mo, d, h, mi, sec);
                printf("{\"type\":\"rtc_set\",\"source\":\"controller\",\"master_id\":%d,\"result\":\"%s\","
                       "\"time\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"weekday_1_sunday\":%d}\n",
                       MASTER_ID, written ? "applied" : "i2c_failed", y, mo, d, h, mi, sec,
                       weekday_1_sunday(y, mo, d));
#endif
            }
        } else if (!strncmp(line, "SCL ", 4)) {
            char *end = NULL;
            unsigned long hz = strtoul(line + 4, &end, 10);
            bool ok = end && *end == 0 && (hz == SCL_SUPPORTED[0] || hz == SCL_SUPPORTED[1]);
            if (!ok) {
                report_scl("rejected", (unsigned)hz, "unsupported rate; allowed 100000 or 400000");
            } else {
                scl_requested = (unsigned)hz;
                scl_applied = i2c_set_baudrate(BUS, scl_requested);
                report_scl("applied", scl_requested, "divider setting; verify on the bus with the analyzer");
            }
        } else if (!strcmp(line, "SEQ OFF")) {
            seq_mode = false;
            report_seq();
        } else if (!strcmp(line, "SEQ ON") || !strncmp(line, "SEQ ON ", 7)) {
            unsigned long g = seq_gap_ms;
            char *end = NULL;
            if (line[6]) g = strtoul(line + 7, &end, 10);
            if ((line[6] && (!end || *end)) || g < SEQ_GAP_MIN_MS || g > SEQ_GAP_MAX_MS || !TURNS_ACTIVE) {
                printf("{\"type\":\"error\",\"source\":\"controller\",\"master_id\":%d,\"message\":\"%s\"}\n", MASTER_ID,
                       TURNS_ACTIVE ? "SEQ ON gap_ms must be 5..5000" : "SEQ needs the turn-taking build");
            } else {
                seq_mode = true;
                seq_gap_ms = (uint32_t)g;
                report_seq();
            }
        } else if (!strncmp(line, "PROBE ", 6)) {
            char *end = NULL;
            unsigned long a = strtoul(line + 6, &end, 16);
            if (!end || *end || end == line + 6 || a < 0x08 || a > 0x77) {
                printf("{\"type\":\"error\",\"source\":\"controller\",\"master_id\":%d,"
                       "\"message\":\"PROBE needs a 7-bit hex address 08..77\"}\n", MASTER_ID);
            } else {
                pending_probe.set = true;
                pending_probe.addr = (uint8_t)a;
                printf("{\"type\":\"probe\",\"source\":\"controller\",\"master_id\":%d,\"state\":\"queued\",\"addr\":%lu}\n",
                       MASTER_ID, a);
            }
        } else if (!strcmp(line, "ARB STOP")) {
            arb_stop = true;
            arb_request = false;
        } else if (!strcmp(line, "ARB") || !strncmp(line, "ARB ", 4)) {
            char *end = NULL;
            unsigned long a = line[3] ? strtoul(line + 4, &end, 16) : 0;
            if (line[3] && (!end || *end || end == line + 4 || a < 0x08 || a > 0x77)) {
                printf("{\"type\":\"error\",\"source\":\"controller\",\"master_id\":%d,"
                       "\"message\":\"ARB [addr]: optional 7-bit hex address 08..77\"}\n", MASTER_ID);
            } else {
                arb_target = (uint8_t)a;
                arb_request = true;
            }
        } else if (!strcmp(line, "CONDUCT ON") || !strcmp(line, "CONDUCT OFF")) {
            conduct_mode = TURNS_ACTIVE && line[9] == 'N';
            printf("{\"type\":\"conduct\",\"source\":\"controller\",\"master_id\":%d,\"enabled\":%s%s}\n",
                   MASTER_ID, conduct_mode ? "true" : "false",
                   TURNS_ACTIVE ? "" : ",\"reason\":\"needs the turn-taking build\"");
        } else if (!strcmp(line, "DO CAL")) {
            pending_cal = true;                                 /* CONDUCT: next step; turns: start of the next turn */
        } else if (!strcmp(line, "DO RTC") || !strcmp(line, "DO BME") || !strcmp(line, "DO OLED")) {
            if (!conduct_mode) {
                printf("{\"type\":\"error\",\"source\":\"controller\",\"master_id\":%d,"
                       "\"message\":\"DO needs CONDUCT ON\"}\n", MASTER_ID);
            } else {
                pending_step = line[3];                         /* 'R' / 'B' / 'O' */
            }
        } else {
            printf("{\"type\":\"error\",\"source\":\"controller\",\"master_id\":%d,\"message\":\"unknown_command\"}\n",
                   MASTER_ID);
        }
    }
}

#if TURNS_ACTIVE
/* ------------------------------------------------------------------ turn taking (SCHEDULE_TURNS) */
static uint32_t turn_no;
static uint64_t last_bus_activity_us;      /* newest START / LOW line seen on the bus (any master) */

/* Passive observation only: START_DET is latched by ANY master's START, a LOW line means a transfer in progress. */
static bool observe_bus(void) {
    bool act = hal_start_det_seen() || !lines_high();
    if (act) {
        last_bus_activity_us = time_us_64();
        hal_start_det_clear();
    }
    return act;
}

typedef struct {
    bool bmp_ok, oled_ok, first;
    absolute_time_t next_scan;
    float temp, pressure;
    bool reading_ok, rtc_ok;                /* the newest BMP280 / RTC results: what the OLED line shows */
    RTC_Data rtc;
    unsigned oled_chars;                    /* OLED text writes of this master since boot */
} TurnState;

static const char *result_name(bool ok) { return ok ? "ok" : XR_NAME[last.result]; }

/* Idle bus that hands the turn over: TURN_GAP_MS normally; 2 x gap in SEQ mode, longer than the gap INSIDE a SEQ turn,
 * so the waiting master never mistakes it for the end of the other master's turn.  First turn: + takeover margin. */
static uint64_t handover_us(bool first) {
    uint64_t g = seq_mode ? 2000ull * seq_gap_ms : 1000ull * TURN_GAP_MS;
    return first ? g + 1000ull * TURN_TAKEOVER_EXTRA_MS : g;
}

static uint64_t peer_timeout_us(void) {
    uint64_t turn = seq_mode ? 3000ull * seq_gap_ms + 200000ull : 1000ull * TURN_MAX_MS;
    return 3 * (handover_us(false) + turn);
}

/* SEQ mode: gap_ms of idle bus after a transfer.  USB commands are served meanwhile; an ARB test starts at once (a
 * SEQ turn lasts seconds, the other master's ARB window only ARB_WINDOW_US). */
static void seq_pause(void) {
    uint64_t end = time_us_64() + 1000ull * seq_gap_ms;
    while (time_us_64() < end) {
        poll_commands();
        if (arb_request) run_arb_test();
        busy_wait_us(200);
    }
}

/* ---- 2026-11-17 (2.5-turns): every turn is ONE transfer per device, in this order, for both masters:
 *   RTC     pointer write 0x00, repeated START, read the 7 time / date registers 0x00..0x06
 *   BMP280  pointer write 0xF7, repeated START, read the 6 pressure / temperature ADC bytes 0xF7..0xFC
 *   OLED    one data write [0x40, 5 columns of this master's number, 1 blank column]: the OLED "types" 1 or 2 as its
 *           next character, so the screen shows the real order of the turns (1 2 1 2 ...)
 * 2026-11-17 (2.6-live): the OLED write shows LIVE readings instead of typing 1 / 2 - one write per OLED step (Co = 1
 * window commands + 0x40 + three 13-character lines: "M1 12:34:56", "T 25.31 C", "P 1008.25 HPA"; Master 1 on pages
 * 0-2, Master 2 on pages 4-6), and "DO CAL" re-reads the BMP280 chip ID + calibration on request.
 * Set-up transfers (BMP280 identity + configuration, OLED init + clear) run only in a master's first turn after boot or
 * after a failure.  (2.4 and earlier drew a full OLED frame every turn: ~70 OLED transfers next to one RTC and one
 * BMP280 read, and a periodic identity check added extra 0x76 reads in the middle of a turn.) */
static bool rtc_step(RTC_Data *t) {
    uint8_t b[7];
    memset(t, 0, sizeof(*t));
    if (!read_regs(RTC_ADDR, 0x00, b, 7)) return false;
    t->second = decimal(b[0] & 0x7F);
    t->minute = decimal(b[1] & 0x7F);
    if (b[2] & 0x40) {
        t->hour = decimal(b[2] & 0x1F) % 12;
        if (b[2] & 0x20) t->hour += 12;
    } else {
        t->hour = decimal(b[2] & 0x3F);
    }
    t->day = decimal(b[4] & 0x3F);
    t->month = decimal(b[5] & 0x1F);
    t->year = 2000 + decimal(b[6]) + ((b[5] & 0x80) ? 100 : 0);
    int weekday = b[3] & 0x07;
    return t->second <= 59 && t->minute <= 59 && t->hour <= 23 && weekday >= 1 && weekday <= 7 && t->day >= 1 &&
           t->day <= 31 && t->month >= 1 && t->month <= 12;
}

static const char *bmp_step(TurnState *ts, bool *reading_ok) {
    *reading_ok = false;
    if (!ts->bmp_ok) {                          /* set-up: first turn after boot / after a failure only */
        ts->bmp_ok = bmp_init();
        if (ts->first) printf("BMP280: %s\n", ts->bmp_ok ? "OK" : "FAILED");
        if (!ts->bmp_ok) return last.result == XR_OK ? "not_detected" : XR_NAME[last.result];
        if (seq_mode) seq_pause();
    }
    *reading_ok = bmp_read(&ts->temp, &ts->pressure);
    if (!*reading_ok && last.result != XR_OK && !contention_only()) ts->bmp_ok = false;
    if (!*reading_ok && last.result == XR_OK) return "invalid";          /* transfer fine, value not plausible yet */
    return result_name(*reading_ok);
}

static bool oled_clear(void) {
    const uint8_t win[7] = {0x00, 0x21, 0, 127, 0x22, 0, 7};      /* whole screen, pointer to column 0 / page 0 */
    if (!xfer(OLED_ADDR, win, sizeof(win), NULL, 0)) return false;
    uint8_t b[17] = {0x40};
    for (int i = 0; i < 64; i++)
        if (!xfer(OLED_ADDR, b, 17, NULL, 0)) return false;
    return true;                                                    /* the pointer wrapped back to the top left */
}

static const char *oled_step(TurnState *ts) {
    if (!ts->oled_ok) {                         /* set-up: first turn after boot / after a failure only */
        ts->oled_ok = oled_init() && oled_clear();
        if (ts->first) printf("OLED: %s\n", ts->oled_ok ? "OK" : "FAILED");
        if (!ts->oled_ok) return last.result == XR_OK ? "not_detected" : XR_NAME[last.result];
        if (seq_mode) seq_pause();
    }
    /* ONE write transfer: Co = 1 command pairs [0x80, cmd] set horizontal addressing and a window of this master's
     * three lines (Master 1 pages 0-2, Master 2 pages 4-6, columns 0..77), then [0x40] and the 3 x 78 text columns:
     *   "M1 12:34:56"   time from this master's newest RTC read
     *   "T 25.31 C"     temperature from its newest BMP280 read (compensated here; the GUI decodes the same bytes)
     *   "P 1008.25 HPA" pressure                                       ("N/A" when that read failed)        */
    enum { W = OLED_LINE_CHARS * 6, HDR = 17 };
    static uint8_t b[HDR + 3 * W];
    const uint8_t page0 = MASTER_ID == 1 ? 0 : 4;
    const uint8_t cmds[] = {0x20, 0x00, 0x21, 0, W - 1, 0x22, page0, (uint8_t)(page0 + 2)};
    for (int i = 0; i < 8; i++) {
        b[2 * i] = 0x80;                        /* control byte: Co = 1, D/C# = 0 -> one command byte follows */
        b[2 * i + 1] = cmds[i];
    }
    b[16] = 0x40;                               /* control byte: Co = 0, D/C# = 1 -> display RAM data follows */
    char lines[3][32];
    if (ts->rtc_ok)
        snprintf(lines[0], sizeof(lines[0]), "M%d %02d:%02d:%02d", MASTER_ID, ts->rtc.hour, ts->rtc.minute,
                 ts->rtc.second);
    else
        snprintf(lines[0], sizeof(lines[0]), "M%d RTC N/A", MASTER_ID);
    if (ts->reading_ok) {
        snprintf(lines[1], sizeof(lines[1]), "T %.2f C", ts->temp);
        snprintf(lines[2], sizeof(lines[2]), "P %.2f HPA", ts->pressure);
    } else {
        snprintf(lines[1], sizeof(lines[1]), "T N/A");
        snprintf(lines[2], sizeof(lines[2]), "P N/A");
    }
    for (int ln = 0; ln < 3; ln++) {
        uint8_t *row = &b[HDR + ln * W];
        memset(row, 0, W);                      /* blanks overwrite the previous, possibly longer, text */
        for (int c = 0; c < OLED_LINE_CHARS && lines[ln][c]; c++)
            glyph(lines[ln][c], &row[c * 6]);   /* 5 columns + 1 blank */
    }
    bool ok = xfer(OLED_ADDR, b, sizeof(b), NULL, 0);
    if (ok) ts->oled_chars++;
    else if (!contention_only()) ts->oled_ok = false;
    return result_name(ok);
}

/* CONDUCT mode: one step requested by the GUI ("DO RTC" / "DO BME" / "DO OLED"), reported as {"type":"step"}. */
static void run_step(TurnState *ts, char what) {
    uint32_t seq0 = txn_seq;
    uint64_t t0 = time_us_64();
    const char *res, *name;
    RTC_Data rtc;
    bool reading_ok;
    if (what == 'R') {
        name = "rtc";
        ts->rtc_ok = rtc_step(&rtc);
        if (ts->rtc_ok) ts->rtc = rtc;
        res = ts->rtc_ok ? "ok" : last.result == XR_OK ? "invalid" : XR_NAME[last.result];
    } else if (what == 'B') {
        name = "bmp";
        res = bmp_step(ts, &reading_ok);
        ts->reading_ok = reading_ok;
    } else if (what == 'C') {
        name = "cal";
        ts->bmp_ok = bmp_identity();
        res = ts->bmp_ok ? "ok" : last.result == XR_OK ? "not_detected" : XR_NAME[last.result];
    } else {
        name = "oled";
        res = oled_step(ts);
    }
    ts->first = false;
    printf("{\"type\":\"step\",\"source\":\"controller\",\"master_id\":%d,\"what\":\"%s\",\"result\":\"%s\","
           "\"t_us\":%llu,\"transfers\":%lu}\n", MASTER_ID, name, res, (unsigned long long)t0,
           (unsigned long)(txn_seq - seq0));
    if (what == 'B' && ts->rtc_ok && ts->reading_ok)       /* the same values the OLED line will show */
        printf("{\"type\":\"sensor_telemetry\",\"version\":1,\"source\":\"master\",\"master_id\":%d,"
               "\"rtc\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"temperature_c\":%.2f,"
               "\"pressure_hpa\":%.2f}\n", MASTER_ID, ts->rtc.year, ts->rtc.month, ts->rtc.day, ts->rtc.hour,
               ts->rtc.minute, ts->rtc.second, ts->temp, ts->pressure);
}

/* One turn: RTC 0x68 -> BMP280 0x76 -> OLED 0x3C, one transfer each, for BOTH masters (SEQ: gap_ms between them). */
static void run_turn(TurnState *ts, const char *reason, uint64_t idle_us) {
    turn_no++;
    bool seq = seq_mode;
    uint64_t t0 = time_us_64();
    uint32_t seq0 = txn_seq;
    printf("{\"type\":\"turn_start\",\"source\":\"controller\",\"master_id\":%d,\"turn\":%lu,\"t_us\":%llu,"
           "\"reason\":\"%s\",\"idle_before_us\":%llu,\"seq\":%s,\"gap_ms\":%lu}\n", MASTER_ID, (unsigned long)turn_no,
           (unsigned long long)t0, reason, (unsigned long long)idle_us, seq ? "true" : "false",
           (unsigned long)(seq ? seq_gap_ms : TURN_GAP_MS));
#if SET_RTC_ON_BOOT && MASTER_ID == 1
    if (ts->first) printf(set_rtc() ? "RTC time set\n" : "RTC setting failed\n");
#endif
    if (pending_time.set) {                     /* TIME command received between turns */
        pending_time.set = false;
        bool written = rtc_write_time(pending_time.y, pending_time.mo, pending_time.d, pending_time.h,
                                      pending_time.mi, pending_time.s);
        printf("{\"type\":\"rtc_set\",\"source\":\"controller\",\"master_id\":%d,\"result\":\"%s\","
               "\"time\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"weekday_1_sunday\":%d}\n",
               MASTER_ID, written ? "applied" : "i2c_failed", pending_time.y, pending_time.mo, pending_time.d,
               pending_time.h, pending_time.mi, pending_time.s,
               weekday_1_sunday(pending_time.y, pending_time.mo, pending_time.d));
        if (seq) seq_pause();
    }
    if (pending_probe.set) {                    /* PROBE command: one real transfer to the requested address */
        run_probe();
        if (seq) seq_pause();
    }
    if (pending_cal) {                          /* DO CAL: chip ID + calibration, once */
        pending_cal = false;
        ts->bmp_ok = bmp_identity();
        printf("{\"type\":\"step\",\"source\":\"controller\",\"master_id\":%d,\"what\":\"cal\",\"result\":\"%s\"}\n",
               MASTER_ID, ts->bmp_ok ? "ok" : XR_NAME[last.result]);
        if (seq) seq_pause();
    }
    /* 1. DS3231 RTC */
    RTC_Data rtc;
    bool rtc_ok = rtc_step(&rtc);
    ts->rtc_ok = rtc_ok;
    if (rtc_ok) ts->rtc = rtc;
    const char *rtc_res = rtc_ok ? "ok" : last.result == XR_OK ? "invalid" : XR_NAME[last.result];
    if (seq) seq_pause();
    /* 2. BMP280 */
    bool reading_ok;
    const char *bmp_res = bmp_step(ts, &reading_ok);
    ts->reading_ok = reading_ok;
    if (SCAN_PERIOD_MS && time_reached(ts->next_scan)) {
        ts->next_scan = make_timeout_time_ms(SCAN_PERIOD_MS);
        bus_scan();
    }
    if (seq) seq_pause();
    /* 3. SSD1306 OLED */
    const char *oled_res = oled_step(ts);
    ts->first = false;
    uint64_t t1 = time_us_64();
    if (rtc_ok)
        printf("RTC %02d:%02d:%02d %02d/%02d/%04d  ", rtc.hour, rtc.minute, rtc.second, rtc.day, rtc.month, rtc.year);
    else
        printf("RTC read failed  ");
    if (reading_ok) printf("BMP280 %.2f C %.2f hPa\n", ts->temp, ts->pressure);
    else printf("BMP280 not available\n");
    if (rtc_ok && reading_ok)
        printf("{\"type\":\"sensor_telemetry\",\"version\":1,\"source\":\"master\",\"master_id\":%d,"
               "\"rtc\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"temperature_c\":%.2f,"
               "\"pressure_hpa\":%.2f}\n", MASTER_ID, rtc.year, rtc.month, rtc.day, rtc.hour,
               rtc.minute, rtc.second, ts->temp, ts->pressure);
    printf("{\"type\":\"turn_end\",\"source\":\"controller\",\"master_id\":%d,\"turn\":%lu,\"t_us\":%llu,"
           "\"dur_us\":%llu,\"transfers\":%lu,\"rtc\":\"%s\",\"bmp\":\"%s\",\"oled\":\"%s\",\"seq\":%s}\n",
           MASTER_ID, (unsigned long)turn_no, (unsigned long long)t1, (unsigned long long)(t1 - t0),
           (unsigned long)(txn_seq - seq0), rtc_res, bmp_res, oled_res, seq ? "true" : "false");
}

/* Wait for our turn by watching the bus; run it; repeat.  Never returns. */
static void turns_main(void) {
    enum { WAIT_PEER, WAIT_GAP } state = MASTER_ID == 1 ? WAIT_GAP : WAIT_PEER;
    bool first = MASTER_ID == 1;                                   /* first turn: longer than a normal gap */
    const char *reason = "first_turn";
    TurnState ts = {.bmp_ok = false, .oled_ok = false, .first = true, .reading_ok = false, .rtc_ok = false,
                    .oled_chars = 0};
    ts.next_scan = get_absolute_time();
    bool conducted = false;
    absolute_time_t next_stats = make_timeout_time_ms(STATS_PERIOD_MS);
    absolute_time_t next_bus_check = make_timeout_time_ms(STUCK_CHECK_PERIOD_MS);
    hal_start_det_clear();
    last_bus_activity_us = time_us_64();
    uint64_t wait_start = last_bus_activity_us, ignore_until = 0;
#ifndef I2C_HOST_SIM
    bool usb_was_connected = stdio_usb_connected();
#endif
    while (true) {
        poll_commands();
#ifndef I2C_HOST_SIM
        bool usb = stdio_usb_connected();
        if (usb && !usb_was_connected) print_hello();
        usb_was_connected = usb;
#endif
        if (arb_request) {
            /* ARB: both masters hammer at once.  Afterwards Master 1 takes the next turn and Master 2 ignores the
             * remaining ARB traffic, so the strict turn order resumes instead of both waiting for the same gap. */
            run_arb_test();
            hal_start_det_clear();
            last_bus_activity_us = time_us_64();
            wait_start = last_bus_activity_us;
            if (MASTER_ID == 1) {
                state = WAIT_GAP;
                first = true;
                reason = "first_turn";
            } else {
                state = WAIT_PEER;
                ignore_until = last_bus_activity_us + 300000ull;
            }
        }
        if (consecutive_timeouts >= TIMEOUTS_BEFORE_RECOVERY || time_reached(next_bus_check)) {
            next_bus_check = make_timeout_time_ms(STUCK_CHECK_PERIOD_MS);
            check_bus();
        }
        if (time_reached(next_stats)) {
            next_stats = delayed_by_ms(next_stats, STATS_PERIOD_MS);
            print_stats();
        }
        if (conduct_mode) {                       /* the GUI drives every transfer: nothing starts by itself */
            conducted = true;
            if (pending_probe.set) run_probe();
            if (pending_cal) {
                pending_cal = false;
                run_step(&ts, 'C');
            }
            if (pending_step) {
                char what = pending_step;
                pending_step = 0;
                run_step(&ts, what);
            }
            busy_wait_us(500);
            continue;
        }
        if (conducted) {                          /* CONDUCT OFF: the turn order starts again from Master 1 */
            conducted = false;
            hal_start_det_clear();
            last_bus_activity_us = time_us_64();
            wait_start = last_bus_activity_us;
            state = MASTER_ID == 1 ? WAIT_GAP : WAIT_PEER;
            first = MASTER_ID == 1;
            reason = "first_turn";
        }
        for (int i = 0; i < 1000 / TURN_POLL_US; i++) {         /* ~1 ms of bus observation */
            if (observe_bus() && state == WAIT_PEER && time_us_64() >= ignore_until) {
                state = WAIT_GAP;                                   /* the other master is taking its turn */
                first = false;
                reason = "after_peer_turn";
            }
            busy_wait_us(TURN_POLL_US);
        }
        uint64_t now = time_us_64(), idle = now - last_bus_activity_us;
        bool go = state == WAIT_GAP && idle >= handover_us(first);
        if (!go && state == WAIT_PEER && now - wait_start >= peer_timeout_us() && idle >= handover_us(false)) {
            printf("{\"type\":\"peer_absent\",\"source\":\"controller\",\"master_id\":%d,\"t_us\":%llu,"
                   "\"silent_ms\":%llu}\n", MASTER_ID, (unsigned long long)now,
                   (unsigned long long)((now - wait_start) / 1000));
            reason = "peer_absent";
            go = true;
        }
        if (go) {
            run_turn(&ts, reason, idle);
            hal_start_det_clear();                                  /* our own STARTs are not the peer's */
            last_bus_activity_us = time_us_64();
            wait_start = last_bus_activity_us;
            state = WAIT_PEER;
            first = false;
        }
    }
}
#endif

/* ------------------------------------------------------------------ main */
int main(void) {
    stdio_init_all();
    bus_init();
    sleep_ms(2000 + SCHEDULE_OFFSET_MS);
    printf("\nI2C %s (%s): RTC + BMP280%s  (SDA GP4, SCL GP5, multi-master)\n", MASTER_NAME, BOARD_NAME,
           DRIVES_OLED ? " + OLED" : "");
    print_hello();
    report_scl("boot", scl_requested, "");

#if TURNS_ACTIVE
    turns_main();
#else
#if SET_RTC_ON_BOOT && MASTER_ID == 1
    printf(set_rtc() ? "RTC time set\n" : "RTC setting failed\n");
#endif

    bool bmp_ok = bmp_init();
    printf("BMP280: %s\n", bmp_ok ? "OK" : "FAILED");
#if DRIVES_OLED
    bool oled_ok = oled_init();
    printf("OLED: %s\n", oled_ok ? "OK" : "FAILED");
#endif
    if (SCAN_PERIOD_MS) bus_scan();

    absolute_time_t next_bmp = get_absolute_time();
    absolute_time_t next_display = next_bmp;
    absolute_time_t next_identity = make_timeout_time_ms(IDENTITY_PERIOD_MS);
    absolute_time_t next_scan = make_timeout_time_ms(SCAN_PERIOD_MS ? SCAN_PERIOD_MS : 1000);
    absolute_time_t next_bmp_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
#if DRIVES_OLED
    absolute_time_t next_oled_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
#endif
    absolute_time_t next_stats = make_timeout_time_ms(STATS_PERIOD_MS);
    absolute_time_t next_bus_check = make_timeout_time_ms(STUCK_CHECK_PERIOD_MS);
    float temp = 0, pressure = 0;
    bool reading_ok = false;
#ifndef I2C_HOST_SIM
    bool usb_was_connected = stdio_usb_connected();
#endif

    while (true) {
        poll_commands();
        if (arb_request) run_arb_test();
        if (pending_probe.set) run_probe();
#ifndef I2C_HOST_SIM
        bool usb = stdio_usb_connected();
        if (usb && !usb_was_connected) print_hello();      /* GUI (re)opened this COM port */
        usb_was_connected = usb;
#endif
        if (consecutive_timeouts >= TIMEOUTS_BEFORE_RECOVERY || time_reached(next_bus_check)) {
            next_bus_check = make_timeout_time_ms(STUCK_CHECK_PERIOD_MS);
            check_bus();
        }
        if (time_reached(next_stats)) {
            next_stats = delayed_by_ms(next_stats, STATS_PERIOD_MS);
            print_stats();
        }

#ifdef MM_STRESS
        /* different targets per master: identical simultaneous transfers would merge silently and
         * never arbitrate.  0x68 (RTC) beats 0x76 (BMP): the lower address wins at address bit 3. */
        { float st_t, st_p; RTC_Data st_r;
#if MASTER_ID == 1
          (void)bmp_read(&st_t, &st_p);
#else
          (void)read_rtc(&st_r);
#endif
          (void)st_t; (void)st_p; (void)st_r; continue; }
#endif
        if (time_reached(next_bmp)) {
            next_bmp = delayed_by_ms(next_bmp, BMP_PERIOD_MS);
            if (!bmp_ok && time_reached(next_bmp_retry)) {    /* re-detect after hot-swap */
                next_bmp_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
                bmp_ok = bmp_init();
            }
            reading_ok = bmp_ok && bmp_read(&temp, &pressure);
            /* lost arbitration / busy bus: skip this sample, the sensor is still there */
            if (bmp_ok && !reading_ok && !contention_only()) bmp_ok = false;
        }

        if (time_reached(next_identity)) {
            next_identity = delayed_by_ms(next_identity, IDENTITY_PERIOD_MS);
            if (bmp_ok && !bmp_identity() && !contention_only()) bmp_ok = false;
#if NACK_TEST_ADDR
            uint8_t dummy = 0;
            xfer(NACK_TEST_ADDR, &dummy, 1, NULL, 0);      /* expected NACK */
#endif
        }

        if (SCAN_PERIOD_MS && time_reached(next_scan)) {
            next_scan = delayed_by_ms(next_scan, SCAN_PERIOD_MS);
            bus_scan();
        }

        if (time_reached(next_display)) {
            next_display = delayed_by_ms(next_display, RTC_PERIOD_MS);
            RTC_Data rtc = {0};
            bool rtc_ok = read_rtc(&rtc);

            if (rtc_ok)
                printf("RTC %02d:%02d:%02d %02d/%02d/%04d  ", rtc.hour, rtc.minute, rtc.second,
                       rtc.day, rtc.month, rtc.year);
            else if (rtc.osf)
                printf("RTC invalid: oscillator stopped (OSF), set the time  ");
            else if (contention_only())
                printf("RTC read skipped (bus contention: %s)  ", XR_NAME[last.result]);
            else
                printf("RTC read failed  ");
            if (reading_ok) printf("BMP280 %.2f C %.2f hPa\n", temp, pressure);
            else printf("BMP280 not available\n");
            if (rtc_ok && reading_ok)            /* separate master COM port, never bus data */
                printf("{\"type\":\"sensor_telemetry\",\"version\":1,\"source\":\"master\",\"master_id\":%d,"
                       "\"rtc\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"temperature_c\":%.2f,"
                       "\"pressure_hpa\":%.2f}\n", MASTER_ID, rtc.year, rtc.month, rtc.day, rtc.hour,
                       rtc.minute, rtc.second, temp, pressure);

#if OLED_CMD_WRITES
            {   /* Master 2 -> OLED 0x3C: one command WRITE per display cycle (see OLED_CMD_WRITES) */
                const uint8_t cmd[2] = {0x00, 0xA6};
                if (!xfer(OLED_ADDR, cmd, 2, NULL, 0) && !contention_only())
                    printf("OLED command write failed (%s)\n", XR_NAME[last.result]);
            }
#endif
#if DRIVES_OLED
            if (!oled_ok && time_reached(next_oled_retry)) { /* re-detect after hot-swap */
                next_oled_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
                oled_ok = oled_init();
            }
            if (oled_ok) {
                if (!oled_draw("I2C MONITOR", &rtc, rtc_ok, reading_ok, temp, pressure)) {
                    printf("OLED transfer failed (%s)\n", XR_NAME[last.result]);
                    if (!contention_only()) oled_ok = false;   /* frame is simply redrawn next time */
                }
            }
#endif
        }
        sleep_ms(1);
    }
#endif
}
