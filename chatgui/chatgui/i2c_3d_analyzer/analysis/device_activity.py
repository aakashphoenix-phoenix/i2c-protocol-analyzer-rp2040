"""Device Communication Activity: per-address READ/WRITE activity from decoded records.

Units (every chart/label uses exactly these definitions):
  * READ frame / WRITE frame - one acknowledged address phase (START or repeated
    START + 7-bit address + R/W, address ACKed) inside a COMPLETE captured
    transaction.  A combined WRITE->READ register read contributes one WRITE
    frame and one READ frame to its address.
  * bus transaction - one complete START ... STOP transaction that addresses the
    device (a combined WRITE->READ counts once).
  * data bytes - data bytes after the address byte in those frames.
  * rate - frames per second of CAPTURE time over the rolling window.

Nothing here talks to the controller: counts come only from records the
passive analyzer (or the clearly labelled demo generator) delivered.

Status per device (window = rolling WINDOW_S of capture time):
  ACTIVE                - >=1 READ or WRITE frame in the window.
  NO OBSERVED ACTIVITY  - zero frames, the device ACKed earlier in this capture
                          (verified connected) and the window is reliable.
  PARTIAL               - an incomplete/gap transaction for this address lies in
                          the window, so its counts are only lower bounds.
  UNKNOWN               - window unreliable (disconnected, capture loss, window
                          not fully observed) or device never verified connected.
Rankings use only ACTIVE and NO OBSERVED ACTIVITY devices (exact counts).
Ties: every tied device is reported together, ordered by ascending address.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

WINDOW_S = 10.0
RETAIN_S = 300.0
UNITS = ("READ/WRITE frames = acknowledged address phases in complete captured transactions "
         "(combined WRITE→READ = 1 WRITE + 1 READ frame, 1 bus transaction)")


@dataclass(frozen=True)
class FrameRecord:
    t: float              # frame start (captured START / repeated START time, ns)
    addr: int
    rw: str               # 'R' | 'W'
    nbytes: int
    acked: bool | None
    complete: bool        # the enclosing transaction was complete (STOP captured, no gap)
    txn_id: int
    first_frame_of_txn: bool


class FrameLog:
    """Incremental per-frame log kept by the capture engine (bounded retention)."""

    def __init__(self):
        self.frames: deque = deque()
        self.latest: dict = {}           # addr -> {'R': (t, txn_id), 'W': (t, txn_id)}
        self.first_t: float | None = None

    def add(self, txn):
        seen = set()
        for f in txn.frames:
            if f.addr is None or f.rw not in ("R", "W"):
                continue
            rec = FrameRecord(f.t_start, f.addr, f.rw, len(f.data), f.addr_ack, txn.status == "ok",
                              txn.id, f.addr not in seen)
            seen.add(f.addr)
            self.frames.append(rec)
            if self.first_t is None or rec.t < self.first_t:
                self.first_t = rec.t
            if f.addr_ack:
                slot = self.latest.setdefault(f.addr, {})
                if f.rw not in slot or rec.t >= slot[f.rw][0]:
                    slot[f.rw] = (rec.t, txn.id)
        if self.frames:
            horizon = self.frames[-1].t - RETAIN_S * 1e9
            while self.frames and self.frames[0].t < horizon:
                self.frames.popleft()


def _window_reliability(engine, lo, now):
    """(reliable, reason) for the window [lo, now] of capture time."""
    if now is None:
        return False, "no capture time yet"
    if engine.source == "live":
        trusted, reason = engine.measurement_trust()
        if not trusted:
            return False, reason
    elif engine.source == "none":
        return False, "not capturing"
    origin = engine.frame_log.first_t if engine.capture_start_ns is None else engine.capture_start_ns
    if origin is None or origin > lo:
        return False, f"window not fully observed yet (< {WINDOW_S:g} s of capture)"
    if any(lo <= g <= now for g in engine.edge_gaps):
        return False, "capture gap/loss inside the window"
    return True, "reliable"


def _observed_window(engine, lo, now, window_s, why):
    """(window_s, note) for a window that is only unreliable because it is SHORTER than WINDOW_S (a capture of < 10 s,
    the demo / presentation sequence whose capture clock advances only microseconds per transfer): the counts are
    then exact over the time actually observed, so the rates use that span.  None when the window is unreliable for a
    real reason (gap, loss, disagreement, a live stream that stopped delivering records)."""
    origin = engine.frame_log.first_t if engine.capture_start_ns is None else engine.capture_start_ns
    if now is None or origin is None or not engine.frame_log.frames:
        return None
    start = min(lo, origin) if lo is not None else origin
    if any(start <= g <= now for g in engine.edge_gaps):          # real capture loss: stays UNKNOWN
        return None
    simulated = engine.source == "demo"
    if not simulated and not why.startswith("window not fully observed"):
        return None                                               # e.g. an untrusted / stopped live stream
    short = origin > lo
    if not (short or simulated):
        return None
    span = min(window_s, max(0.001, (now - origin) / 1e9))
    parts = []
    if short:
        parts.append(f"observed window {span:.6g} s (< {window_s:g} s of capture)")
    if simulated:
        parts.append("SIMULATED records")
    return span, "; ".join(parts)


def compute(engine, window_s: float = WINDOW_S):
    now = engine.hw_now
    lo = (now - window_s * 1e9) if now is not None else None
    reliable, why = _window_reliability(engine, lo, now)
    note = ""
    if not reliable and not engine.decode_disagreements:
        obs = _observed_window(engine, lo, now, window_s, why)
        if obs is not None:
            window_s, note = obs
            lo = now - window_s * 1e9
            reliable, why = True, note
    reg = engine.registry
    addrs = sorted(set(a for a, d in reg.devices.items() if d.last_ack_seen is not None)
                   | set(reg.configured) | set(a for a, d in reg.devices.items()))
    rows = {}
    for a in addrs:
        rows[a] = dict(addr=a, name=reg.name(a)[0], conf=reg.name(a)[1],
                       reads=0, writes=0, read_bytes=0, write_bytes=0, transactions=0,
                       nack_attempts=0, incomplete=0,
                       verified=bool(reg.devices.get(a) and reg.devices[a].last_ack_seen is not None),
                       latest_read=engine.frame_log.latest.get(a, {}).get("R"),
                       latest_write=engine.frame_log.latest.get(a, {}).get("W"))
    if lo is not None:
        for fr in reversed(engine.frame_log.frames):
            if fr.t < lo:
                break
            if fr.t > now or fr.addr not in rows:
                continue
            r = rows[fr.addr]
            if not fr.complete:
                r["incomplete"] += 1
                continue
            if fr.acked is False:
                r["nack_attempts"] += 1
                continue
            if fr.acked is not True:
                r["incomplete"] += 1
                continue
            if fr.rw == "R":
                r["reads"] += 1; r["read_bytes"] += fr.nbytes
            else:
                r["writes"] += 1; r["write_bytes"] += fr.nbytes
            if fr.first_frame_of_txn:
                r["transactions"] += 1
    for r in rows.values():
        r["read_rate"] = r["reads"] / window_s
        r["write_rate"] = r["writes"] / window_s
        if not reliable:
            r["status"], r["why"] = "UNKNOWN", why
        elif r["incomplete"]:
            r["status"], r["why"] = "PARTIAL", "incomplete capture for this address in window; counts are lower bounds"
        elif r["reads"] or r["writes"]:
            r["status"], r["why"] = "ACTIVE", "observed" + (f" · {note}" if note else "")
        elif r["verified"]:
            r["status"], r["why"] = "NO OBSERVED ACTIVITY", "verified connected (ACKed earlier); reliable window, zero frames"
        else:
            r["status"], r["why"] = "UNKNOWN", "never acknowledged in this capture; zero cannot be verified"
    return dict(rows=rows, window_s=window_s, reliable=reliable, reason=why, now=now, window_note=note,
                simulated=engine.source == "demo", units=UNITS,
                read=rank(rows, "reads"), write=rank(rows, "writes"))


def rank(rows, key):
    """Most/least for one direction among devices with exact counts. Ties listed together."""
    known = [r for r in rows.values() if r["status"] in ("ACTIVE", "NO OBSERVED ACTIVITY")]
    excluded = sorted(a for a, r in rows.items() if r["status"] not in ("ACTIVE", "NO OBSERVED ACTIVITY"))
    if not known:
        return dict(most=None, least=None, most_value=None, least_value=None, excluded=excluded,
                    text="UNKNOWN — no device with a reliable, exact count")
    hi = max(r[key] for r in known)
    lo = min(r[key] for r in known)
    most = sorted(r["addr"] for r in known if r[key] == hi) if hi > 0 else []
    least = sorted(r["addr"] for r in known if r[key] == lo)
    return dict(most=most, least=least, most_value=hi, least_value=lo, excluded=excluded,
                most_tie=len(most) > 1, least_tie=len(least) > 1, single=len(known) == 1)


def ranking_text(act, key, label):
    rk = act["read" if key == "reads" else "write"]
    if rk.get("most") is None:
        return f"{label}: {rk['text']}"
    rows = act["rows"]

    def names(addrs):
        return ", ".join(f"0x{a:02X} {rows[a]['name']}" for a in addrs)
    most = (f"most {names(rk['most'])} ({rk['most_value']} frames{', tie' if rk['most_tie'] else ''})"
            if rk["most"] else "most: none (0 frames for every verified device)")
    least = f"least {names(rk['least'])} ({rk['least_value']} frames{', tie' if rk['least_tie'] else ''})"
    extra = f" · excluded (UNKNOWN/PARTIAL): {', '.join(f'0x{a:02X}' for a in rk['excluded'])}" if rk["excluded"] else ""
    return f"{label}: {most} · {least}{extra}"
