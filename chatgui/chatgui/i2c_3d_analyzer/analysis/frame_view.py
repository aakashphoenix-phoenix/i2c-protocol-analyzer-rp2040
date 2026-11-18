"""I²C Communication Data Frame of ONE captured packet, plus the error / arbitration explanation shown with it.

Everything here is built from the decoded record (analysis.i2c_decoder.Transaction) and the multi-master tracker; nothing
is estimated or invented.  A field the capture did not contain (a STOP that was not captured, a byte without exact clock
timestamps) is reported as such.  Pure functions: used by the Packet Analyzer, the Digital Waveform and Error &
Diagnostics, and tested without Qt.

Field order (UM10204 §3.1): START (S) · 7-bit ADDRESS · R/W bit · address ACK/NACK · DATA bytes, each with its own
ACK/NACK · Repeated START (Sr) · ... · STOP (P).
"""
from __future__ import annotations

from analysis.communication_health import acknowledgment_counts
from analysis.playback import _nack_note, format_capture_ns


def _master(txn) -> str:
    import config
    mid = getattr(txn, "master_id", None)
    if mid is None:
        return "an unattributed master"
    return config.MASTERS.get(mid, {}).get("label", f"Master {mid}")


def _device(engine, addr) -> str:
    if addr is None:
        return "?"
    try:
        return engine.registry.name(addr)[0]
    except Exception:
        return f"0x{addr:02X}"


def _ns(engine, t):
    return format_capture_ns(t) if t is not None else "not captured"


def frame_rows(engine, txn):
    """[dict(field, value, detail, kind, ok)] in bus order.  kind: cond | addr | rw | ack | nack | data | status."""
    rows = []
    stop_t = next((t for t, k in txn.events if k == "P"), None)
    for fi, f in enumerate(txn.frames):
        first = f.start_kind == "S"
        rows.append(dict(field="START (S)" if first else "Repeated START (Sr)", value=_ns(engine, f.t_start),
                         detail=("SDA 1→0 while SCL = 1: the bus is taken" if first else
                                 "SDA 1→0 while SCL = 1 with no STOP before it: same transaction, direction can change"),
                         kind="cond", ok=True))
        data_index = 0
        for b in (b for b in txn.bytes if b.frame == fi):
            if b.kind == "addr":
                a = f.addr
                rows.append(dict(field="ADDRESS (7-bit)", value=f"0x{a:02X}",
                                 detail=f"b6..b0 = {a:07b} · {_device(engine, a)}", kind="addr", ok=True))
                rw = "R = 1 (Slave → Master)" if f.rw == "R" else "W = 0 (Master → Slave)"
                rows.append(dict(field="R/W bit", value=rw,
                                 detail=f"8-bit wire byte 0x{b.value:02X} = {b.value:08b} (address << 1 | R/W)",
                                 kind="rw", ok=True))
                if b.ack:
                    rows.append(dict(field="Address ACK", value="ACK (SDA = 0)",
                                     detail=f"9th clock: 0x{a:02X} pulled SDA LOW - the slave answered",
                                     kind="ack", ok=True))
                else:
                    rows.append(dict(field="Address NACK", value="NACK (SDA = 1)",
                                     detail=(f"Error: Invalid / unacknowledged slave address 0x{a:02X} — no slave pulled "
                                             "SDA LOW at the 9th clock (Address NACK)"), kind="nack", ok=False))
            elif b.kind == "addr10":
                rows.append(dict(field="ADDRESS (10-bit, 2nd byte)", value=f"0x{b.value:02X}",
                                 detail=f"{b.value:08b}", kind="addr", ok=True))
                rows.append(dict(field="Address ACK" if b.ack else "Address NACK",
                                 value="ACK (SDA = 0)" if b.ack else "NACK (SDA = 1)", detail="",
                                 kind="ack" if b.ack else "nack", ok=bool(b.ack)))
            else:
                direction = "Slave → Master (read)" if f.rw == "R" else "Master → Slave (write)"
                rows.append(dict(field=f"DATA [{data_index}]", value=f"0x{b.value:02X}",
                                 detail=f"b7..b0 = {b.value:08b} · {direction}", kind="data", ok=True))
                who = "controller" if f.rw == "R" else f"slave 0x{f.addr:02X}" if f.addr is not None else "slave"
                if b.ack:
                    rows.append(dict(field=f"DATA [{data_index}] ACK", value="ACK (SDA = 0)",
                                     detail=f"9th clock: the {who} pulled SDA LOW", kind="ack", ok=True))
                else:
                    note = _nack_note(txn, fi, "data", data_index)
                    expected = "normal end-of-read" in note
                    rows.append(dict(field=f"DATA [{data_index}] NACK", value="NACK (SDA = 1)", detail=note,
                                     kind="ack" if expected else "nack", ok=expected))
                data_index += 1
    dur = (txn.t_end - txn.t_start) if txn.t_end is not None else None
    hz = (txn.timing or {}).get("frequency_hz")
    if stop_t is not None:
        rows.append(dict(field="STOP (P)", value=_ns(engine, stop_t),
                         detail=(f"SDA 0→1 while SCL = 1: the bus is released · transaction "
                                 f"{dur / 1e3:.1f} µs" if dur is not None else "SDA 0→1 while SCL = 1")
                         + (f" · measured f_SCL {hz / 1e3:.2f} kHz" if hz else " · f_SCL UNKNOWN (no captured cycles)"),
                         kind="cond", ok=True))
    else:
        rows.append(dict(field="STOP (P)", value="not captured",
                         detail="no STOP was captured for this packet: its end is unverified", kind="status", ok=False))
    if txn.status != "ok":
        rows.append(dict(field="STATUS", value=txn.status.upper(),
                         detail="; ".join(txn.notes) or "the capture did not contain the whole transfer",
                         kind="status", ok=False))
    return rows


