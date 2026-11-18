"""LIVE HARDWARE verification of the 3D I2C network (no demo, no simulated records).

Launches the real GUI exactly like main.py (Real Hardware Mode), presses the real buttons and checks, on the traffic of the
attached boards (analyzer + Master 1 + Master 2):
  * routing     every dot runs Master 1 / Master 2 <-> target; the passive analyzer (Pico 2) is never a dot's endpoint
  * dots        every dot reaches its receiver (none cut), crossing time follows the stage pace
  * ACK / NACK  acknowledge dots use the dedicated ACK / NACK colours
  * Stop        the packet on display finishes (all stages + dots), then the network freezes and nothing new starts
  * Connect all after Stop: queue flushed, capture restarted, only fresh live packets are played

Usage:  python -X utf8 tools/verify_live_network.py [--observe 40]
Writes work/verification/live_*.png and work/verification/live_hw_report.json.
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


def _hook(t, v, tb):
    ERRORS.append("".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


sys.excepthook = _hook

import logging  # noqa: E402
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

fmt = QSurfaceFormat(); fmt.setSamples(4); fmt.setDepthBufferSize(24)       # same as main.py
QSurfaceFormat.setDefaultFormat(fmt)
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
app.setApplicationName("I2C Packet Analyzer")
app.setStyle("Fusion")
app.setFont(QFont("Segoe UI", 10))
from gui.theme import STYLESHEET  # noqa: E402
app.setStyleSheet(STYLESHEET)

import config  # noqa: E402
import gui.network_3d as net_mod  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

OBSERVE_S = float(sys.argv[sys.argv.index("--observe") + 1]) if "--observe" in sys.argv else 40.0
SPEED = float(sys.argv[sys.argv.index("--speed") + 1]) if "--speed" in sys.argv else None   # a toolbar speed

w = MainWindow()
w.resize(1680, 980)
w.show()
assert w.demo is None and not w.btn_demo.isChecked(), "demo mode must stay OFF"
w.nav.setCurrentRow(1)                     # 3D I2C Network page
if SPEED is not None:
    w.visual_speed.setCurrentIndex(w.visual_speed.findData(SPEED))
assert w.visual_speed.currentData() in (0.1, 0.25, 0.4, 0.5), "review speeds only"
net = w.p_net.net

DOTS = []          # every stage dot spawned on the 3D page


def pump(seconds, until=None):
    """Run a REAL (nested) Qt event loop in 20 ms slices - like app.exec() in main.py, so deferred deletes, timers and
    queued serial signals behave exactly as in the shipped GUI (processEvents() alone never runs deleteLater())."""
    end = time.monotonic() + seconds
    loop = QEventLoop()
    while time.monotonic() < end:
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        if until is not None and until():
            return True
    return until() if until is not None else True


def stray_labels():
    """Visible caption widgets on the 3D page that the layout no longer tracks (ghost labels)."""
    from PyQt6.QtWidgets import QLabel
    tracked = {id(x) for x in net._labels.values()} | {id(net._stage_label)}
    view = net.view
    return [lb.text() for lb in view.findChildren(QLabel) if lb.isVisible() and id(lb) not in tracked]


_orig_spawn = net._spawn_stage_particle


def spy_spawn(stage, txn):
    before = list(net.particles)
    _orig_spawn(stage, txn)
    for p in net.particles:
        if not any(p is q for q in before):
            DOTS.append(dict(p=p, stage=stage, txn=txn, mid=getattr(txn, "master_id", None), t=time.monotonic(),
                             wall=w.playback.stage_wall_seconds(), phase=PHASE[0], txn_id=txn.id,
                             tstart=txn.t_start, run=w.net_run_state))


net._spawn_stage_particle = spy_spawn
PHASE = ["connect"]
shots = []


def shot(tag):
    pm = w.grab()
    p = OUT / f"live_{tag}.png"
    pm.save(str(p))
    shots.append(str(p.relative_to(ROOT)))


report = dict(renderer="3D (OpenGL)" if net.is_3d else "2D fallback", ports={}, failures=[], notes=[])
fail = report["failures"]

# ------------------------------------------------------------------ 1. Connect all (real hardware)
report["ports"] = {"analyzer": w.port.currentData(), **{f"master{m}": c.currentData() for m, c in w.master_ports.items()}}
w.btn_connect_all.click()
ok = pump(25, lambda: w.capturing and w.engine.masters.can_attribute())
report["connect_all"] = dict(capturing=w.capturing, masters=[(r["master_id"], r["state"], r["clock"], r["connected"])
                                                          for r in w.engine.masters.rows()],
                             can_attribute=bool(w.engine.masters.can_attribute()), button=w.btn_connect_all.text())
if not w.capturing:
    fail.append("Connect all did not start the capture")
if not ok:
    report["notes"].append("masters not clock-aligned within 25 s (some packets may play unattributed)")

# ------------------------------------------------------------------ 2. observe live traffic
PHASE[0] = "live"
t0 = time.monotonic()
seen_ack = seen_nack = False
seen_m2 = set()
k = 0
while time.monotonic() - t0 < OBSERVE_S:
    pump(0.1)
    st = w.playback.current
    if st is not None and st.kind == "ack" and w.playback.stage_fraction > 0.5:
        if st.ack and not seen_ack:
            seen_ack = True; shot("ack")
        if st.ack is False and not seen_nack:
            seen_nack = True; shot("nack")
    t_now = w.playback.txn
    if st is not None and t_now is not None and t_now.master_id == 2 and w.playback.stage_fraction > 0.45:
        tag = "m2_tx" if st.direction == "W" and st.kind in ("addr", "data") else               "m2_rx" if st.direction == "R" and st.kind == "data" else None
        if tag and tag not in seen_m2:
            seen_m2.add(tag); shot(tag)
    if time.monotonic() - t0 > 10 * (k + 1):
        k += 1; shot(f"live_{k}")
report["live"] = dict(packets=len(w.engine.packets), dots=len([d for d in DOTS if d["phase"] == "live"]),
                      cut=net.particles_cut, arrived=net.particles_arrived,
                      packets_by_master={str(m): sum(1 for t in w.engine.packets if t.master_id == m)
                                         for m in (1, 2, None)},
                      packets_by_addr={f"0x{a:02X}": sum(1 for t in w.engine.packets if t.address == a)
                                       for a in sorted({t.address for t in w.engine.packets if t.address is not None})})

# ------------------------------------------------------------------ 3. Stop mid-transaction
PHASE[0] = "pre-stop"
pump(20, lambda: w.playback.txn is not None and not w.playback.finished and 1 <= w.playback.position <= 3)
stop_txn = w.playback.txn
stop_pos = w.playback.position
w.btn_stop.click()
PHASE[0] = "stop"
t_stop = time.monotonic()
report["stop"] = dict(state_after_click=w.net_run_state, button=w.btn_stop.text(), txn=getattr(stop_txn, "id", None),
                      position=stop_pos, stages=len(w.playback.events), capturing=w.capturing)
pump(0.6)
shot("stopping")
pump(240, lambda: w.net_run_state == "stopped")         # a 17-byte OLED frame plays ~2 min at 0.25x
report["stop"].update(drain_s=round(time.monotonic() - t_stop, 2), final_state=w.net_run_state,
                      same_txn=w.playback.txn is stop_txn, finished=w.playback.finished,
                      dots_in_flight=w._dots_in_flight(),
                      txns_during_drain=sorted({d["txn_id"] for d in DOTS if d["phase"] == "stop"}))
shot("stopped")
if w.net_run_state != "stopped":
    fail.append("Stop never reached the stopped state")
if w.playback.txn is not stop_txn:
    fail.append("another packet started while stopping")
if report["stop"]["txns_during_drain"] and report["stop"]["txns_during_drain"] != [stop_txn.id]:
    fail.append(f"dots of other packets during drain: {report['stop']['txns_during_drain']}")
last_stage_idx = len(w.playback.events) - 1
PHASE[0] = "frozen"
n_dots, arrived = len(DOTS), net.particles_arrived
pump(5)
report["frozen"] = dict(new_dots=len(DOTS) - n_dots, txn_unchanged=w.playback.txn is stop_txn,
                        particles=len(net.particles), packets_engine=len(w.engine.packets),
                        hud="STOPPED" in net.hud_row1.text())
if len(DOTS) != n_dots or w.playback.txn is not stop_txn:
    fail.append("the network kept running after Stop")

# ------------------------------------------------------------------ 4. Connect all = resume with fresh data
PHASE[0] = "resume"
old = {id(t) for t in w.engine.packets}
old_hw = w.engine.hw_now
report["resume"] = dict(button_before=w.btn_connect_all.text())
w.btn_connect_all.click()
report["resume"].update(capturing=w.capturing, state=w.net_run_state, packets_after_click=len(w.engine.packets),
                        queue=len(w._follow_queue), txn_after_click=getattr(w.playback.txn, "id", None))
pump(20)
resumed = [d for d in DOTS if d["phase"] == "resume"]
report["resume"].update(dots=len(resumed), packets=len(w.engine.packets),
                        stale_played=sum(1 for d in resumed if id(d["txn"]) in old),
                        masters=[(r["master_id"], r["state"], r["clock"]) for r in w.engine.masters.rows()])
shot("resumed")
if not w.capturing or w.net_run_state != "running":
    fail.append("Connect all did not resume")
if report["resume"]["stale_played"]:
    fail.append("Connect all replayed packets from before Stop")
if not resumed:
    fail.append("no dots after resume")

# ------------------------------------------------------------------ 5. per-dot checks (all phases)
ana_tap = (net.model.tap_x("SDA"), net_mod.RAIL_Y["SDA"])
bad_route, bad_col, cut_dots, timing = [], [], [], []
for d in DOTS:
    p, st, mid = d["p"], d["stage"], d["mid"]
    key = net.model.controller_key(mid)
    if "analyzer" in (p.src, p.dst):
        bad_route.append(f"#{d['txn_id']} {st.text}: analyzer endpoint")
    for end in (p.path[0], p.path[-1]):
        if abs(end[0] - ana_tap[0]) < 1e-6 and abs(end[1] - ana_tap[1]) < 1e-6:
            bad_route.append(f"#{d['txn_id']} {st.text}: starts/ends at analyzer tap")
    addr = st.address if st.address is not None else d["txn"].address
    want = net.model.path_to(addr, "SDA", master=net.model.resolve_master(mid))
    direction = "W" if st.kind == "cond" else st.direction
    start = want[0] if direction == "W" else want[-1]
    if max(abs(a - b) for a, b in zip(p.path[0], start)) > 1e-6:
        bad_route.append(f"#{d['txn_id']} {st.text} M{mid}: wrong origin")
    if key is not None and (key not in (p.src, p.dst)):
        bad_route.append(f"#{d['txn_id']} {st.text}: master {key} not an endpoint ({p.src}->{p.dst})")
    if st.kind == "ack":
        if p.color != (net_mod.ACK_COL if st.ack else net_mod.NACK_COL):
            bad_col.append(f"#{d['txn_id']} {st.text}: {p.color}")
    elif p.color != net_mod.master_color(net.model.resolve_master(mid), direction):
        bad_col.append(f"#{d['txn_id']} {st.text} M{mid}: {p.color}")
    if not p.done and d["phase"] not in ("resume",):
        cut_dots.append(f"#{d['txn_id']} {st.text} ({d['phase']}) at {p.s / p.total:.0%}")
    timing.append(p.travel_s)
report["dots"] = dict(total=len(DOTS), by_master={str(m): sum(1 for d in DOTS if d["mid"] == m) for m in (1, 2, None)},
                      by_target={f"0x{a:02X}": sum(1 for d in DOTS if d["txn"].address == a)
                                 for a in sorted({d["txn"].address for d in DOTS if d["txn"].address is not None})},
                      ack=sum(1 for d in DOTS if d["stage"].kind == "ack" and d["stage"].ack),
                      nack=sum(1 for d in DOTS if d["stage"].kind == "ack" and d["stage"].ack is False),
                      tx=sum(1 for d in DOTS if d["stage"].direction == "W" and d["stage"].kind != "ack"),
                      rx=sum(1 for d in DOTS if d["stage"].direction == "R" and d["stage"].kind == "data"),
                      travel_s=[round(min(timing), 3), round(sum(timing) / len(timing), 3), round(max(timing), 3)]
                      if timing else None,
                      particles_cut_counter=net.particles_cut,
                      bad_route=bad_route[:20], bad_colour=bad_col[:20], not_arrived=cut_dots[:20])
# uniform pace on real traffic: every stage dot of every device crosses in 0.8 x STAGE_S / speed (never faster)
from analysis.playback import Playback
spd = w.visual_speed.currentData()
want_travel = 0.8 * Playback.STAGE_S / spd
fast = [f"#{d['txn_id']} 0x{(d['txn'].address or 0):02X} {d['stage'].text}: {d['p'].travel_s:.2f}s" for d in DOTS
        if d["p"].travel_s < 0.97 * want_travel]
report["uniform_pace"] = dict(speed=spd, dot_travel_s=round(want_travel, 2), faster_dots=fast[:10])
if fast:
    fail.append(f"{len(fast)} dot(s) faster than the uniform pace (fast-forward)")
cov = {}
for d in DOTS:
    t = d["txn"]
    if t.address is None:
        continue
    key = f"M{d['mid']} 0x{t.address:02X} {t.operation}"
    c = cov.setdefault(key, dict(m2s=0, s2m=0))
    if d["stage"].kind == "cond" or d["stage"].direction == "W":
        c["m2s"] += 1
    else:
        c["s2m"] += 1
report["pairs_seen_live"] = cov
if not any(d["mid"] == 2 for d in DOTS):
    fail.append("no Master 2 transfer was animated")
if not any(d["mid"] == 1 for d in DOTS):
    fail.append("no Master 1 transfer was animated")
for k, v in (("routing", bad_route), ("colour", bad_col), ("dot not arrived", cut_dots)):
    if v:
        fail.append(f"{len(v)} {k} problem(s)")
report["colours"] = dict(ACK=net_mod.ACK_COL, NACK=net_mod.NACK_COL, M1_TX=config.MASTERS[1]["color"],
                         M1_RX=config.MASTERS[1]["color_read"], M2_TX=config.MASTERS[2]["color"],
                         M2_RX=config.MASTERS[2]["color_read"], unattributed=net_mod.UNATTR_COL, analyzer=net_mod.OBS_COL)
report["stray_labels"] = stray_labels()
if report["stray_labels"]:
    fail.append(f"ghost caption(s) on the 3D view: {report['stray_labels']}")
report["engine_errors"] = [f"{e.category} {e.kind}: {e.message}" for e in list(w.engine.events)
                           if e.severity == "error"][-15:]
report["transport_errors"] = w.engine.transport_errors

# ------------------------------------------------------------------ 6. clean shutdown
w.btn_connect_all.click() if w.btn_connect_all.text() == "Disconnect all" else None
pump(2)
report["shutdown"] = dict(analyzer_connected=bool(w.worker and w.worker.isRunning()),
                          masters_connected=[m for m in w.master_ports if w._master_running(m)])
w.close()
pump(0.5)
report["python_exceptions"] = ERRORS
if ERRORS:
    fail.append(f"{len(ERRORS)} Python exception(s)")
report["screenshots"] = shots
report["result"] = "PASS" if not fail else "FAIL"
(OUT / "live_hw_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
print(json.dumps(report, indent=2, default=str))
os._exit(0 if not fail else 1)
