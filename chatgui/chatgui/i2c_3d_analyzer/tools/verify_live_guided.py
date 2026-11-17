"""LIVE HARDWARE check of the GUIDED FLOW (controller firmware 2.6-live, CONDUCT mode).  Real Hardware Mode only.
Runs in the PRESENTATION THEME at the laptop's 1536 x 816 working area (the review set-up).

Connect all, then '▶ Guided flow' with 'Inject errors' and checks on the REAL captured bus traffic:
  * order: Master 1 RTC 0x68 -> BMP280 0x76 -> OLED 0x3C, Master 2 RTC -> BMP280 -> OLED, then one injected error,
    repeated; every packet attributed to its master by the master's own report
  * every played packet is shown COMPLETELY: all protocol stages, every byte with its OWN ACK / NACK stage, on screen for
    its whole stage time before the next command
  * only the commanded transfers are on the bus (one RTC, one BMP280, one OLED per master per cycle)
  * invalid address: rotating addresses, alternating masters, a real Address NACK, logged as an error, the waveform
    labels S, ADDR 0x.. W, red NACK, P
  * arbitration: a real arb_lost from a master's firmware every time, between DIFFERENT slave pairs, the winner packet
    matched and shown with the amber collision marker
  * the waveform tab shows each guided packet decoded; Performance / Device Activity / Capture History tabs hidden
  * 2.6-live: DO CAL puts the BMP280 calibration on the bus -> temperature / pressure decoded from captured bytes; every
    OLED write carries the master's live time / temperature / pressure text (read back from the captured pixels) and
    the Sensor page mirrors the captured OLED RAM; long OLED writes play their first 8 bytes byte by byte, the rest
    condensed (each ACK still shown)
  * presentation-mode controls on real hardware: Pause + 'NACK next transfer' + 'Step ▸' -> the next REAL transfer is
    an invalid address, opened held at phase 1, stepped phase by phase
Usage: python -X utf8 tools/verify_live_guided.py [--cycles 4] [--pace 0.15]
Writes work/verification/guided/*.png + guided_report.json
"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
OUT = ROOT / "work" / "verification" / "guided"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS = []
sys.excepthook = lambda t, v, tb: (ERRORS.append("".join(traceback.format_exception(t, v, tb))),
                                   sys.__excepthook__(t, v, tb))
from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer, QtMsgType, qInstallMessageHandler  # noqa: E402
from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

BOXES = []
for _k in ("warning", "information", "critical", "question"):
    setattr(QMessageBox, _k, staticmethod(lambda *a, _k=_k, **k: (BOXES.append((_k, str(a[1:3])[:300])),
                                                                   QMessageBox.StandardButton.Ok)[1]))
QT_ERRORS = []
qInstallMessageHandler(lambda kind, _c, msg: QT_ERRORS.append(msg) if kind in (QtMsgType.QtCriticalMsg,
                                                                                QtMsgType.QtFatalMsg) else None)


def native_settings():
    s = QSettings(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, "I2CAnalyzer", "I2C3DAnalyzer")
    return {k: str(s.value(k)) for k in s.allKeys()}


NATIVE_BEFORE = native_settings()
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tempfile.mkdtemp(prefix="guided_settings_"))
fmt = QSurfaceFormat(); fmt.setSamples(4); fmt.setDepthBufferSize(24)
QSurfaceFormat.setDefaultFormat(fmt)
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
app.setApplicationName("I2C Packet Analyzer")
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
from gui.theme import STYLESHEET  # noqa: E402
app.setStyleSheet(STYLESHEET)
import config  # noqa: E402
from analysis.packet_proof import oled_text_write_meaning  # noqa: E402
from analysis.playback import Playback, capped_stages, stages  # noqa: E402
from gui.guided_flow import ARB_PAIRS, PROBE_ADDRS  # noqa: E402
from gui.main_window import HIDDEN_NAV, NAV_INDEX, MainWindow  # noqa: E402

CYCLES = int(sys.argv[sys.argv.index("--cycles") + 1]) if "--cycles" in sys.argv else 4
PACE = float(sys.argv[sys.argv.index("--pace") + 1]) if "--pace" in sys.argv else 0.15
w = MainWindow()
w.set_theme("presentation")
w.resize(1536, 790)                       # the evaluation laptop: 1536 x 816 working area
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED LIVE GUIDED-FLOW CHECK (input disabled)")
w.show()
assert w.demo is None and w.presentation is None, "Real Hardware Mode only"
loop = QEventLoop()
results, failures = {}, []
PLAYS = []                       # (monotonic, txn, stage_s, n_events) every guided select
_orig_select = Playback.select


def _select(self, txn, *a, **k):
    r = _orig_select(self, txn, *a, **k)
    if k.get("stage_s"):
        PLAYS.append(dict(t=time.monotonic(), txn=txn, stage_s=k["stage_s"], n=len(self.events), events=list(self.events)))
    return r


Playback.select = _select
WAVE = {}                        # packet id -> (decoder-track kinds, amber marks, alert text) seen while it played


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        pb = w.playback
        if pb.txn is not None and getattr(pb, "fixed_stage_s", None) and w.p_wave.txn is pb.txn:
            segs = sorted(getattr(w.p_wave.decoder_track, "segs", []), key=lambda s: s["x0"])
            if segs:
                WAVE[pb.txn.id] = ([s["kind"] for s in segs], len(w.p_wave._arb_items), w.p_wave.alert.text())
        if until is not None and until():
            return True
    return until() if until is not None else True


def check(name, ok, detail=""):
    results[name] = dict(ok=bool(ok), detail=str(detail)[:1800])
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  ->  {str(detail)[:300]}"), flush=True)
    if not ok:
        failures.append(name)


def shot(tag):
    w.grab().save(str(OUT / f"{tag}.png"))


hidden = [NAV[0] for NAV in []]
check("Performance Monitoring / Device Activity / Capture History hidden from the navigation",
      all(w.nav.item(NAV_INDEX[k]).isHidden() for k in HIDDEN_NAV) and not any(
          w.nav.item(i).isHidden() for i in range(w.nav.count()) if i not in [NAV_INDEX[k] for k in HIDDEN_NAV]),
      [w.nav.item(i).text().strip() for i in range(w.nav.count()) if not w.nav.item(i).isHidden()])
w.nav.setCurrentRow(NAV_INDEX["network"])
w.btn_connect_all.click()
ok = pump(45, lambda: w.capturing and all(s.clock.locked for s in w.engine.masters.state.values()))
e = w.engine
check("Connect all: analyzer + Master 1 + Master 2 capturing, clocks aligned (Real Hardware Mode)",
      ok and e.source == "live", [(r["master_id"], r["clock"]) for r in e.masters.rows()])
fw = {mid: (s.hello or {}).get("fw_version") for mid, s in e.masters.state.items()}
check("both masters run controller firmware 2.6-live", all(v == "2.6-live" for v in fw.values()), fw)
check("window fits the 1536 px laptop screen (presentation theme)", w.minimumSizeHint().width() <= 1536 and
      w.minimumWidth() <= 1536, (w.minimumSizeHint().width(), w.minimumWidth()))

w.guided_pace.setCurrentIndex(w.guided_pace.findData(PACE))
w.chk_inject.setChecked(True)
n0, t0 = len(e.packets), time.monotonic()
w.btn_guided.setChecked(True)
check("Guided flow started (CONDUCT ON sent to both masters)", w.guided is not None and w.guided.active,
      getattr(w.guided, "state", None))
shots = {}


def watch_done():
    g = w.guided
    return g is not None and g.cycle > CYCLES
while time.monotonic() - t0 < 60 * 12 and not watch_done():
    pump(0.5)
    pb = w.playback
    if pb.txn is not None and getattr(pb, "fixed_stage_s", None):
        k = (pb.txn.address, pb.current.kind if pb.events else "")
        if pb.txn.address == 0x68 and pb.current.kind == "data" and "rtc_byte" not in shots:
            shots["rtc_byte"] = 1; shot("01_3d_rtc_byte_by_byte")
        if pb.txn.addr_nack and "probe" not in shots:
            pump(0.8); shots["probe"] = 1; w.nav.setCurrentRow(NAV_INDEX["waveform"]); pump(1.0)
            shot("02_waveform_invalid_address"); w.nav.setCurrentRow(NAV_INDEX["network"])
        g = w.guided
        ev_ = g.step.get("arb_event") if (g and g.step) else None
        if g and g.step and g.step.get("kind") == "ARB" and "arb" not in shots and ev_ is not None and pb.txn is ev_.winner:
            pump(0.8); shots["arb"] = 1; w.nav.setCurrentRow(NAV_INDEX["waveform"]); pump(1.0)
            shot("03_waveform_arbitration"); w.nav.setCurrentRow(NAV_INDEX["network"])
g = w.guided
hist = list(g.history) if g else []
w.nav.setCurrentRow(NAV_INDEX["error"])
pump(1.2)
shot("04_error_diagnostics")
w.nav.setCurrentRow(NAV_INDEX["packets"])
pump(1.2)
shot("05_packet_analyzer")
# ---- presentation-mode controls on REAL hardware: Pause, NACK next transfer, Step ▸ phase by phase
w.nav.setCurrentRow(NAV_INDEX["network"])
pump(0.2)
pump(30, lambda: w.guided is not None and w.guided.state == "next" and (w.playback.txn is None or w.playback.finished))
T_STEP = time.monotonic()
w.btn_play.click()                                  # ⏸ Pause: the guided flow waits for Step ▸
nack_ok = w.btn_nack.isEnabled()
w.btn_nack.click()
queued = w.guided.steps[0] if w.guided.steps else {}
pump(1.0)
sent_before = len(w.guided.history)
w.btn_step.click()                                  # sends the queued PROBE now (stage playback held)
got = pump(6, lambda: len(w.guided.history) > sent_before and w.playback.txn is not None
           and w.playback.txn.address == queued.get("addr"))
held_at_1 = got and w.stage_hold and w.playback.stage_index == 0
pk = w.playback.txn
shot("07_step_nack_phase1")
phases = [w.playback.stage_index]
for _ in range(12):
    w.btn_step.click()
    pump(0.4)
    if w.playback.txn is not pk:
        break
    phases.append(w.playback.stage_index)
shot("08_step_nack_last_phase")
check("presentation-mode controls on real hardware: NACK next transfer → the next REAL transfer is an invalid "
      "address, opened held at phase 1 and stepped phase by phase",
      nack_ok and queued.get("kind") == "PROBE" and held_at_1 and pk is not None and pk.addr_nack and
      phases == sorted(phases) and len(set(phases)) >= 3 and not w.btn_nack.isChecked(),
      dict(enabled=nack_ok, queued=queued, held=held_at_1, addr=pk and f"0x{pk.address:02X}",
           nack=pk and pk.addr_nack, phases=phases))
w.btn_play.click()                                  # ▶ Play: the guided flow resumes by itself
pump(6)
for key in ("dashboard", "network", "packets", "waveform", "sensor", "error", "settings"):
    w.nav.setCurrentRow(NAV_INDEX[key])
    pump(1.5)
    shot(f"tab_{key}_presentation_theme")
w.btn_guided.setChecked(False)
pump(2.0)

# ------------------------------------------------------------------ analysis of what was PLAYED
plays = [p for p in PLAYS if t0 <= p["t"] < T_STEP]   # the Step / NACK demo is checked on its own
seq = []
for p in plays:
    t = p["txn"]
    kind = ("ARB" if any(getattr(ev, "winner", None) is t for ev in e.masters.arb_events) else
            "PROBE" if t.address in PROBE_ADDRS else "DO")
    seq.append((kind, t.master_id, t.address, t))
results["_played"] = dict(ok=True, detail=str([(k, m, f"0x{a:02X}") for k, m, a, _t in seq]))
cycles, cur = [], []
for item in seq:
    cur.append(item)
    if item[0] != "DO":
        cycles.append(cur)
        cur = []
want = [("DO", 1, 0x68), ("DO", 1, 0x76), ("DO", 1, 0x3C), ("DO", 2, 0x68), ("DO", 2, 0x76), ("DO", 2, 0x3C)]
order_ok = len(cycles) >= CYCLES and all([(k, m, a) for k, m, a, _t in c[:6]] == want for c in cycles)
check(f"order: Master 1 RTC→BMP280→OLED, Master 2 RTC→BMP280→OLED, then an injected error ({len(cycles)} cycles)",
      order_ok, [[(k, m, f"0x{a:02X}") for k, m, a, _t in c] for c in cycles[:3]])
errs = [c[-1] for c in cycles]
check("errors alternate: invalid address, arbitration, invalid address, ...",
      [k for k, *_ in errs[:4]] == ["PROBE", "ARB", "PROBE", "ARB"][:len(errs[:4])] and len(errs) >= 4,
      [k for k, *_ in errs])
probes = [(m, a, t) for k, m, a, t in errs if k == "PROBE"]
check("invalid addresses rotate and the master alternates",
      len(probes) >= 2 and [a for _m, a, _t in probes] == list(PROBE_ADDRS[:len(probes)]) and
      [m for m, _a, _t in probes] == [1, 2, 1, 2][:len(probes)], [(m, f"0x{a:02X}") for m, a, _t in probes])
check("each invalid address: a real Address NACK, attributed by its master's own report, logged as an error",
      probes and all(t.addr_nack and t.master_method == "telemetry" for _m, _a, t in probes) and all(
          any(ev.severity == "error" and f"0x{a:02X}" in ev.message for ev in e.events) for _m, a, _t in probes),
      [(t.id, t.ack_seq(), t.master_method) for _m, _a, t in probes])
arbs = [t for k, _m, _a, t in errs if k == "ARB"]
arb_evs = [ev for ev in e.masters.arb_events if ev.winner is not None and any(ev.winner is t for t in arbs)]
pairs = [(ev.master_id, ev.addr, ev.winner.master_id, ev.winner.address) for ev in arb_evs]
check("each arbitration: a real arb_lost from a master's firmware, the winner packet matched",
      len(arbs) >= 2 and len({id(ev.winner) for ev in arb_evs}) == len(arbs), pairs)
want_pairs = {tuple(sorted(p)) for p in ARB_PAIRS[:len(arbs)]}
got_pairs = {tuple(sorted((a, wa))) for _l, a, _wm, wa in pairs}
check("arbitration between DIFFERENT slave pairs (rotating), never counted as a NACK",
      len(got_pairs) >= min(2, len(arbs)) and got_pairs <= {tuple(sorted(p)) for p in ARB_PAIRS} and all(
          (s.stats_latest or {}).get("data_nack", 0) == 0 for s in e.masters.state.values()),
      dict(pairs=[(f"M{l} lost at 0x{a:02X}", f"M{wm} won 0x{wa:02X}") for l, a, wm, wa in pairs], expected=want_pairs))
# every played packet: full stages (long OLED writes: first 8 bytes in full, rest condensed), played for its whole time
bad_stage, short = [], []
for i, p in enumerate(plays):
    t, ev = p["txn"], p["events"]
    nbytes = len(t.bytes)
    acks = [s for s in ev if s.kind == "ack"]
    sig = lambda xs: [(x.kind, x.byte, x.ack) for x in xs]    # structure (notes are refined once a report matches)
    if sig(ev) != sig(capped_stages(t)):
        bad_stage.append((t.id, "not the guided view"))
    elif sig(ev) == sig(stages(t)) and (len(acks) != nbytes or any(s.text.startswith("DATA ×") for s in ev)):
        bad_stage.append((t.id, nbytes, len(acks)))
    elif sig(ev) != sig(stages(t)) and not (t.address == 0x3C and any("condensed" in s.text for s in ev)
                                  and all(s.ack is not False for s in ev if s.kind == "ack")):
        bad_stage.append((t.id, "condensed a non-OLED packet or hid a NACK"))
    if i + 1 < len(plays):
        on = plays[i + 1]["t"] - p["t"]
        need = sum(0.5 if s.kind == "rw" else 1.0 for s in ev) * p["stage_s"] * 0.9
        if on < need:
            short.append((t.id, round(on, 2), round(need, 2)))
check("every played packet shown byte by byte (one ACK/NACK per byte; OLED text writes: first 8 bytes, rest condensed)",
      plays and not bad_stage, bad_stage[:5] or f"{len(plays)} packets")
check("every packet stays on screen for its full stage time before the next command", plays and not short, short[:5])
# bus: only the commanded transfers (outside the ARB bursts)
g_pk = [t for t in e.packets[n0:] if plays and plays[0]["txn"].t_start <= t.t_start <= plays[-1]["txn"].t_start]
arb_ids = set()
for ev in e.masters.arb_events:
    if ev.winner is not None:
        arb_ids.add(id(ev.winner))
burst = [t for t in g_pk if t.master_method == "telemetry" and id(t) not in {id(p["txn"]) for p in plays}]
results["_unplayed_bus_packets"] = dict(ok=True, detail=str([(t.id, t.master_id, f"0x{t.address:02X}") for t in burst][:40]))
non_arb_extra = [t for t in burst if t.address not in {a for pr in ARB_PAIRS for a in pr} or False]
check("bus: no transfer except the commanded ones (ARB bursts aside)", not non_arb_extra,
      [(t.id, f"0x{t.address:02X}") for t in non_arb_extra][:10])
check("all guided packets attributed by telemetry (0 unknown), 0 lost records, 0 stream gaps",
      all(p["txn"].master_id in (1, 2) for p in plays) and e.lost_records == 0 and e.stream_gaps == 0 and
      not e.transport_errors, f"lost {e.lost_records} gaps {e.stream_gaps} framing {e.transport_errors}")
def wave_of(t):
    """What the waveform showed while t played; if that moment fell inside a screenshot, show it again and read it."""
    if WAVE.get(t.id) is None:
        w.p_wave.show_packet(t, synchronize=False)
        pump(0.6)
        segs = sorted(getattr(w.p_wave.decoder_track, "segs", []), key=lambda s: s["x0"])
        WAVE[t.id] = ([s["kind"] for s in segs], len(w.p_wave._arb_items), w.p_wave.alert.text()) if segs else None
    return WAVE.get(t.id)


pw = [wave_of(t) for _m, _a, t in probes]
check("waveform: invalid-address packet decoded above SCL/SDA (S, ADDR, red NACK, P) with the error banner",
      pw and all(v and v[0][:1] == ["S"] and "nack" in v[0] and v[0][-1:] == ["P"] and "0x" in v[2] for v in pw),
      pw[:2])
aw = [WAVE.get(t.id) for t in arbs]
check("waveform: arbitration winner decoded (S, ADDR, ACK, DATA, P) with the amber collision marker",
      aw and all(v and v[0][:1] == ["S"] and "ack" in v[0] and v[0][-1:] == ["P"] and v[1] > 0 and
                 "ARBITRATION" in v[2].upper() for v in aw), [(v[0][:8], v[1], v[2][:80]) if v else None for v in aw])
# ------------------------------------------------------------------ 2.6-live: sensors + OLED from the real bus
b = e.sensors.bmp
check("DO CAL: BMP280 calibration captured from the bus (not simulated)", b.calib and not b.calib_simulated,
      (bool(b.calib), b.calib_simulated, b.reason))
check("temperature / pressure decoded from the captured BMP280 bytes",
      b.temperature is not None and 0 < b.temperature.value < 50 and b.pressure is not None and
      800 < b.pressure.value < 1100, (b.temperature and b.temperature.value, b.pressure and b.pressure.value))
oled_plays = [p["txn"] for p in plays if p["txn"].address == 0x3C and p["txn"].frames and p["txn"].frames[0].rw == "W"]
texts = {}
for t in oled_plays:
    data = list(t.frames[0].data)
    m = [ln for ln in oled_text_write_meaning(data) if "text \"" in ln]
    texts.setdefault(t.master_id, []).append([ln.split('"')[1] for ln in m])
ok_text = all(any(len(x) == 3 and x[0].startswith(f"M{mid} ") and ":" in x[0] and x[1].startswith("T ") and
                  x[1].endswith(" C") and x[2].startswith("P ") and x[2].endswith(" HPA") for x in texts.get(mid, []))
              for mid in (1, 2))
check("OLED: each master writes its LIVE time / temperature / pressure (text read back from the captured pixels)",
      ok_text, {k: v[-1:] for k, v in texts.items()})
rows = e.sensors.oled_pixels()
lit = [sum(sum(r) for r in rows[pg * 8:pg * 8 + 8]) for pg in range(8)]
check("OLED mirror: Master 1 lines on pages 0-2, Master 2 lines on pages 4-6 (captured RAM)",
      all(lit[pg] > 20 for pg in (0, 1, 2, 4, 5, 6)), lit)
w.nav.setCurrentRow(NAV_INDEX["sensor"])
pump(2.0)
check("Sensor page shows the CAPTURED OLED pixels and the decoded readings",
      w.p_sens.preview_src.text().startswith("CAPTURED") and "°C" in w.p_sens.temp.text(),
      (w.p_sens.preview_src.text()[:90], w.p_sens.temp.text()))
shot("06_sensor_monitoring")

h = e.health_summary()
check("capture stays trustworthy (health judged; the injected NACKs count as the real faults they are)",
      h["state"] in ("HEALTHY", "DEGRADED") and e.measurement_trust()[0], (h["state"], h.get("reason")))
w.btn_connect_all.click() if w.btn_connect_all.text() == "Disconnect all" else None
pump(2.0)
w.close()
pump(0.5)
check("no Python / Qt errors, no modal boxes", not ERRORS and not QT_ERRORS and not BOXES, (ERRORS[:1], QT_ERRORS[:2], BOXES[:2]))
check("user's native QSettings untouched", native_settings() == NATIVE_BEFORE)
(OUT / "guided_report.json").write_text(json.dumps(dict(results=results, failures=failures, history=[
    (round(t - t0, 1), lab, pid, out) for t, lab, pid, out in hist]), indent=1, default=str, ensure_ascii=False),
    encoding="utf-8")
print(json.dumps(dict(failures=failures), ensure_ascii=False), flush=True)
os._exit(0 if not failures else 1)
