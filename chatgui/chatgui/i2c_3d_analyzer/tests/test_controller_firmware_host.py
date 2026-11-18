"""Compile the real controller firmware (firmware/native_rp2040_master/I2C.c, identical
to the flashed VS Code project I2C/I2C/I2C.c) on the host against stub Pico SDK
headers and fake I²C targets, then check the bus transactions it issues.

The firmware's register-level RP2040 layer (hal_transfer, START_DET, MST_ACTIVITY)
is replaced by I2C_HOST_SIM fakes below, which can inject arbitration losses,
START conditions from another master, a stuck SDA line or another master clocking.

This is a host simulation of the firmware's I²C call sequence and multi-master
policy. It does NOT exercise the RP2040 I2C peripheral, electrical timing, USB
or real wiring.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware/native_rp2040_master/I2C.c"

STDLIB_H = r'''
#pragma once
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#define PICO_ERROR_GENERIC -1
#define PICO_ERROR_TIMEOUT -2
#define GPIO_FUNC_I2C 3
#define GPIO_IN false
#define GPIO_OUT true
typedef uint64_t absolute_time_t;
extern uint64_t fake_us;
void stdio_init_all(void);
int getchar_timeout_us(uint32_t us);
void sleep_us(uint64_t us);
void sleep_ms(uint32_t ms);
void busy_wait_us(uint64_t us);
static inline uint64_t time_us_64(void) { return fake_us; }
static inline absolute_time_t get_absolute_time(void) { return fake_us; }
static inline absolute_time_t make_timeout_time_ms(uint32_t ms) { return fake_us + 1000ull * ms; }
static inline absolute_time_t delayed_by_ms(absolute_time_t t, uint32_t ms) { return t + 1000ull * ms; }
static inline bool time_reached(absolute_time_t t) { return fake_us >= t; }
void gpio_init(unsigned pin); void gpio_set_dir(unsigned pin, bool out); void gpio_put(unsigned pin, bool v);
bool gpio_get(unsigned pin); void gpio_set_function(unsigned pin, int fn); void gpio_pull_up(unsigned pin);
'''
I2C_H = r'''
#pragma once
#include "pico/stdlib.h"
typedef struct { int id; } i2c_inst_t;
extern i2c_inst_t i2c0_inst;
#define i2c0 (&i2c0_inst)
unsigned i2c_init(i2c_inst_t *i, unsigned hz); void i2c_deinit(i2c_inst_t *i);
unsigned i2c_set_baudrate(i2c_inst_t *i, unsigned hz);
'''
FAKE_C = r'''
#include "pico/stdlib.h"
#include "hardware/i2c.h"
uint64_t fake_us = 0;
i2c_inst_t i2c0_inst;
static uint64_t limit_us;
static uint8_t regs[128][256];
static uint8_t ptr[128];
static bool present[128];
static int recoveries;
void stdio_init_all(void) { const char *l = getenv("FAKE_LIMIT_MS"); limit_us = 1000ull * (l ? atoi(l) : 12000);
    present[0x68] = present[0x76] = present[0x3C] = true;
    const char *osf = getenv("FAKE_OSF");
    uint8_t rtc[] = {0x05, 0x22, 0x08, 0x01, 0x18, 0x10, 0x26};
    memcpy(regs[0x68], rtc, 7); regs[0x68][0x0F] = (osf && *osf == '1') ? 0x80 : 0x00;
    const char *bmp = getenv("FAKE_BMP_ID"); regs[0x76][0xD0] = bmp ? (uint8_t)strtol(bmp, 0, 16) : 0x58;
    uint8_t cal[24] = {0x70,0x6B,0x43,0x67,0x18,0xFC,0x7D,0x8E,0x43,0xD6,0xD0,0x0B,0x27,0x0B,0x8C,0x00,0xF9,0xFF,0x8C,0x3C,0xF8,0xC6,0x70,0x17};
    memcpy(&regs[0x76][0x88], cal, 24);
    uint8_t raw[6] = {0x65,0x5D,0x40,0x7E,0xF4,0x00}; memcpy(&regs[0x76][0xF7], raw, 6);
    const char *absent = getenv("FAKE_ABSENT"); if (absent) present[strtol(absent, 0, 16)] = false; }
static int clock_pulses;
/* ---- FAKE_PEER=1: the OTHER master, following the turn protocol.  It takes its turn (bus busy for
 * FAKE_PEER_TURN_MS) FAKE_PEER_GAP_MS after our last transfer, or first at FAKE_PEER_FIRST_AT_MS, and stops
 * for good at FAKE_PEER_STOP_AT_MS.  Its turns are printed at exit as "FAKE_PEER_TURN start end". */
static int peer_on = -1;
static uint64_t peer_gap, peer_len, peer_stop, peer_next = ~0ull, peer_s = ~0ull, peer_e, det_clear_at;
static uint64_t pw_s[512], pw_e[512]; static int npw;
static uint64_t env_ms(const char *k);
static void peer_update(void) {
    if (peer_on < 0) { const char *p = getenv("FAKE_PEER"); peer_on = p && *p == '1';
        const char *g = getenv("FAKE_PEER_GAP_MS"), *l = getenv("FAKE_PEER_TURN_MS"), *f = getenv("FAKE_PEER_FIRST_AT_MS");
        peer_gap = 1000ull * (g ? atoi(g) : 4000); peer_len = 1000ull * (l ? atoi(l) : 160);
        peer_stop = env_ms("FAKE_PEER_STOP_AT_MS"); if (f) peer_next = 1000ull * atoi(f); }
    if (peer_on && peer_next != ~0ull && fake_us >= peer_next) {
        if (peer_next < peer_stop) { peer_s = peer_next; peer_e = peer_next + peer_len;
            if (npw < 512) { pw_s[npw] = peer_s; pw_e[npw] = peer_e; npw++; } }
        peer_next = ~0ull; } }
static bool peer_active(void) { peer_update(); return peer_on && peer_s != ~0ull && fake_us >= peer_s && fake_us < peer_e; }
static void advance(uint64_t us) { fake_us += us; if (fake_us > limit_us) {
    for (int k = 0; k < npw; k++) printf("FAKE_PEER_TURN %llu %llu\n", (unsigned long long)pw_s[k], (unsigned long long)pw_e[k]);
    printf("FAKE recoveries=%d pulses=%d\n", recoveries, clock_pulses); fflush(stdout); exit(0); } }
void sleep_us(uint64_t us) { advance(us); }
void busy_wait_us(uint64_t us) { advance(us ? us : 1); }
void sleep_ms(uint32_t ms) { advance(1000ull * ms); }
static uint64_t env_ms(const char *k) { const char *v = getenv(k); return v ? 1000ull * strtoull(v, 0, 10) : ~0ull; }
void gpio_init(unsigned p) { (void)p; }
/* recovery drives SCL (GP5) as an output: count the clock pulses it generates */
void gpio_set_dir(unsigned p, bool o) { if (p == 5 && o) clock_pulses++; }
void gpio_put(unsigned p, bool v) { (void)p; (void)v; }
/* Line levels.  Default: idle bus, both HIGH.  FAKE_SDA_STUCK_AT_MS: a target holds SDA LOW
 * until the first recovery clock pulse.  FAKE_OTHER_MASTER_AT_MS: another master owns the
 * bus from then on (SDA LOW, SCL toggling every 5 us = 100 kHz). */
bool gpio_get(unsigned p) {
    if (fake_us >= env_ms("FAKE_OTHER_MASTER_AT_MS")) return p == 5 ? ((fake_us / 5) & 1) : false;
    if (peer_active()) return p == 5 ? ((fake_us / 5) & 1) : ((fake_us / 37) & 1);    /* the peer's transfers */
    if (fake_us >= env_ms("FAKE_SDA_STUCK_AT_MS") && clock_pulses == 0) return p != 4;
    return true; }
void gpio_set_function(unsigned p, int f) { (void)p; (void)f; } void gpio_pull_up(unsigned p) { (void)p; }
/* Pico SDK i2c_set_baudrate() divider arithmetic, clk_sys = 125 MHz (RP2040 default). */
static unsigned baud;
static unsigned sdk_rate(unsigned hz) { unsigned f = 125000000u, period = (f + hz / 2) / hz; return f / period; }
unsigned i2c_init(i2c_inst_t *i, unsigned hz) { (void)i; baud = sdk_rate(hz); return baud; }
unsigned i2c_set_baudrate(i2c_inst_t *i, unsigned hz) { (void)i; baud = sdk_rate(hz); return baud; }
/* Scripted USB input: FAKE_STDIN delivered once fake time passes FAKE_CMD_AT_MS ('|' = newline). */
/* '~' in FAKE_STDIN: the rest arrives 300 ms of fake time later (a GUI that waits for each step's packet). */
static const char *cmd; static size_t cmd_pos; static uint64_t cmd_hold;
int getchar_timeout_us(uint32_t us) { (void)us;
    if (!cmd) { cmd = getenv("FAKE_STDIN"); if (!cmd) cmd = ""; }
    const char *at = getenv("FAKE_CMD_AT_MS");
    if (fake_us < 1000ull * (at ? atoi(at) : 0) || !cmd[cmd_pos] || fake_us < cmd_hold) return PICO_ERROR_TIMEOUT;
    char c = cmd[cmd_pos++];
    if (c == '~') { cmd_hold = fake_us + 300000; return PICO_ERROR_TIMEOUT; }
    return c == '|' ? '\n' : c; }
void i2c_deinit(i2c_inst_t *i) { (void)i; recoveries++; }
static uint64_t bus_us(size_t bytes) { return (uint64_t)(bytes + 1) * 90 + 20; }   /* 9 clocks/byte @100 kHz */
/* ---- I2C_HOST_SIM hardware layer (replaces the RP2040 register code in I2C.c) */
static unsigned busy_starts_seen;
void hal_start_det_clear(void) { det_clear_at = fake_us + 1; }
/* FAKE_BUSY_STARTS=N: another master's START lands inside the first N idle windows.
 * FAKE_PEER: START_DET latches any START of the peer's turn (it starts STARTs during its whole turn). */
bool hal_start_det_seen(void) { const char *n = getenv("FAKE_BUSY_STARTS");
    if (peer_active()) return true;
    if (peer_on > 0 && peer_s != ~0ull && peer_s <= fake_us && peer_e >= det_clear_at) return true;
    return n && busy_starts_seen++ < (unsigned)atoi(n); }
bool hal_controller_busy(void) { return false; }
uint32_t hal_status(void) { return 0; }
static int tar_set = -1;
void hal_set_target(uint8_t a) { tar_set = a; }
uint32_t hal_random(void) { return (uint32_t)rand(); }
static unsigned transfers;
/* Arbitration injection: FAKE_ARB_EVERY=N loses every Nth attempt; FAKE_ARB_ADDR=hex loses
 * every attempt to that address.  A lost attempt is invisible on the bus: nothing printed. */
static bool lose_arbitration(uint8_t a) {
    const char *every = getenv("FAKE_ARB_EVERY"), *addr = getenv("FAKE_ARB_ADDR");
    transfers++;
    if (addr && strtol(addr, 0, 16) == a) return true;
    return every && fake_us > 2000000 && transfers % (unsigned)atoi(every) == 0; }
int hal_transfer(uint8_t a, const uint8_t *w, size_t wlen, uint8_t *r, size_t rlen,
                 size_t *wdone, size_t *rdone, uint32_t timeout_us, uint32_t *src) {
    (void)timeout_us; *wdone = *rdone = 0; *src = 0;
    /* IC_TAR must already be set (xfer() does it BEFORE the idle check, never between idle check and START) */
    if (tar_set != a) printf("FAKE_TAR_NOT_SET_BEFORE_TRANSFER want=%02X had=%d\n", a, tar_set);
    if (peer_active()) printf("FAKE_COLLISION %llu %02X\n", (unsigned long long)fake_us, a);   /* inside the peer's turn */
    if (peer_on > 0) peer_next = fake_us + peer_gap + bus_us(wlen + rlen + 1);  /* peer goes 4 s after our last STOP */
    if (lose_arbitration(a)) { advance(40); *src = 0x1000; return 3; }      /* ARB_LOST */
    { static int left = -1; if (left < 0) { const char *n = getenv("FAKE_TIMEOUTS"); left = n ? atoi(n) : 0; }
      if (left > 0 && fake_us > 2000000) { left--; advance(15000); return 4; } }  /* idle-bus timeout */
    if (wlen) {
        advance(bus_us(present[a] ? wlen : 0));
        printf("I2C %llu %u W %02X %s", (unsigned long long)fake_us, baud, a, present[a] ? "ACK" : "NACK");
        if (present[a]) for (size_t k = 0; k < wlen; k++) printf(" %02X", w[k]);
        printf("%s\n", (rlen && present[a]) ? " Sr" : " P");
        if (!present[a]) { *src = 0x1; return 1; }                          /* ADDR_NACK */
        ptr[a] = w[0]; for (size_t k = 1; k < wlen; k++) regs[a][(uint8_t)(w[0] + k - 1)] = w[k];
        *wdone = wlen;
    }
    if (rlen) {
        advance(bus_us(present[a] ? rlen : 0));
        printf("I2C %llu %u R %02X %s %u P\n", (unsigned long long)fake_us, baud, a, present[a] ? "ACK" : "NACK",
               (unsigned)rlen);
        if (!present[a]) { *src = 0x1; return 1; }
        for (size_t k = 0; k < rlen; k++) r[k] = regs[a][(uint8_t)(ptr[a] + k)];
        ptr[a] = (uint8_t)(ptr[a] + rlen); *rdone = rlen;
    }
    return 0; }
'''


def run_firmware(tmp_path, limit_ms=12000, master_id=1, schedule="free", stress=False, **env):
    """schedule="free": the free-running build (-DSCHEDULE_TURNS=0) that the tests above were written for;
    schedule="turns": the default build (strict turn taking, 2026-11-17)."""
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("Host GCC not installed")
    inc = tmp_path / "inc"
    (inc / "pico").mkdir(parents=True, exist_ok=True)
    (inc / "hardware").mkdir(exist_ok=True)
    (inc / "pico/stdlib.h").write_text(STDLIB_H)
    (inc / "hardware/i2c.h").write_text(I2C_H)
    (tmp_path / "fake.c").write_text(FAKE_C)
    exe = tmp_path / f"controller_m{master_id}_{schedule}{'_stress' if stress else ''}.exe"
    defs = [] if schedule == "turns" else ["-DSCHEDULE_TURNS=0"]
    if stress:
        defs.append("-DMM_STRESS")
    if not exe.exists():
        built = subprocess.run([gcc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-DI2C_HOST_SIM",
                                f"-DMASTER_ID={master_id}", *defs, f"-I{inc}", str(FIRMWARE),
                                str(tmp_path / "fake.c"), "-o", str(exe), "-lm"], capture_output=True, text=True)
        assert built.returncode == 0, built.stderr
    import os
    e = dict(os.environ, FAKE_LIMIT_MS=str(limit_ms), **{f"FAKE_{k.upper()}": v for k, v in env.items()})
    out = subprocess.run([str(exe)], capture_output=True, text=True, env=e, timeout=60).stdout
    return out


def transactions(out):
    """Group the firmware's I2C calls into bus transactions (S ... [Sr ...] P)."""
    txns, cur = [], []
    for line in out.splitlines():
        m = re.match(r"I2C (\d+) (\d+) ([WR]) ([0-9A-F]{2}) (ACK|NACK)(.*) (Sr|P)$", line)
        if not m:
            continue
        cur.append(dict(t=int(m[1]), baud=int(m[2]), rw=m[3], addr=int(m[4], 16), ack=m[5] == "ACK",
                        rest=m[6].split()))
        if m[7] == "P":
            txns.append(cur); cur = []
    return txns


