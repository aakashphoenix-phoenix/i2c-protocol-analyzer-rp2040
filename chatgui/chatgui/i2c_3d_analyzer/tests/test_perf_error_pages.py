"""Performance Monitoring (100 / 400 kHz targets, Reset View, M1 vs M2) and Error & Diagnostics (jump to packet /
waveform, CSV export) on the REAL MainWindow, fed with a sample-accurate 2 MS/s two-master capture plus both
controllers' telemetry (tests/bus_synth.py).  Every master below comes from the attribution tracker, never from the test."""
import csv
import json
from unittest.mock import patch

import numpy as np
import pytest

import config
from analysis import scl_timing
from gui.widgets import fmt_freq
from tests.bus_synth import BusSynth, TRUE_HZ_100K, TRUE_HZ_400K, feed, standard_traffic
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)

PERF, PACKETS, WAVE, ERRORS = 4, 2, 3, 7               # MainWindow navigation rows


def window(qt_app, true_hz=TRUE_HZ_100K, telemetry=True, native=False):
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    w.timer.stop()
    b = standard_traffic(BusSynth(), true_hz)
    e = w.engine
    e.set_source("live"); e.stream_connected = True; e.capture_active = True
    for m in (1, 2):
        e.masters.set_connection(m, f"COM3{m + 1}", True)
    if native:
        e.ingest_lines([b.native_hello().encode()])
        recs = b.segment_records() + b.stream_records()
    else:
        e.ingest_lines([b.raw_hello().encode()])
        recs = b.raw_records()
    feed(e, recs + (b.master_records() if telemetry else []), ports=(1, 2) if telemetry else ())
    w.resize(1500, 1000)
    w.show()
    qt_app.processEvents()
    w._refresh()
    return w, b


def pooled_hz(packets):
    ps = pn = 0
    for t in packets:
        kept, _ = scl_timing.robust_periods(t)
        ps += sum(kept)
        pn += len(kept)
    return pn / ps * 1e9 if pn else None


# ------------------------------------------------------------------ Performance Monitoring
def show_perf(w, qt_app):
    w.nav.setCurrentRow(PERF)
    qt_app.processEvents()
    w._refresh()
    qt_app.processEvents()
    return w.p_perf


@pytest.mark.parametrize("hz_map,near,far", [(TRUE_HZ_100K, 100.0, 400.0), (TRUE_HZ_400K, 400.0, 100.0)])
def test_target_lines_show_the_near_target_without_squashing_the_curve(qt_app, hz_map, near, far):
    w, _b = window(qt_app, hz_map)
    p = show_perf(w, qt_app)
    assert sorted(ln.value() for ln in p.target_lines) == [100.0, 400.0]
    assert all(ln.isVisible() for ln in p.target_lines) and p.show_targets.isChecked()
    labels = sorted(ln.label.toPlainText() for ln in p.target_lines)
    assert labels == ["100 kHz · Standard-mode target", "400 kHz · Fast-mode target"]
    assert "100 / 400 kHz targets" in p.freq_plot.getPlotItem().titleLabel.text
    y = p._freq_y
    assert len(y) >= 1                                                    # completed (>= 0.5 s observed) seconds
    lo, hi = p.freq_plot.getPlotItem().getViewBox().viewRange()[1]
    assert lo <= y.min() and y.max() <= hi                                # every measured point in view
    assert p.targets_in_view() == {near: True, far: False}               # the near target legible, the far one not
    assert hi - lo < 0.3 * near                                          # the curve keeps a readable scale
    p.show_targets.setChecked(False)                                     # toggle: lines gone, range = data only
    assert not any(ln.isVisible() for ln in p.target_lines)
    lo2, hi2 = p.freq_plot.getPlotItem().getViewBox().viewRange()[1]
    assert hi2 - lo2 <= hi - lo
    p.show_targets.setChecked(True)
    assert p.targets_in_view()[near]
    w.close()


