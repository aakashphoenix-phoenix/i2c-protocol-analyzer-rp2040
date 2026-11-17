/*
 * master_main.c - Raspberry Pi Pico 1 (RP2040): I2C master for the analyzer project.
 *
 *   SDA GP4, SCL GP5, i2c0 @ 100 kHz
 *   DS3231 RTC 0x68 · BMP280 0x76 · SSD1306 OLED 0x3C
 *
 * Changes from the earlier test program (same sensors, same OLED screen):
 *   - BMP280 read every 200 ms, RTC + OLED every 1 s  -> steady, measurable traffic
 *   - BMP280 chip-ID + calibration re-read every 10 s -> the GUI can compute
 *     temperature/pressure even if capture started after the master booted,
 *     and a swapped sensor is re-identified
 *   - bus scan every 5 s (0x08..0x77)                 -> a node inserted during the
 *     demo appears in the GUI topology, a removed one turns "not responding"
 *   - devices that fail are re-initialised automatically (hot-swap friendly)
 *   - I2C bus recovery (9 SCL pulses + STOP) if the bus gets stuck
 * The master's own USB port still prints a readable log (separate from the analyzer).
 *
 * Upgrade 2026-11-08 (source-evidence fixes; physical bus still unverified):
 *   - periodic bus scan is now OPT-IN (SCAN_PERIOD_MS 0).  Each scan probed every
 *     address 0x08..0x77; all absent addresses were NACKed (~108 NACKs per scan),
 *     which the analyzer correctly counts as unexpected address NACKs.
 *   - bus recovery is triggered by timeouts or a stuck-LOW line, not by NACKs.
 *     An address NACK from an absent device is not a stuck bus.
 *   - DS3231: time + status read in ONE combined transaction (0x00..0x0F); BCD
 *     ranges are validated and the oscillator-stop flag (OSF) marks the time invalid.
 *   - BMP280/BME280: chip ID 0x58 or 0x60 accepted (same T/P register map);
 *     implausible measurements are rejected.
 *   - absent devices are re-detected every REDETECT_PERIOD_MS instead of every
 *     loop, so a missing part does not flood the bus with address probes.
 *   - optional machine-readable telemetry line on this (separate) USB port.
 *
 * SCL setting (2026-11-08): commands on THIS controller's USB port (never the analyzer):
 *   "SCL?"        -> {"type":"scl_config","result":"current",...}
 *   "SCL 400000"  -> applied between transactions with i2c_set_baudrate(); the reply
 *                    carries the divider-computed rate, NOT a bus measurement.
 * Only rates every configured part supports are accepted (see SCL_SUPPORTED).
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>

#include "pico/stdlib.h"
#include "hardware/i2c.h"

#define BUS i2c0
#define SDA_PIN 4
#define SCL_PIN 5
#define I2C_HZ 100000                /* boot default: Standard-mode */
#define RTC_ADDR 0x68
#define BMP_ADDR 0x76
#define OLED_ADDR 0x3C
#define I2C_TIMEOUT 10000

#define SET_RTC_ON_BOOT 0            /* 1 = write the time below once at boot */
#define BMP_PERIOD_MS 200
#define DISPLAY_PERIOD_MS 1000       /* RTC read + OLED refresh */
#define IDENTITY_PERIOD_MS 10000     /* BMP280 chip ID + calibration */
#define SCAN_PERIOD_MS 0             /* opt-in: e.g. 5000. Each scan NACKs every absent address */
#define REDETECT_PERIOD_MS 2000      /* retry interval for a device that failed init */
#define TIMEOUTS_BEFORE_RECOVERY 3
#define NACK_TEST_ADDR 0x00          /* e.g. 0x2A: probe an empty address every 10 s (test 5) */

#define OLED_WIDTH 128
#define OLED_HEIGHT 64
static uint8_t screen[OLED_WIDTH * OLED_HEIGHT / 8];

static int consecutive_timeouts;

