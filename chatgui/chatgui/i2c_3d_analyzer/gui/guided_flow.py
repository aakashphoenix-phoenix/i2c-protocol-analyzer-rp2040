"""GUIDED FLOW (Real Hardware Mode): the GUI conducts both masters, one REAL transfer at a time.

Both controller Picos run I2C.c 2.6-live in CONDUCT mode: they start nothing on their own.  The conductor sends
    Master 1: DO RTC -> DO BME -> DO OLED,   Master 2: DO RTC -> DO BME -> DO OLED,   (an injected error),   repeat
and after each command waits until that transfer has been captured by the analyzer and attributed to the master by the
master's own transfer report, then plays it COMPLETELY - every byte and its own ACK / NACK on the 9th clock - in the 3D
view, the Packet Analyzer and the Digital Waveform, and only then sends the next command.  So the order on the bus is
exactly the order on screen, and every packet is shown byte by byte.

Injected errors (real bus events, never simulated), one after every cycle, alternating:
  * invalid address: a master writes to an address with no device (rotating 0x2A, 0x45, 0x13, 0x5B and alternating
    Master 1 / Master 2) -> a genuine Address NACK, logged in Error & Diagnostics
  * arbitration: both masters start together (ARB <addr>, synchronised START) towards different slaves (rotating pairs);
    the wired-AND bus decides - the master sending the first 1 where the other sends 0 loses, backs off and retries.
    The winner's packet is played; the loser's report is matched to it (Visualize last arbitration shows the collision).
"""
from __future__ import annotations

import time

import config

PROBE_ADDRS = (0x2A, 0x45, 0x13, 0x5B)              # no device at these addresses on this bench
ARB_PAIRS = ((0x76, 0x68), (0x3C, 0x76), (0x68, 0x3C))   # (Master 1 target, Master 2 target)
BASE_CYCLE = ((1, "RTC", 0x68), (1, "BME", 0x76), (1, "OLED", 0x3C),
              (2, "RTC", 0x68), (2, "BME", 0x76), (2, "OLED", 0x3C))
WAIT_PACKET_S = 4.0          # a step whose packet does not arrive within this is logged and skipped
SETTLE_S = 1.5               # after the packet arrives: time for its master's report to attribute it
ARB_WAIT_S = 4.5