def test_reset_view_restores_every_graph_after_pan_and_zoom(qt_app):
    w, _b = window(qt_app)
    p = show_perf(w, qt_app)
    defaults = {id(pw): pw.getPlotItem().getViewBox().viewRange() for pw in p._rel_widgets}
    for pw in p._rel_widgets:                                            # the user drags / wheels every graph
        vb = pw.getPlotItem().getViewBox()
        vb.setXRange(-300, -250, padding=0)
        vb.setYRange(-5, 5, padding=0)
    p.p_addr.getPlotItem().setXRange(5, 9, padding=0)
    p.reset_view()
    for pw in p._rel_widgets:
        (x0, x1), (y0, y1) = pw.getPlotItem().getViewBox().viewRange()
        (dx0, dx1), (dy0, dy1) = defaults[id(pw)]
        assert (x0, x1) == pytest.approx((dx0, dx1))                    # the [-span, 0.5] window again
        if pw is p.health_plot:
            assert (y0, y1) == pytest.approx((dy0, dy1))                # fixed 0-100 % again
    assert p.targets_in_view()[100.0]                                    # target band re-applied on the SCL graph
    n = len(w.engine.registry.devices)
    assert p.p_addr.getPlotItem().getViewBox().viewRange()[0] == pytest.approx((-0.7, n - 0.3))
    w.close()


def test_m1_vs_m2_comparison_is_keyed_by_attribution_and_adds_up(qt_app):
    w, b = window(qt_app, TRUE_HZ_400K, native=True)
    p = show_perf(w, qt_app)
    s = w.engine.perf.compute()
    pk = w.engine.packets
    assert [t.master_id for t in pk] == [tx["master"] for tx in b.txns]                # tracker = ground truth
    texts = p.comparison_texts(s)
    for mid in (1, 2):
        mine = [t for t in pk if t.master_id == mid]
        g = s.by_master[mid]
        assert g.transactions == len(mine)
        assert texts[(mid, "txns")].startswith(f"{len(mine):,} (")
        assert texts[(mid, "bytes")] == f"{sum(t.byte_count for t in mine):,}"
        v, u = fmt_freq(pooled_hz(mine))
        assert texts[(mid, "fscl")] == f"{v} {u}"
        assert g.freq_mean_hz == pytest.approx(TRUE_HZ_400K[mid], rel=0.005)
        assert p.cmp[(mid, "fscl")].text() == texts[(mid, "fscl")]                    # rendered
    assert texts[(1, "nacks")] == "2 / 2" and texts[(2, "nacks")] == "0 / 32"          # probe + data NACK · read ends
    shares = [s.by_master[m].busy_share_pct for m in (1, 2)]
    assert sum(shares) == pytest.approx(100.0)
    assert not p.cmp[(None, "txns")].isVisible()                                       # no unattributed packet
    w.close()


def test_unattributed_column_appears_only_with_unattributed_packets_and_heads_follow_the_theme(qt_app):
    from gui import theme
    w, _b = window(qt_app, telemetry=False)
    p = show_perf(w, qt_app)
    s = w.engine.perf.compute()
    assert s.by_master[None].transactions > 0 and p.cmp[(None, "txns")].isVisible()
    assert p.cmp[(2, "fscl")].text() == "—"                                            # nothing proven for Master 2
    try:
        for name in ("presentation", "dark"):
            theme.apply_theme(qt_app, name)
            for mid in (1, 2):
                assert config.MASTERS[mid]["color"].lower() in p._cmp_heads[mid].styleSheet().lower()
            assert all(ln.pen.color().name().lower() == config.COLORS["text2"].lower() for ln in p.target_lines)
    finally:
        theme.apply_theme(qt_app, "dark")
    w.close()


def test_selected_packet_marker_follows_the_shared_selection(qt_app):
    w, _b = window(qt_app)
    p = show_perf(w, qt_app)
    txn = w.engine.packets[10]
    w.select_transaction(txn)
    ref = p._ref_s(w.engine)          # latest whole second, or the latest transaction while < 2 s are captured
    assert ref is not None
    for _pw, ln in p._sel_lines:
        assert ln.isVisible() and ln.value() == pytest.approx(txn.t_start / 1e9 - ref)
    w.close()


# ------------------------------------------------------------------ Error & Diagnostics
def show_errors(w, qt_app):
    w.nav.setCurrentRow(ERRORS)
    qt_app.processEvents()
    w._refresh()
    return w.p_err


def row_of(page, kind):
    return next(i for i in range(page.proxy.rowCount()) if page.proxy.index(i, 4).data() == kind)


