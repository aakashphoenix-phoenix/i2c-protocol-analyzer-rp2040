"""Capture History UI & Excel export tests (Step 2)."""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl
import pytest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QMessageBox, QPushButton, QWidget

import config
from analysis.capture_engine import CaptureEngine
from config import COLORS, MASTERS, THEMES
from gui import theme as theme_mod
from gui.capture_history import PREVIEW_COLUMNS, CaptureHistoryPage
from sources.demo_source import DemoGenerator
from storage.capture_storage import (
    _read_xlsx_fallback,
    _write_xlsx_fallback,
    build_session_report,
    export_packets_csv,
    export_session_excel,
    load_excel_preview,
    save_capture,
)
from tests.helpers import lines_for, seq_of, synth, write, write_read
from tests.test_hardware_pipeline import qt_app  # noqa: F401
from tests.test_theme_report_foreground import TRAFFIC, load_traffic


def _demo_engine(seconds: float = 0.8) -> CaptureEngine:
    g = DemoGenerator(seed=7)
    e = CaptureEngine()
    e.set_source("demo")
    e.capture_wall_start = _dt.datetime(2026, 11, 15, 10, 30, 0)
    e.ingest_lines([g.hello()])
    for k in range(1, int(seconds / 0.05) + 1):
        e.ingest_lines(g.lines_until(k * 0.05))
    return e


def _live_engine_with_data() -> CaptureEngine:
    e = CaptureEngine()
    e.set_source("live")
    e.capture_wall_start = _dt.datetime(2026, 11, 15, 9, 15, 0)
    raw = lines_for(synth(seq_of(*[b for _, b in TRAFFIC], write_read(0x76, 0xD0, [0x58]))))
    e.ingest_lines(raw)
    now = time.monotonic()
    for mid, port in ((1, "COM32"), (2, "COM34")):
        e.masters.set_connection(mid, port, True)
        e.masters.ingest(mid, dict(type="master_stats", master_id=mid, txlog=True), wall=now - 1)
        e.masters.ingest(
            mid,
            dict(type="master_stats", master_id=mid, txn_ok=25 * mid, addr_nack=mid, arb_lost=mid, retries=mid, gave_up=0, txlog=True),
            wall=now,
        )
    e.masters.ingest(2, dict(type="arb_lost", master_id=2, t_us=5000, addr=0x76, attempt=1, byte=0, backoff_us=300, retry=True), wall=now)
    return e


def _make_ctx(captures_dir: str, engine: CaptureEngine | None = None):
    eng = engine if engine is not None else CaptureEngine()
    timer = SimpleNamespace(isActive=lambda: False)
    toasts: list[str] = []
    opened: list[tuple[str, int]] = []
    ctx = SimpleNamespace(
        engine=eng,
        session_id="S20261115-103000",
        replay_idx=0,
        replay_lines=[],
        replay_timer=timer,
        replay_paused=False,
        replay_speed=0,
        captures_dir=lambda: str(captures_dir),
        toast=lambda msg, ms=4000: toasts.append(msg),
        save_capture=lambda: None,
        open_capture=lambda path, speed=0: opened.append((path, speed)),
        pause_replay=lambda on: None,
        stop_replay=lambda: None,
        export_report=lambda: None,
        _toasts=toasts,
        _opened=opened,
    )
    return ctx