def test_controller_compiles_and_has_no_bus_scan_by_default(tmp_path):
    out = run_firmware(tmp_path, 12000)
    txns = transactions(out)
    addrs = {f["addr"] for t in txns for f in t}
    assert addrs == {0x68, 0x76, 0x3C}                    # no 0x08..0x77 probe sweep
    assert not any(not f["ack"] for t in txns for f in t)  # all configured targets present -> no NACK
    assert "FAKE recoveries=0" in out


def test_controller_rtc_read_is_one_combined_transaction_with_status(tmp_path):
    txns = transactions(run_firmware(tmp_path, 5500))   # firmware waits 2 s for USB first
    rtc = [t for t in txns if t[0]["addr"] == 0x68]
    assert len(rtc) >= 3
    for t in rtc:
        assert [(f["rw"], f["rest"]) for f in t] == [("W", ["00"]), ("R", ["16"])]   # S W 00 Sr R x16 P


def test_controller_schedule_bmp_rtc_oled_identity(tmp_path):
    txns = transactions(run_firmware(tmp_path, 13000))   # 2 s start-up + 11 s of the main loop
    bmp_data = [t for t in txns if t[0]["addr"] == 0x76 and t[0]["rest"] == ["F7"]]
    ident = [t for t in txns if t[0]["addr"] == 0x76 and t[0]["rest"] == ["D0"]]
    oled_data = [t for t in txns if t[0]["addr"] == 0x3C and t[0]["rest"][0] == "40"]
    assert 53 <= len(bmp_data) <= 57                       # BMP_PERIOD_MS 200 over 11 s
    assert len(ident) == 2                                 # boot + refresh 10 s after the loop starts
    assert len(oled_data) % 64 == 0 and len(oled_data) >= 64 * 11
    assert all(len(t[0]["rest"]) == 17 for t in oled_data)