def test_double_click_jumps_to_the_packet_and_the_waveform(qt_app):
    w, _b = window(qt_app)
    pe = show_errors(w, qt_app)
    i = row_of(pe, "Data NACK")
    ev = pe.event_at(pe.proxy.index(i, 0))
    want = next(t for t in w.engine.packets if t.data_nack_write)
    assert want.t_start <= ev.t_hw <= want.t_end and ev.addr == want.address == 0x3C
    pe.table.doubleClicked.emit(pe.proxy.index(i, 2))                  # a double-click on the row
    qt_app.processEvents()
    assert w.stack.currentIndex() == PACKETS
    assert w.p_pk.selected_transaction() is want and w.playback.txn is want
    assert "contains the event time" in pe.last_message and f"#{want.id}" in pe.last_message
    w.nav.setCurrentRow(ERRORS)
    pe.table.selectRow(row_of(pe, "Address NACK"))
    probe = pe.jump("waveform")
    assert probe.address == 0x77 and probe.addr_nack
    assert w.stack.currentIndex() == WAVE and w.p_wave.txn is probe and w.playback.txn is probe
    w.close()


def test_events_without_hardware_time_or_packets_never_navigate(qt_app):
    w, _b = window(qt_app)
    w.engine.ingest_lines([b"this is not a protocol record"])        # transport error: no hardware timestamp
    pe = show_errors(w, qt_app)
    i = row_of(pe, "Serial framing error")
    ev = pe.event_at(pe.proxy.index(i, 0))
    assert ev.t_hw is None
    w.nav.setCurrentRow(ERRORS)
    before = (w.stack.currentIndex(), w.playback.txn)
    pe.table.doubleClicked.emit(pe.proxy.index(i, 0))
    assert (w.stack.currentIndex(), w.playback.txn) == before
    assert "no hardware timestamp" in pe.last_message
    assert "No hardware timestamp" in pe.proxy.index(i, 0).data(3)  # tooltip says so (ToolTipRole)
    pe.table.clearSelection()
    assert pe.jump("packets") is None and "Select a diagnostics row" in pe.last_message
    w.close()
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        empty = MainWindow()
    empty.timer.stop()
    empty.engine.log("warning", "Analyzer", "Edge stream gap", "lost", t_hw=12345.0)
    pe = show_errors(empty, qt_app)
    assert pe.jump("packets", pe.event_at(pe.proxy.index(row_of(pe, "Edge stream gap"), 0))) is None
    assert "No packets captured yet" in pe.last_message
    empty.close()


def test_explain_tooltips_cover_multi_master_and_stream_events(qt_app):
    w, _b = window(qt_app)
    pe = show_errors(w, qt_app)
    i = row_of(pe, "Controller clock aligned")
    tip = pe.proxy.index(i, 4).data(3)
    assert "transfer log is now mapped" in tip
    from gui.error_monitor import EXPLAIN
    for kind in ("Arbitration lost", "Edge stream gap", "Byte cut by START/STOP", "Bus recovery", "Capture gap"):
        assert kind in EXPLAIN
    w.close()


def test_export_csv_writes_exactly_the_filtered_rows(qt_app, tmp_path):
    w, _b = window(qt_app)
    w.engine.ingest_lines([b"garbage line"])
    pe = show_errors(w, qt_app)
    path = tmp_path / "all.csv"
    n_all = pe.export_csv(str(path))
    rows = list(csv.reader(path.open(encoding="utf-8")))
    from gui.error_monitor import HEAD
    assert rows[0] == HEAD == ["Wall time", "HW time (s)", "Severity", "Category", "Event", "Address", "Details"]
    assert len(rows) - 1 == n_all == pe.proxy.rowCount() == len(w.engine.events)
    pe.sev.setCurrentText("Error")              # an Address NACK (absent 0x77) is an Invalid Address Error since 2026-11-17
    pe.cat.setCurrentText("I2C bus")
    pe.search.setText("0x77")
    out = tmp_path / "filtered.csv"
    n = pe.export_csv(str(out))
    got = list(csv.reader(out.open(encoding="utf-8")))[1:]
    assert n == len(got) == pe.proxy.rowCount() >= 1
    assert got == pe.visible_rows()
    assert all(r[2] == "ERROR" and r[3] == "I2C bus" and "0x77" in (r[4] + r[5] + r[6]) for r in got)
    with patch("gui.error_monitor.QFileDialog.getSaveFileName", return_value=(str(tmp_path / "dlg"), "CSV (*.csv)")):
        pe.btn_csv.click()                                                # the button: dialog -> export (+ .csv)
    assert (tmp_path / "dlg.csv").exists() and "Exported" in pe.last_message
    w.close()