def error_cause(engine, txn):
    """dict(kind, title, text) for a packet that contains an error, else None.  The normal end-of-read NACK is no error."""
    who = _master(txn)
    for f in txn.frames:
        if f.addr is not None and f.addr_ack is False:
            rw = "R" if f.rw == "R" else "W"
            return dict(kind="invalid_address", title="INVALID ADDRESS ERROR",
                        text=f"INVALID ADDRESS ERROR: {who} sent 0x{f.addr:02X} ({rw}), no slave answered → Address NACK "
                             "at 9th clock (SDA stayed HIGH)")
    c = acknowledgment_counts(txn)
    if c["write_data_nacks"]:
        f = next(f for f in txn.frames if f.rw == "W" and False in f.acks)
        i = f.acks.index(False)
        return dict(kind="data_nack", title="WRITE DATA NACK",
                    text=f"WRITE DATA NACK: slave 0x{f.addr:02X} refused byte [{i}] 0x{f.data[i]:02X} written by {who} "
                         "(SDA stayed HIGH at its 9th clock)")
    if c["other_nacks"]:
        return dict(kind="early_read_nack", title="EARLY READ NACK",
                    text=f"EARLY READ NACK: {who} NACKed a read byte before the last one (the read was cut short)")
    if c["unverified_read_end_nacks"]:
        return dict(kind="unverified", title="UNVERIFIED READ END",
                    text="UNVERIFIED READ END: final read NACK, but the STOP / Sr after it was not captured")
    if txn.status != "ok":
        return dict(kind="incomplete", title=f"{txn.status.upper()} TRANSFER",
                    text=f"{txn.status.upper()} TRANSFER: " + ("; ".join(txn.notes) or
                                                                 "the capture does not contain the whole transfer"))
    return None


def is_error(engine, txn) -> bool:
    return error_cause(engine, txn) is not None


def arbitration_events(engine, txn):
    """Controller-reported arbitration losses whose winning bus packet is this one."""
    tr = engine.masters
    out = []
    for ev in list(tr.arb_events):
        if ev.winner is None:
            tr._resolve_winner(ev)
        if ev.winner is txn:
            out.append(ev)
    return out


def collision_point(txn, ev):
    """(t_ns, bit, text): where the loser's bits stopped matching the winner's.  For an address-byte loss the first bit in
    which the two 7-bit addresses differ (the loser sent 1 where the winner pulled SDA to 0)."""
    addr_b = next((b for b in txn.bytes if b.kind == "addr"), None)
    if ev.byte == 0 and addr_b is not None and txn.address is not None:
        winner = (txn.address << 1) | (1 if txn.frames[0].rw == "R" else 0)
        loser = (ev.addr << 1)                        # every controller transfer starts with a write of the register
        diff = winner ^ loser
        bit = next((i for i in range(8) if diff & (0x80 >> i)), None)
        if bit is not None and len(addr_b.bit_times) == 8:
            return (addr_b.bit_times[bit], bit,
                    f"address bit {7 - bit} (b{7 - bit}): winner sent {(winner >> (7 - bit)) & 1}, loser sent "
                    f"{(loser >> (7 - bit)) & 1}")
        return (addr_b.t_first, None, "in the address byte")
    data = [x for x in txn.bytes if x.kind == "data"]
    if ev.byte and len(data) >= ev.byte:
        b = data[ev.byte - 1]
        return (b.t_first, None, f"in byte {ev.byte} (data)")
    return (txn.t_start, None, "at the START")


def arbitration_text(engine, ev):
    """One-line explanation of a controller-reported arbitration loss and its winning packet."""
    import config
    loser = config.MASTERS.get(ev.master_id, {}).get("label", f"Master {ev.master_id}")
    w = ev.winner
    if w is not None:
        winner = _master(w).replace("an unattributed master", "the other master")
        waddr = f"0x{w.address:02X}" if w.address is not None else "?"
    else:
        winner, waddr = "the other master", "?"
    outcome = ("backed off %d µs & retried" % ev.backoff_us) if ev.retry else "GAVE UP after %d attempts" % ev.attempt
    return (f"⚡ ARBITRATION LOST: {winner} ({waddr}) vs {loser} (0x{ev.addr:02X}) — wired-AND SDA=0 won; "
            f"loser {outcome}, NOT a NACK")


def arbitration_details(engine, ev):
    """Multi-line summary: winner, loser, addresses, bit / byte position, back-off and outcome."""
    import config
    loser = config.MASTERS.get(ev.master_id, {}).get("label", f"Master {ev.master_id}")
    w = ev.winner
    lines = [arbitration_text(engine, ev)]
    if w is not None:
        t, bit, where = collision_point(w, ev)
        lines.append(f"Winner: {_master(w)} · bus packet #{w.id} to 0x{w.address:02X} ({_device(engine, w.address)}), "
                     f"START {format_capture_ns(w.t_start)}")
        lines.append(f"Collision: {where} at {format_capture_ns(t)}")
    else:
        lines.append("Winner: bus packet not matched yet (controller clock not aligned, or packet outside the capture)")
    lines.append(f"Loser: {loser} addressing 0x{ev.addr:02X} ({_device(engine, ev.addr)}) · attempt {ev.attempt} · "
                 f"lost at byte {ev.byte} · back-off {ev.backoff_us} µs · "
                 + ("retried (the transfer then completed on a later attempt)" if ev.retry else
                    "GAVE UP (transfer skipped, the target was NOT marked offline)"))
    lines.append("A passive analyzer only sees the wired-AND result: the loser's bits matched the winner's until it "
                 "stopped driving, so the bus shows only the winner's packet. Arbitration is never counted as a NACK.")
    return lines