def test_controller_accepts_bme280_chip_id(tmp_path):
    txns = transactions(run_firmware(tmp_path, 3000, bmp_id="60"))
    assert any(t[0]["addr"] == 0x76 and t[0]["rest"] == ["F7"] for t in txns)


def test_controller_reports_rtc_oscillator_stop_as_invalid(tmp_path):
    out = run_firmware(tmp_path, 2500, osf="1")
    assert "oscillator stopped (OSF)" in out and "RTC 08:22" not in out
    assert '"type":"sensor_telemetry"' not in out


def test_absent_target_is_retried_slowly_and_nack_does_not_trigger_recovery(tmp_path):
    out = run_firmware(tmp_path, 10000, absent="76")
    txns = transactions(out)
    probes = [t for t in txns if t[0]["addr"] == 0x76]
    assert all(not t[0]["ack"] for t in probes)            # every probe of the absent part NACKs (truthful)
    assert len(probes) <= 6                                # boot + one retry per REDETECT_PERIOD_MS (2 s)
    assert "FAKE recoveries=0" in out                      # NACK is not a stuck bus


def scl_replies(out):
    import json
    return [json.loads(l) for l in out.splitlines() if l.startswith('{"type":"scl_config"')]


def test_scl_boot_report_and_query(tmp_path):
    out = run_firmware(tmp_path, 3000, stdin="SCL?|", cmd_at_ms="2500")
    r = scl_replies(out)
    assert r[0]["result"] == "boot" and r[0]["requested_hz"] == 100000 and r[0]["applied_hz"] == 100000
    assert r[1]["result"] == "current" and r[1]["supported_hz"] == [100000, 400000]