def test_i2ccap_preview_populates_metadata_and_table_without_mutating_engine(qt_app, tmp_path):
    cap_dir = tmp_path / "captures"
    cap_dir.mkdir()

    demo_eng = _demo_engine()
    demo_file = cap_dir / "capture_demo.i2ccap"
    save_capture(demo_file, demo_eng, notes="bench demo")

    live_eng = _live_engine_with_data()
    live_file = cap_dir / "capture_live.i2ccap"
    save_capture(live_file, live_eng)

    # Active engine holds a distinct single packet; selecting saved files must NOT touch it.
    active_eng = CaptureEngine()
    active_eng.set_source("live")
    active_eng.ingest_lines(lines_for(synth(write(0x50, [0xAA, 0xBB]))))
    before_snapshot = (
        active_eng.source,
        len(active_eng.packets),
        active_eng.packets[0].id,
        active_eng.packets[0].data_hex(),
        active_eng.revision,
        len(active_eng.raw_lines),
    )

    ctx = _make_ctx(str(cap_dir), active_eng)
    page = CaptureHistoryPage(ctx)
    try:
        assert page.list.count() == 2

        # Select demo capture
        demo_row = next(i for i in range(page.list.count()) if "capture_demo.i2ccap" in page.list.item(i).text())
        page.list.setCurrentRow(demo_row)
        qt_app.processEvents()

        meta_txt = page.meta.toPlainText()
        assert "DEMO · SIMULATED" in meta_txt
        assert "2026-11-15T10:30:00" in meta_txt
        assert "lost records: 0" in meta_txt
        assert '"format": "i2ccap"' not in meta_txt  # raw JSON hidden by default

        page.chk_raw_meta.setChecked(True)
        qt_app.processEvents()
        assert '"format": "i2ccap"' in page.meta.toPlainText()
        page.chk_raw_meta.setChecked(False)

        headers = [page.preview_table.horizontalHeaderItem(c).text() for c in range(page.preview_table.columnCount())]
        assert headers == PREVIEW_COLUMNS
        total_rows = page.preview_table.rowCount()
        assert total_rows == len(demo_eng.packets)

        # Quick address, status, and text filtering
        idx_68 = page.preview_addr.findData(0x68)
        assert idx_68 > 0
        page.preview_addr.setCurrentIndex(idx_68)
        assert 0 < page.preview_table.rowCount() < total_rows
        for r in range(page.preview_table.rowCount()):
            assert page.preview_table.item(r, 2).text() == "0x68"

        page.preview_addr.setCurrentIndex(0)
        page.preview_search.setText("BMP280")
        assert page.preview_table.rowCount() > 0
        for r in range(page.preview_table.rowCount()):
            assert "BMP280" in page.preview_table.item(r, 3).text()
        page.preview_search.clear()

        # Select live capture and verify LIVE badge + Has NACK status filter
        live_row = next(i for i in range(page.list.count()) if "capture_live.i2ccap" in page.list.item(i).text())
        page.list.setCurrentRow(live_row)
        qt_app.processEvents()
        live_meta = page.meta.toPlainText()
        assert "LIVE HARDWARE" in live_meta
        assert "DEMO · SIMULATED" not in live_meta
        assert "2026-11-15T09:15:00" in live_meta

        page.preview_status.setCurrentText("Has NACK")
        assert 0 < page.preview_table.rowCount() < len(live_eng.packets)

        # Verify active engine was NEVER mutated by previewing files
        after_snapshot = (
            active_eng.source,
            len(active_eng.packets),
            active_eng.packets[0].id,
            active_eng.packets[0].data_hex(),
            active_eng.revision,
            len(active_eng.raw_lines),
        )
        assert before_snapshot == after_snapshot
    finally:
        page.close()


def test_preview_current_session_reads_in_memory_packets_without_mutation(qt_app, tmp_path):
    eng = _live_engine_with_data()
    before_rev = eng.revision
    before_count = len(eng.packets)
    ctx = _make_ctx(str(tmp_path), eng)
    page = CaptureHistoryPage(ctx)
    try:
        page.b_preview_live.click()
        qt_app.processEvents()
        assert "LIVE HARDWARE" in page.meta.toPlainText()
        assert page.preview_table.rowCount() == len(eng.packets)
        assert "In-memory" in page.meta.toPlainText()
        assert eng.revision == before_rev and len(eng.packets) == before_count
    finally:
        page.close()