/* Selectable controller SCL rates.  Limits from component documentation:
 *   DS3231  - Fast-mode I2C, 400 kHz maximum
 *   SSD1306 - I2C clock cycle >= 2.5 us, i.e. 400 kHz maximum
 *   BMP280/BME280 - up to 3.4 MHz (not the limiting part)
 *   RP2040 I2C - up to 1 MHz (Fast-mode Plus), not usable with DS3231/SSD1306
 * so Standard-mode 100 kHz and Fast-mode 400 kHz (UM10204) are offered. */
static const unsigned SCL_SUPPORTED[] = {100000, 400000};
static unsigned scl_requested = I2C_HZ;
static unsigned scl_applied;               /* rate returned by the SDK divider */

static void report_scl(const char *result, unsigned requested, const char *reason) {
    printf("{\"type\":\"scl_config\",\"source\":\"controller\",\"result\":\"%s\","
           "\"requested_hz\":%u,\"applied_hz\":%u,\"supported_hz\":[%u,%u],\"reason\":\"%s\"}\n",
           result, requested, scl_applied, SCL_SUPPORTED[0], SCL_SUPPORTED[1], reason);
}

/* ---------------------------------------------------------------- basic I2C */
/* Pico SDK: PICO_ERROR_GENERIC = address/data NACK (abort), PICO_ERROR_TIMEOUT =
 * transfer did not finish.  Only timeouts can indicate a stuck bus. */
static bool note(int rc, int expected) {
    if (rc == PICO_ERROR_TIMEOUT) consecutive_timeouts++;
    else if (rc == expected) consecutive_timeouts = 0;
    return rc == expected;
}

static bool write_reg(uint8_t addr, uint8_t reg, uint8_t value) {
    uint8_t b[2] = {reg, value};
    return note(i2c_write_timeout_us(BUS, addr, b, 2, false, I2C_TIMEOUT), 2);
}

static bool read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t len) {
    /* write register pointer, repeated START, read -> decoded by the GUI as S ... Sr ... P */
    if (!note(i2c_write_timeout_us(BUS, addr, &reg, 1, true, I2C_TIMEOUT), 1)) return false;
    return note(i2c_read_timeout_us(BUS, addr, buf, len, false, I2C_TIMEOUT), (int)len);
}

/* Idle bus with SDA or SCL held LOW (checked twice, 200 us apart). */
static bool bus_stuck(void) {
    if (gpio_get(SDA_PIN) && gpio_get(SCL_PIN)) return false;
    sleep_us(200);
    return !gpio_get(SDA_PIN) || !gpio_get(SCL_PIN);
}

static void bus_init(void) {
    scl_applied = i2c_init(BUS, scl_requested);     /* recovery keeps the selected rate */
    gpio_set_function(SDA_PIN, GPIO_FUNC_I2C);
    gpio_set_function(SCL_PIN, GPIO_FUNC_I2C);
    /* module pull-ups do the job; internal ones are weak but harmless */
    gpio_pull_up(SDA_PIN);
    gpio_pull_up(SCL_PIN);
}

/* A slave reset mid-byte can hold SDA low forever: clock it out, then send STOP. */
static void bus_recover(void) {
    printf("I2C bus recovery\n");
    i2c_deinit(BUS);
    gpio_init(SCL_PIN);
    gpio_init(SDA_PIN);
    gpio_set_dir(SDA_PIN, GPIO_IN);
    for (int i = 0; i < 9 && !gpio_get(SDA_PIN); i++) {
        gpio_set_dir(SCL_PIN, GPIO_OUT);   /* drive low (open-drain style) */
        gpio_put(SCL_PIN, 0);
        sleep_us(5);
        gpio_set_dir(SCL_PIN, GPIO_IN);    /* release: pull-up takes it high */
        sleep_us(5);
    }
    gpio_set_dir(SDA_PIN, GPIO_OUT);       /* STOP: SDA low -> high while SCL high */
    gpio_put(SDA_PIN, 0);
    sleep_us(5);
    gpio_set_dir(SDA_PIN, GPIO_IN);
    sleep_us(5);
    bus_init();
    consecutive_timeouts = 0;
}

/* ---------------------------------------------------------------- DS3231 */
static uint8_t bcd(uint8_t n) { return ((n / 10) << 4) | (n % 10); }
static uint8_t decimal(uint8_t n) { return (n >> 4) * 10 + (n & 15); }

