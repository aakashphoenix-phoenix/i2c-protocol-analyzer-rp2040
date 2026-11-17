"""PacketModel colour roles, dark <-> presentation theme switching, and the one-click session report export."""
import re
import time
from unittest.mock import patch

import pytest
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtCore import Qt

import config
from analysis.capture_engine import CaptureEngine
from config import COLORS, MASTERS, THEMES
from gui import theme as theme_mod
from gui.packet_analyzer import packet_detail_html
from models.packet_model import (C_ACK, C_BOOK, C_BYTES, C_DATA, C_DEV, C_MASTER, C_RW, C_SEMANTIC, C_STAT, PacketModel)
from storage.capture_storage import build_session_report, export_session_report
from tests.helpers import lines_for, seq_of, synth, write_read
from tests.test_hardware_pipeline import qt_app  # noqa: F401  (module-scoped QApplication + temp QSettings)
from tests.test_stage_pills_semantic import TRAFFIC, load_traffic


def hexes(brush):
    assert isinstance(brush, QBrush)
    return brush.color().name().lower()


# ====================================================================== PacketModel colour / tooltip roles
@pytest.fixture
def model():
    e = CaptureEngine()
    e.set_source("demo")
    pk = load_traffic(e)
    m = PacketModel(e)
    m.sync()
    return m, pk, {name: i for i, (name, _) in enumerate(TRAFFIC)}


def fg(m, row, col):
    return m.data(m.index(row, col), Qt.ItemDataRole.ForegroundRole)


def test_rw_and_ack_foreground_follow_the_palette(model):
    m, pk, idx = model
    c = lambda k: COLORS[k].lower()
    assert hexes(fg(m, idx["bmp"], C_RW)) == c("cyan")                 # WRITE→READ combined: cyan
    assert hexes(fg(m, idx["oled_frame"], C_RW)) == c("blue")          # pure write
    assert hexes(fg(m, idx["addr_nack"], C_ACK)) == c("red")           # address NACK = unexpected
    assert hexes(fg(m, idx["oled_frame"], C_ACK)) == c("green")
    low = {"acks": 1, "raw_nacks": 1, "expected_read_nacks": 0, "unexpected_nacks": 0, "unverified_read_end_nacks": 1}
    with patch("models.packet_model.acknowledgment_counts", return_value=low):
        assert hexes(fg(m, idx["oled_frame"], C_ACK)) == c("amber")   # end-of-read NACK whose STOP was not captured


def test_master_column_color_is_reachable(model):
    m, pk, idx = model
    for mid in (1, 2):
        pk["rtc"].master_id = mid
        assert hexes(fg(m, idx["rtc"], C_MASTER)) == QColor(MASTERS[mid]["color"]).name().lower()
    pk["rtc"].master_id = None
    assert hexes(fg(m, idx["rtc"], C_MASTER)) == COLORS["amber"].lower()          # unattributed = amber, never a master colour


def test_status_bookmark_and_row_styling(model):
    m, pk, idx = model
    row = idx["rtc"]
    assert hexes(fg(m, row, C_STAT)) == COLORS["green"].lower()
    assert hexes(fg(m, row, C_BOOK)) == COLORS["amber"].lower()                   # bookmark star
    assert fg(m, row, C_DEV) is None                                              # complete packet: theme text colour
    pk["rtc"].status = "malformed"
    assert hexes(fg(m, row, C_STAT)) == COLORS["red"].lower()
    assert hexes(fg(m, row, C_DEV)) == COLORS["red"].lower() and hexes(fg(m, row, C_DATA)) == COLORS["red"].lower()
    for st in ("incomplete", "gap"):
        pk["rtc"].status = st
        assert hexes(fg(m, row, C_STAT)) == COLORS["gray"].lower()
        assert hexes(fg(m, row, C_DEV)) == COLORS["gray"].lower()
    pk["rtc"].status = "ok"
    # the stranded-branch regression: the explicit column colours still win over the row colour
    pk["rtc"].status = "malformed"
    assert hexes(fg(m, row, C_RW)) == COLORS["cyan"].lower()
    pk["rtc"].status = "ok"