class GuidedFlow:
    def __init__(self, win, stage_s=0.25, inject_errors=True):
        self.w = win
        self.stage_s = stage_s
        self.inject_errors = inject_errors
        self.steps = []
        self.cycle = 0
        self.n_probe = self.n_arb = 0
        self.state = "idle"
        self.step = None
        self.history = []        # (monotonic, label, packet id or None, outcome)
        self.n_user_nack = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self, now=None):
        self.w._review_send(self.w._review_masters(), "CONDUCT ON")
        cal = getattr(self.w, "_request_calibration", None)
        if cal is not None:
            cal(force=True)        # DO CAL: BMP280 calibration on the bus first, so temperature / pressure decode live
        self.state = "next"
        self._t = (time.monotonic() if now is None else now) + 0.4   # let a transfer already on the bus finish
        self.w.engine.log("info", "Session", "Guided flow",
                          "Real hardware, one transfer at a time: Master 1 RTC → BMP280 → OLED, Master 2 RTC → BMP280 → "
                          "OLED" + (", then an injected error (invalid address / arbitration)" if self.inject_errors
                                    else ""))

    def stop(self):
        self.w._review_send(self.w._review_masters(), "CONDUCT OFF")
        self.state = "idle"

    @property
    def active(self):
        return self.state != "idle"

    # ------------------------------------------------------------------ the plan
    def _plan(self):
        self.cycle += 1
        steps = [dict(kind="DO", mid=m, what=w, addr=a) for m, w, a in BASE_CYCLE]
        if self.inject_errors:
            if self.cycle % 2 == 1:
                mid = 1 if self.n_probe % 2 == 0 else 2
                steps.append(dict(kind="PROBE", mid=mid, addr=PROBE_ADDRS[self.n_probe % len(PROBE_ADDRS)]))
                self.n_probe += 1
            else:
                a1, a2 = ARB_PAIRS[self.n_arb % len(ARB_PAIRS)]
                steps.append(dict(kind="ARB", a1=a1, a2=a2))
                self.n_arb += 1
        self.steps = steps

    def queue_nack(self):
        """Presentation-mode 'NACK next transfer' on REAL hardware: the master due next addresses a device that is not
        on the bus instead (rotating invalid addresses) - a genuine Address NACK.  Returns the queued step."""
        if not self.steps:
            self._plan()
        nxt = next((s for s in self.steps if s["kind"] == "DO"), None)
        mid = nxt["mid"] if nxt is not None else 1
        st = dict(kind="PROBE", mid=mid, addr=PROBE_ADDRS[self.n_user_nack % len(PROBE_ADDRS)], user=True)
        self.n_user_nack += 1
        self.steps.insert(0, st)
        return st

    @staticmethod
    def label(st):
        if st["kind"] == "DO":
            return f"{config.MASTERS[st['mid']]['label']} → {st['what']} 0x{st['addr']:02X}"
        if st["kind"] == "PROBE":
            return f"{config.MASTERS[st['mid']]['label']} → invalid address 0x{st['addr']:02X}"
        return f"arbitration: Master 1 → 0x{st['a1']:02X} vs Master 2 → 0x{st['a2']:02X}"

    # ------------------------------------------------------------------ one GUI refresh
    def tick(self, now=None, force=False):
        """Called by MainWindow._refresh while the playback is idle (previous packet finished, its last dot arrived).
        force: Step ▸ (stage playback held) - send the next command now instead of waiting for the pace."""
        now = time.monotonic() if now is None else now
        w, e = self.w, self.w.engine
        if self.state == "next":
            if now < self._t and not force:
                return
            if not self.steps:
                self._plan()
            st = self.step = self.steps.pop(0)
            st["n0"] = len(e.packets)
            st["sent"] = now
            if st["kind"] == "DO":
                w._review_send([st["mid"]], f"DO {st['what']}")
            elif st["kind"] == "PROBE":
                w._review_send([st["mid"]], f"PROBE {st['addr']:02X}")
            else:
                st["arb0"] = e.masters.arb_total
                w._review_send([1], f"ARB {st['a1']:02X}")       # back to back: the STARTs are synchronised on the bus
                w._review_send([2], f"ARB {st['a2']:02X}")
            self.state = "wait"
            return
        if self.state != "wait":
            return
        st = self.step
        txn = self._find(st, now)
        if txn is None:
            limit = ARB_WAIT_S if st["kind"] == "ARB" else WAIT_PACKET_S
            if now - st["sent"] > limit:
                self.history.append((now, self.label(st), None, "no packet"))
                e.log("warning", "Session", "Guided flow", f"{self.label(st)}: no packet within {limit:g} s - skipped")
                self.state, self._t = "next", now + 0.2
            return
        self.history.append((now, self.label(st), txn.id, "played"))
        w.play_guided(txn, self.stage_s, st)
        self.state, self._t = "next", now

    def _find(self, st, now):
        e = self.w.engine
        new = e.packets[st["n0"]:]
        if st["kind"] == "ARB":
            if e.masters.arb_total <= st["arb0"]:
                return None
            if not st.get("stopped"):                         # the loser reported: the winner can stop hammering
                st["stopped"] = True
                self.w._review_send([1, 2], "ARB STOP")
            evs = list(e.masters.arb_events)[-(e.masters.arb_total - st["arb0"]):]
            for ev in evs:
                if ev.winner is None:
                    e.masters._resolve_winner(ev)
            won = [ev for ev in evs if ev.winner is not None]
            if not won or now - st["sent"] < 2.0:                # let the burst end before playing the winner
                return None
            ev = st["arb_event"] = won[0]
            loser = getattr(ev, "master_id", None)
            winner = getattr(ev.winner, "master_id", None) or ({1: 2, 2: 1}.get(loser))
            try:      # the same phase-1 arbitration picture as presentation mode, from the REAL collision
                ev.winner.presentation = dict(arbitration=dict(
                    winner=winner, loser=loser, live=True,
                    text=(f"REAL arbitration: both masters started together (Master 1 → 0x{st['a1']:02X}, Master 2 → "
                          f"0x{st['a2']:02X}); {config.MASTERS.get(loser, {}).get('label', 'a master')} sent a 1 where "
                          f"the other sent a 0, read back 0 (wired-AND), lost and retried after the STOP")))
            except AttributeError:
                pass
            return ev.winner
        want = st["addr"]
        for t in new:
            if t.address == want and t.status != "open" and t.master_id == st["mid"]:
                return t
        claim = getattr(e.masters, "claim_reported", None)
        if claim is not None and now - st["sent"] > 0.6:     # not matched by time: use that master's own report
            for t in new:
                if t.address == want and t.status != "open" and t.master_id is None and claim(t, st["mid"]):
                    return t
        late = [t for t in new if t.address == want and t.status != "open"]
        if late and now - st["sent"] > SETTLE_S + 0.5:            # captured, but its master never claimed it
            return late[0]
        return None