static bool set_rtc(void) {
    uint8_t b[8] = {0x00, bcd(0), bcd(22), bcd(8), bcd(1), bcd(18), bcd(10), bcd(26)};
    if (!note(i2c_write_timeout_us(BUS, RTC_ADDR, b, 8, false, I2C_TIMEOUT), 8)) return false;
    /* time is valid again: clear OSF (status bit 7), keep the other status bits */
    uint8_t status;
    if (!read_regs(RTC_ADDR, 0x0F, &status, 1)) return false;
    return write_reg(RTC_ADDR, 0x0F, status & 0x7F);
}

typedef struct { int hour, minute, second, day, month, year; bool osf; } RTC_Data;

/* One combined read of registers 0x00..0x0F: time/date (0x00..0x06) and the
 * status register (0x0F) whose OSF bit says the oscillator stopped, i.e. the
 * time is not trustworthy until it is set again. */
static bool read_rtc(RTC_Data *t) {
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

/* ---------------------------------------------------------------- BMP280 */
typedef struct {
    uint16_t T1, P1;
    int16_t T2, T3, P2, P3, P4, P5, P6, P7, P8, P9;
} Calibration;
static Calibration cal;

static uint16_t u16(const uint8_t *b) { return (uint16_t)b[0] | ((uint16_t)b[1] << 8); }
static int16_t s16(const uint8_t *b) { return (int16_t)u16(b); }

/* Chip ID (0xD0) + calibration (0x88..0x9F). The analyzer GUI uses exactly these
 * bytes to confirm the device and to compute temperature/pressure itself. */
static bool bmp_identity(void) {
    uint8_t id, b[24];
    /* 0x58 = BMP280, 0x60 = BME280: identical calibration/T/P register map */
    if (!read_regs(BMP_ADDR, 0xD0, &id, 1) || (id != 0x58 && id != 0x60)) return false;
    if (!read_regs(BMP_ADDR, 0x88, b, 24)) return false;
    cal.T1 = u16(b);      cal.T2 = s16(b + 2);  cal.T3 = s16(b + 4);
    cal.P1 = u16(b + 6);  cal.P2 = s16(b + 8);  cal.P3 = s16(b + 10);
    cal.P4 = s16(b + 12); cal.P5 = s16(b + 14); cal.P6 = s16(b + 16);
    cal.P7 = s16(b + 18); cal.P8 = s16(b + 20); cal.P9 = s16(b + 22);
    return cal.P1 != 0;
}

static bool bmp_init(void) {
    if (!bmp_identity()) return false;
    /* normal mode, temperature x1, pressure x1, standby 1000 ms */
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

/* ---------------------------------------------------------------- OLED SSD1306 */
static bool oled_cmd(uint8_t command) {
    uint8_t b[2] = {0x00, command};
    return note(i2c_write_timeout_us(BUS, OLED_ADDR, b, 2, false, I2C_TIMEOUT), 2);
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
        if (!note(i2c_write_timeout_us(BUS, OLED_ADDR, b, 17, false, I2C_TIMEOUT), 17)) return false;
    }
    return true;
}

/* ---------------------------------------------------------------- bus scan */
static bool present[128];

static bool reserved_addr(uint8_t a) { return (a & 0x78) == 0 || (a & 0x78) == 0x78; }

static void bus_scan(void) {
    printf("Bus scan:");
    for (uint8_t a = 0x08; a < 0x78; a++) {
        if (reserved_addr(a)) continue;
        uint8_t rx;
        bool ack = i2c_read_timeout_us(BUS, a, &rx, 1, false, 2000) == 1;
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

/* ---------------------------------------------------------------- USB commands */
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
        } else {
            printf("{\"type\":\"error\",\"source\":\"controller\",\"message\":\"unknown_command\"}\n");
        }
    }
}