def test_tooltips_are_separate_from_colours(model):
    m, pk, idx = model
    tip = lambda name, col: m.data(m.index(idx[name], col), Qt.ItemDataRole.ToolTipRole)
    assert isinstance(tip("rtc", C_MASTER), str) and tip("rtc", C_MASTER)         # attribution text, never a QBrush
    assert "raw NACK" in tip("addr_nack", C_ACK) and "unexpected NACK 1" in tip("addr_nack", C_ACK)
    assert "verified" in tip("rtc", C_SEMANTIC) and "SIMULATED" in tip("rtc", C_SEMANTIC)
    assert "nothing verified" in tip("unknown_write", C_SEMANTIC)
    assert tip("rtc", C_BYTES) is None                                           # plain short column: no tooltip
    pk["rtc"].notes.append("bus free time < tBUF")
    assert tip("rtc", C_BYTES) == "bus free time < tBUF"
    assert tip("rtc", C_DEV).startswith("DS3231") and tip("rtc", C_DEV).endswith("bus free time < tBUF")   # long-text column: full text + notes
    # every colour comes from foreground(), the data() role is a thin wrapper
    assert m.data(m.index(idx["rtc"], C_RW), Qt.ItemDataRole.ForegroundRole).color() == m.foreground(pk["rtc"], C_RW).color()


def test_detail_html_shows_semantic_summary_only_when_present():
    e = CaptureEngine()
    e.set_source("demo")
    pk = load_traffic(e)
    t0 = e.packets[0].t_start
    rtc = packet_detail_html(e, pk["rtc"], t0)
    assert "DECODED" in rtc and "Read RTC: 08:22:13" in rtc and "DEMO · SIMULATED" in rtc
    assert "Read BMP280: 25.08 °C" in packet_detail_html(e, pk["bmp"], t0)
    for name in ("unknown_write", "addr_nack", "bmp_uncalibrated"):          # nothing verified: nothing invented
        assert "DECODED" not in packet_detail_html(e, pk[name], t0), name
    pk["rtc"].demo = False                                                      # a real (non-demo) packet: no DEMO tag
    summary = re.search(r"DECODED.*?</p>", packet_detail_html(e, pk["rtc"], t0), re.S).group(0)
    assert "Read RTC: 08:22:13" in summary and "DEMO" not in summary


# ====================================================================== theme presets
def lum(h):
    q = QColor(h)
    f = lambda v: (v / 255 / 12.92) if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(q.red()) + 0.7152 * f(q.green()) + 0.0722 * f(q.blue())


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_presets_are_complete_unambiguous_and_high_contrast():
    d, p = THEMES["dark"], THEMES["presentation"]
    assert set(d) == set(p) and config.THEME_DEFAULT == "dark"
    for a, b in ((d, p), (p, d)):                               # hex -> hex translation must be unambiguous both ways
        seen = {}
        for k, v in a.items():
            if isinstance(v, str) and v.startswith("#"):
                prev = seen.setdefault(v.lower(), (k, b[k].lower()))
                assert prev[1] == b[k].lower(), f"{k} and {prev[0]} share {v} but map to different colours"
    assert p["text"].upper() == "#0F172A" and p["card"].upper() in ("#FFFFFF", "#FEFEFE")
    assert contrast(p["text"], p["card"]) >= 12 and contrast(p["text2"], p["card"]) >= 7
    for k in ("cyan", "blue", "green", "amber", "red", "demo", "gray"):
        assert contrast(p[k], p["card"]) >= 4.5, k             # saturated accents readable on the white cards
        assert contrast(p[k], p["bg"]) >= 4.0, k
    assert contrast(p["primary_text"], p["primary_bg"]) >= 7 and contrast(p["danger_text"], p["danger_bg"]) >= 7
    assert contrast(p["demo_text"], p["demo_bg"]) >= 7 and contrast(p["warn_text"], p["warn_bg"]) >= 7
    assert contrast(p["replay_text"], p["replay_bg"]) >= 7
    assert contrast(d["text"], d["card"]) >= 12                # the default palette is untouched
    assert d["bg"] == "#080D19" and d["card"] == "#151F32" and d["cyan"] == "#00D9FF"