def test_export_session_excel_matches_csv_and_report_and_previews_in_ui(qt_app, tmp_path):
    eng = _live_engine_with_data()
    xlsx_path = tmp_path / "session_export.xlsx"
    csv_path = tmp_path / "reference.csv"

    n_xlsx = export_session_excel(xlsx_path, eng, session_id="S-EXCEL-01")
    n_csv = export_packets_csv(csv_path, eng)
    assert n_xlsx == n_csv == len(eng.packets)
    assert xlsx_path.exists()

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    try:
        assert wb.sheetnames == ["Summary", "Packets", "Devices", "Masters & Arbitration"]

        # Packets sheet matches export_packets_csv verbatim
        ws_pkt = wb["Packets"]
        pkt_rows = list(ws_pkt.iter_rows(values_only=True))
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            csv_rows = list(csv.reader(f))
        assert list(pkt_rows[0]) == csv_rows[0]
        assert len(pkt_rows) == len(csv_rows)
        for xl_row, c_row in zip(pkt_rows[1:], csv_rows[1:]):
            assert len(xl_row) == len(c_row)
            for xv, cv in zip(xl_row, c_row):
                if xv is None:
                    assert cv == ""
                elif isinstance(xv, (int, float)) and not isinstance(xv, bool) and cv != "":
                    assert float(xv) == pytest.approx(float(cv), rel=1e-12, abs=1e-12)
                else:
                    assert str(xv) == cv

        # Devices sheet matches verified device counts
        ws_dev = wb["Devices"]
        dev_rows = list(ws_dev.iter_rows(values_only=True))
        assert list(dev_rows[0]) == ["Address", "Device", "Confidence", "Transactions", "Reads", "Writes", "Bytes", "ACK", "NACK"]
        addrs = {r[0] for r in dev_rows[1:]}
        assert {"0x3C", "0x68", "0x76"}.issubset(addrs)

        # Summary & Masters sheets match build_session_report (no invented values)
        ws_sum = wb["Summary"]
        sum_text = "\n".join(" ".join(str(c) for c in r if c is not None) for r in ws_sum.iter_rows(values_only=True))
        rep = build_session_report(eng, "S-EXCEL-01")
        assert rep["title"] in sum_text
        assert "S-EXCEL-01" in sum_text

        ws_mm = wb["Masters & Arbitration"]
        mm_text = "\n".join(" ".join(str(c) for c in r if c is not None) for r in ws_mm.iter_rows(values_only=True))
        assert "COM32" in mm_text and "COM34" in mm_text and "Arbitration losses reported" in mm_text
    finally:
        wb.close()

    # Empty engine must write UNAVAILABLE with reason, never zero-fill
    empty = CaptureEngine()
    empty.set_source("live")
    empty_xlsx = tmp_path / "empty.xlsx"
    assert export_session_excel(empty_xlsx, empty) == 0
    wb2 = openpyxl.load_workbook(empty_xlsx, data_only=True)
    try:
        sum2 = "\n".join(str(c) for r in wb2["Summary"].iter_rows(values_only=True) for c in r if c is not None)
        assert "UNAVAILABLE — no analyzer handshake received" in sum2
        assert "UNAVAILABLE — no measured SCL cycles" in sum2
    finally:
        wb2.close()

    # Stdlib OOXML fallback round-trip
    fb_path = tmp_path / "fallback.xlsx"
    _write_xlsx_fallback(fb_path, [("Summary", [["Section", "Metric", "Value"], ["Test", "Count", 42]])])
    fb_loaded = _read_xlsx_fallback(fb_path)
    assert fb_loaded["sheets"][0]["name"] == "Summary"
    assert fb_loaded["sheets"][0]["rows"] == [["Test", "Count", "42"]]

    # UI integration: Export button + selecting .xlsx file previews sheets
    cap_dir = tmp_path / "ui_caps"
    cap_dir.mkdir()
    ctx = _make_ctx(str(cap_dir), eng)
    page = CaptureHistoryPage(ctx)
    page.show()
    try:
        out_ui = cap_dir / "ui_export.xlsx"
        with patch("gui.capture_history.QFileDialog.getSaveFileName", return_value=(str(out_ui), "Excel Workbook (*.xlsx)")):
            page.b_excel.click()
        assert out_ui.exists()
        assert page.list.count() == 1
        page.list.setCurrentRow(0)
        qt_app.processEvents()
        assert "EXCEL WORKBOOK (.xlsx)" in page.meta.toPlainText()
        assert not page.sheet_combo.isHidden()
        assert page.sheet_combo.isVisible()
        assert [page.sheet_combo.itemData(i) for i in range(page.sheet_combo.count())] == [
            "Summary",
            "Packets",
            "Devices",
            "Masters & Arbitration",
        ]
        pkt_idx = page.sheet_combo.findData("Packets")
        page.sheet_combo.setCurrentIndex(pkt_idx)
        qt_app.processEvents()
        assert page.preview_table.rowCount() == len(eng.packets)
    finally:
        page.close()


