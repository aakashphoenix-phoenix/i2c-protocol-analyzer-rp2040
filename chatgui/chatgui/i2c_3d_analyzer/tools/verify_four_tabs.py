"""Verify the Live Dashboard, Performance Monitoring, Error & Diagnostics, Sensor Monitoring and Device Activity in the real GUI
(same start-up as main.py, real Qt event loop), first in Presentation mode, then in Demo mode.

Writes work/verification/tabs_<mode>_<tab>.png and tabs_report.json.   Usage:  python -X utf8 tools/verify_four_tabs.py
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
OUT = ROOT / "work" / "verification"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS = []
sys.excepthook = lambda t, v, tb: (ERRORS.append("".join(traceback.format_exception(t, v, tb))), sys.__excepthook__(t, v, tb))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _ErrorLog(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            ERRORS.append(f"log {record.name}: {record.getMessage()}")


logging.getLogger().addHandler(_ErrorLog())
from PyQt6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, QtMsgType, qInstallMessageHandler  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QSurfaceFormat  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _qt_messages(kind, _ctx, msg):
    if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        ERRORS.append(f"Qt: {msg}")


qInstallMessageHandler(_qt_messages)
fmt = QSurfaceFormat(); fmt.setSamples(4); fmt.setDepthBufferSize(24)
QSurfaceFormat.setDefaultFormat(fmt)
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
from gui.theme import STYLESHEET  # noqa: E402
app.setStyleSheet(STYLESHEET)
from config import COLORS as C  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

HUMAN = []


class _InputSpy(QObject):
    def eventFilter(self, obj, ev):
        if ev.spontaneous() and ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel):
            HUMAN.append((round(time.monotonic(), 1), ev.type().name))
        return False


_spy = _InputSpy()
app.installEventFilter(_spy)
w = MainWindow()
w.resize(1680, 980)
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED VERIFICATION RUN (input disabled)")
w.show()
report = dict(failures=[], checks={}, shots=[], values={})
fail = report["failures"]
loop = QEventLoop()
DASH, PERF, SENS, DEV, ERR = 0, 4, 5, 6, 7


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(15, loop.quit)
        loop.exec()
        if until is not None and until():
            return True
    return until() if until is not None else True


def check(name, ok, detail=""):
    report["checks"][name] = dict(ok=bool(ok), detail=str(detail)[:1500])
    if not ok:
        fail.append(f"{name}: {str(detail)[:400]}")


def shot(tag):
    p = OUT / f"tabs_{tag}.png"
    w.grab().save(str(p))
    report["shots"].append(p.name)


def curve_len(c):
    x, _y = c.getData()
    return 0 if x is None else len(x)


def glow_len(pair_plot, key):
    # glow_curve returns a helper; read the data back from the plot's own curves (the visible line is the 2nd item)
    pw = pair_plot
    items = [it for it in pw.getPlotItem().listDataItems()]
    return max((curve_len(it) for it in items), default=0)


PAGE_LOG = []


def go(page):
    PAGE_LOG.append((round(time.time() - LIVE_T0[0], 2) if "LIVE_T0" in globals() else None, "page", page))
    w.nav.setCurrentRow(page)
    pump(1.5)


def expected_health(mode):
    """HEALTHY / DEGRADED only on trustworthy live hardware; demo and presentation are UNKNOWN (amber)."""
    return ("HEALTHY",) if mode == "live" else ("UNKNOWN",)


def colour_of(word):
    return QColor(C["green"] if word == "HEALTHY" else C["red"] if word == "DEGRADED" else C["amber"]).name()


def plot_fit(pw):
    """(points, data x-range, view x-range, fraction of the view the data spans) of the plot's longest curve."""
    best = None
    for it in pw.getPlotItem().listDataItems():
        x, _y = it.getData()
        if x is not None and len(x) and (best is None or len(x) > len(best)):
            best = x
    vx = pw.getPlotItem().viewRange()[0]
    if best is None or len(best) == 0:
        return 0, None, vx, 0.0
    lo, hi = float(min(best)), float(max(best))
    return len(best), (lo, hi), vx, (hi - lo) / max(1e-12, vx[1] - vx[0])