@pytest.fixture
def themed_window(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    saved_qss = qt_app.styleSheet()
    masters = {k: (m["color"], m["color_read"]) for k, m in MASTERS.items()}
    theme_mod.apply_theme(qt_app, "dark")
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow()
        w.settings.remove("theme")
        w.engine.set_source("demo")
        load_traffic(w.engine)
        w.packet_model.sync()
        w.resize(1280, 760)
        w.show()
        qt_app.processEvents()
        yield w
        w.settings.remove("theme")
        theme_mod.apply_theme(qt_app, "dark")
        w.close()
        qt_app.processEvents()
    qt_app.setStyleSheet(saved_qss)
    for k, (c1, c2) in masters.items():
        MASTERS[k]["color"], MASTERS[k]["color_read"] = c1, c2


def widget_sheets(app):
    return " ".join(w.styleSheet() for w in app.allWidgets() if w.styleSheet()).lower()


def test_default_is_dark_and_controls_start_unchecked(themed_window, qt_app):
    w = themed_window
    assert theme_mod.current_theme() == "dark" and COLORS["bg"] == "#080D19"
    assert not w.btn_theme.isChecked() and not w.p_set.theme_box.isChecked()
    assert w.btn_theme.text() == "Presentation theme"


def test_header_toggle_switches_everything_and_settings_follow(themed_window, qt_app):
    w = themed_window
    pres = THEMES["presentation"]
    w.btn_theme.click()
    qt_app.processEvents()
    assert theme_mod.current_theme() == "presentation"
    assert {k: COLORS[k] for k in pres} == pres                       # COLORS mutated IN PLACE, every token
    assert qt_app.styleSheet() == theme_mod.build_stylesheet(pres) and pres["bg"] in qt_app.styleSheet()
    assert w.settings.value("theme") == "presentation"
    assert w.p_set.theme_box.isChecked() and w.btn_theme.isChecked()  # Settings <-> header stay in sync
    # inline style sheets / badges / status bar were translated, not left dark
    assert pres["status_bg"].lower() in w.status.styleSheet().lower()
    assert pres["cyan"].lower() in w.lbl_dur.styleSheet().lower()
    assert "#080d19" not in widget_sheets(qt_app) and "#0b1222" not in widget_sheets(qt_app)
    dark_only = {v.lower() for v in THEMES["dark"].values() if isinstance(v, str) and v.startswith("#")} - \
        {v.lower() for v in pres.values() if isinstance(v, str) and v.startswith("#")}
    stale = [h for h in dark_only if h in widget_sheets(qt_app)]
    assert stale == [], f"dark colours left in inline style sheets: {stale}"
    # config.MASTERS wire colours follow the preset; the 3D module palette was reloaded
    import gui.network_3d as n3
    assert MASTERS[2]["color"] == pres["m2"] and n3.ACK_COL == pres["ack"] and n3.LINE_COL["GND"] == pres["gnd"]
    assert n3.overlay_q(200).name().lower() == QColor(pres["bg"]).name().lower()
    # widgets with cached colours
    card = w.p_dash.cards["freq"]
    assert card.color.lower() == pres["cyan"].lower() and card.spark.color.name().lower() == pres["cyan"].lower()
    assert hexes(w.packet_model.foreground(w.engine.packets[0], C_RW)) in (pres["cyan"].lower(), pres["blue"].lower())


def test_settings_checkbox_switches_back_and_default_is_fully_restored(themed_window, qt_app):
    w = themed_window
    dark = dict(THEMES["dark"])
    w.p_set.theme_box.click()
    assert theme_mod.current_theme() == "presentation" and w.btn_theme.isChecked()
    w.p_set.theme_box.click()                                          # back through the Settings checkbox
    qt_app.processEvents()
    assert theme_mod.current_theme() == "dark" and not w.btn_theme.isChecked() and not w.p_set.theme_box.isChecked()
    assert dict(COLORS) == dark and w.settings.value("theme") == "dark"
    assert qt_app.styleSheet() == theme_mod.build_stylesheet(dark)
    assert MASTERS[1]["color"] == dark["m1"] and MASTERS[2]["color_read"] == dark["m2_read"]
    pres_only = {v.lower() for v in THEMES["presentation"].values() if isinstance(v, str) and v.startswith("#")} - \
        {v.lower() for v in dark.values() if isinstance(v, str) and v.startswith("#")}
    assert [h for h in pres_only if h in widget_sheets(qt_app)] == []
    assert w.p_dash.cards["freq"].color.lower() == dark["cyan"].lower()
    assert theme_mod.apply_theme(qt_app, "dark") is False              # already active: nothing to do
    with pytest.raises(ValueError):
        theme_mod.apply_theme(qt_app, "neon")


def test_plots_are_restyled_live_and_restored(themed_window, qt_app):
    w = themed_window
    pres = THEMES["presentation"]
    dark = THEMES["dark"]
    pi = w.p_dash.p_freq.getPlotItem()
    alpha_dark = pi.ctrl.gridAlphaSlider.value()
    w.set_theme("presentation")
    qt_app.processEvents()
    name = lambda col: col.name().lower()
    assert name(w.p_dash.p_freq.backgroundBrush().color()) == pres["card"].lower()
    assert name(w.p_wave.plot.backgroundBrush().color()) == pres["plot_deep"].lower()
    assert name(pi.getAxis("left").pen().color()) == pres["border"].lower()
    assert pi.ctrl.gridAlphaSlider.value() > alpha_dark                # the light grid is stronger
    assert name(w.p_perf.c_ack.opts["pen"].color()) == pres["green"].lower()
    assert name(w.p_wave.c_scl.opts["pen"].color()) == pres["scl"].lower()
    assert pres["text"].lower() in w.p_dash.p_freq.getPlotItem().titleLabel.text.lower()
    assert name(w.p_wave.ca.pen.color()) == pres["blue"].lower()
    import pyqtgraph as pg
    assert name(pg.mkColor(pg.getConfigOption("background"))) == pres["card"].lower()
    for i in range(w.stack.count()):                                    # every page renders without errors in the new theme
        w.nav.setCurrentRow(i)
        qt_app.processEvents()
        w.pages[i].refresh(w.engine)
        assert not w.grab().isNull()
    w.set_theme("dark")
    qt_app.processEvents()
    assert name(w.p_dash.p_freq.backgroundBrush().color()) == dark["card"].lower()
    assert name(w.p_wave.plot.backgroundBrush().color()) == dark["plot_deep"].lower()
    assert name(w.p_wave.c_scl.opts["pen"].color()) == dark["scl"].lower()
    assert pi.ctrl.gridAlphaSlider.value() == alpha_dark


def test_saved_theme_is_applied_at_startup(themed_window, qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    w = themed_window
    w.set_theme("presentation")
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w2 = MainWindow()
        try:
            assert theme_mod.current_theme() == "presentation" and w2.btn_theme.isChecked() and w2.p_set.theme_box.isChecked()
            assert COLORS["bg"] == THEMES["presentation"]["bg"]
            w2.settings.setValue("theme", "bogus")                      # a corrupt setting falls back to the default
        finally:
            w2.close()
        w3 = MainWindow()
        try:
            assert theme_mod.current_theme() == "dark" and not w3.btn_theme.isChecked()
        finally:
            w3.close()


# ====================================================================== session report
def rich_engine():
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines(lines_for(synth(seq_of(*[b for _, b in TRAFFIC], write_read(0x76, 0xD0, [0x58])))))
    now = time.monotonic()
    for mid, port in ((1, "COM32"), (2, "COM34")):
        e.masters.set_connection(mid, port, True)
        # controller counters are reported as the change since its FIRST stats record of the session: send a baseline first
        e.masters.ingest(mid, dict(type="master_stats", master_id=mid, txlog=True), wall=now - 1)
        e.masters.ingest(mid, dict(type="master_stats", master_id=mid, txn_ok=40 * mid, addr_nack=mid, arb_lost=2 * mid,
                                   retries=2 * mid, gave_up=0, txlog=True), wall=now)
    e.masters.ingest(2, dict(type="arb_lost", master_id=2, t_us=5000, addr=0x76, attempt=1, byte=0, backoff_us=300,
                             retry=True), wall=now)
    e.masters.ingest(1, dict(type="arb_lost", master_id=1, t_us=9000, addr=0x68, attempt=3, byte=2, backoff_us=900,
                             retry=False), wall=now)
    return e


def test_report_html_is_self_contained_and_complete(tmp_path):
    e = rich_engine()
    path = tmp_path / "report.html"
    assert export_session_report(path, e, "S20260101-120000") == "html"
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>") and "<style>" in html and "</html>" in html
    assert "<link" not in html and "<script" not in html and "src=" not in html          # self-contained
    s = e.perf.compute()
    for needle in ("Session overview", "S20260101-120000", "DEMO · SIMULATED", "Bus health &amp; timing",
                   "Master 1 vs Master 2 &amp; arbitration", "Devices observed", "Decoded sensor readings",
                   f"{s.total:,} / {s.complete:,}", "Measured SCL frequency", "Unexpected NACK", "end-of-read",
                   "Arbitration losses reported (total)", "COM32", "COM34", "ok 40", "ok 80",
                   "0x76", "BMP280", "Confirmed (reg 0xD0 = 0x58)", "Configured", "0x68", "DS3231",
                   "08:22:13", "2026-10-18", "25.08", "hPa", "OLED frames completed"):
        assert needle in html, needle
    assert e.masters.arb_total == 2 and re.search(r"Arbitration losses reported \(total\)</td><td class=\"v\">2<", html)
    assert re.search(r"retried by the loser</td><td class=\"v\">1<", html) and "gave up (transfer skipped)" in html
    assert "SIMULATED" in html.split("Decoded sensor readings")[1]


def test_report_markdown_by_extension_and_explicit_format(tmp_path):
    e = rich_engine()
    md = tmp_path / "report.md"
    assert export_session_report(md, e, "S1") == "md"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# I²C Session Report — DEMO · SIMULATED") and "> **DEMO · SIMULATED" in text
    for needle in ("## Session overview", "## Bus health & timing", "## Master 1 vs Master 2 & arbitration",
                   "## Devices observed", "## Decoded sensor readings", "| Metric | Value |",
                   "| Master | COM port |", "0x3C"):
        assert needle in text, needle
    assert "| Address | Device | Confidence | Transactions | Reads | Writes | Bytes | ACK | NACK |" in text
    assert export_session_report(tmp_path / "x.txt", e, fmt="md") == "md" and (tmp_path / "x.txt").read_text().startswith("# ")
    assert export_session_report(tmp_path / "y.md", e, fmt="html") == "html" and (tmp_path / "y.md").read_text().startswith("<!DOCTYPE")
    assert export_session_report(tmp_path / "z", e, fmt="markdown") == "md"
    with pytest.raises(ValueError):
        export_session_report(tmp_path / "bad.pdf", e, fmt="pdf")


def test_report_labels_live_and_replay_and_never_invents_values(tmp_path):
    live = CaptureEngine()
    live.set_source("live")
    rep = build_session_report(live)
    assert rep["banner"] is None and not rep["demo"] and "DEMO" not in rep["title"]
    export_session_report(tmp_path / "live.md", live)
    text = (tmp_path / "live.md").read_text(encoding="utf-8")
    assert "DEMO" not in text and "LIVE hardware capture" in text
    # empty capture: every missing value says why, nothing is zero-filled or estimated
    for needle in ("UNAVAILABLE — no analyzer handshake received", "UNAVAILABLE — no measured SCL cycles",
                   "No complete transaction was captured", "No device address was observed on the bus.",
                   "UNAVAILABLE — no controller telemetry received", "not connected",
                   "UNAVAILABLE — Awaiting valid RTC read", "UNAVAILABLE — Awaiting valid sensor data",
                   "Measurement trust | NOT verified"):
        assert needle in text, needle
    rep_engine = CaptureEngine()
    rep_engine.set_source("replay")
    assert "RECORDED CAPTURE" in build_session_report(rep_engine)["banner"]


def test_report_without_calibration_gives_reason_not_a_value(tmp_path):
    e = CaptureEngine()
    e.set_source("demo")
    from tests.test_stage_pills_semantic import bmp_data_bytes
    e.ingest_lines(lines_for(synth(write_read(0x76, 0xF7, bmp_data_bytes()))))
    export_session_report(tmp_path / "r.md", e, "S")
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "BMP280 | UNAVAILABLE — Raw data seen, but calibration" in text and "not captured" in text and "°C" not in text.split("## Decoded")[1]


def test_report_html_escapes_session_id(tmp_path):
    e = rich_engine()
    export_session_report(tmp_path / "r.html", e, "<b>x</b>&")
    html = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "&lt;b&gt;x&lt;/b&gt;&amp;" in html and "<b>x</b>&" not in html


def test_export_report_button_default_name_and_dialog(themed_window, qt_app, tmp_path):
    w = themed_window
    assert w.btn_report.text() == "Export Report" and w.btn_report.isVisible()
    hdr = w.btn_report.parent()
    assert w.btn_report.geometry().right() <= hdr.width() and w.btn_csv.geometry().right() < w.btn_report.geometry().left()
    w.session_id = "S20260101-120000"
    seen = {}

    def fake_dialog(parent, title, path, filters):
        seen.update(path=path, filters=filters)
        return str(tmp_path / "out"), "Markdown Report (*.md)"          # no extension typed: the chosen filter decides
    with patch("gui.main_window.QFileDialog.getSaveFileName", side_effect=fake_dialog):
        w.btn_report.click()
    assert seen["path"].endswith("report_S20260101-120000_DEMO.html")
    assert seen["filters"] == "HTML Report (*.html);;Markdown Report (*.md)"
    out = tmp_path / "out.md"
    assert out.exists() and out.read_text(encoding="utf-8").startswith("# I²C Session Report — DEMO · SIMULATED")
    assert "S20260101-120000" in out.read_text(encoding="utf-8")
    # Capture History page offers the same export
    from PyQt6.QtWidgets import QPushButton
    b = next(x for x in w.p_hist.findChildren(QPushButton) if x.text() == "Export summary report")
    with patch("gui.main_window.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "hist.html"), "HTML Report (*.html)")):
        b.click()
    assert (tmp_path / "hist.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_export_report_with_nothing_captured_does_not_write(themed_window, qt_app, tmp_path):
    w = themed_window
    w.engine.clear()
    w.engine.raw_lines.clear()
    w.packet_model.sync()
    with patch("gui.main_window.QFileDialog.getSaveFileName") as dlg:
        w.btn_report.click()
        assert not dlg.called