def test_filters_open_rename_delete_and_external_actions(qt_app, tmp_path):
    cap_dir = tmp_path / "caps"
    cap_dir.mkdir()
    ext_dir = tmp_path / "external"
    ext_dir.mkdir()

    demo_eng = _demo_engine(0.4)
    live_eng = _live_engine_with_data()
    save_capture(cap_dir / "alpha_demo.i2ccap", demo_eng)
    save_capture(cap_dir / "beta_live.i2ccap", live_eng)
    export_session_excel(cap_dir / "gamma_export.xlsx", live_eng)
    export_packets_csv(cap_dir / "delta_export.csv", live_eng)

    ext_file = ext_dir / "outside_live.i2ccap"
    save_capture(ext_file, live_eng)

    ctx = _make_ctx(str(cap_dir), live_eng)
    page = CaptureHistoryPage(ctx)
    try:
        assert page.list.count() == 4

        # Filter pills <-> combo sync
        page.filter_pills["Demo"].click()
        assert page.file_filter.currentText() == "Demo"
        assert page.list.count() == 1 and "alpha_demo.i2ccap" in page.list.item(0).text()

        page.file_filter.setCurrentText("Exports")
        assert page.filter_pills["Exports"].isChecked()
        assert page.list.count() == 2

        page.file_filter.setCurrentText("Live")
        assert page.list.count() == 1 and "beta_live.i2ccap" in page.list.item(0).text()

        page.filter_pills["All"].click()
        page.file_search.setText("gamma")
        assert page.list.count() == 1 and "gamma_export.xlsx" in page.list.item(0).text()
        page.file_search.clear()
        assert page.list.count() == 4

        # _open() adds external file with consistent size/metadata and immediately previews it
        with patch("gui.capture_history.QFileDialog.getOpenFileName", return_value=(str(ext_file), "")):
            page.b_open.click()
        qt_app.processEvents()
        cur = page.list.currentItem()
        assert cur is not None and "outside_live.i2ccap" in cur.text() and "KB" in cur.text()
        assert "LIVE HARDWARE" in page.meta.toPlainText()
        assert page.preview_table.rowCount() == len(live_eng.packets)

        # Open folder / Open externally invoke QDesktopServices.openUrl
        urls: list[QUrl] = []
        with patch("gui.capture_history.QDesktopServices.openUrl", side_effect=lambda u: urls.append(u) or True):
            page.b_folder.click()
            page.b_open_ext.click()
        assert len(urls) == 2 and urls[0].isLocalFile() and urls[1].toLocalFile().endswith("outside_live.i2ccap")

        # Rename file
        beta_row = next(i for i in range(page.list.count()) if "beta_live.i2ccap" in page.list.item(i).text())
        page.list.setCurrentRow(beta_row)
        with patch("gui.capture_history.QInputDialog.getText", return_value=("beta_renamed.i2ccap", True)):
            page.b_rename.click()
        assert not (cap_dir / "beta_live.i2ccap").exists()
        assert (cap_dir / "beta_renamed.i2ccap").exists()
        assert any("beta_renamed.i2ccap" in page.list.item(i).text() for i in range(page.list.count()))

        # Delete cancelled on No, executed on Yes
        ren_row = next(i for i in range(page.list.count()) if "beta_renamed.i2ccap" in page.list.item(i).text())
        page.list.setCurrentRow(ren_row)
        with patch("gui.capture_history.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            page.b_delete.click()
        assert (cap_dir / "beta_renamed.i2ccap").exists()

        with patch("gui.capture_history.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            page.b_delete.click()
        assert not (cap_dir / "beta_renamed.i2ccap").exists()
    finally:
        page.close()


def test_stay_on_capture_history_replay_progress_and_themes(qt_app, tmp_path):
    import gui.network_3d as network
    from gui.main_window import MainWindow

    cap_dir = tmp_path / "mw_caps"
    cap_dir.mkdir()
    demo_eng = _demo_engine(0.4)
    cap_file = cap_dir / "replay_test.i2ccap"
    save_capture(cap_file, demo_eng)

    saved_qss = qt_app.styleSheet()
    masters = {k: (m["color"], m["color_read"]) for k, m in MASTERS.items()}
    theme_mod.apply_theme(qt_app, "dark")
    try:
        with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
            w = MainWindow()
            try:
                w.settings.setValue("captures_dir", str(cap_dir))
                w.p_hist.scan()
                hist_idx = w.pages.index(w.p_hist)
                w.nav.setCurrentRow(hist_idx)
                w.p_hist.list.setCurrentRow(0)
                qt_app.processEvents()

                # Checked Stay on Capture History keeps tab on Capture History even at 1x real-time replay
                w.p_hist.stay_on_page.setChecked(True)
                w.p_hist.speed.setCurrentIndex(1)  # 1x real time
                w.p_hist.b_play.click()
                qt_app.processEvents()
                assert w.nav.currentRow() == hist_idx and w.stack.currentIndex() == hist_idx

                # Read-only refresh(engine) shows live replay progress
                w.replay_idx = 3
                w.replay_lines = [(0, b"{}")] * 10
                w.p_hist.refresh(w.engine)
                assert w.p_hist.replay_progress.value() == 30
                assert "3 / 10" in w.p_hist.replay_lbl.text()

                # Unchecked jumps to Live Dashboard (row 0) as usual
                w.stop_replay()
                w.p_hist.stay_on_page.setChecked(False)
                w.p_hist.speed.setCurrentIndex(0)  # Instant
                w.p_hist.b_play.click()
                qt_app.processEvents()
                assert w.nav.currentRow() == 0

                # Switch back to Capture History and verify dark <-> presentation themes
                w.nav.setCurrentRow(hist_idx)
                w.p_hist.list.setCurrentRow(0)
                qt_app.processEvents()

                w.set_theme("presentation")
                qt_app.processEvents()
                pres = THEMES["presentation"]
                assert pres["table_bg"].lower() in w.p_hist.list.styleSheet().lower()
                assert pres["cyan"].lower() in w.p_hist.replay_progress.styleSheet().lower()
                assert pres["demo_bg"].lower() in w.p_hist.meta.toHtml().lower()

                dark_only = {v.lower() for v in THEMES["dark"].values() if isinstance(v, str) and v.startswith("#")} - {
                    v.lower() for v in pres.values() if isinstance(v, str) and v.startswith("#")
                }
                hist_sheets = " ".join(
                    c.styleSheet().lower() for c in [w.p_hist] + w.p_hist.findChildren(QWidget) if c.styleSheet()
                )
                assert not [h for h in dark_only if h in hist_sheets]

                w.set_theme("dark")
                qt_app.processEvents()
                dark = THEMES["dark"]
                assert dark["table_bg"].lower() in w.p_hist.list.styleSheet().lower()
                assert dark["cyan"].lower() in w.p_hist.replay_progress.styleSheet().lower()
            finally:
                w.stop_replay()
                w.close()
                qt_app.processEvents()
    finally:
        theme_mod.apply_theme(qt_app, "dark")
        qt_app.setStyleSheet(saved_qss)
        for k, (c1, c2) in masters.items():
            MASTERS[k]["color"], MASTERS[k]["color_read"] = c1, c2
