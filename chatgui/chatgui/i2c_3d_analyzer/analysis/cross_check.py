"""CROSS-CHECK the captured packets against independent references.

1. Known transactions: every master logs each transfer it makes on its OWN USB port (I2C.c report_txn: address, bytes
   written, bytes read, first byte, result).  Those are known transactions: each should appear on the bus exactly once,
   and each bus packet should be one of them.
2. Two decoders: on the RP2040 analyzer the firmware decodes on the Pico (segments) while the PC decodes the raw edge
   stream; every packet both saw is compared (CaptureEngine.decode_agreements / decode_disagreements).
3. Known device content: the chip-ID register 0xD0 at 0x76 must read 0x60 (BME280) or 0x58 (BMP280) - the value says
   which chip is really fitted; the DS3231 time registers must be valid BCD.
4. A reference logic analyzer: import its I2C decode (sigrok-cli / PulseView annotation text, or a Saleae Logic 1 / 2
   I2C analyzer CSV) and compare it transaction by transaction with this capture (sequence alignment of address, R/W,
   data bytes and ACK/NACK).  Export the same window as VCD (storage.signal_export) to decode it in PulseView.
"""
from __future__ import annotations

import csv
import difflib
import re

from analysis.multi_master import VISIBLE_RESULTS


# ------------------------------------------------------------------ 1. known transactions (controller logs)
def known_transactions(engine):
    tr = engine.masters
    rows = []
    t_first, t_last = tr.first_packet_t, tr.last_packet_t
    for mid, s in tr.state.items():
        txns = [m for m in s.txns if m.result in VISIBLE_RESULTS]
        row = dict(master_id=mid, master=s.label, connected=s.connected or s.simulated, reported=len(txns),
                   clock_locked=s.clock.locked, matched=0, missing=0, settling=0, before_capture=0,
                   bus_packets=sum(1 for t in engine.packets if t.master_id == mid and t.master_method == "telemetry"),
                   results=dict(s.results))
        if s.clock.locked and t_first is not None:
            for m in txns:
                tb = m.t_us * 1000.0 + s.clock.offset_ns
                if m.packet is not None:
                    row["matched"] += 1
                elif tb < t_first - tr.tol:
                    row["before_capture"] += 1          # sent before the analyzer started capturing
                elif tb > t_last - 2e9:
                    row["settling"] += 1                # its packet may still be on the way
                else:
                    row["missing"] += 1
        comparable = row["matched"] + row["missing"]
        row["match_pct"] = (100.0 * row["matched"] / comparable) if comparable else None
        rows.append(row)
    unknown = sum(1 for t in engine.packets if t.master_id is None)
    return rows, unknown


# ------------------------------------------------------------------ 2. firmware vs PC decoder
def decoder_agreement(engine):
    return dict(agree=getattr(engine, "decode_agreements", 0), disagree=len(engine.decode_disagreements),
                applicable=engine.caps.has("segment") if engine.caps else False)


# ------------------------------------------------------------------ 3. known device content
CHIP_IDS = {0x60: "BME280", 0x58: "BMP280"}      # Bosch register 0xD0 "id" (BME280 / BMP280 datasheets)

def known_content(engine):
    chip = [0, 0]                      # chip-ID reads, == 0x60 (BME280) or 0x58 (BMP280)
    ids = {}
    rtc = [0, 0]                       # time reads, valid BCD
    for t in engine.packets:
        w = next((f for f in t.frames if f.rw == "W" and f.data), None)
        r = next((f for f in t.frames if f.rw == "R" and f.data), None)
        if not (w and r) or t.status != "ok":
            continue
        if t.address in (0x76, 0x77) and w.data[0] == 0xD0:
            chip[0] += 1
            chip[1] += r.data[0] in CHIP_IDS
            ids[r.data[0]] = ids.get(r.data[0], 0) + 1
        if t.address == 0x68 and w.data[0] == 0x00 and len(r.data) >= 7:
            d = r.data
            ok = all((v & 0x0F) <= 9 for v in d[:7]) and (d[0] >> 4 & 7) <= 5 and (d[1] >> 4 & 7) <= 5
            rtc[0] += 1
            rtc[1] += ok
    return dict(chip_id_reads=chip[0], chip_id_ok=chip[1], rtc_reads=rtc[0], rtc_bcd_ok=rtc[1],
                chip_ids={f"0x{k:02X}": n for k, n in ids.items()},
                chip=" / ".join(CHIP_IDS.get(k, f"unknown 0x{k:02X}") for k in ids) or "not read yet")


