"""Stage progress chips (3D page), quick-filter pills (Packet Analyzer) and the inline semantic-decode column."""
import re
import time
from unittest.mock import patch

import pytest
from PyQt6.QtCore import Qt

from analysis.capture_engine import CaptureEngine
from analysis.playback import Playback, stage_chips, stages
from analysis.sensor_decoder import bmp280_compensate
from models.packet_model import C_DATA, C_SEMANTIC, is_error_packet, semantic_text
from tests.helpers import lines_for, seq_of, synth, write, write_read
from tests.test_hardware_pipeline import qt_app  # noqa: F401  (module-scoped QApplication + temp QSettings)

# Bosch BMP280 datasheet example calibration (registers 0x88..0x9F, little endian) and raw ADC values
CALIB = dict(T1=27504, T2=26435, T3=-1000, P1=36477, P2=-10685, P3=3024, P4=2855, P5=140, P6=-7, P7=15500,
             P8=-14600, P9=6000)
ADC_T, ADC_P = 519888, 415148


def calib_bytes():
    out = []
    for k in ("T1", "T2", "T3", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"):
        v = CALIB[k] & 0xFFFF
        out += [v & 0xFF, v >> 8]
    return out


def bmp_data_bytes():
    return [(ADC_P >> 12) & 0xFF, (ADC_P >> 4) & 0xFF, (ADC_P & 0xF) << 4,
            (ADC_T >> 12) & 0xFF, (ADC_T >> 4) & 0xFF, (ADC_T & 0xF) << 4]


RTC_BYTES = [0x13, 0x22, 0x08, 0x01, 0x18, 0x10, 0x26]            # 08:22:13, Mon 18-10-2026

# (name, builder).  Order matters: the BMP280 data read comes BEFORE the calibration read on purpose.
TRAFFIC = [
    ("bmp_uncalibrated", write_read(0x76, 0xF7, bmp_data_bytes())),
    ("bmp_calib", write_read(0x76, 0x88, calib_bytes())),
    ("bmp", write_read(0x76, 0xF7, bmp_data_bytes())),
    ("rtc", write_read(0x68, 0x00, RTC_BYTES)),
    ("oled_cmds", write(0x3C, [0x00, 0x20, 0x00, 0x21, 0x00, 0x03, 0x22, 0x00, 0x00])),
    ("oled_frame", write(0x3C, [0x40, 1, 2, 3, 4])),
    ("oled_data", write(0x3C, [0x40, 9, 9])),
    ("unknown_write", write(0x50, [0x01, 0x02])),
    ("addr_nack", write(0x51, [0x01], nack_addr=True)),
]


def load_traffic(engine):
    engine.ingest_lines(lines_for(synth(seq_of(*[b for _, b in TRAFFIC]))))
    assert len(engine.packets) == len(TRAFFIC)
    return {name: engine.packets[i] for i, (name, _) in enumerate(TRAFFIC)}


@pytest.fixture
def window(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow()
        w.engine.set_source("demo")
        w.packets = load_traffic(w.engine)
        w.packet_model.sync()
        yield w
        w.close()
        qt_app.processEvents()


# ====================================================================== semantic decode
def test_semantic_text_from_verified_values_only():
    e = CaptureEngine()
    e.set_source("demo")
    p = load_traffic(e)
    temp, press = bmp280_compensate(CALIB, ADC_T, ADC_P)
    assert p["rtc"].semantic == "Read RTC: 08:22:13"
    assert p["bmp"].semantic == f"Read BMP280: {temp:.2f} °C / {press:.2f} hPa"
    assert re.fullmatch(r"Read BMP280: 25\.08 °C / 1006\.\d\d hPa", p["bmp"].semantic)
    assert p["oled_cmds"].semantic == "OLED Cmd Write: 8 bytes"
    assert p["oled_frame"].semantic == "OLED Frame Write: 4 data bytes"
    assert p["oled_data"].semantic == "OLED Data Write: 2 bytes"
    # unverified: raw BMP bytes without calibration, unknown device, NACKed address -> nothing is invented
    for name in ("bmp_uncalibrated", "bmp_calib", "unknown_write", "addr_nack"):
        assert p[name].semantic == "", name
        assert semantic_text(p[name]) == "—"


def test_semantic_column_in_model_next_to_hex(window):
    pm = window.packet_model
    by_name = {name: i for i, (name, _) in enumerate(TRAFFIC)}
    col = lambda name: pm.data(pm.index(by_name[name], C_SEMANTIC))
    assert col("rtc") == "Read RTC: 08:22:13" and col("bmp").startswith("Read BMP280: 25.08 °C")
    assert col("oled_frame").startswith("OLED Frame Write") and col("unknown_write") == "—"
    assert pm.headerData(C_SEMANTIC, Qt.Orientation.Horizontal) == "Decoded"
    hh = window.p_pk.table.horizontalHeader()
    assert hh.visualIndex(C_SEMANTIC) == hh.visualIndex(C_DATA) - 1       # directly left of the raw hex bytes
    assert not window.p_pk.table.isColumnHidden(C_SEMANTIC)
    # the dashboard table keeps its compact column set, the device-page table shows it too
    assert window.p_dash.table.isColumnHidden(C_SEMANTIC)
    assert not window.p_dev.pt.isColumnHidden(C_SEMANTIC)
    # searchable like every other column
    window.p_pk.search.setText("08:22:13")
    assert window.p_pk.proxy.rowCount() == 1
    window.p_pk.search.clear()


# ====================================================================== filter pills
def shown(page):
    m = page.ctx.packet_model
    return [m.txn(page.proxy.mapToSource(page.proxy.index(i, 0)).row()) for i in range(page.proxy.rowCount())]


def test_device_pills_mirror_the_device_dropdown(window):
    page = window.p_pk
    rtc, bmp = page.pills[("addr", "rtc")], page.pills[("addr", "bmp")]
    assert not rtc.isChecked() and page.proxy.rowCount() == len(TRAFFIC)
    rtc.click()
    assert rtc.isChecked() and page.addr.currentData() == 0x68
    assert [t.address for t in shown(page)] == [0x68]
    bmp.click()                                              # one device at a time, exactly like the dropdown
    assert bmp.isChecked() and not rtc.isChecked() and page.addr.currentData() == 0x76
    assert {t.address for t in shown(page)} == {0x76} and page.proxy.rowCount() == 3
    bmp.click()                                              # un-check -> all devices again
    assert not bmp.isChecked() and page.addr.currentData() is None and page.proxy.rowCount() == len(TRAFFIC)
    # the other way round: a filter set elsewhere (3D node click, dropdown) lights the pill
    page.set_address_filter(0x68)
    assert rtc.isChecked() and not bmp.isChecked()
    page.addr.setCurrentIndex(0)
    assert not rtc.isChecked()
    window.focus_device(0x76)
    assert bmp.isChecked() and page.proxy.addr == 0x76
    window.focus_device(None)
    assert not bmp.isChecked()


def test_oled_pill_follows_configured_oled_address(window):
    page = window.p_pk
    oled = page.pills[("addr", "oled")]
    assert oled.text() == "SSD1306 (0x3C)"
    oled.click()
    assert page.addr.currentData() == 0x3C and {t.address for t in shown(page)} == {0x3C} and page.proxy.rowCount() == 3
    oled.click()
    window.engine.set_oled_address(0x3D)
    page._sync_pills()
    assert oled.text() == "SSD1306 (0x3D)"


def test_master_pills_mirror_the_master_dropdown(window):
    page = window.p_pk
    for i, t in enumerate(window.engine.packets):                 # attribute packets alternately to Master 1 / Master 2
        t.master_id = 1 if i % 2 == 0 else 2
    m1, m2 = page.pills[("master", 1)], page.pills[("master", 2)]
    m1.click()
    assert m1.isChecked() and not m2.isChecked() and page.master.currentData() == 1
    assert page.proxy.rowCount() == 5 and all(t.master_id == 1 for t in shown(page))
    m2.click()
    assert m2.isChecked() and not m1.isChecked() and page.master.currentData() == 2
    assert page.proxy.rowCount() == 4 and all(t.master_id == 2 for t in shown(page))
    m2.click()
    assert not m2.isChecked() and page.master.currentData() == "All" and page.proxy.rowCount() == len(TRAFFIC)
    page.master.setCurrentIndex(page.master.findData(1))          # dropdown -> pill
    assert m1.isChecked() and not m2.isChecked()
    page.master.setCurrentIndex(0)
    assert not m1.isChecked()


def test_errors_pill_keeps_real_errors_not_the_normal_end_of_read_nack(window):
    page = window.p_pk
    err = page.pills[("errors", None)]
    # every read ends with the controller's NACK: raw "Has NACK" lists those, the pill must not
    assert any(t.nack_count for t in window.engine.packets if t.has_read)
    err.click()
    assert page.proxy.errors_only
    rows = shown(page)
    assert [t.address for t in rows] == [0x51] and rows[0].addr_nack
    assert all(is_error_packet(t) for t in rows)
    # combined (AND) with the device dropdown / pills
    page.pills[("addr", "bmp")].click()
    assert page.proxy.rowCount() == 0
    page.pills[("addr", "bmp")].click()
    err.click()
    assert not page.proxy.errors_only and page.proxy.rowCount() == len(TRAFFIC)


# ====================================================================== stage chips
def test_stage_chips_group_address_and_rw_and_cover_every_stage_once():
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines(lines_for(synth(write_read(0x76, 0xF7, [0x65, 0x66]))))
    evs = stages(e.packets[0])
    chips = stage_chips(evs)
    assert [c.label for c in chips] == ["START", "0x76 W", "ACK", "0xF7", "ACK", "Sr", "0x76 R", "ACK", "0x65", "ACK",
                                       "0x66", "NACK", "STOP"]
    flat = [i for c in chips for i in c.indices]
    assert flat == list(range(len(evs)))                              # in order, none missing, none twice
    assert chips[1].indices == (1, 2) and chips[0].kind == "cond" and chips[3].kind == "data"
    assert chips[-2].ack is False and chips[2].ack is True            # the final read NACK is a NACK chip
    assert "end-of-read" in chips[-2].tip


@pytest.fixture
def staged(window):
    pkt = window.packets["bmp"]
    window.select_transaction(pkt)
    window.set_stage_hold(False)
    return window, pkt


def test_stage_bar_shows_active_packet_and_highlights_active_stage(staged):
    w, pkt = staged
    bar = w.p_net.stage_bar
    labels = [c[1].label for c in bar.chips]
    assert labels[0] == "START" and labels[1] == "0x76 W" and labels[-1] == "STOP"
    assert bar.chips[0][2] == "active" and all(c[2] == "todo" for c in bar.chips[1:])
    w.playback.position = 4.2                                         # stage index 4 = DATA 0xF7 = chip 3
    w._sync_playback()
    states = [c[2] for c in bar.chips]
    assert states[:3] == ["done"] * 3 and states[3] == "active" and set(states[4:]) == {"todo"}
    assert f"Packet #{pkt.id}" in bar.info.text() and "stage 5/" in bar.info.text()


def test_clicking_a_chip_jumps_to_that_stage_and_holds(staged):
    w, pkt = staged
    bar = w.p_net.stage_bar
    chip = bar.chips[6]                                               # '0x76 R' (covers two stages)
    chip[0].click()
    assert w.stage_hold and w.playback.stage_index == chip[1].indices[0] and w.playback.position == float(chip[1].indices[0])
    assert bar.chips[6][2] == "active" and bar.btn_hold.isChecked() and bar.btn_hold.text() == "▶ Play"
    assert w.p_net.net._stage_idx == chip[1].indices[0]               # the 3D diagram shows the same stage
    w.playback.position = float(chip[1].indices[1])                   # the R/W-bit stage belongs to the same chip
    w._sync_playback()
    assert bar.chips[6][2] == "active"


def test_step_buttons_walk_the_packet_and_clamp_at_both_ends(staged):
    w, pkt = staged
    bar = w.p_net.stage_bar
    n = len(w.playback.events)
    assert not bar.btn_prev.isEnabled() and bar.btn_next.isEnabled()
    bar.btn_next.click()
    assert w.stage_hold and w.playback.stage_index == 1
    bar.btn_next.click(); bar.btn_next.click()
    assert w.playback.stage_index == 3
    bar.btn_prev.click()
    assert w.playback.stage_index == 2 and bar.btn_prev.isEnabled()
    for _ in range(n + 3):
        bar.btn_next.click()
    assert w.playback.stage_index == n - 1 and not bar.btn_next.isEnabled() and bar.chips[-1][2] == "active"
    for _ in range(n + 3):
        bar.btn_prev.click()
    assert w.playback.stage_index == 0 and not bar.btn_prev.isEnabled()


def test_hold_freezes_playback_without_fighting_live_traffic(staged):
    w, pkt = staged
    bar = w.p_net.stage_bar
    w.stage_goto(4)
    held_txn, held_pos = w.playback.txn, w.playback.position
    w._playback_wall = time.monotonic() - 3.0                         # 3 s of wall time pass ...
    w._refresh()
    assert w.playback.position == held_pos and w.playback.txn is held_txn          # ... nothing moves
    # live traffic keeps being captured and listed, the held packet stays on display even when it would be finished
    w.playback.pinned = False
    w.playback.position = float(len(w.playback.events))
    newer = w.packets["rtc"]
    w._follow_latest_txn = newer
    w._playback_wall = time.monotonic() - 1.0
    w._refresh()
    assert w.playback.txn is held_txn and w.stage_hold
    # Play: resumes and is allowed to move on to the newest packet
    bar.btn_hold.click()
    assert not w.stage_hold and bar.btn_hold.text() == "⏸ Pause"
    w._playback_wall = time.monotonic() - 0.2
    w._refresh()
    assert w.playback.txn is not held_txn and w.playback.txn in w.engine.packets      # the fair queue moves on
    # 'Follow latest' (button / F key) always releases a hold
    w.stage_goto(2)
    assert w.stage_hold
    w.follow_latest()
    assert not w.stage_hold and not bar.btn_hold.isChecked()


def test_pause_display_and_stage_hold_are_independent(staged):
    w, pkt = staged
    w.stage_goto(2)
    assert w.stage_hold and not w.display_paused
    w.btn_pause.setChecked(True)
    assert w.display_paused and w.stage_hold
    w.btn_pause.setChecked(False)
    w.set_stage_hold(False)
    assert not w.stage_hold and not w.display_paused


def test_stage_bar_is_empty_and_disabled_without_a_packet(window):
    w = window
    w.playback = Playback()
    w._sync_playback()
    bar = w.p_net.stage_bar
    assert bar.chips == [] and not bar.btn_hold.isEnabled() and not bar.btn_next.isEnabled() and not bar.btn_prev.isEnabled()
    assert "No packet selected" in bar.info.text()