def test_scl_400k_applied_between_transactions_and_reported(tmp_path):
    out = run_firmware(tmp_path, 4500, stdin="SCL 400000|", cmd_at_ms="3000")
    r = scl_replies(out)
    applied = [x for x in r if x["result"] == "applied"]
    assert applied and applied[0]["requested_hz"] == 400000
    assert applied[0]["applied_hz"] == 399361          # SDK divider at 125 MHz: 125e6 / 313
    assert "verify on the bus" in applied[0]["reason"]  # the reply is not a bus measurement
    frames = [f for t in transactions(out) for f in t]
    before = [f for f in frames if f["t"] < 3_000_000]
    after = [f for f in frames if f["t"] > 3_200_000]
    assert before and all(f["baud"] == 100000 for f in before)
    assert after and all(f["baud"] == 399361 for f in after)
    for t in transactions(out):                          # never changed inside a transaction
        assert len({f["baud"] for f in t}) == 1


@pytest.mark.parametrize("bad", ["1000000", "3400000", "250000", "abc", "400000x"])
def test_scl_unsupported_rate_rejected_and_unchanged(tmp_path, bad):
    out = run_firmware(tmp_path, 3500, stdin=f"SCL {bad}|", cmd_at_ms="2500")
    r = scl_replies(out)
    assert [x["result"] for x in r] == ["boot", "rejected"] and r[1]["applied_hz"] == 100000
    assert all(f["baud"] == 100000 for t in transactions(out) for f in t)


def rtc_replies(out):
    import json
    return [json.loads(l) for l in out.splitlines() if l.startswith('{"type":"rtc_set"')]


def test_time_command_sets_ds3231_clears_osf_and_time_becomes_valid(tmp_path):
    # RTC starts with the oscillator-stop flag set, as measured on the real bench 2026-11-09.
    out = run_firmware(tmp_path, 6000, osf="1", stdin="TIME 2026-11-09T10:15:30|", cmd_at_ms="3000")
    r = rtc_replies(out)
    assert r == [{"type": "rtc_set", "source": "controller", "master_id": 1, "result": "applied",
                  "time": "2026-11-09T10:15:30", "weekday_1_sunday": 2}]          # 9 Nov 2026 = Monday
    lines = out.splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith('{"type":"rtc_set"'))
    before, after = lines[:i], lines[i + 1:]
    assert any("oscillator stopped (OSF)" in l for l in before)
    assert not any("oscillator stopped" in l for l in after)
    assert any(l.startswith("RTC 10:15:30 09/11/2026") for l in after)          # fake RTC does not tick
    # the bus writes: S D0 00 30 15 10 02 09 11 26 P, then read/clear of status 0x0F
    w = [f for t in transactions(out) for f in t if f["addr"] == 0x68 and f["rw"] == "W"]
    assert any(f["rest"] == ["ACK", "00", "30", "15", "10", "02", "09", "11", "26"][1:] for f in w)
    assert any(f["rest"] == ["0F", "00"] for f in w)                               # OSF bit cleared


@pytest.mark.parametrize("bad", ["2026-13-01T00:00:00", "2026-02-30T00:00:00", "2025-02-29T00:00:00",
                                 "2100-01-01T00:00:00", "2026-11-09T24:00:00", "2026-11-09T10:15:30x",
                                 "now"])
def test_time_command_rejects_invalid_input_and_leaves_rtc_untouched(tmp_path, bad):
    out = run_firmware(tmp_path, 4000, osf="1", stdin=f"TIME {bad}|", cmd_at_ms="3000")
    assert [x["result"] for x in rtc_replies(out)] == ["rejected"]
    assert not any(f["addr"] == 0x68 and f["rw"] == "W" and len(f["rest"]) > 1
                   for t in transactions(out) for f in t)                         # no RTC register write
    assert "oscillator stopped (OSF)" in out.splitlines()[-2] or "oscillator stopped" in out


def test_leap_day_accepted(tmp_path):
    out = run_firmware(tmp_path, 4000, stdin="TIME 2028-02-29T23:59:58|", cmd_at_ms="3000")
    assert rtc_replies(out)[0]["result"] == "applied" and rtc_replies(out)[0]["weekday_1_sunday"] == 3  # Tuesday


# ------------------------------------------------------------------ multi-master (2026-11-14)
def json_lines(out, kind):
    import json
    return [json.loads(l) for l in out.splitlines() if l.startswith('{"type":"%s"' % kind)]


def test_hello_identifies_master_role_and_schedule(tmp_path):
    h1 = json_lines(run_firmware(tmp_path, 2500), "hello")[0]
    assert h1["role"] == "controller" and h1["master_id"] == 1 and h1["drives_oled"] is True
    assert h1["periods_ms"] == {"bmp": 200, "rtc": 1000, "identity": 10000}
    h2 = json_lines(run_firmware(tmp_path, 2500, master_id=2), "hello")[0]
    assert h2["master_id"] == 2 and h2["drives_oled"] is False and h2["offset_ms"] == 97
    assert h1["oled_cmd_writes"] is False and h2["oled_cmd_writes"] is True
    assert h2["periods_ms"]["bmp"] == 330 and h2["idle_window_us"] > h1["idle_window_us"]
    assert h2["holdoff_us"] > h1["idle_window_us"] and h1["holdoff_us"] > h2["idle_window_us"]   # round robin


def test_master2_only_writes_one_oled_command_per_cycle_and_is_staggered(tmp_path):
    out = run_firmware(tmp_path, 8000, master_id=2)
    txns = transactions(out)
    assert {f["addr"] for t in txns for f in t} == {0x68, 0x76, 0x3C}
    oled = [t for t in txns if t[0]["addr"] == 0x3C]                  # framebuffer stays Master 1's: commands only
    assert oled and all(len(t) == 1 and t[0]["rw"] == "W" and t[0]["rest"] == ["00", "A6"] for t in oled)
    o_gaps = [b[0]["t"] - a[0]["t"] for a, b in zip(oled, oled[1:])]
    assert o_gaps and all(1_090_000 <= g <= 1_110_000 for g in o_gaps)   # once per 1.1 s display cycle
    first = min(t[0]["t"] for t in txns)
    assert first > 2_097_000                                          # 2 s USB wait + 97 ms offset
    bmp = [t[0]["t"] for t in txns if t[0]["addr"] == 0x76 and t[0]["rest"] == ["F7"]]
    gaps = [b - a for a, b in zip(bmp, bmp[1:])]
    assert gaps and all(320_000 <= g <= 340_000 for g in gaps)        # 330 ms, not Master 1's 200 ms grid
    assert json_lines(out, "sensor_telemetry")[0]["master_id"] == 2


