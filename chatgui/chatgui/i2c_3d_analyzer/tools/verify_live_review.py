"""LIVE HARDWARE review: turn order, SEQ, PROBE, ARB and every tab, on the real bench (Real Hardware Mode only).

Analyzer COM28 + Master 1 COM32 + Master 2 COM34 (controller firmware 2.4-turns), slaves DS3231 0x68, BMP280 0x76,
SSD1306 0x3C.  Starts the GUI like main.py, presses the GUI's own buttons and checks on CAPTURED bus traffic:
  1. Real-time follow: capture order, never more than ~2.5 s behind the bus
  2. SEQ ON (500 ms): Master 1 RTC -> BMP280 -> OLED, then Master 2 RTC -> BMP280 -> OLED, alternating; each transfer
     alone on the bus; both directions per pair (write pointer + read data, ACKs); the 3D view shows every SEQ packet in
     order; every tab rendered (screenshots); all packets attributed, no lost records, no stream gaps
  3. PROBE 2A -> a real Address NACK, logged, Visualize Error Waveform (S, ADDR 0x2A W, red NACK, P)
  4. ARB -> a real arb_lost from a master's firmware, winner packet matched, Device Activity -> Arbitration, Visualize
     Arbitration with the amber collision marker and the packet decoded above SCL/SDA
The window ignores mouse / keyboard; QSettings go to a temp INI; modal boxes are logged instead of shown.
Usage: python -X utf8 tools/verify_live_review.py      Writes work/verification/live_review/*.png + live_review_report.json
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
OUT = ROOT / "work" / "verification" / "live_review"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS = []
sys.excepthook = lambda t, v, tb: (ERRORS.append("".join(traceback.format_exception(t, v, tb))),
                                   sys.__excepthook__(t, v, tb))

from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer, QtMsgType, qInstallMessageHandler  # noqa: E402
from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

BOXES = []


def _box(kind):
    def f(*a, **k):
        BOXES.append((kind, str(a[1] if len(a) > 1 else ""), str(a[2] if len(a) > 2 else "")[:300]))
        return QMessageBox.StandardButton.Ok
    return f


for _k in ("warning", "information", "critical", "question"):
    setattr(QMessageBox, _k, staticmethod(_box(_k)))
QT_ERRORS = []
qInstallMessageHandler(lambda kind, _c, msg: QT_ERRORS.append(msg) if kind in (QtMsgType.QtCriticalMsg,
                                                                                QtMsgType.QtFatalMsg) else None)


def native_settings():
    s = QSettings(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, "I2CAnalyzer", "I2C3DAnalyzer")
    return {k: str(s.value(k)) for k in s.allKeys()}


NATIVE_BEFORE = native_settings()
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tempfile.mkdtemp(prefix="review_settings_"))
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
from gui.main_window import NAV_INDEX, MainWindow  # noqa: E402

w = MainWindow()
w.resize(1680, 980)
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED LIVE REVIEW (input disabled)")
w.show()
assert w.demo is None and w.presentation is None, "Real Hardware Mode only"
loop = QEventLoop()
results, failures = {}, []
REC = []                                      # (monotonic, master_id, record) controller review records
_orig_ingest = w.engine.ingest_master_record


def _ingest(slot, d, *a, **k):
    if d.get("type") in ("seq", "probe", "arb_test", "turn_start", "turn_end", "arb_lost", "hello"):
        REC.append((time.monotonic(), d.get("master_id", slot), dict(d)))
    return _orig_ingest(slot, d, *a, **k)


w.engine.ingest_master_record = _ingest
PLAYED, LAGS = [], []
SKIPS, SHOTS = [], []                 # (monotonic, packet id, age vs bus s, why) / (monotonic, tag)
_rtq = w._rt_queue
_orig_pop = _rtq.pop


POPPED, SELECTED = [], []
from analysis.playback import Playback as _PB  # noqa: E402
_orig_select = _PB.select


def _select(self, txn, *a, **k):
    SELECTED.append((time.monotonic(), txn.id, k.get("packet_s"), k.get("pinned", a[0] if a else True)))
    return _orig_select(self, txn, *a, **k)


_PB.select = _select


def _pop(*a, **k):
    before = [t for t, _s in _rtq.q]
    got = _orig_pop(*a, **k)
    if got is not None:
        POPPED.append((time.monotonic(), got.id))
    after = {id(t) for t, _s in _rtq.q}
    bus = k.get("bus_now_ns")
    for t in before:
        if id(t) not in after and t is not got:
            SKIPS.append((time.monotonic(), t.id, round((bus - t.t_start) / 1e9, 2) if bus else None,
                          f"queue {len(before)}"))
    return got


_rtq.pop = _pop
REFRESH = []                          # (start monotonic, duration s) of every MainWindow._refresh
_orig_refresh = w._refresh


def _timed_refresh(*a, **k):
    t = time.perf_counter()
    try:
        return _orig_refresh(*a, **k)
    finally:
        REFRESH.append((time.monotonic(), time.perf_counter() - t))


w._refresh = _timed_refresh
try:
    w.timer.timeout.disconnect()
except TypeError:
    pass
w.timer.timeout.connect(w._refresh)


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        pb = w.playback
        if pb.txn is not None and (not PLAYED or PLAYED[-1][1] is not pb.txn):
            PLAYED.append((time.monotonic(), pb.txn))
        if pb.txn is not None and not pb.finished and w.bus_now_ns():
            LAGS.append((time.monotonic(), (w.bus_now_ns() - pb.txn.t_start) / 1e9))
        if until is not None and until():
            return True
    return until() if until is not None else True


def check(name, ok, detail=""):
    results[name] = dict(ok=bool(ok), detail=str(detail)[:1500])
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  ->  {str(detail)[:300]}"), flush=True)
    if not ok:
        failures.append(name)


def shot(tag, page=None):
    SHOTS.append((time.monotonic(), tag))
    if page is not None:
        w.nav.setCurrentRow(NAV_INDEX[page])
        pump(1.2)
    w.grab().save(str(OUT / f"{tag}.png"))


def ah(a):
    return f"0x{a:02X}" if a is not None else "?"


# ------------------------------------------------------------------ 1. connect, real-time
w.nav.setCurrentRow(NAV_INDEX["network"])
w.btn_connect_all.click()
ok = pump(30, lambda: w.capturing and all(s.clock.locked for s in w.engine.masters.state.values()))
e = w.engine
check("Connect all: analyzer + Master 1 + Master 2 capturing (Real Hardware Mode)",
      ok and w.capturing and e.source == "live" and w.demo is None, f"capturing={w.capturing} source={e.source}")
fw = {mid: (s.hello or {}).get("fw_version") for mid, s in e.masters.state.items()}
check("both masters report firmware 2.4-turns", all(v == "2.4-turns" for v in fw.values()), fw)
check("Real-time playback selected", w._realtime(), w.visual_speed.currentText())
t_rt = time.monotonic()
pump(25)
rt_lags = [l for t, l in LAGS if t >= t_rt]
rt_played = [p for t, p in PLAYED if t >= t_rt]
check("Real-time: packets play strictly in capture order", rt_played and all(
    a.t_start < b.t_start for a, b in zip(rt_played, rt_played[1:])), f"{len(rt_played)} played")
check("Real-time: never more than ~2.5 s behind the bus", rt_lags and max(rt_lags) <= 2.5,
      f"max lag {max(rt_lags):.2f} s over {len(rt_lags)} frames" if rt_lags else "no frames")
shot("01_realtime_3d")

# ------------------------------------------------------------------ 2. SEQ
w.seq_gap.setCurrentIndex(w.seq_gap.findData(500))
t_seq_cmd = time.monotonic()
w.btn_seq.setChecked(True)
acked = pump(10, lambda: {m for _t, m, d in REC if d["type"] == "seq" and d.get("enabled") and _t >= t_seq_cmd} >= {1, 2})
check("SEQ ON 500 acknowledged by both masters", acked,
      [(m, d.get("enabled"), d.get("gap_ms")) for _t, m, d in REC if d["type"] == "seq"])
# wait until both masters have started a SEQ turn, then record ~2 full rounds
pump(30, lambda: {m for _t, m, d in REC if d["type"] == "turn_start" and d.get("seq") and _t >= t_seq_cmd} >= {1, 2})
t_seq0 = time.monotonic()
seq_bus0 = w.bus_now_ns()
for page in ("network", "packets", "waveform", "sensor", "chip", "error"):
    pump(6.5)
    shot(f"02_seq_{page}", page)
w.nav.setCurrentRow(NAV_INDEX["network"])
pump(12)
seq_bus1 = w.bus_now_ns()
seq_pk = [t for t in e.packets if seq_bus0 <= t.t_start <= seq_bus1]
gaps = [(b.t_start - (a.t_end or a.t_start)) / 1e9 for a, b in zip(seq_pk, seq_pk[1:])]
check("SEQ: every transfer stands alone (>= 0.45 s idle bus between packets)", gaps and min(gaps) >= 0.45,
      f"{len(seq_pk)} packets, gaps min {min(gaps):.3f} s" if gaps else "none")
runs = []
for t in seq_pk:
    a = ah(t.address)
    if runs and runs[-1][0] == t.master_id:
        if runs[-1][1][-1] != a:
            runs[-1][1].append(a)
    else:
        runs.append([t.master_id, [a]])
full = runs[1:-1]                                         # the first / last turn may be cut by the window
order_ok = full and all(r[1] == ["0x68", "0x76", "0x3C"] for r in full) and all(
    a[0] != b[0] for a, b in zip(full, full[1:])) and {r[0] for r in full} == {1, 2}
check("SEQ: Master 1 RTC→BMP280→OLED, then Master 2 RTC→BMP280→OLED, alternating", order_ok,
      [(m, s) for m, s in runs])


def pair_ok(t, addr):
    fr = t.frames
    if t.status != "ok" or t.master_method != "telemetry":
        return False
    if addr == 0x68:
        return (len(fr) == 2 and fr[0].rw == "W" and fr[0].data[:1] == [0x00] and fr[1].rw == "R"
                and len(fr[1].data) >= 7 and fr[0].addr_ack and fr[1].addr_ack and fr[1].acks[-1] is False)
    if addr == 0x76:
        return (len(fr) == 2 and fr[0].rw == "W" and fr[0].data[:1] == [0xF7] and fr[1].rw == "R"
                and len(fr[1].data) == 6 and fr[0].addr_ack and fr[1].addr_ack)
    return (len(fr) == 1 and fr[0].rw == "W" and fr[0].data[:1] in ([0x00], [0x40]) and fr[0].addr_ack
            and all(fr[0].acks))


for mid in (1, 2):
    for addr, desc in ((0x68, "RTC: write 0x00, read time bytes"), (0x76, "BMP280: write 0xF7, read 6 P/T ADC bytes"),
                       (0x3C, "OLED: write 0x00/0x40, every byte ACKed by the OLED")):
        mine = [t for t in seq_pk if t.master_id == mid and t.address == addr]
        good = [t for t in mine if pair_ok(t, addr)]
        kinds = sorted({t.frames[0].data[0] for t in mine if t.frames and t.frames[0].data}) if addr == 0x3C else None
        check(f"SEQ Master {mid} ↔ {desc}", mine and len(good) == len(mine) and (addr != 0x3C or {0x00, 0x40} <= set(kinds)),
              f"{len(good)}/{len(mine)} packets" + (f", first bytes {[hex(k) for k in kinds]}" if kinds else ""))
played_seq = [t for _tt, t in PLAYED if seq_bus0 <= t.t_start <= seq_bus1]
captured_cmp = [t for t in seq_pk if t.t_start <= seq_bus1 - 3e9 and (not played_seq or t.t_start >= played_seq[0].t_start)]
missing = [t.id for t in captured_cmp if not any(p is t for p in played_seq)]
_rf = [(t, d) for t, d in REFRESH if t >= t_seq0]
_gaps = sorted(b[0] - a[0] for a, b in zip(_rf, _rf[1:]))
_durs = sorted(d for _t, d in _rf)
results["_refresh"] = dict(ok=True, detail=(
    f"{len(_rf)} refreshes · interval p50 {_gaps[len(_gaps) // 2]:.3f} p95 {_gaps[int(len(_gaps) * .95)]:.3f} max "
    f"{_gaps[-1]:.3f} s · refresh cost p50 {_durs[len(_durs) // 2]:.3f} p95 {_durs[int(len(_durs) * .95)]:.3f} max "
    f"{_durs[-1]:.3f} s") if len(_gaps) > 2 else "")
results["_seq_skips"] = dict(ok=True, detail=str([(round(t - t_seq0, 1), i, age, why) for t, i, age, why in SKIPS
                                                   if t >= t_seq0][:40]))
results["_shots"] = dict(ok=True, detail=str([(round(t - t_seq0, 1), g) for t, g in SHOTS if t >= t_seq0]))
_pt = [(round(tt - t_seq0, 1), t.id) for tt, t in PLAYED if tt >= t_seq0]
results["_seq_played"] = dict(ok=True, detail=str(_pt[:120]))
# "shown" = selected by the playback (its own trace, not a 20 ms sampler) AND on screen >= 80 ms before the next one
_sel = [(tt, i) for tt, i, _ps, _pn in SELECTED]
_on = {i: (_sel[k + 1][0] - tt if k + 1 < len(_sel) else 9.9) for k, (tt, i) in enumerate(_sel)}
played_seq = [t for t in seq_pk if t.id in _on]
_short = [(i, round(_on[i], 3)) for i in (t.id for t in seq_pk) if i in _on and _on[i] < 0.075]
results["_seq_on_screen"] = dict(ok=True, detail=f"min on screen {min(_on[t.id] for t in played_seq):.3f} s, "
                                                 f"too short {_short[:8]}" if played_seq else "")
_missing = [t.id for t in seq_pk if not any(p is t for _tt, p in PLAYED)]
results["_seq_missing_trace"] = dict(ok=True, detail=str([
    (i, "popped" if any(x == i for _t, x in POPPED) else "never popped",
     [(round(tt - t_seq0, 2), round(ps or 0, 3), pn) for tt, x, ps, pn in SELECTED if x == i],
     next((t.master_method for t in seq_pk if t.id == i), None)) for i in _missing]))
captured_cmp = [t for t in seq_pk if t.t_start <= seq_bus1 - 3e9 and (not played_seq or t.t_start >= played_seq[0].t_start)]
missing = [t.id for t in captured_cmp if t.id not in _on]
sel_order = [i for _tt, i in _sel if any(t.id == i for t in seq_pk)]
check("SEQ: the 3D view showed every SEQ packet in order (none skipped)",
      captured_cmp and not missing and sel_order == sorted(sel_order, key=lambda i: next(
          t.t_start for t in seq_pk if t.id == i)),
      f"{len(played_seq)} shown, {len(captured_cmp)} comparable captured, missing {missing[:10]}")
check("SEQ: every packet stayed on screen >= ~80 ms (never replaced unseen)", played_seq and not _short,
      results["_seq_on_screen"]["detail"])
seq_lags = [l for t, l in LAGS if t >= t_seq0]
check("SEQ: never more than ~2.5 s behind the bus", seq_lags and max(seq_lags) <= 2.5,
      f"max {max(seq_lags):.2f} s" if seq_lags else "")
s_ = e.sensors
check("Sensor Monitoring: RTC time + BMP280 temperature / pressure decoded from the bus",
      s_.rtc.time is not None and s_.bmp.temperature is not None and s_.bmp.pressure is not None,
      f"rtc {s_.rtc.time.value[:3] if s_.rtc.time else None} · {s_.bmp.temperature.value if s_.bmp.temperature else None:.2f} °C"
      if s_.bmp.temperature else "missing")
mx = {(r["master_id"], r["addr"]): r["bus_packets"] for r in e.masters.matrix([0x68, 0x76, 0x3C])}
check("Device Activity: both masters with all three targets on the bus",
      all(mx.get((m, a), 0) > 0 for m in (1, 2) for a in (0x68, 0x76, 0x3C)), mx)
check("all packets attributed by telemetry (0 unknown), 0 lost records, 0 stream gaps",
      not [t for t in e.packets if t.master_id is None] and e.lost_records == 0 and e.stream_gaps == 0
      and not e.transport_errors,
      f"unknown {sum(1 for t in e.packets if t.master_id is None)} · lost {e.lost_records} · gaps {e.stream_gaps} · "
      f"framing {e.transport_errors}")

# ------------------------------------------------------------------ 3. PROBE 2A
w.probe_master.setCurrentIndex(w.probe_master.findData(1))
w.probe_addr.setText("0x2A")
t_probe = time.monotonic()
w.btn_probe.click()
got = pump(25, lambda: any(t.address == 0x2A for t in e.packets))
pr = next((t for t in reversed(e.packets) if t.address == 0x2A), None)
if pr is not None:                       # the master's own report arrives on its USB port shortly after the packet
    pump(3.0, lambda: pr.master_id is not None)
check("PROBE 2A: a real 0x2A transfer on the bus with an Address NACK", got and pr is not None and pr.addr_nack,
      f"packet #{pr.id} {pr.ack_seq()}" if pr else "not captured")
check("PROBE 2A: attributed to Master 1 by its telemetry", pr is not None and pr.master_id == 1 and
      pr.master_method == "telemetry", f"{getattr(pr, 'master_id', None)} {getattr(pr, 'master_method', '')}")
errs = [ev for ev in e.events if ev.severity == "error" and "0x2A" in ev.message]
check("PROBE 2A: logged in Error & Diagnostics as an error", errs, errs[-1].message[:200] if errs else "")
w.nav.setCurrentRow(NAV_INDEX["error"])
pump(1.0)
w.p_err.btn_vis_err.click()
pump(2.0)
wv = w.p_wave
kinds = [s["kind"] for s in sorted(getattr(wv.decoder_track, "segs", []), key=lambda s: s["x0"])]
check("Visualize Error Waveform: the 0x2A packet, labels S, ADDR 0x2A W, red NACK, P above SCL/SDA",
      wv.txn is pr and kinds[:1] == ["S"] and "addr" in kinds and "nack" in kinds and kinds[-1:] == ["P"],
      f"shown #{getattr(wv.txn, 'id', None)} {kinds}")
check("Error banner above the waveform states the cause", wv.alert.isVisible() and "0x2A" in wv.alert.text(),
      wv.alert.text()[:200])
shot("03_error_waveform")

# ------------------------------------------------------------------ 4. ARB
w.nav.setCurrentRow(NAV_INDEX["network"])
pump(1.0)
t_arb = time.monotonic()
arb0 = e.masters.arb_total
w.btn_arb.click()
done = pump(20, lambda: {m for _t, m, d in REC if d["type"] == "arb_test" and _t >= t_arb} >= {1, 2})
tests = {m: d for _t, m, d in REC if d["type"] == "arb_test" and _t >= t_arb}
check("ARB: both masters ran the arbitration test", done, tests)
pump(4.0)
evs = list(e.masters.arb_events)[-(e.masters.arb_total - arb0):] if e.masters.arb_total > arb0 else []
check("ARB: real arb_lost reported by a master's firmware", evs,
      [(ev.master_id, hex(ev.addr), ev.attempt, ev.retry) for ev in evs])
for ev in evs:
    if ev.winner is None:
        e.masters._resolve_winner(ev)
won = [ev for ev in evs if ev.winner is not None]
check("ARB: the winning bus packet was matched", won,
      [(ev.master_id, getattr(ev.winner, "id", None), ah(getattr(ev.winner, "address", None)),
        getattr(ev.winner, "master_id", None)) for ev in evs])
check("Arbitration never counted as a NACK", all((s.stats_latest or {}).get("data_nack", 0) == 0
                                                 for s in e.masters.state.values()),
      {m: s.stats_latest for m, s in e.masters.state.items()})
w.nav.setCurrentRow(NAV_INDEX["chip"])
pump(1.5)
dev = w.p_dev
check("Device Activity → Arbitration lists it", dev.arb.rowCount() > 0, dev.arb.rowCount())
shot("04_device_activity_arbitration")
if dev.arb.rowCount():
    dev.arb.selectRow(0)
dev.btn_vis_arb.click()
pump(2.0)
kinds = [s["kind"] for s in sorted(getattr(wv.decoder_track, "segs", []), key=lambda s: s["x0"])]
check("Visualize Arbitration: the winner's waveform with the ARBITRATION banner",
      won and wv.txn is won[-1].winner and "ARBITRATION" in wv.alert.text().upper(), wv.alert.text()[:200])
check("Amber collision marker drawn above the waveform", len(wv._arb_items) > 0, len(wv._arb_items))
wt = won[-1].winner if won else None
check("Arbitration packet fully decoded above SCL/SDA (S, ADDR, ACK, DATA, P)",
      kinds[:1] == ["S"] and "addr" in kinds and "ack" in kinds and "data" in kinds and kinds[-1:] == ["P"],
      f"{kinds[:12]} · winner #{getattr(wt, 'id', None)} bit times "
      f"{all(b.bit_times for b in wt.bytes) if wt is not None else None} raw_verified "
      f"{getattr(wt, 'raw_verified', None)}")
shot("05_arbitration_waveform")
arb_log = [ev for ev in e.events if ev.kind == "Arbitration lost"]
check("Error & Diagnostics logs the arbitration loss (Multi-master, not a NACK)", arb_log,
      arb_log[-1].message[:220] if arb_log else "")
w.nav.setCurrentRow(NAV_INDEX["error"])
pump(1.2)
shot("06_error_diagnostics_arbitration")
w.p_wave.show_packet(pr, synchronize=False)                 # something else on display first
pump(0.5)
w.p_err.btn_vis_arb.click()                                 # Visualize Arbitration from Error & Diagnostics too
pump(2.0)
check("Error & Diagnostics → Visualize Arbitration opens the winner with the amber marker",
      won and wv.txn is won[-1].winner and len(wv._arb_items) > 0 and "ARBITRATION" in wv.alert.text().upper(),
      f"#{getattr(wv.txn, 'id', None)} marks {len(wv._arb_items)}")
shot("07_arbitration_waveform_from_errors")

# ------------------------------------------------------------------ wrap up
w.btn_seq.setChecked(False)
pump(2.0)
w.btn_connect_all.click() if w.btn_connect_all.text() == "Disconnect all" else None
pump(2.0)
w.close()
pump(0.5)
check("no Python / Qt errors", not ERRORS and not QT_ERRORS, (ERRORS[:1], QT_ERRORS[:2]))
check("no modal message boxes", not BOXES, BOXES[:3])
check("user's native QSettings untouched", native_settings() == NATIVE_BEFORE)
rep = dict(results=results, failures=failures, errors=ERRORS, qt_errors=QT_ERRORS, boxes=BOXES,
           records=[(round(t - t_rt, 2), m, d) for t, m, d in REC if d["type"] != "hello"][-200:])
(OUT / "live_review_report.json").write_text(json.dumps(rep, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
print(json.dumps(dict(failures=failures), ensure_ascii=False), flush=True)
os._exit(0 if not failures else 1)
