// Raspberry Pi Pico RP2040 MASTER: DS3231 + BMP280 + SSD1306 128x64
// I2C0 SDA=GP4 SCL=GP5 100 kHz; USB CDC status.
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stddef.h>
#include "pico/stdlib.h"
#include "hardware/i2c.h"

#define BUS i2c0
#define SDA_PIN 4
#define SCL_PIN 5
#define BUS_HZ 100000
#define TIMEOUT_US 20000
#define RTC_ADDR 0x68
#define BMP_ADDR 0x76
#define OLED_ADDR 0x3C // set 0x3D only if your OLED responds there
#define SET_RTC_ON_BOOT 0 // Use 1 ONCE to set 18/10/2026 08:22:00, then change back to 0
#define OLED_W 128
#define OLED_H 64

static bool reg_write(uint8_t dev, uint8_t reg, uint8_t val) {
    uint8_t payload[] = {reg, val};
    return i2c_write_timeout_us(BUS, dev, payload, sizeof(payload), false, TIMEOUT_US) == (int)sizeof(payload);
}
static bool reg_read(uint8_t dev, uint8_t reg, uint8_t *buf, size_t n) {
    if (i2c_write_timeout_us(BUS, dev, &reg, 1, true, TIMEOUT_US) != 1) return false;
    return i2c_read_timeout_us(BUS, dev, buf, n, false, TIMEOUT_US) == (int)n;
}
static uint8_t bcd(uint8_t v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); }
static uint8_t unbcd(uint8_t v) { return (uint8_t)(((v >> 4) * 10) + (v & 15)); }

typedef struct { int hour, minute, second, day, month, year; } RTC;
static bool rtc_set_once(void) {
    // 18 Oct 2026 Sunday, DS3231 weekday convention set to 1=Sunday.
    uint8_t p[] = {0, bcd(0), bcd(22), bcd(8), bcd(1), bcd(18), bcd(10), bcd(26)};
    return i2c_write_timeout_us(BUS, RTC_ADDR, p, sizeof(p), false, TIMEOUT_US) == (int)sizeof(p);
}
static bool rtc_read(RTC *r) {
    uint8_t x[7];
    if (!reg_read(RTC_ADDR, 0, x, sizeof(x))) return false;
    r->second = unbcd(x[0] & 0x7f);
    r->minute = unbcd(x[1] & 0x7f);
    r->hour = (x[2] & 0x40) ? (unbcd(x[2] & 0x1f) % 12 + ((x[2] & 0x20) ? 12 : 0))
                            : unbcd(x[2] & 0x3f);
    r->day = unbcd(x[4] & 0x3f);
    r->month = unbcd(x[5] & 0x1f);
    r->year = 2000 + unbcd(x[6]) + ((x[5] & 0x80) ? 100 : 0);
    return r->second <= 59 && r->minute <= 59 && r->hour <= 23 &&
           r->day >= 1 && r->day <= 31 && r->month >= 1 && r->month <= 12;
}