def verify_dashboard(mode):
    e = w.engine
    go(DASH)
    dp = w.p_dash
    fits = {}
    for name, pw in (("SCL frequency", dp.p_freq), ("Transactions/s", dp.p_tps), ("Bus Communication Health", dp.p_util)):
        n, data, vx, frac = plot_fit(pw)
        fits[name] = dict(points=n, data=data, view=[round(v, 6) for v in vx], fill=round(frac, 3))
        ok = n >= 2 and data is not None and vx[0] <= data[0] + 1e-9 and data[1] <= vx[1] + 1e-9 and frac >= 0.25
        check(f"{mode} Dashboard: {name} graph drawn inside its window and not squashed", ok, fits[name])
    report["values"][f"{mode}_dashboard_graphs"] = fits
    card = dp.cards["health"]
    word, col = card.value.text(), QColor(card._color).name()
    report["values"][f"{mode}_dashboard_health"] = (word, col, card.sub.text().split("\n")[0])
    check(f"{mode} Dashboard: Bus communication health card {expected_health(mode)}",
          word in expected_health(mode) and col == colour_of(word), (word, col))
    if mode != "live":
        check(f"{mode} Dashboard: health card says why (not live hardware)",
              "not live hardware" in card.sub.text().lower(), card.sub.text()[:160])
    dev_txt = dp.device_health_text.text()
    report["values"][f"{mode}_dashboard_device_health"] = dev_txt
    words = [part.rsplit(": ", 1)[-1] for part in dev_txt.split("  |  ") if ": " in part]
    check(f"{mode} Dashboard: per-device health {expected_health(mode)}",
          words and all(wd in expected_health(mode) for wd in words), dev_txt)
    # the selected-packet marker sits on the graph's relative clock (<= 0), on the selected packet
    txn = e.packets[len(e.packets) // 2]
    w.select_transaction(txn)
    pump(0.3)
    w._sync_playback()                       # marker placed for the packet playback is on right now
    txn = w.playback.txn or txn
    ref = dp._ref_s(e)
    v = dp.sel_line.value()
    report["values"][f"{mode}_dashboard_marker"] = dict(value=v, expected=None if ref is None else txn.t_start / 1e9 - ref)
    check(f"{mode} Dashboard: selected-packet marker on the relative clock",
          dp.sel_line.isVisible() and ref is not None and v <= 0.5 and abs(v - (txn.t_start / 1e9 - ref)) < 1.0,
          report["values"][f"{mode}_dashboard_marker"])
    shot(f"{mode}_dashboard")


def trust_mark(mode, where):
    if mode == "live":
        e = w.engine
        report["values"].setdefault("live_trust_timeline", []).append(
            (where, e.measurement_trust()[0], e.lost_records, e.transport_errors))


def verify_tabs(mode):
    e = w.engine
    verify_dashboard(mode)
    trust_mark(mode, "after Dashboard")
    # ---------------- Performance Monitoring
    go(PERF)
    pp = w.p_perf
    lens = {k: glow_len(pw, k) for k, pw in (("freq", pp.freq_plot),) }
    graph_points = {}
    for name, pw in (("SCL frequency", pp.freq_plot), ("Transactions/s", pp._rel_widgets[1]),
                     ("Utilization", pp._rel_widgets[2]), ("Duration", pp._rel_widgets[3]),
                     ("Throughput", pp._rel_widgets[4]), ("ACK/NACK", pp.p_ack), ("NACK rate", pp.health_plot)):
        graph_points[name] = glow_len(pw, name)
    report["values"][f"{mode}_perf_graph_points"] = graph_points
    pfits = {}
    for name, pw in zip(("SCL frequency", "Transactions/s", "Utilization", "Duration", "Throughput", "ACK/NACK",
                         "NACK rate"), pp._rel_widgets):
        n_, data, vx, frac = plot_fit(pw)
        pfits[name] = dict(points=n_, data=data, view=[round(v, 6) for v in vx], fill=round(frac, 3))
    report["values"][f"{mode}_perf_graph_fit"] = pfits
    check(f"{mode} Performance: every rolling graph inside its window and not squashed",
          all(f["points"] >= 2 and f["view"][0] <= f["data"][0] + 1e-6 and f["data"][1] <= f["view"][1] + 1e-6
              and f["fill"] >= 0.25 for f in pfits.values()), pfits)
    check(f"{mode} Performance: all 6 rolling graphs + NACK rate have curves",
          all(v >= 2 for v in graph_points.values()), graph_points)
    hs = pp.health_state.text().split("\n")[0]
    report["values"][f"{mode}_health"] = hs
    hcol = QColor(pp.health_state.styleSheet().split("color:")[-1].strip().rstrip(";")).name()
    check(f"{mode} Performance: Bus Communication Health {expected_health(mode)} in its colour",
          hs in expected_health(mode) and hcol == colour_of(hs), (hs, hcol))
    if mode != "live":
        check(f"{mode} Performance: health says why (not live hardware)",
              "not live hardware" in pp.health_state.text().lower(), pp.health_state.text()[:160])
    cmp = {k: v.text() for k, v in pp.cmp.items() if k[0] in (1, 2)}
    check(f"{mode} Performance: M1 vs M2 comparison filled", all(v != "—" for k, v in cmp.items() if k[1] in ("txns", "bytes")),
          cmp)
    roll = pp.stats["froll"].text()
    check(f"{mode} Performance: Rolling 1 s computed", roll != "—", roll)
    shot(f"{mode}_performance")
    trust_mark(mode, "after Performance")
    # ---------------- Error & Diagnostics
    go(ERR)
    ep = w.p_err
    hits = {}
    for a in ("0x3C", "0x68", "0x76"):
        ep.search.setText(a)
        pump(0.6)
        rows = [ep.model.rows[ep.proxy.mapToSource(ep.proxy.index(i, 0)).row()] for i in range(ep.proxy.rowCount())]
        hits[a] = len(rows)
        has = any(ev.addr == int(a, 16) for ev in e.events)
        ok = all(r.addr == int(a, 16) or a.lower() in f"{r.kind} {r.message} {r.category}".lower() for r in rows) and             (bool(rows) or not has)
        check(f"{mode} Errors: search {a} finds exactly the events of that address", ok, (len(rows), has))
    ep.search.setText("")
    report["values"][f"{mode}_search_hits"] = hits
    mm = [ev for ev in e.events if ev.category == "Multi-master"]
    report["values"][f"{mode}_multimaster_events"] = [(ev.kind, ev.message[:80]) for ev in mm[:8]]
    if mode != "live":                     # live: only what the bus really did (may be none on a quiet bus)
        check(f"{mode} Errors: Multi-master events logged", mm, len(mm))
    if mode in ("presentation", "demo"):
        arb = [ev for ev in mm if ev.kind == "Arbitration lost"]
        check(f"{mode} Errors: arbitration event logged", arb, len(arb))
    if mode == "presentation":
        nack = [ev for ev in e.events if ev.kind == "Address NACK"]
        check(f"{mode} Errors: injected NACK logged", nack, len(nack))
    vx, vy = ep.trend.getPlotItem().viewRange()
    check(f"{mode} Errors: trend X = last 10 min, Y from 0", abs(vx[0] + 10) < 1e-6 and abs(vx[1]) < 1e-6
          and abs(vy[0]) < 1e-6 and vy[1] >= 5, (vx, vy))
    shot(f"{mode}_errors")
    trust_mark(mode, "after Errors")
    # ---------------- Sensor Monitoring
    go(SENS)
    sp = w.p_sens
    clock, temp, press = sp.clock.text(), sp.temp.text(), sp.press.text()
    report["values"][f"{mode}_sensor"] = dict(clock=clock, date=sp.date.text(), temp=temp, press=press,
                                              preview=sp._preview_lines, calib=sp.bmp_kv["calib"].text(),
                                              identity=sp.bmp_kv["id"].text())
    check(f"{mode} Sensor: DS3231 clock decoded", clock != "--:--:--", clock)
    calib_txt = sp.bmp_kv["calib"].text()
    if mode == "live" and "not captured" in calib_txt:
        # live: the master read the calibration before this capture started; the GUI must say so, not invent values
        check(f"{mode} Sensor: BMP280 without captured calibration gives the reason, no value",
              temp == "—" and "calibration" in sp.bmp_reason.text().lower(), (temp, sp.bmp_reason.text()))
    else:
        check(f"{mode} Sensor: BMP280 temperature + pressure decoded", temp != "—" and press != "—", (temp, press))
    if mode == "live":
        check(f"{mode} Sensor: live values not labelled SIMULATED / demo",
              "simulated" not in (sp.date.text() + calib_txt).lower(), (sp.date.text(), calib_txt))
    check(f"{mode} Sensor: BMP280 identity 0x58 confirmed", "confirmed" in sp.bmp_kv["id"].text().lower(),
          sp.bmp_kv["id"].text())
    if mode in ("presentation", "demo"):
        check(f"{mode} Sensor: date is Monday 16-11-2026", sp.date.text().startswith("Mon  16-11-2026"), sp.date.text())
    import datetime
    try:
        wd_txt, dmy = sp.date.text().split()[:2]
        real = datetime.datetime.strptime(dmy, "%d-%m-%Y").strftime("%a")
    except ValueError:
        wd_txt, real = sp.date.text(), None
    check(f"{mode} Sensor: weekday matches the calendar date", wd_txt == real, (sp.date.text(), real))
    tl, pl = glow_len(sp.p_t, "t"), glow_len(sp.p_p, "p")
    if temp != "—":
        check(f"{mode} Sensor: temperature + pressure trends drawn", tl >= 1 and pl >= 1, (tl, pl))
    if temp != "—":
        check(f"{mode} Sensor: OLED preview shows TIME / DATE / TEMP / PRESS",
              not any("N/A" in str(l) for l in sp._preview_lines), sp._preview_lines)
    sb = sp._scroll.verticalScrollBar()
    geo = {nm: (c.geometry().getRect(), c.isVisible()) for nm, c in (("rtc", sp.rtc), ("bmp", sp.bmp), ("oled", sp.oled))}
    report["values"][f"{mode}_sensor_layout"] = dict(scroll=(sb.value(), sb.maximum()), cols=getattr(sp, "_cols", None),
                                                     viewport=sp._scroll.viewport().size().toTuple()
                                                     if hasattr(sp._scroll.viewport().size(), "toTuple") else
                                                     (sp._scroll.viewport().width(), sp._scroll.viewport().height()),
                                                     cards=geo)
    check(f"{mode} Sensor: page shown from the top (RTC card in view)", sb.value() == 0 and geo["rtc"][1], (sb.value(), geo))
    shot(f"{mode}_sensor")
    trust_mark(mode, "after Sensor")
    # ---------------- Device Activity
    go(DEV)
    dp = w.p_dev
    health = {dp.tbl.item(r, 0).text(): (dp.tbl.item(r, 11).text(), dp.tbl.item(r, 11).foreground().color().name())
              for r in range(dp.tbl.rowCount())}
    report["values"][f"{mode}_device_health"] = health
    green = QColor(C["green"]).name()
    slaves = {k: v for k, v in health.items() if k in ("0x68", "0x76", "0x3C")}
    nacked = {f"0x{ev.addr:02X}" for ev in e.events if ev.kind == "Address NACK" and ev.addr is not None}
    red = QColor(C["red"]).name()
    report["values"][f"{mode}_nacked_targets"] = sorted(nacked)
    if mode == "live":
        check(f"{mode} Device: working slaves HEALTHY in green",
              slaves and all(v[0] == "HEALTHY" and v[1] == green for k, v in slaves.items() if k not in nacked), slaves)
        check(f"{mode} Device: a target with a fresh NACK is DEGRADED in red",
              all(slaves[k][0] == "DEGRADED" and slaves[k][1] == red for k in nacked if k in slaves), slaves)
    else:
        amber = QColor(C["amber"]).name()
        tips = {dp.tbl.item(r, 0).text(): dp.tbl.item(r, 11).toolTip() for r in range(dp.tbl.rowCount())}
        check(f"{mode} Device: every device health UNKNOWN in amber (not live hardware)",
              health and all(v == ("UNKNOWN", amber) for v in health.values())
              and all("not live hardware" in t.lower() for t in tips.values()), (health, tips))
    mm_rows = [[dp.mm.item(r, c).text() for c in (0, 3, 4, 7)] for r in range(dp.mm.rowCount())]
    report["values"][f"{mode}_controllers"] = mm_rows
    check(f"{mode} Device: Controllers table populated", mm_rows and all(r[2] not in ("—", "0") for r in mm_rows), mm_rows)
    mx = [[dp.mx.item(r, c).text() for c in range(dp.mx.columnCount())] for r in range(dp.mx.rowCount())]
    check(f"{mode} Device: Master x Target populated", mx and any(int(r[9] or 0) > 0 for r in mx), mx[:3])
    report["values"][f"{mode}_arbitration_rows"] = dp.arb.rowCount()
    if mode in ("presentation", "demo"):
        check(f"{mode} Device: Arbitration table populated", dp.arb.rowCount() >= 1,
              [dp.arb.item(0, c).text() for c in range(8)] if dp.arb.rowCount() else None)
    act = {dp.act.item(r, 0).text(): (dp.act.item(r, 2).text(), dp.act.item(r, 3).text(), dp.act.item(r, 4).text())
           for r in range(dp.act.rowCount())}
    report["values"][f"{mode}_activity"] = act
    check(f"{mode} Device: activity rows ACTIVE with exact counts",
          act and all(v[0].startswith("ACTIVE") and "UNKNOWN" not in v[1] for k, v in act.items() if k in slaves), act)
    if mode == "live":
        check(f"{mode} Device: live activity not labelled SIMULATED",
              all("SIMULATED" not in v[0] for v in act.values()), act)
    heights = [list(b.opts.get("height", [])) for b in dp.act_bars]
    check(f"{mode} Device: Frames per device bar chart drawn", any(sum(h) > 0 for h in heights), heights)
    shot(f"{mode}_device_activity")


LIVE = "--live" in sys.argv
ONLY_LIVE = "--live-only" in sys.argv


LIVE_T0 = [time.time()]


def run_live():
    LIVE_T0[0] = time.time()
    PAGE_LOG.append((0.0, "connect", w.nav.currentRow()))
    w.btn_connect_all.click()
    ok = pump(30, lambda: w.capturing and w.engine.masters.can_attribute())
    report["values"]["live_connect"] = dict(capturing=w.capturing, source=w.engine.source,
                                            masters=[(r["master_id"], r["state"], r["connected"])
                                                     for r in w.engine.masters.rows()])
    check("live: Connect all -> analyzer capturing, both masters attributable, source live",
          ok and w.engine.source == "live", report["values"]["live_connect"])
    pump(30.0)
    report["values"]["live_packets"] = len(w.engine.packets)
    e = w.engine
    report["values"]["live_trust"] = dict(trust=e.measurement_trust(), transport_errors=e.transport_errors,
                                          lost_records=e.lost_records, overflows=e.fw_stats.get("overflows"),
                                          dropped=e.fw_stats.get("dropped"), synced=e.decoder.synced,
                                          disagreements=e.decode_disagreements)
    t0w = LIVE_T0[0]
    report["values"]["live_loss_events"] = [(round(ev.wall.timestamp() - t0w, 2), ev.category, ev.kind, ev.message[:120])
                                            for ev in e.events if ev.category in ("Transport", "Analyzer", "Capture")
                                            or "lost" in ev.kind.lower() or "disagree" in ev.kind.lower()][:30]
    report["values"]["live_page_log"] = PAGE_LOG[:]
    check("live: capture trustworthy (no loss) before checking health", e.measurement_trust()[0],
          report["values"]["live_trust"])
    check("live: real packets captured", len(w.engine.packets) > 20, len(w.engine.packets))
    verify_tabs("live")
    w.btn_connect_all.click() if w.btn_connect_all.text() == "Disconnect all" else None
    pump(2.0)


if ONLY_LIVE:
    run_live()
# ================================================================== Presentation mode
if not ONLY_LIVE:
    w.btn_present.click()
    w.set_stage_hold(True)
    w.btn_nack.setChecked(True)                                   # one injected NACK (a demonstrable address NACK)
    guard = 0
    while len(w.engine.packets) < 16 and guard < 600:
        w.btn_step.click()
        pump(0.03)
        guard += 1
    w.set_stage_hold(False)
    pump(3.0)
    verify_tabs("presentation")
    w.btn_present.click()                                         # presentation off
    pump(1.0)
    pump(60, lambda: w.net_run_state == "stopped")

    # ================================================================== Demo mode
    w.btn_demo.click()
    pump(25.0)
    verify_tabs("demo")
    w.btn_demo.click()
    pump(1.0)
    pump(30, lambda: w.net_run_state == "stopped")
if LIVE:
    run_live()

report["outside_input"] = HUMAN[:20]
check("no outside mouse / key input during the run", not HUMAN, HUMAN[:5])
report["python_or_qt_errors"] = ERRORS
check("no Python / Qt errors", not ERRORS, ERRORS[:2])
report["result"] = "PASS" if not fail else "FAIL"
w.close()
pump(0.3)
(OUT / "tabs_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: report[k] for k in ("result", "failures")}, indent=2, ensure_ascii=False), flush=True)
os._exit(0 if not fail else 1)