def test_shared_bmp280_is_configured_only_once(tmp_path):
    out = run_firmware(tmp_path, 3000)
    writes = [f for t in transactions(out) for f in t if f["addr"] == 0x76 and f["rw"] == "W" and len(f["rest"]) == 2]
    assert [f["rest"] for f in writes] == [["F5", "A0"], ["F4", "27"]]  # boot only; skipped once configured


def test_arbitration_loss_is_retried_not_nack_and_target_stays_online(tmp_path):
    out = run_firmware(tmp_path, 6000, arb_every="4", stdin="TXLOG 1|", cmd_at_ms="2050")
    arb = json_lines(out, "arb_lost")
    assert arb and all(a["retry"] is True and a["abort_source"] == 0x1000 for a in arb)
    assert all(a["backoff_us"] >= 150 for a in arb)                   # board-specific back-off
    txn = json_lines(out, "txn")
    retried = [t for t in txn if t["arb_lost"]]
    assert retried and all(t["result"] == "ok" and t["attempts"] == t["arb_lost"] + 1 for t in retried)
    assert not any(t["result"] in ("addr_nack", "data_nack") for t in txn)   # never reported as a NACK
    stats = json_lines(out, "master_stats")[-1]
    assert 0 < stats["arb_lost"] <= len(arb) and stats["addr_nack"] == 0 and stats["gave_up"] == 0
    assert stats["recoveries"] == 0 and "FAKE recoveries=0 pulses=0" in out  # no bus recovery
    assert "BMP280 not available" not in out.split('"txlog"')[-1]          # sensor never marked offline
    assert len(json_lines(out, "sensor_telemetry")) >= 3
    # every logical transfer reaches the bus exactly once (a lost attempt is invisible);
    # TXLOG takes effect between transfers, so count from the first logged transfer
    on_bus = [t for t in transactions(out) if t[0]["t"] > txn[0]["t_us"]]
    assert len(on_bus) == len(txn)


def test_arbitration_always_lost_gives_up_without_recovery(tmp_path):
    out = run_firmware(tmp_path, 4500, arb_addr="76")
    arb = [a for a in json_lines(out, "arb_lost") if a["addr"] == 0x76]
    assert arb and arb[-1]["attempt"] == 6 and arb[-1]["retry"] is False
    stats = json_lines(out, "master_stats")[-1]
    assert stats["gave_up"] >= 1 and stats["addr_nack"] == 0 and stats["recoveries"] == 0
    assert stats["retries"] == 5 * stats["gave_up"]
    assert not any(f["addr"] == 0x76 for t in transactions(out) for f in t)   # nothing of ours on the bus


def test_start_from_other_master_defers_our_transfer(tmp_path):
    out = run_firmware(tmp_path, 4200, busy_starts="5")
    assert json_lines(out, "master_stats")[-1]["busy_waits"] >= 1
    assert json_lines(out, "master_stats")[-1]["busy_timeouts"] == 0
    assert transactions(out)                                          # traffic continues afterwards


def test_stuck_sda_is_recovered_with_clock_pulses(tmp_path):
    out = run_firmware(tmp_path, 4500, sda_stuck_at_ms="3000")
    rec = json_lines(out, "bus_recovery")
    assert rec and "no SCL edges" in rec[0]["reason"]
    assert "pulses=0" not in out.splitlines()[-1]
    assert any(t[0]["t"] > rec[0]["t_us"] for t in transactions(out))  # bus usable again


def test_other_master_clocking_is_never_treated_as_stuck(tmp_path):
    out = run_firmware(tmp_path, 4500, other_master_at_ms="3000")
    assert not json_lines(out, "bus_recovery")
    assert out.splitlines()[-1].endswith("pulses=0")                  # never fought the other master
    stats = json_lines(out, "master_stats")[-1]
    assert stats["busy_timeouts"] >= 1 and stats["timeouts"] == 0 and stats["recoveries"] == 0


def test_timeout_on_idle_bus_resets_controller_and_retries_transfer(tmp_path):
    out = run_firmware(tmp_path, 6000, timeouts="2")
    resets = json_lines(out, "controller_reset")
    assert len(resets) == 2 and all("retried" in r["reason"] for r in resets)
    st = json_lines(out, "master_stats")[-1]
    assert st["timeouts"] == 2 and st["controller_resets"] == 2
    assert st["recoveries"] == 0 and "FAKE recoveries=2 pulses=0" in out        # re-init only: no clock pulses
    assert len(json_lines(out, "sensor_telemetry")) >= 3                         # transfers still completed
    assert not any(not f["ack"] for t in transactions(out) for f in t)


def test_master_stats_report_last_timeout_diagnostics(tmp_path):
    st = json_lines(run_firmware(tmp_path, 4000, timeouts="1"), "master_stats")[-1]
    lt = st["last_timeout"]
    assert lt["t_us"] > 2_000_000 and lt["addr"] in (0x68, 0x76, 0x3C) and lt["sda"] == 1 and lt["scl"] == 1


def test_target_address_is_set_before_the_idle_check_for_every_transfer(tmp_path):
    for mid in (1, 2):
        out = run_firmware(tmp_path, 6000, master_id=mid, arb_every="5")      # incl. retries after arbitration loss
        assert "FAKE_TAR_NOT_SET_BEFORE_TRANSFER" not in out
        assert {f["addr"] for t in transactions(out) for f in t} >= {0x68, 0x76}


# ------------------------------------------------------------------ strict turn taking (SCHEDULE_TURNS, 2026-11-17)
ORDER = {0x68: 0, 0x76: 1, 0x3C: 2}                     # RTC -> BMP280 -> OLED inside every turn


def turn_runs(out):
    """[(start_us, end_us, record_start, record_end)] of this firmware's turns."""
    starts, ends = json_lines(out, "turn_start"), json_lines(out, "turn_end")
    return [(s["t_us"], e["t_us"], s, e) for s, e in zip(starts, ends)]


def peer_turns(out):
    return [tuple(map(int, l.split()[1:3])) for l in out.splitlines() if l.startswith("FAKE_PEER_TURN")]


