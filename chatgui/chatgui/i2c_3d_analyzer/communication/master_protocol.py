"""Controller (master) USB protocol: JSON lines from firmware I2C.c on each master's COM port.

Every multi-master record carries "source":"controller" (sensor telemetry: "master") and
"master_id" (1 = Pico H, 2 = Pico W).  These lines are NEVER bus data: they are what a
controller says it did.  Bus truth comes only from the passive analyzer.

  hello            role, master_id, board, schedule, idle window / hold-off
  txn              one logical transfer (after retries): addr, wlen/rlen ON THE BUS, b0 (first
                   written byte or -1), result ok|addr_nack|data_nack|arb_lost|timeout|
                   abort_other|bus_busy, attempts, arb_lost (attempts lost to arbitration)
  arb_lost         one lost attempt: addr, attempt, byte, abort_source, backoff_us, retry
  master_stats     cumulative counters since controller boot (once per second)
  bus_recovery / controller_reset / txlog / scl_config / rtc_set / sensor_telemetry / error

Older single-master firmware (no master_id, plain-text RTC/BMP lines) is still accepted:
parse_master_line() returns None for non-JSON text and the caller keeps its legacy parsing.
"""
from __future__ import annotations

import json

RESULTS = ("ok", "addr_nack", "data_nack", "arb_lost", "timeout", "abort_other", "bus_busy")
MASTER_TYPES = {"hello", "txn", "arb_lost", "master_stats", "bus_recovery", "controller_reset", "txlog",
                "scl_config", "rtc_set", "sensor_telemetry", "error", "master_clock",
                # turn-taking firmware (I2C.c 2.2-turns, SCHEDULE_TURNS): one record at the start / end of each turn,
                # and peer_absent when the other master stayed silent for PEER_TIMEOUT_MS
                "turn_start", "turn_end", "peer_absent",
                # live review commands (I2C.c: SEQ ON/OFF, PROBE <addr>, ARB): replies / results
                "seq", "probe", "arb_test",
                # I2C.c 2.5-turns+: CONDUCT ON/OFF reply and the result of each GUI-driven step (DO RTC / BME / OLED / CAL)
                "conduct", "step"}
TURN_REASONS = ("first_turn", "after_peer_turn", "peer_absent")
STAT_KEYS = ("txn_ok", "addr_nack", "data_nack", "arb_lost", "retries", "gave_up", "busy_waits", "busy_timeouts",
             "timeouts", "other_aborts", "recoveries", "recovery_skipped", "controller_resets")


class MasterProtocolError(ValueError):
    pass


def _uint(d, k, hi=2 ** 64, required=True):
    v = d.get(k)
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < hi:
        raise MasterProtocolError(f"{k} must be a non-negative integer")
    return v


def parse_master_line(line) -> dict | None:
    """Validated controller record, or None for plain text (legacy prints / boot banner)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise MasterProtocolError("bad JSON from controller")
    if not isinstance(d, dict) or d.get("type") not in MASTER_TYPES:
        raise MasterProtocolError(f"unknown controller record {d.get('type') if isinstance(d, dict) else d!r}")
    if d.get("role") == "analyzer":
        raise MasterProtocolError("analyzer record on a master port: connect this COM port as the analyzer")
    if "master_id" in d and d["master_id"] not in (1, 2):
        raise MasterProtocolError("master_id must be 1 or 2")
    t = d["type"]
    if t == "txn":
        for k in ("seq", "t_us", "addr", "wlen", "rlen", "attempts", "arb_lost"):
            _uint(d, k)
        if d["addr"] > 0x7F:
            raise MasterProtocolError("addr out of range")
        if d.get("result") not in RESULTS:
            raise MasterProtocolError("unknown txn result")
        b0 = d.get("b0", -1)
        if isinstance(b0, bool) or not isinstance(b0, int) or not -1 <= b0 <= 255:
            raise MasterProtocolError("b0 out of range")
        _uint(d, "dur_us", required=False)
    elif t == "arb_lost":
        for k in ("t_us", "addr", "attempt"):
            _uint(d, k)
        if type(d.get("retry", True)) is not bool:
            raise MasterProtocolError("retry must be true/false")
    elif t == "master_stats":
        for k in STAT_KEYS:
            _uint(d, k, required=False)
    elif t == "turn_start":
        for k in ("turn", "t_us"):
            _uint(d, k)
        _uint(d, "idle_before_us", required=False)
        if d.get("reason") not in TURN_REASONS:
            raise MasterProtocolError("unknown turn reason")
    elif t == "turn_end":
        for k in ("turn", "t_us", "dur_us", "transfers"):
            _uint(d, k)
        for k in ("rtc", "bmp", "oled"):
            if not isinstance(d.get(k), str):
                raise MasterProtocolError(f"turn_end {k} result must be text")
    elif t == "seq":
        if type(d.get("enabled")) is not bool:
            raise MasterProtocolError("seq enabled must be true/false")
        _uint(d, "gap_ms")
    elif t == "probe":
        if d.get("state") not in ("queued", "done"):
            raise MasterProtocolError("probe state must be queued/done")
        if _uint(d, "addr") > 0x7F:
            raise MasterProtocolError("probe addr out of range")
        if d["state"] == "done":
            _uint(d, "t_us")
            if d.get("result") not in RESULTS:
                raise MasterProtocolError("unknown probe result")
    elif t == "arb_test":
        for k in ("target", "t_us", "transfers", "arb_lost", "gave_up"):
            _uint(d, k)
        if d.get("result") not in ("lost_arbitration", "no_loss_observed"):
            raise MasterProtocolError("unknown arb_test result")
    elif t == "peer_absent":
        for k in ("t_us", "silent_ms"):
            _uint(d, k)
    elif t == "master_clock":
        # the clock mapping of a SIMULATED controller (demo bench), known at its boot; hardware never sends this
        if d.get("simulated") is not True:
            raise MasterProtocolError("master_clock is only accepted from a SIMULATED controller")
        if isinstance(d.get("offset_ns"), bool) or not isinstance(d.get("offset_ns"), (int, float)):
            raise MasterProtocolError("offset_ns must be a number")
    return d
