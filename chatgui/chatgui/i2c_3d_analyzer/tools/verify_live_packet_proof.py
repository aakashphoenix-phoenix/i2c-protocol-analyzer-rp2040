"""LIVE HARDWARE check of Packet Proof, Cross-check, signal export, address filter and 'Unknown' master labels.

Real Hardware Mode only.  Starts the GUI like main.py, presses Connect all (analyzer + Master 1 + Master 2), lets the bus
run, then for every master x target pair takes the NEWEST packet whose raw edges are in memory and opens Packet Proof on
it (exactly the Packet Analyzer button), saving the HTML report and a screenshot of the window.  Then the Cross-check
window, a VCD export of the Digital Waveform view (read back and re-decoded), and the Packet Analyzer address filter.
The window ignores mouse / keyboard input; QSettings go to a temporary INI file (the registry is compared before/after).
Usage:  python -X utf8 tools/verify_live_packet_proof.py [--settle 30]
Writes work/verification/proofs/*.html|png|vcd and proof_report.json.
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
OUT = ROOT / "work" / "verification" / "proofs"
OUT.mkdir(parents=True, exist_ok=True)
ERRORS = []
sys.excepthook = lambda t, v, tb: (ERRORS.append("".join(traceback.format_exception(t, v, tb))),
                                   sys.__excepthook__(t, v, tb))

from PyQt6.QtCore import QEventLoop, QSettings, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

BOXES = []                       # an input-proof window cannot dismiss a modal box: log it instead of blocking


def _box(kind):
    def f(*args, **kw):
        BOXES.append((kind, str(args[1] if len(args) > 1 else ""), str(args[2] if len(args) > 2 else "")[:300]))
        print("MESSAGE BOX", BOXES[-1], flush=True)
        return QMessageBox.StandardButton.Ok
    return f


for _k in ("warning", "information", "critical", "question"):
    setattr(QMessageBox, _k, staticmethod(_box(_k)))


def native_settings():
    s = QSettings(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, "I2CAnalyzer", "I2C3DAnalyzer")
    return {k: str(s.value(k)) for k in s.allKeys()}


NATIVE_BEFORE = native_settings()
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tempfile.mkdtemp(prefix="proof_settings_"))
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
from analysis.i2c_decoder import I2CDecoder  # noqa: E402
from analysis.multi_master import attribution_text  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from storage.signal_export import read_vcd  # noqa: E402

SETTLE = float(sys.argv[sys.argv.index("--settle") + 1]) if "--settle" in sys.argv else 30.0
w = MainWindow()
w.resize(1680, 980)
w.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
w.setWindowTitle(w.windowTitle() + "  —  AUTOMATED LIVE VERIFICATION (input disabled)")
w.show()
assert w.demo is None and w.presentation is None, "Real Hardware Mode only"
loop = QEventLoop()
report = dict(mode="LIVE HARDWARE", checks={}, failures=[], proofs=[])
fail = report["failures"]


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QTimer.singleShot(20, loop.quit)
        loop.exec()
        if until is not None and until():
            return True
    return until() if until is not None else True


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name, flush=True)
    report["checks"][name] = dict(ok=bool(ok), detail=str(detail)[:2000])
    if not ok:
        fail.append(f"{name}: {str(detail)[:300]}")


w.nav.setCurrentRow(2)                                  # Packet Analyzer
w.btn_connect_all.click()
locked = pump(30, lambda: w.capturing and all(s.clock.locked for s in w.engine.masters.state.values()))
check("Connect all: capturing, both masters clock-aligned", w.capturing and locked,
      [(r["master_id"], r["state"], r["clock"]) for r in w.engine.masters.rows()])
if not w.capturing:                      # ports busy (another GUI instance?) - nothing else can be verified
    print(json.dumps(dict(result="FAIL", failures=fail, message_boxes=BOXES), default=str), flush=True)
    w.close()
    os._exit(2)
pump(SETTLE)
e = w.engine
report["capture"] = dict(sample_hz=e.caps.sample_hz, tick_ns=e.tick_ns, firmware=e.caps.firmware,
                         packets=len(e.packets), segment_decoder=e.caps.has("segment"))

# ------------------------------------------------------------------ Packet Proof on every master x target pair
display_paused = False
for mid in (1, 2):
    for addr, label in ((0x76, "BMP280"), (0x68, "RTC"), (0x3C, "OLED")):
        end = e.edges[-1][0] if e.edges else 0
        t = next((t for t in reversed(e.packets[-6000:]) if t.master_id == mid and t.address == addr
                  and t.status == "ok" and (t.t_end or 0) + 70_000 <= end and e.edges[0][0] < t.t_start - 70_000), None)
        tag = f"M{mid}_{label}"
        if t is None:
            check(f"Packet Proof {tag}: a recent packet exists", False, "no packet of this pair with raw edges")
            continue
        dlg = w.p_pk.open_proof(t)
        pump(1.2)
        p = dlg.proof
        dlg.grab().save(str(OUT / f"proof_{tag}.png"))
        dlg.save_report(str(OUT / f"proof_{tag}.html"))
        if p.samples:
            dlg._export("vcd", str(OUT / f"proof_{tag}.vcd"))
        rec = dict(pair=tag, packet=t.id, op=t.operation, data=t.data_hex(), verdict=p.verdict,
                   checks=[(c.name, c.ok, c.detail) for c in p.checks], spec=[(c.name, c.ok, c.detail) for c in p.spec],
                   bits=len(p.bits), master=attribution_text(t), meaning=p.meaning[:6],
                   timing={k: v for k, v in p.timing.items() if isinstance(v, (int, float)) and v is not None})
        report["proofs"].append(rec)
        check(f"Packet Proof {tag} (#{t.id} {t.operation}) PROVEN", p.verdict == "PROVEN",
              [(c.name, c.ok, c.detail) for c in p.checks if c.ok is not True])
        dlg.close()
        pump(0.3)

# ------------------------------------------------------------------ Cross-check window
cc = w.p_pk.open_cross_check()
pump(1.0)
cc.grab().save(str(OUT / "cross_check.png"))
from analysis.cross_check import decoder_agreement, known_content, known_transactions  # noqa: E402
rows, unknown = known_transactions(e)
agree = decoder_agreement(e)
kc = known_content(e)
report["cross_check"] = dict(known_transactions=rows, unknown_master_packets=unknown, decoders=agree, content=kc)
check("Known transactions: every report of each master found on the bus",
      all(r["missing"] == 0 and r["matched"] > 0 for r in rows), rows)
check("Two decoders (Pico firmware vs PC raw edges) never disagree", agree["applicable"] and agree["disagree"] == 0
      and agree["agree"] > 0, agree)
check(f"Known content: chip ID 0xD0 = {kc['chip_ids']} ({kc['chip']}) and DS3231 BCD time",
      kc["chip_id_reads"] and kc["chip_id_ok"] ==
      kc["chip_id_reads"] and kc["rtc_reads"] and kc["rtc_bcd_ok"] == kc["rtc_reads"], kc)
cc.close()

# ------------------------------------------------------------------ Digital Waveform export -> VCD -> re-decode
w.nav.setCurrentRow(3)
pump(2.0)
wv = w.p_wave
t_last = next(t for t in reversed(e.packets) if t.status == "ok" and (t.t_end or 0) + 70_000 <= e.edges[-1][0])
wv.show_packet(t_last, synchronize=False)
pump(1.0)
vcd = OUT / "waveform_view.vcd"
got = wv.export_view(str(vcd))
ok = False
detail = "no export"
if got:
    sm = read_vcd(str(vcd))
    dec = I2CDecoder()
    redone = [x for x in dec.feed(sm) if x.status == "ok"]
    keys = {tuple((f.addr, f.rw, tuple(f.data)) for f in x.frames) for x in redone}
    k_last = tuple((f.addr, f.rw, tuple(f.data)) for f in t_last.frames)
    ok = k_last in keys
    detail = f"{len(sm)} samples in the file, {len(redone)} transactions re-decoded from it, packet #{t_last.id} found: {ok}"
check("Waveform 'Export view' VCD holds the captured packet (re-decoded from the file)", ok, detail)
w.grab().save(str(OUT / "waveform_export.png"))

# ------------------------------------------------------------------ address filter + Unknown label
w.nav.setCurrentRow(2)
pump(0.5)
pk = w.p_pk
idx = pk.addr.findData(0x76)
if idx >= 0:
    pk.addr.setCurrentIndex(idx)
    pump(0.8)
    px = pk.proxy
    addrs = {px.mapToSource(px.index(r, 0)).row() for r in range(min(px.rowCount(), 300))}
    model = w.packet_model
    seen = {model.txn(r).address for r in addrs}
    check("Packet Analyzer: address filter shows only that slave", px.rowCount() > 0 and seen == {0x76},
          f"{px.rowCount()} rows, addresses {sorted(seen)}")
    pk.addr.setCurrentIndex(0)
unknown_pk = [t for t in e.packets if t.master_id is None]
check("Unvalidated packets are labelled 'Unknown' (never guessed)",
      all(attribution_text(t) == "Unknown" for t in unknown_pk), f"{len(unknown_pk)} unknown-master packet(s)")

w.btn_connect_all.click() if w.btn_connect_all.text() == "Disconnect all" else None
pump(2)
w.close()
pump(0.5)
report["message_boxes"] = BOXES
report["python_errors"] = ERRORS
check("no Python errors", not ERRORS, ERRORS[:2])
check("user's native QSettings untouched", native_settings() == NATIVE_BEFORE)
report["result"] = "PASS" if not fail else "FAIL"
(OUT / "proof_report.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
print(json.dumps(dict(result=report["result"], failures=fail, capture=report["capture"]), indent=1, default=str,
                 ensure_ascii=False))
sys.stdout.flush()
os._exit(0 if not fail else 1)