GLYPH = {"M": ["7F", "02", "0C", "02", "7F"], "1": ["00", "42", "7F", "40", "00"], "2": ["42", "61", "51", "49", "46"],
         "T": ["01", "01", "7F", "01", "01"], "P": ["7F", "09", "09", "09", "06"]}


def is_oled_line_write(t, mid):
    """2.6-live OLED step: ONE write - [80 cmd] x 8 (horizontal mode, columns 0..77, this master's 3 pages), 40, then
    3 x 78 text columns starting "M<mid> ", "T ", "P "."""
    r = t[0]["rest"]
    p0 = 0 if mid == 1 else 4
    head = ["80", "20", "80", "00", "80", "21", "80", "00", "80", "4D", "80", "22", "80", f"{p0:02X}", "80", f"{p0 + 2:02X}",
            "40"]
    return (len(t) == 1 and t[0]["rw"] == "W" and len(r) == 17 + 3 * 78 and r[:17] == head
            and r[17:22] == GLYPH["M"] and r[23:28] == GLYPH[str(mid)]
            and r[17 + 78:17 + 83] == GLYPH["T"] and r[17 + 156:17 + 161] == GLYPH["P"])


def test_turns_hello_reports_the_schedule(tmp_path):
    for mid in (1, 2):
        h = json_lines(run_firmware(tmp_path, 2500, master_id=mid, schedule="turns"), "hello")[0]
        assert h["fw_version"] == "2.6-live" and h["schedule"] == "turns" and h["turn_gap_ms"] == 4000
        assert h["conduct"] is False and "CONDUCT" in h["review_cmds"] and "DO" in h["review_cmds"]
        assert "DO CAL" in h["review_cmds"] and h["oled_layout"]["columns"] == [0, 77]
        assert h["oled_layout"]["master1_pages"] == [0, 2] and h["oled_layout"]["master2_pages"] == [4, 6]
        assert h["turn_order"] == [0x68, 0x76, 0x3C] and h["first_turn_owner"] == 1
        assert h["drives_oled"] is True and h["oled_cmd_writes"] is False      # both masters draw, in their turn
        assert h["peer_timeout_ms"] == 13500 and h["turn_takeover_ms"] == 4500