# ------------------------------------------------------------------ 4. reference logic analyzer
_HEX = r"(?:0x)?([0-9A-Fa-f]{1,2})"
_SIGROK = [
    ("Sr", re.compile(r"\b(Start repeat|Repeated start|Restart)\b", re.I)),
    ("S", re.compile(r"\bStart\b", re.I)),
    ("P", re.compile(r"\bStop\b", re.I)),
    ("AW", re.compile(r"Address write:\s*" + _HEX, re.I)),
    ("AR", re.compile(r"Address read:\s*" + _HEX, re.I)),
    ("DW", re.compile(r"Data write:\s*" + _HEX, re.I)),
    ("DR", re.compile(r"Data read:\s*" + _HEX, re.I)),
    ("N", re.compile(r"\bNACK\b", re.I)),
    ("A", re.compile(r"\bACK\b", re.I)),
]


class _Builder:
    def __init__(self):
        self.out, self.cur, self.last = [], None, None     # last = ("addr"|"data", frame) awaiting its ACK bit

    def start(self, kind, t=None):
        if self.cur is not None and kind == "S" and self.cur["frames"]:
            self.out.append(self.cur)                     # a plain START while a transaction is open: no STOP was seen
            self.cur = None
        if self.cur is None:
            self.cur = dict(t=t, frames=[])
        self.cur["frames"].append(dict(start="Sr" if self.cur["frames"] else "S", addr=None, rw=None, data=[],
                                       acks=[], addr_ack=None))
        self.last = None

    def address(self, addr, rw):
        if self.cur is None:
            self.start("S")
        f = self.cur["frames"][-1]
        f["addr"], f["rw"] = addr, rw
        self.last = ("addr", f)

    def data(self, v):
        if self.cur is None or not self.cur["frames"]:
            return
        f = self.cur["frames"][-1]
        f["data"].append(v)
        self.last = ("data", f)

    def ack(self, ok):
        if self.last is None:
            return
        kind, f = self.last
        if kind == "addr":
            f["addr_ack"] = ok
        else:
            f["acks"].append(ok)
        self.last = None

    def stop(self):
        if self.cur is not None and self.cur["frames"]:
            self.out.append(self.cur)
        self.cur, self.last = None, None

    def done(self):
        self.stop()
        return self.out


def parse_sigrok_text(text):
    """sigrok-cli '-P i2c -A i2c' output or a PulseView 'export annotations' file."""
    b = _Builder()
    for ln in text.splitlines():
        if not ln.strip():
            continue
        for key, rx in _SIGROK:
            m = rx.search(ln)
            if not m:
                continue
            if key in ("S", "Sr"):
                b.start(key)
            elif key == "P":
                b.stop()
            elif key in ("AW", "AR"):
                b.address(int(m.group(1), 16), "W" if key == "AW" else "R")
            elif key in ("DW", "DR"):
                b.data(int(m.group(1), 16))
            elif key in ("A", "N"):
                b.ack(key == "A")
            break
    return b.done()


def _bool(v):
    return str(v).strip().lower() in ("true", "1", "yes", "ack", "a")


def _num(v):
    v = str(v).strip()
    return int(v, 16) if v.lower().startswith("0x") else int(v, 16) if re.fullmatch(r"[0-9A-Fa-f]{1,2}", v) else int(v)


