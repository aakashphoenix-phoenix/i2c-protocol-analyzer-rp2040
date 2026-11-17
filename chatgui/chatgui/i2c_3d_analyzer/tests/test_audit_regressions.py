"""Regression tests for the end-to-end GUI audit (stale decoder boxes, layout, contrast, settings isolation, tooltips)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

import config
from config import THEMES
from analysis.playback import stages
from tests.test_hardware_pipeline import qt_app  # noqa: F401  (module-scoped QApplication + temp QSettings)
from tests.test_stage_pills_semantic import TRAFFIC, load_traffic
from tests.test_theme_report_foreground import contrast


@pytest.fixture
def window(qt_app):
    import gui.network_3d as network
    from gui import theme
    from gui.main_window import MainWindow
    theme.apply_theme(qt_app, "dark")
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow()
        w.settings.remove("theme")
        w.engine.set_source("demo")
        w.packets = load_traffic(w.engine)
        w.packet_model.sync()
        w.resize(1280, 720)
        w.show()
        qt_app.processEvents()
        yield w
        w.settings.remove("theme")
        theme.apply_theme(qt_app, "dark")
        w.demo = None
        w.close()
        qt_app.processEvents()


# ====================================================================== waveform: no stale decoder boxes
def test_decoder_track_is_cleared_when_there_is_no_waveform(window):
    page = window.p_wave
    pkt = window.packets["bmp"]
    window.select_transaction(pkt)
    page._redraw(force=True, fit=True)
    assert len(page.decoder_track.segs) > 10                           # boxes of the packet on display
    page.follow.setChecked(False)
    page.txn = None
    page._redraw(force=True)                                           # "Select a packet from this capture ..."
    assert "No raw waveform" in page.title.text()
    assert page.decoder_track.segs == [] and page.decoder_track.layout() == []      # no protocol boxes without a waveform


def test_decoder_track_ink_is_readable_on_every_box_colour_in_both_themes():
    from gui.waveform_viewer import _ink
    for name in ("dark", "presentation"):
        p = THEMES[name]
        for key in ("amber", "gray", "cyan", "blue", "green", "red"):
            assert contrast(_ink(p[key]), p[key]) >= 4.0, (name, key)  # the label text on S/P/ADDR/DATA/ACK/NACK boxes


def test_presentation_write_and_read_colours_are_clearly_different_hues():
    for name in ("dark", "presentation"):
        c, b = QColor(THEMES[name]["cyan"]), QColor(THEMES[name]["blue"])
        assert abs(c.hueF() - b.hueF()) * 360 >= 28, name              # READ (cyan) vs WRITE (blue) boxes / chips


# ====================================================================== layout / structure
def test_fitscroll_reserves_height_for_wrapped_labels_at_the_final_width(qt_app):
    from gui.main_window import FitScroll
    page = QWidget()
    lay = QVBoxLayout(page)
    wrapped = QLabel("word " * 120)
    wrapped.setWordWrap(True)
    lay.addWidget(wrapped)
    lay.addWidget(QLabel("below"))
    sa = FitScroll(300, 100)
    sa.setWidget(page)
    sa.resize(420, 160)
    sa.show()
    qt_app.processEvents()
    sa._fit()
    need = lay.minimumHeightForWidth(page.width())
    assert need > page.minimumSizeHint().height()                      # the situation that used to squeeze the labels
    assert page.height() >= need                                       # the page is now at least as tall as the layout needs
    wrapped_bottom = wrapped.geometry().bottom()
    assert wrapped_bottom < lay.itemAt(1).widget().geometry().top()    # and the wrapped label no longer overlaps its neighbour
    sa.close()


def test_network_page_is_compact_so_the_stage_bar_fits_on_a_laptop_screen(window):
    from gui.main_window import FitScroll
    sa = window.stack.widget(1)
    assert isinstance(sa, FitScroll) and sa.min_h == 480               # the 3D page does not force the 640 px minimum
    net_page = window.p_net
    assert net_page.rot.parent() is not None and net_page.rot.isAncestorOf(net_page.stage_bar) is False
    # the view controls moved into the card header (one row less) instead of a separate row under the stage bar
    header = net_page.stage_bar.parentWidget().layout().itemAt(0).layout()
    in_header = [header.itemAt(i).widget() for i in range(header.count()) if header.itemAt(i).widget() is not None]
    assert net_page.rot in in_header and net_page.renderer_lbl in in_header
    assert net_page.renderer_lbl.text().startswith("Renderer:") and "2D" in net_page.renderer_lbl.toolTip()
    assert not any(isinstance(x, QLabel) and x.wordWrap() and "Arrows show" in x.text() for x in net_page.net.findChildren(QLabel))


def test_demo_mode_folds_the_port_rows_and_ending_it_brings_them_back(window):
    assert window.port_panel.isVisible()
    window.demo = SimpleNamespace(isRunning=lambda: True)             # the simulator is running: no ports to pick
    window._refresh_port_strip()
    assert not window.port_panel.isVisible() and window.btn_ports.text() == "Ports ▾"
    window.demo = None
    window._refresh_port_strip()
    assert window.port_panel.isVisible() and window.btn_ports.text() == "Ports ▴"


def test_stage_chips_leave_the_layout_immediately_when_the_packet_changes(window):
    bar = window.p_net.stage_bar
    window.select_transaction(window.packets["bmp"])
    n1 = len(bar.chips)
    window.select_transaction(window.packets["oled_cmds"])             # no event-loop turn in between
    window.select_transaction(window.packets["rtc"])
    n3 = len(bar.chips)
    assert n1 > 3 and n3 > 3
    assert bar.row.count() == n3 + (n3 - 1) + 1                        # chips + arrows + the trailing stretch, nothing stale
    assert [c[1].label for c in bar.chips][0] == "START"


# ====================================================================== packet table
def test_packet_table_default_column_order_and_long_cell_tooltips(window):
    from models.packet_model import C_ACK, C_ADDR, C_DATA, C_DEV, C_ID, C_MASTER, C_RW, C_SEMANTIC, C_TS
    hh = window.p_pk.table.horizontalHeader()
    order = [hh.visualIndex(c) for c in (C_ID, C_TS, C_ADDR, C_DEV, C_SEMANTIC, C_DATA, C_RW, C_MASTER, C_ACK)]
    assert order == sorted(order) and order[4] + 1 == order[5]          # Decoded sits directly left of the raw hex bytes
    pm = window.packet_model
    long_pkt = max(window.engine.packets, key=lambda t: t.byte_count)
    row = window.engine.packets.index(long_pkt)
    assert pm.data(pm.index(row, C_DATA), Qt.ItemDataRole.ToolTipRole).startswith(long_pkt.data_hex()[:20])
    assert long_pkt.data_hex() in pm.data(pm.index(row, C_DATA), Qt.ItemDataRole.ToolTipRole)   # the FULL bytes, not the 12-byte preview
    unknown = window.engine.packets[TRAFFIC.index(next(t for t in TRAFFIC if t[0] == "unknown_write"))]
    tip = pm.data(pm.index(window.engine.packets.index(unknown), C_DEV), Qt.ItemDataRole.ToolTipRole)
    assert "0x50" in tip or "Unknown" in tip or "EEPROM" in tip


# ====================================================================== settings isolation
def test_main_window_honours_the_default_settings_format(window):
    # (org, app) alone ignores QSettings.setDefaultFormat and would write the REAL registry from tests / scripts
    assert window.settings.format() == QSettings.defaultFormat() == QSettings.Format.IniFormat
    assert "I2CAnalyzer" in window.settings.fileName() and "HKEY" not in window.settings.fileName()


# ====================================================================== detail pane
def test_detail_byte_table_keeps_the_nack_explanation_on_its_own_row():
    from analysis.capture_engine import CaptureEngine
    from gui.packet_analyzer import packet_detail_html
    e = CaptureEngine()
    e.set_source("demo")
    pk = load_traffic(e)
    html = packet_detail_html(e, pk["bmp"], e.packets[0].t_start)          # the read ends with the controller's NACK
    assert ">N</td></tr>" in html                                          # the ACK cell itself is just the letter ...
    assert "↳ controller left SDA HIGH: normal end-of-read NACK" in html   # ... the explanation (unchanged text) has a row
    assert html.count("<tr><td></td><td colspan=5") == 1
