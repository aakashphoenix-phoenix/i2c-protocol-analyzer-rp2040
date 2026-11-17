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
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "pico/stdlib.h"
#include "hardware/i2c.h"

#define BUS i2c0
#define SDA_PIN 4
#define SCL_PIN 5
#define I2C_HZ 100000
#define RTC_ADDR 0x68
#define BMP_ADDR 0x76
#define OLED_ADDR 0x3C
#define I2C_TIMEOUT 10000

#define SET_RTC_ON_BOOT 0            /* 1 = write the time below once at boot */
#define BMP_PERIOD_MS 200
#define DISPLAY_PERIOD_MS 1000       /* RTC read + OLED refresh */
#define IDENTITY_PERIOD_MS 10000     /* BMP280 chip ID + calibration */
#define SCAN_PERIOD_MS 5000          /* set 0 to disable the bus scan */
#define NACK_TEST_ADDR 0x00          /* e.g. 0x2A: probe an empty address every 10 s (test 5) */

#define OLED_WIDTH 128
#define OLED_HEIGHT 64
static uint8_t screen[OLED_WIDTH * OLED_HEIGHT / 8];

static int consecutive_failures;

/* ---------------------------------------------------------------- basic I2C */
static void note(bool ok) { consecutive_failures = ok ? 0 : consecutive_failures + 1; }

static bool write_reg(uint8_t addr, uint8_t reg, uint8_t value) {
    uint8_t b[2] = {reg, value};
    bool ok = i2c_write_timeout_us(BUS, addr, b, 2, false, I2C_TIMEOUT) == 2;
    note(ok);
    return ok;
}

static bool read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t len) {
    /* write register pointer, repeated START, read -> decoded by the GUI as S ... Sr ... P */
    bool ok = i2c_write_timeout_us(BUS, addr, &reg, 1, true, I2C_TIMEOUT) == 1 &&
              i2c_read_timeout_us(BUS, addr, buf, len, false, I2C_TIMEOUT) == (int)len;
    note(ok);
    return ok;
}

static void bus_init(void) {
    i2c_init(BUS, I2C_HZ);
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
    consecutive_failures = 0;
}

/* ---------------------------------------------------------------- DS3231 */
static uint8_t bcd(uint8_t n) { return ((n / 10) << 4) | (n % 10); }
static uint8_t decimal(uint8_t n) { return (n >> 4) * 10 + (n & 15); }

static bool set_rtc(void) {
    uint8_t b[8] = {0x00, bcd(0), bcd(22), bcd(8), bcd(1), bcd(18), bcd(10), bcd(26)};
    return i2c_write_timeout_us(BUS, RTC_ADDR, b, 8, false, I2C_TIMEOUT) == 8;
}

typedef struct { int hour, minute, second, day, month, year; } RTC_Data;

static bool read_rtc(RTC_Data *t) {
    uint8_t b[7];
    if (!read_regs(RTC_ADDR, 0, b, 7)) return false;
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
    return true;
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
    if (!read_regs(BMP_ADDR, 0xD0, &id, 1) || id != 0x58) return false;
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

    *temp = (float)temperature;
    *press = (float)(p / 100.0);
    return true;
}

/* ---------------------------------------------------------------- OLED SSD1306 */
static bool oled_cmd(uint8_t command) {
    uint8_t b[2] = {0x00, command};
    bool ok = i2c_write_timeout_us(BUS, OLED_ADDR, b, 2, false, I2C_TIMEOUT) == 2;
    note(ok);
    return ok;
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
        bool ok = i2c_write_timeout_us(BUS, OLED_ADDR, b, 17, false, I2C_TIMEOUT) == 17;
        note(ok);
        if (!ok) return false;
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
            else if (a == 0x68 || a == 0x69) read_regs(a, 0x75, &id, 1);   /* MPU-6050 WHO_AM_I    */
        }
        if (!ack && present[a]) printf(" [0x%02X removed]", a);
        present[a] = ack;
    }
    printf("\n");
}

/* ---------------------------------------------------------------- main */
int main(void) {
    stdio_init_all();
    bus_init();
    sleep_ms(2000);
    printf("\nI2C MASTER: RTC + BMP280 + OLED  (100 kHz, SDA GP4, SCL GP5)\n");

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
    float temp = 0, pressure = 0;
    bool reading_ok = false;

    while (true) {
        if (consecutive_failures >= 6) bus_recover();

        if (time_reached(next_bmp)) {
            next_bmp = delayed_by_ms(next_bmp, BMP_PERIOD_MS);
            if (!bmp_ok) bmp_ok = bmp_init();                 /* re-detect after hot-swap */
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
            RTC_Data rtc;
            bool rtc_ok = read_rtc(&rtc);
            char line[32];

            if (rtc_ok)
                printf("RTC %02d:%02d:%02d %02d/%02d/%04d  ", rtc.hour, rtc.minute, rtc.second,
                       rtc.day, rtc.month, rtc.year);
            else
                printf("RTC read failed  ");
            if (reading_ok) printf("BMP280 %.2f C %.2f hPa\n", temp, pressure);
            else printf("BMP280 not available\n");

            if (!oled_ok) oled_ok = oled_init();            /* re-detect after hot-swap */
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