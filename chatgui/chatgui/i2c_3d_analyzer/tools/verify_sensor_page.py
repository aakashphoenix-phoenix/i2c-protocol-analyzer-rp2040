"""Render the Sensor Monitoring page and check: no clipped text, no overlapping cards,
OLED preview values identical to the RTC/BMP280 cards.  Screenshots: work/verification/sensor_*.png

    python -X utf8 tools/verify_sensor_page.py --jsonl work/measurements/after.jsonl --master COM32
    python -X utf8 tools/verify_sensor_page.py --demo
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / ".runtime_dependencies"))
from PyQt6.QtWidgets import QApplication, QLabel   # noqa: E402

app = QApplication([])
from gui.theme import STYLESHEET   # noqa: E402
app.setStyleSheet(STYLESHEET)
import gui.network_3d as network   # noqa: E402


def clipped_labels(card):
    bad = []
    for lb in card.findChildren(QLabel):
        if not lb.isVisible() or not lb.text() or lb.pixmap() is not None and not lb.pixmap().isNull():
            continue
        if lb.wordWrap():
            need_h = lb.heightForWidth(lb.width())
            if need_h > lb.height() + 1:
                bad.append((lb.text()[:40], f"height {lb.height()} < needed {need_h}"))
        elif lb.sizeHint().width() > lb.width() + 1:
            bad.append((lb.text()[:40], f"width {lb.width()} < needed {lb.sizeHint().width()}"))
        r = lb.geometry().translated(lb.parentWidget().mapTo(card, lb.parentWidget().rect().topLeft()))
        if r.right() > card.width() + 1:
            bad.append((lb.text()[:40], "extends past card edge"))
    return bad


def main():
    tag = "demo" if "--demo" in sys.argv else "live"
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    e = w.engine
    if tag == "demo":
        from sources.demo_source import DemoGenerator
        e.set_source("demo"); g = DemoGenerator(seed=4); e.ingest_lines([g.hello()])
        for k in range(1, 121):
            e.ingest_lines(g.lines_until(k * 0.05))
    else:
        jsonl = sys.argv[sys.argv.index("--jsonl") + 1]
        e.set_source("replay")
        e.ingest_lines([ln for ln in Path(jsonl).read_bytes().splitlines() if ln.strip()])
    master = None
    if tag != "demo" and "--master" in sys.argv:
        import serial
        master = serial.Serial(sys.argv[sys.argv.index("--master") + 1], 115200, timeout=0.2)
        master.dtr = False; time.sleep(0.3); master.dtr = True

    def pump_master(seconds):
        """Feed the controller's live USB telemetry exactly as the GUI's master port does."""
        if master is None:
            return
        buf, t0 = b"", time.time()
        while time.time() - t0 < seconds:
            buf += master.read(master.in_waiting or 1)
        w._on_master_lines([ln for ln in buf.splitlines() if ln.strip()])
    page = w.p_sens
    out = ROOT / "work" / "verification"; out.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, size in (("normal", (1920, 1040)), ("narrow", (1180, 900))):
        w.showNormal(); w.resize(*size); w.show()
        w.nav.setCurrentRow(5)
        for _ in range(4):
            app.processEvents(); pump_master(0.6); page.refresh(e); app.processEvents()
        cards = [page.rtc, page.bmp, page.oled]
        geo = [c.geometry() for c in cards]
        overlaps = [(i, j) for i in range(3) for j in range(i + 1, 3) if geo[i].intersects(geo[j])]
        clip = {c.title.text(): clipped_labels(c) for c in cards}
        print(f"\n[{tag} · {name} {size[0]}x{size[1]}] columns={page._cols} "
              f"cards={[f'{g.x()},{g.y()} {g.width()}x{g.height()}' for g in geo]} overlaps={overlaps}")
        for t, b in clip.items():
            print(f"  {t}: clipped labels = {b if b else 'none'}")
        ok &= not overlaps and not any(clip.values())
        w.grab().save(str(out / f"sensor_{tag}_{name}.png"))
        # scroll to the bottom too, to capture the lower part of the stacked layout
        sb = page._scroll.verticalScrollBar()
        if sb.maximum() > 0:
            sb.setValue(sb.maximum()); app.processEvents()
            w.grab().save(str(out / f"sensor_{tag}_{name}_scrolled.png")); sb.setValue(0)
    print("\nOLED preview lines:", page._preview_lines)
    print("RTC card:", page.clock.text(), "|", page.date.text(), "| BMP card:", page.temp.text(), "|", page.press.text())
    print("preview source:", page.preview_src.text())
    pv = page._preview_lines
    agree = (pv[2] == f"TIME {page.clock.text()}" or (page.clock.text() == "--:--:--" and "TIME " in pv[2])) and \
            (page.temp.text() == "—" or pv[5] == "TEMP " + page.temp.text().replace(" °C", " C")) and \
            (page.press.text() == "—" or pv[6] == "PRESS " + page.press.text().replace(" hPa", " HPA"))
    print("preview agrees with cards:", agree)
    print("RESULT:", "PASS" if ok and agree else "FAIL")
    if master is not None:
        master.close()
    w.close()


main()