/* ---------------------------------------------------------------- main */
int main(void) {
    stdio_init_all();
    bus_init();
    sleep_ms(2000);
    printf("\nI2C MASTER: RTC + BMP280 + OLED  (SDA GP4, SCL GP5)\n");
    report_scl("boot", scl_requested, "");

#if SET_RTC_ON_BOOT
    printf(set_rtc() ? "RTC time set\n" : "RTC setting failed\n");
#endif

    bool bmp_ok = bmp_init();
    bool oled_ok = oled_init();
    printf("BMP280: %s\nOLED: %s\n", bmp_ok ? "OK" : "FAILED", oled_ok ? "OK" : "FAILED");
    if (SCAN_PERIOD_MS) bus_scan();

    absolute_time_t next_bmp = get_absolute_time();
    absolute_time_t next_display = next_bmp;
    absolute_time_t next_identity = make_timeout_time_ms(IDENTITY_PERIOD_MS);
    absolute_time_t next_scan = make_timeout_time_ms(SCAN_PERIOD_MS ? SCAN_PERIOD_MS : 1000);
    absolute_time_t next_bmp_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
    absolute_time_t next_oled_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
    float temp = 0, pressure = 0;
    bool reading_ok = false;

    while (true) {
        poll_commands();
        if (consecutive_timeouts >= TIMEOUTS_BEFORE_RECOVERY || bus_stuck()) bus_recover();

        if (time_reached(next_bmp)) {
            next_bmp = delayed_by_ms(next_bmp, BMP_PERIOD_MS);
            if (!bmp_ok && time_reached(next_bmp_retry)) {    /* re-detect after hot-swap */
                next_bmp_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
                bmp_ok = bmp_init();
            }
            reading_ok = bmp_ok && bmp_read(&temp, &pressure);
            if (bmp_ok && !reading_ok) bmp_ok = false;
        }

        if (time_reached(next_identity)) {
            next_identity = delayed_by_ms(next_identity, IDENTITY_PERIOD_MS);
            if (bmp_ok) bmp_ok = bmp_identity();
#if NACK_TEST_ADDR
            uint8_t dummy = 0;
            i2c_write_timeout_us(BUS, NACK_TEST_ADDR, &dummy, 1, false, I2C_TIMEOUT);  /* expected NACK */
#endif
        }

        if (SCAN_PERIOD_MS && time_reached(next_scan)) {
            next_scan = delayed_by_ms(next_scan, SCAN_PERIOD_MS);
            bus_scan();
        }

        if (time_reached(next_display)) {
            next_display = delayed_by_ms(next_display, DISPLAY_PERIOD_MS);
            RTC_Data rtc = {0};
            bool rtc_ok = read_rtc(&rtc);
            char line[32];

            if (rtc_ok)
                printf("RTC %02d:%02d:%02d %02d/%02d/%04d  ", rtc.hour, rtc.minute, rtc.second,
                       rtc.day, rtc.month, rtc.year);
            else if (rtc.osf)
                printf("RTC invalid: oscillator stopped (OSF), set the time  ");
            else
                printf("RTC read failed  ");
            if (reading_ok) printf("BMP280 %.2f C %.2f hPa\n", temp, pressure);
            else printf("BMP280 not available\n");
            if (rtc_ok && reading_ok)            /* separate master COM port, never bus data */
                printf("{\"type\":\"sensor_telemetry\",\"version\":1,\"source\":\"master\","
                       "\"rtc\":\"%04d-%02d-%02dT%02d:%02d:%02d\",\"temperature_c\":%.2f,"
                       "\"pressure_hpa\":%.2f}\n", rtc.year, rtc.month, rtc.day, rtc.hour,
                       rtc.minute, rtc.second, temp, pressure);

            if (!oled_ok && time_reached(next_oled_retry)) { /* re-detect after hot-swap */
                next_oled_retry = make_timeout_time_ms(REDETECT_PERIOD_MS);
                oled_ok = oled_init();
            }
            if (oled_ok) {
                memset(screen, 0, sizeof(screen));
                oled_text(0, 0, "I2C MONITOR");
                if (rtc_ok) {
                    snprintf(line, sizeof(line), "TIME %02d:%02d:%02d", rtc.hour, rtc.minute, rtc.second);
                    oled_text(0, 16, line);
                    snprintf(line, sizeof(line), "DATE %02d/%02d/%04d", rtc.day, rtc.month, rtc.year);
                    oled_text(0, 26, line);
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
                if (!oled_show()) {
                    printf("OLED transfer failed\n");
                    oled_ok = false;
                }
            }
        }
        sleep_ms(1);
    }
}