def test_turns_master1_goes_first_then_strict_alternation_with_4s_idle_gaps(tmp_path):
    out = run_firmware(tmp_path, 40000, master_id=1, schedule="turns", peer="1")
    mine, peer = turn_runs(out), peer_turns(out)
    assert "FAKE_COLLISION" not in out and "FAKE recoveries=0 pulses=0" in out
    assert mine[0][2]["reason"] == "first_turn" and mine[0][0] >= 2_000_000 + 4_500_000      # boot + 4.5 s idle
    assert len(mine) >= 4 and len(peer) >= 3
    seq = sorted([(s, e, "me") for s, e, *_ in mine] + [(s, e, "peer") for s, e in peer])
    assert [who for *_, who in seq] == ["me", "peer"] * (len(seq) // 2) + ["me"] * (len(seq) % 2)   # M1, M2, M1 ...
    for (s0, e0, _), (s1, e1, _) in zip(seq, seq[1:]):
        assert e0 <= s1                                                     # turns never overlap
    for s, e, rs, re_ in mine[1:]:
        prev_peer_end = max(pe for ps, pe in peer if pe <= s)
        assert 4_000_000 <= s - prev_peer_end <= 4_002_000                  # 4 s of idle bus, then our turn
        assert rs["reason"] == "after_peer_turn" and rs["idle_before_us"] >= 4_000_000
        assert re_["rtc"] == re_["bmp"] == re_["oled"] == "ok"


def test_turns_order_rtc_bmp_oled_and_no_traffic_between_turns(tmp_path):
    out = run_firmware(tmp_path, 40000, master_id=1, schedule="turns", peer="1")
    runs, txns = turn_runs(out), transactions(out)
    assert txns
    for t in txns:                                                          # every transaction inside one of our turns
        assert any(s <= t[0]["t"] <= e for s, e, *_ in runs), t
    for s, e, rs, re_ in runs:
        inside = [t for t in txns if s <= t[0]["t"] <= e]
        ranks = [ORDER[t[0]["addr"]] for t in inside]
        assert ranks == sorted(ranks) and set(ranks) == {0, 1, 2}           # RTC, then BMP280, then OLED
        rtc = [t for t in inside if t[0]["addr"] == 0x68]
        assert [[(f["rw"], f["rest"]) for f in t] for t in rtc] == [[("W", ["00"]), ("R", ["7"])]]   # time 0x00..0x06
        bmp = [t for t in inside if t[0]["addr"] == 0x76]
        assert [(f["rw"], f["rest"]) for f in bmp[-1]] == [("W", ["F7"]), ("R", ["6"])]   # data read last
        data = [t for t in inside if t[0]["addr"] == 0x3C and t[0]["rest"][0] == "80"]
        assert is_oled_line_write(data[-1], 1)                                   # "M1 hh:mm:ss / T .. C / P .. HPA"
        assert re_["transfers"] == len(inside)
    for s, e, rs, re_ in runs[1:]:                     # after set-up: exactly ONE transfer per device, in order
        assert [t[0]["addr"] for t in txns if s <= t[0]["t"] <= e] == [0x68, 0x76, 0x3C] and re_["transfers"] == 3
    first = [t for t in txns if runs[0][0] <= t[0]["t"] <= runs[0][1]]
    assert sum(1 for t in first if t[0]["addr"] == 0x3C and t[0]["rest"][0] == "00") == 26 + 1   # init + clear window
    assert sum(1 for t in first if t[0]["addr"] == 0x3C and t[0]["rest"][0] == "40") == 64       # clear
    assert sum(1 for t in first if t[0]["addr"] == 0x3C and t[0]["rest"][0] == "80") == 1        # the live text


def test_turns_master2_waits_for_master1_draws_its_own_frame(tmp_path):
    out = run_firmware(tmp_path, 30000, master_id=2, schedule="turns", peer="1", peer_first_at_ms="2500")
    mine, peer = turn_runs(out), peer_turns(out)
    assert "FAKE_COLLISION" not in out
    assert peer[0][0] == 2_500_000 and mine[0][0] > peer[0][1]               # Master 1 (the peer) went first
    assert 4_000_000 <= mine[0][0] - peer[0][1] <= 4_002_000
    assert all(r[2]["reason"] == "after_peer_turn" for r in mine)
    oled = [t for t in transactions(out) if t[0]["addr"] == 0x3C and t[0]["rest"][0] == "80"]
    assert len(oled) == len(mine) and all(is_oled_line_write(t, 2) for t in oled)          # pages 4-6, "M2 ..."


def test_turns_peer_absent_fallback_never_deadlocks(tmp_path):
    out = run_firmware(tmp_path, 32000, master_id=2, schedule="turns")      # Master 1 missing entirely
    absent, runs = json_lines(out, "peer_absent"), turn_runs(out)
    assert absent and len(runs) == len(absent) >= 2
    assert runs[0][0] >= 2_097_000 + 13_500_000                              # silent for PEER_TIMEOUT_MS first
    assert all(r[2]["reason"] == "peer_absent" for r in runs)
    assert all(b[0] - a[1] >= 13_500_000 for a, b in zip(runs, runs[1:]))
    out = run_firmware(tmp_path, 45000, master_id=1, schedule="turns", peer="1", peer_stop_at_ms="15000")
    runs = turn_runs(out)
    assert [r[2]["reason"] for r in runs][:2] == ["first_turn", "after_peer_turn"]
    assert runs[-1][2]["reason"] == "peer_absent" and json_lines(out, "peer_absent")


def test_turns_arbitration_loss_is_still_retried(tmp_path):
    out = run_firmware(tmp_path, 25000, master_id=1, schedule="turns", peer="1", arb_every="4",
                       stdin="TXLOG 1|", cmd_at_ms="2050")
    arb, txn = json_lines(out, "arb_lost"), json_lines(out, "txn")
    assert arb and all(a["retry"] is True for a in arb)
    assert txn and all(t["result"] == "ok" for t in txn)                    # never a NACK, never given up
    assert any(t["arb_lost"] for t in txn)
    assert all(r[3]["rtc"] == r[3]["bmp"] == r[3]["oled"] == "ok" for r in turn_runs(out))


def test_turns_time_command_is_written_inside_the_next_turn(tmp_path):
    out = run_firmware(tmp_path, 8000, master_id=1, schedule="turns", osf="1",
                       stdin="TIME 2026-11-17T09:30:00|", cmd_at_ms="3000")
    r = rtc_replies(out)
    assert [x["result"] for x in r] == ["queued", "applied"]
    s, e, *_ = turn_runs(out)[0]
    writes = [t for t in transactions(out) if t[0]["addr"] == 0x68 and t[0]["rw"] == "W" and len(t[0]["rest"]) > 1]
    assert writes and all(s <= t[0]["t"] <= e for t in writes)              # bus untouched until our turn


def test_turns_bench_stress_build_keeps_its_hammer_loop(tmp_path):
    out = run_firmware(tmp_path, 3000, master_id=1, schedule="turns", stress=True)
    assert not json_lines(out, "turn_start")
    assert len([t for t in transactions(out) if t[0]["addr"] == 0x76]) > 100   # back-to-back BMP reads


# ------------------------------------------------------------------ live review commands: SEQ / PROBE / ARB (2026-11-17)
def test_seq_mode_one_transfer_per_target_with_gap_and_double_gap_handover(tmp_path):
    out = run_firmware(tmp_path, 9000, master_id=1, schedule="turns", peer="1", peer_gap_ms="200", peer_turn_ms="30",
                       stdin="SEQ ON 100|", cmd_at_ms="2500")
    assert json_lines(out, "seq") == [{"type": "seq", "source": "controller", "master_id": 1, "enabled": True,
                                       "gap_ms": 100}]
    runs, txns = turn_runs(out), transactions(out)
    assert "FAKE_COLLISION" not in out and len(runs) >= 6
    for s, e, rs, re_ in runs[1:]:     # after the init turn: RTC, BMP280, OLED - one transfer each, in this order
        inside = [t for t in txns if s <= t[0]["t"] <= e]
        assert [t[0]["addr"] for t in inside] == [0x68, 0x76, 0x3C] and re_["transfers"] == 3
        assert re_["seq"] is True
        assert is_oled_line_write(inside[2], 1)                               # the OLED's live text lines
        starts = [t[0]["t"] for t in inside]
        assert all(b - a >= 100_000 for a, b in zip(starts, starts[1:]))   # 100 ms idle bus between transfers
        assert rs["seq"] is True and rs["gap_ms"] == 100 and rs["idle_before_us"] >= 200_000   # 2 x gap hand-over
    later = [t for t in txns if t[0]["addr"] == 0x3C and t[0]["t"] > runs[0][1]]
    assert later and all(is_oled_line_write(t, 1) for t in later)          # one text write, no full frame


def test_seq_mode_master2_also_drives_the_oled_in_order(tmp_path):
    out = run_firmware(tmp_path, 9000, master_id=2, schedule="turns", peer="1", peer_first_at_ms="2600",
                       peer_gap_ms="200", peer_turn_ms="30", stdin="SEQ ON 50|", cmd_at_ms="2300")
    runs, txns = turn_runs(out), transactions(out)
    assert len(runs) >= 4 and "FAKE_COLLISION" not in out
    for s, e, rs, re_ in runs[1:]:
        inside = [t[0]["addr"] for t in txns if s <= t[0]["t"] <= e]
        assert inside == [0x68, 0x76, 0x3C] and re_["oled"] == "ok"             # same order as Master 1


def test_seq_off_restores_normal_turns_and_bad_gap_is_rejected(tmp_path):
    out = run_firmware(tmp_path, 26000, master_id=1, schedule="turns", peer="1",
                       stdin="SEQ ON 1|SEQ ON 100|SEQ OFF|", cmd_at_ms="2500")
    err = json_lines(out, "error")
    assert err and "5..5000" in err[0]["message"]
    assert [d["enabled"] for d in json_lines(out, "seq")] == [True, False]
    runs = turn_runs(out)
    assert runs and all(r[3]["seq"] is False for r in runs)                  # normal turns again (no gaps)
    assert all(r[3]["transfers"] == 3 for r in runs[1:])                        # one transfer per device


def test_probe_sends_one_real_transfer_and_reports_the_address_nack(tmp_path):
    out = run_firmware(tmp_path, 8000, master_id=1, schedule="turns", stdin="TXLOG 1|PROBE 2A|PROBE 07|PROBE 7F|",
                       cmd_at_ms="3000")
    pr = json_lines(out, "probe")
    assert [(p["state"], p["addr"]) for p in pr] == [("queued", 0x2A), ("done", 0x2A)]
    assert pr[1]["result"] == "addr_nack"
    assert len([e for e in json_lines(out, "error") if "PROBE" in e["message"]]) == 2   # 0x07 / 0x7F reserved
    nack = [t for t in transactions(out) if t[0]["addr"] == 0x2A]
    assert len(nack) == 1 and not nack[0][0]["ack"]                         # the bus shows S 0x54 NACK P, once
    s, e, *_ = turn_runs(out)[0]
    assert s <= nack[0][0]["t"] <= e                                        # inside this master's own turn
    assert json_lines(out, "master_stats")[-1]["addr_nack"] == 1
    assert any(t["addr"] == 0x2A and t["result"] == "addr_nack" for t in json_lines(out, "txn"))


def test_probe_runs_at_once_in_the_free_running_build(tmp_path):
    out = run_firmware(tmp_path, 4000, master_id=2, stdin="PROBE 2A|", cmd_at_ms="3000")
    pr = json_lines(out, "probe")
    assert pr[-1]["state"] == "done" and pr[-1]["result"] == "addr_nack" and pr[-1]["t_us"] >= 3_000_000


def test_arb_command_synchronised_starts_until_arbitration_is_lost(tmp_path):
    out = run_firmware(tmp_path, 9000, master_id=1, schedule="turns", arb_addr="76", stdin="TXLOG 1|ARB|",
                       cmd_at_ms="3000")
    at = json_lines(out, "arb_test")
    assert len(at) == 1 and at[0]["target"] == 0x76 and at[0]["result"] == "lost_arbitration"
    assert at[0]["transfers"] == 1 and at[0]["arb_lost"] >= 1             # stops after the first lost transfer
    arb = json_lines(out, "arb_lost")
    assert arb and arb[0]["addr"] == 0x76 and arb[0]["t_us"] >= 3_000_000
    st = json_lines(out, "master_stats")[-1]
    assert st["arb_lost"] >= 1 and st["addr_nack"] == 0 and st["recoveries"] == 0   # never a NACK, never recovery
    out2 = run_firmware(tmp_path, 4000, master_id=2, schedule="turns", stdin="ARB|", cmd_at_ms="3000")
    at2 = json_lines(out2, "arb_test")[0]
    assert at2["target"] == 0x68 and at2["result"] == "no_loss_observed" and at2["transfers"] > 60   # 1.5 s window
    rtc = [t for t in transactions(out2) if t[0]["addr"] == 0x68 and t[0]["t"] >= 3_000_000][:60]
    gaps = [b[0]["t"] - a[-1]["t"] for a, b in zip(rtc, rtc[1:])]
    assert len(rtc) == 60 and all(390 <= g < 1000 for g in gaps)   # synchronised START: ARB_SYNC_US after each STOP


# ------------------------------------------------------------------ CONDUCT mode / ARB <addr> (2026-11-17, 2.5-turns)
def test_conduct_mode_does_exactly_the_requested_steps_and_nothing_else(tmp_path):
    out = run_firmware(tmp_path, 12000, master_id=1, schedule="turns",
                       stdin="TXLOG 1|CONDUCT ON|~DO RTC|~DO BME|~DO OLED|~PROBE 45|~DO OLED|~CONDUCT OFF|",
                       cmd_at_ms="8000")
    assert [c["enabled"] for c in json_lines(out, "conduct")] == [True, False]
    steps = json_lines(out, "step")
    assert [(st["what"], st["result"]) for st in steps] == [("rtc", "ok"), ("bmp", "ok"), ("oled", "ok"), ("oled", "ok")]
    t_on = next(c for c in json_lines(out, "turn_end"))                        # turns ran before CONDUCT ON
    txns = [t for t in transactions(out) if t[0]["t"] >= steps[0]["t_us"]]
    addrs = [t[0]["addr"] for t in txns][:5]
    assert addrs == [0x68, 0x76, 0x3C, 0x45, 0x3C]                             # in the commanded order, probe included
    assert [t for t in txns if t[0]["addr"] == 0x45][0][0]["ack"] is False      # the probe: a real address NACK
    assert t_on


def test_do_without_conduct_is_rejected_and_arb_takes_a_target(tmp_path):
    out = run_firmware(tmp_path, 4000, master_id=1, schedule="turns", stdin="DO RTC|ARB 7F|ARB 3C|", cmd_at_ms="3000")
    msgs = [e["message"] for e in json_lines(out, "error")]
    assert any("CONDUCT" in m for m in msgs) and any("ARB" in m for m in msgs)
    at = json_lines(out, "arb_test")
    assert at and at[0]["target"] == 0x3C
    oled = [t for t in transactions(out) if t[0]["addr"] == 0x3C and t[0]["t"] >= 3_000_000 and
            t[0]["rest"] == ["00", "A6"]]
    assert oled                                                                 # ARB 3C: harmless OLED command writes


def test_do_cal_rereads_bmp280_identity_and_steps_report_sensor_telemetry(tmp_path):
    """2.6-live: DO CAL (any mode) puts the chip ID + calibration reads on the bus for a GUI that started late; a CONDUCT
    BMP280 step after an RTC step also reports the values the OLED shows."""
    out = run_firmware(tmp_path, 12000, master_id=1, schedule="turns",
                       stdin="TXLOG 1|CONDUCT ON|~DO CAL|~DO RTC|~DO BME|~DO OLED|~CONDUCT OFF|", cmd_at_ms="8000")
    steps = json_lines(out, "step")
    assert [(st["what"], st["result"]) for st in steps] == [("cal", "ok"), ("rtc", "ok"), ("bmp", "ok"), ("oled", "ok")]
    txns = [t for t in transactions(out) if t[0]["t"] >= steps[0]["t_us"]]
    cal = [[(f["rw"], f["rest"]) for f in t] for t in txns if t[0]["addr"] == 0x76][:2]
    assert cal == [[("W", ["D0"]), ("R", ["1"])], [("W", ["88"]), ("R", ["24"])]]
    tele = [j for j in json_lines(out, "sensor_telemetry") if j.get("master_id") == 1]
    assert tele and -40 < tele[-1]["temperature_c"] < 85 and 300 < tele[-1]["pressure_hpa"] < 1100
    oled = [t for t in txns if t[0]["addr"] == 0x3C]
    assert oled and is_oled_line_write(oled[0], 1)
    # free-running turns: DO CAL is run at the start of the next turn, before the RTC read
    out = run_firmware(tmp_path, 12000, master_id=1, schedule="turns", stdin="DO CAL|", cmd_at_ms="3000")
    assert [st["what"] for st in json_lines(out, "step")] == ["cal"]