def parse_saleae_csv(text):
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        return []
    keys = {k.strip().lower().strip('"'): k for k in rows[0].keys()}
    b = _Builder()
    if "type" in keys:                                      # Logic 2: name,type,start_time,duration,ack,address,read,data
        for r in rows:
            g = lambda k: r.get(keys.get(k, ""), "")
            typ = g("type").strip().lower()
            if typ == "start":
                b.start("S" if b.cur is None else "Sr")
            elif typ == "stop":
                b.stop()
            elif typ == "address":
                b.address(_num(g("address")), "R" if _bool(g("read")) else "W")
                b.ack(_bool(g("ack")))
            elif typ == "data":
                b.data(_num(g("data")))
                b.ack(_bool(g("ack")))
        return b.done()
    # Logic 1.x: Time [s],Packet ID,Address,Data,Read/Write,ACK/NAK  (one row per data byte, grouped by Packet ID)
    pid_k = next((keys[k] for k in keys if "packet" in k), None)
    out, cur_id = [], object()
    for r in rows:
        pid = r.get(pid_k, "")
        rw = "R" if "read" in r.get(keys.get("read/write", ""), "").lower() else "W"
        addr = _num(r.get(keys.get("address", ""), "0")) >> (1 if _num(r.get(keys.get("address", ""), "0")) > 0x7F else 0)
        if pid != cur_id:
            if b.cur is not None:
                b.stop()
            b.start("S")
            cur_id = pid
            b.address(addr, rw)
            b.ack(True)
        elif b.cur["frames"][-1]["rw"] != rw:
            b.start("Sr")
            b.address(addr, rw)
            b.ack(True)
        if r.get(keys.get("data", ""), "").strip():
            b.data(_num(r[keys["data"]]))
            b.ack("ack" in r.get(keys.get("ack/nak", ""), "ack").lower())
    return b.done()


def parse_reference(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    head = text[:400].lower()
    if path.lower().endswith(".csv") or ("," in head.split("\n")[0] and ("type" in head or "packet id" in head)):
        return "Saleae CSV", parse_saleae_csv(text)
    return "sigrok / PulseView annotations", parse_sigrok_text(text)


def _key(frames):
    return tuple((f["addr"], f["rw"], tuple(f["data"]), tuple(bool(a) for a in f["acks"]), f["addr_ack"])
                 for f in frames)


def packet_key(t):
    return tuple((f.addr, f.rw, tuple(f.data), tuple(bool(a) for a in f.acks), f.addr_ack) for f in t.frames)


def compare_with_reference(engine, reference):
    """Align the reference transactions with this capture's packets.  Returns a summary dict."""
    ref = [_key(r["frames"]) for r in reference]
    ours_t = [t for t in engine.packets if t.status in ("ok", "incomplete")]
    ours = [packet_key(t) for t in ours_t]
    if not ref:
        return dict(reference=0, matched=0, ours=len(ours), mismatches=[], start_packet=None)
    # the reference usually covers a short window of this capture: start at the first place its first few
    # transactions appear, then align the rest
    probe = ref[:3]
    starts = [i for i in range(len(ours) - len(probe) + 1) if ours[i:i + len(probe)] == probe] or [0]
    best = None
    for st in starts[:20]:
        seg = ours[st:st + len(ref) + 50]
        sm = difflib.SequenceMatcher(None, ref, seg, autojunk=False)
        matched = sum(b.size for b in sm.get_matching_blocks())
        if best is None or matched > best[0]:
            best = (matched, st, sm)
    matched, st, sm = best
    mism = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        for k in range(i1, min(i2, i1 + 3)):
            mism.append(dict(op=op, reference=_fmt(ref[k]),
                             ours=_fmt(ours[st + j1 + (k - i1)]) if st + j1 + (k - i1) < min(len(ours), st + j2) else "—"))
        if len(mism) >= 12:
            break
    return dict(reference=len(ref), matched=matched, ours=len(ours), mismatches=mism,
                start_packet=ours_t[st].id if ours_t else None,
                match_pct=100.0 * matched / len(ref))


def _fmt(key):
    parts = []
    for addr, rw, data, acks, aack in key:
        a = "?" if addr is None else f"0x{addr:02X}"
        parts.append(f"{a} {rw or '?'} {'A' if aack else 'N'} [" + " ".join(f"{d:02X}{'' if k else '*'}"
                                                                              for d, k in zip(data, acks)) + "]")
    return " | ".join(parts)