typedef struct {
    uint16_t T1, P1;
    int16_t T2,T3,P2,P3,P4,P5,P6,P7,P8,P9;
} BmpCal;
static BmpCal cal;
static uint16_t u16le(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static int16_t s16le(const uint8_t *p) { return (int16_t)u16le(p); }
static bool bmp_init(void) {
    uint8_t id, x[24];
    if (!reg_read(BMP_ADDR, 0xd0, &id, 1) || id != 0x58) return false;
    if (!reg_read(BMP_ADDR, 0x88, x, sizeof(x))) return false;
    cal.T1=u16le(x); cal.T2=s16le(x+2); cal.T3=s16le(x+4);
    cal.P1=u16le(x+6); cal.P2=s16le(x+8); cal.P3=s16le(x+10);
    cal.P4=s16le(x+12); cal.P5=s16le(x+14); cal.P6=s16le(x+16);
    cal.P7=s16le(x+18); cal.P8=s16le(x+20); cal.P9=s16le(x+22);
    if (cal.P1 == 0 || cal.T1 == 0) return false;
    // 1 sec standby, IIR filter x4; normal mode, temp x1, pressure x1.
    if (!reg_write(BMP_ADDR, 0xf5, 0xa0)) return false;
    if (!reg_write(BMP_ADDR, 0xf4, 0x27)) return false;
    sleep_ms(100);
    return true;
}
static bool bmp_read(float *temperature_c, float *pressure_hpa) {
    uint8_t b[6];
    if (!reg_read(BMP_ADDR, 0xf7, b, sizeof(b))) return false;
    int32_t ap = ((int32_t)b[0] << 12) | ((int32_t)b[1] << 4) | (b[2] >> 4);
    int32_t at = ((int32_t)b[3] << 12) | ((int32_t)b[4] << 4) | (b[5] >> 4);
    if (ap == 0x80000 || at == 0x80000) return false;
    // Bosch BST-BMP280-DS001 compensation, floating point (pressure in Pa).
    double v1 = ((double)at / 16384.0 - cal.T1 / 1024.0) * cal.T2;
    double z = ((double)at / 131072.0 - cal.T1 / 8192.0);
    double v2 = z * z * cal.T3;
    double t_fine = v1 + v2;
    double t = t_fine / 5120.0;
    v1 = t_fine / 2.0 - 64000.0;
    v2 = v1*v1*cal.P6 / 32768.0;
    v2 = v2 + v1*cal.P5*2.0;
    v2 = v2/4.0 + cal.P4*65536.0;
    v1 = (cal.P3*v1*v1/524288.0 + cal.P2*v1)/524288.0;
    v1 = (1.0 + v1/32768.0)*cal.P1;
    if (v1 == 0.0) return false;
    double p = 1048576.0 - ap;
    p = (p - v2/4096.0)*6250.0/v1;
    v1 = cal.P9*p*p/2147483648.0;
    v2 = p*cal.P8/32768.0;
    p = p + (v1 + v2 + cal.P7)/16.0;
    if (t < -40 || t > 85 || p < 30000 || p > 110000) return false;
    *temperature_c = (float)t;
    *pressure_hpa = (float)(p/100.0);
    return true;
}

// -------- Tiny, stand-alone SSD1306 128x64 I2C driver --------
static uint8_t framebuffer[OLED_W * OLED_H / 8];
static bool oled_command(uint8_t cmd) {
    uint8_t b[2] = {0x00, cmd};
    return i2c_write_timeout_us(BUS, OLED_ADDR, b, 2, false, TIMEOUT_US) == 2;
}
static bool oled_init(void) {
    static const uint8_t cmds[] = {
        0xae,0xd5,0x80,0xa8,0x3f,0xd3,0x00,0x40,0x8d,0x14,
        0x20,0x00,0xa1,0xc8,0xda,0x12,0x81,0x7f,0xd9,0xf1,
        0xdb,0x30,0xa4,0xa6,0x2e,0xaf
    };
    for (unsigned i=0; i<sizeof(cmds); ++i) if(!oled_command(cmds[i])) return false;
    return true;
}
static bool oled_update(void) {
    const uint8_t pos[] = {0x21,0,127,0x22,0,7};
    for (unsigned i=0; i<sizeof(pos); ++i) if(!oled_command(pos[i])) return false;
    for (unsigned i=0; i<sizeof(framebuffer); i+=16) {
        uint8_t payload[17]; payload[0] = 0x40;
        memcpy(payload+1, framebuffer+i, 16);
        if (i2c_write_timeout_us(BUS, OLED_ADDR, payload, sizeof(payload), false, TIMEOUT_US)
            != (int)sizeof(payload)) return false;
    }
    return true;
}
static void font5(char c, uint8_t p[5]) {
    memset(p, 0, 5);
    static const uint8_t numbers[10][5] = {
        {0x3e,0x51,0x49,0x45,0x3e},{0,0x42,0x7f,0x40,0},
        {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4b,0x31},
        {0x18,0x14,0x12,0x7f,0x10},{0x27,0x45,0x45,0x45,0x39},
        {0x3c,0x4a,0x49,0x49,0x30},{1,0x71,9,5,3},
        {0x36,0x49,0x49,0x49,0x36},{6,0x49,0x49,0x29,0x1e}
    };
    if(c>='0' && c<='9') { memcpy(p, numbers[c-'0'], 5); return; }
    static const struct { char c; uint8_t b[5]; } letters[] = {
        {'A',{0x7e,0x11,0x11,0x11,0x7e}}, {'B',{0x7f,0x49,0x49,0x49,0x36}},
        {'C',{0x3e,0x41,0x41,0x41,0x22}}, {'D',{0x7f,0x41,0x41,0x22,0x1c}},
        {'E',{0x7f,0x49,0x49,0x49,0x41}}, {'H',{0x7f,8,8,8,0x7f}},
        {'I',{0x41,0x41,0x7f,0x41,0x41}}, {'M',{0x7f,2,12,2,0x7f}},
        {'N',{0x7f,4,8,0x10,0x7f}}, {'O',{0x3e,0x41,0x41,0x41,0x3e}},
        {'P',{0x7f,9,9,9,6}}, {'R',{0x7f,9,0x19,0x29,0x46}},
        {'S',{0x46,0x49,0x49,0x49,0x31}}, {'T',{1,1,0x7f,1,1}},
        {'U',{0x3f,0x40,0x40,0x40,0x3f}}, {'X',{0x63,0x14,8,0x14,0x63}}
    };
    for(unsigned i=0;i<sizeof(letters)/sizeof(letters[0]);i++)
        if(letters[i].c==c) { memcpy(p, letters[i].b, 5); return; }
    if(c==':') p[2]=0x36;
    if(c=='.') p[2]=0x60;
    if(c=='-') p[2]=8;
    if(c=='/') {uint8_t b[5]={0x20,0x10,8,4,2}; memcpy(p,b,5);}
}
static void oled_text(int x, int page, const char *s) {
    if(page < 0 || page >= 8) return;
    while(*s && x <= OLED_W-6) {
        uint8_t p[5]; font5(*s++,p);
        for(int j=0;j<5;j++) framebuffer[page*OLED_W + x+j] = p[j];
        x+=6;
    }
}

int main(void) {
    stdio_init_all();
    i2c_init(BUS, BUS_HZ);
    gpio_set_function(SDA_PIN, GPIO_FUNC_I2C);
    gpio_set_function(SCL_PIN, GPIO_FUNC_I2C);
    sleep_ms(2000);
    printf("MASTER: DS3231(0x68), BMP280(0x76), OLED(0x%02X), 100 kHz\n", OLED_ADDR);
#if SET_RTC_ON_BOOT
    printf("RTC setting: %s\n", rtc_set_once()?"OK":"FAILED");
#endif
    bool bmp_ok = bmp_init();
    bool oled_ok = oled_init();
    printf("BMP280 init: %s, OLED init: %s\n",bmp_ok?"OK":"FAILED",oled_ok?"OK":"FAILED");
    while(true) {
        RTC r; float t=0, p=0;
        bool rtc_ok=rtc_read(&r);
        bool measurement_ok = bmp_ok && bmp_read(&t,&p);
        if(rtc_ok) printf("RTC %02d:%02d:%02d %02d/%02d/%04d\n",r.hour,r.minute,r.second,r.day,r.month,r.year);
        else printf("RTC READ FAILED\n");
        if(measurement_ok) printf("BMP280: %.2f C, %.2f hPa\n",t,p);
        else printf("BMP280 READ FAILED\n");
        // Optional second USB data source for the GUI (COM port of Master Pico).
        // Clearly labelled MASTER telemetry, distinct from passive analyzer captures.
        if (rtc_ok && measurement_ok) {
            printf("{\"type\":\"sensor_telemetry\",\"version\":1,"
                   "\"source\":\"master\",\"rtc\":\"%04d-%02d-%02dT%02d:%02d:%02d\","
                   "\"temperature_c\":%.2f,\"pressure_hpa\":%.2f}\n",
                   r.year,r.month,r.day,r.hour,r.minute,r.second,t,p);
        }
        if(oled_ok) {
            char line[32]; memset(framebuffer,0,sizeof(framebuffer));
            oled_text(0,0,"I2C MONITOR");
            if(rtc_ok) {
                snprintf(line,sizeof(line),"TIME %02d:%02d:%02d",r.hour,r.minute,r.second);
                oled_text(0,2,line);
                snprintf(line,sizeof(line),"DATE %02d/%02d/%04d",r.day,r.month,r.year);
                oled_text(0,3,line);
            } else oled_text(0,2,"RTC ERROR");
            if(measurement_ok) {
                snprintf(line,sizeof(line),"TEMP %.2f C",t); oled_text(0,5,line);
                snprintf(line,sizeof(line),"PRESS %.2f HPA",p); oled_text(0,6,line);
            } else oled_text(0,5,"BMP ERROR");
            if(!oled_update()) { oled_ok=false; printf("OLED WRITE FAILED\n"); }
        }
        sleep_ms(1000);
    }
}
