"""Graph continuity, flicker and label layout: one clock, no NaN breaks, stable live waveform, reused items,
non-overlapping annotations, one sensor source, and the 'Active communication' banner."""
import json
import math
import os
import time
from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest

from analysis.capture_engine import CaptureEngine
from analysis.series import MIN_OBSERVED_S, health_series, perf_series, reference_second, time_window
from tests.helpers import synth, write
from tests.test_multi_master import run_bench
from tests.test_native_firmware import HELLO


@pytest.fixture(scope="module")
def qt_app(tmp_path_factory):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    d = tmp_path_factory.mktemp("settings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    yield QApplication.instance() or QApplication([])


def bucket(t, observed=1.0, **kw):
    d = dict(t=t, freq_khz=100.0, tps=10.0, util=5.0, acks=20, nacks=0, dur_us=500.0, throughput=300.0, observed=observed)
    d.update(kw)
    return d


# ------------------------------------------------------------------ series: one clock, no breaks, no partial seconds
def test_perf_series_drops_partial_seconds_and_nan_and_normalises_counts():
    e = CaptureEngine()
    for h in (bucket(100, observed=0.2, tps=999.0),              # partial first second: a spike, not a data point
              bucket(101, freq_khz=float("nan"), dur_us=float("nan")),
              bucket(102, acks=40, observed=2.0),                # 40 ACKs over 2 observed seconds = 20 / s
              bucket(103)):
        e.perf.history.append(h)
    x, ser, sig = perf_series(e, ("freq_khz", "tps", "acks"))
    assert list(x) == [-2, -1, 0]                                # partial second gone; clock = latest completed second
    fx, fy = ser["freq_khz"]
    assert list(fx) == [-1, 0] and np.isfinite(fy).all()         # the NaN second is skipped: ONE continuous line
    assert 999.0 not in ser["tps"][1]
    assert ser["acks"][1][-2] == pytest.approx(20.0)             # raw count -> per observed second
    assert reference_second(e) == 103 and MIN_OBSERVED_S == 0.5


def test_health_series_shares_the_clock_and_drops_the_second_in_progress():
    e = CaptureEngine()
    e.perf.history.extend([bucket(10), bucket(11), bucket(12)])
    e.communication.history.extend([(10.4, 0.0, 1.0), (11.7, None, None), (12.2, 0.5, 2.0), (13.6, 9.9, 9.9)])
    e.hw_now = 13.8e9                                            # capture second 13 is still in progress
    xu, yu, xr, yr, sig = health_series(e, reference_second(e))
    assert list(xu) == [-2, 0] and list(yu) == [0.0, 0.5]        # None dropped, in-progress second (13) dropped
    assert 9.9 not in yu and 9.9 not in yr
    assert xu.max() <= 0 and set(xr) <= {-2, 0}                  # same relative-second clock as perf_series
    e.hw_now = 20e9
    assert 9.9 in health_series(e, reference_second(e))[1]       # once that second completes it is plotted


def test_time_window_grows_in_ten_second_steps():
    assert time_window(np.array([-3.0, 0.0])) == 10 and time_window(np.array([-10.0, 0])) == 10
    assert time_window(np.array([-11.0, 0])) == 20 and time_window(np.array([-30.0, 0])) == 30
    assert time_window(np.array([-31.0, 0])) == 40 and time_window(np.empty(0)) == 10


# ------------------------------------------------------------------ waveform: stable live segment display
def snapshot_line(raw, shift=0):
    prev, entries = raw[0][0], []
    for t, st in raw:
        entries.append([t - prev, st])
        prev = t
    return json.dumps(dict(t="edges", snapshot=True, t0=raw[0][0] + shift, e=entries)).encode()


def native_engine():
    e = CaptureEngine()
    e.set_source("live")
    hello = dict(HELLO)
    hello["capabilities"] = hello["capabilities"] + ["edges"]
    hello["waveform"] = True
    e.ingest_lines([json.dumps(hello).encode()])
    return e


IDLE = lambda t0: json.dumps(dict(t="edges", snapshot=True, t0=t0, e=[[0, 3], [4000, 3]])).encode()      # noqa: E731


def big_page(qt_app, engine, w=2800, h=800):
    from gui.waveform_viewer import WaveformPage
    page = WaveformPage(SimpleNamespace(engine=engine))
    page.track.setChecked(False)                     # these tests guard the classic per-byte text rows above SCL
    page.resize(w, h)
    page.show()
    qt_app.processEvents()
    return page


def test_live_segment_waveform_stays_put_between_bursts_and_ignores_idle_windows(qt_app):
    e = native_engine()
    page = big_page(qt_app, e)
    e.ingest_lines([snapshot_line(synth(write(0x76, [0x65])))])
    assert e.waveform_transactions
    page.refresh(e)
    shown = page._shown_snap
    items = list(page.ann_items)
    x0, y0 = page.c_sda.getData()
    assert items and page.msg.toPlainText() == ""
    # an idle window (no START) arrives, then the engine momentarily has no edges: NOTHING on screen changes
    e.ingest_lines([IDLE(900_000)])
    assert not e.waveform_transactions
    for _ in range(5):
        page._shown_at -= 5
        page.refresh(e)
    assert page._shown_snap == shown and page.ann_items == items
    assert np.array_equal(page.c_sda.getData()[1], y0) and page.msg.toPlainText() == ""
    e.edges.clear()                                              # transient: no samples at all
    page.refresh(e)
    page._redraw()
    assert np.array_equal(page.c_sda.getData()[1], y0) and page.msg.toPlainText() == ""
    assert "waiting for the next measured window" in page.info.text()
    page.close()


def test_a_newer_valid_snapshot_replaces_the_display_only_after_the_dwell_time(qt_app):
    e = native_engine()
    page = big_page(qt_app, e)
    e.ingest_lines([snapshot_line(synth(write(0x76, [0x65])))])
    page.refresh(e)
    first = page._shown_snap
    e.ingest_lines([snapshot_line(synth(write(0x3C, [0x11])), shift=700_000)])
    assert e.waveform_snapshots != first and e.waveform_transactions
    page.refresh(e)
    assert page._shown_snap == first                             # shown for less than the dwell time: no strobing
    page._shown_at -= 5
    page.refresh(e)
    assert page._shown_snap == e.waveform_snapshots
    assert any(isinstance(x, pg.TextItem) and x.toPlainText().startswith("DATA 0x11") for x in page.ann_items)
    page.close()


def test_annotation_items_are_reused_not_recreated_and_never_overlap(qt_app):
    e = native_engine()
    page = big_page(qt_app, e)
    e.ingest_lines([snapshot_line(synth(write(0x76, [0x65, 0x66, 0x67])))])
    page.refresh(e)
    pi = page.plot.getPlotItem()
    before = {id(i) for i in page.ann_items}
    n_items = len(pi.items)
    texts_wide = len([i for i in page.ann_items if isinstance(i, pg.TextItem)])
    # zoom way out (labels must thin out, never pile up), then back in
    for lo, hi in ((0, 80_000), (0, 4_000), (0, 120)):
        pi.setXRange(lo, hi, padding=0)
        page._reannotate()
        rects = list(page.label_px)
        assert not [(a, b) for i, a in enumerate(rects) for b in rects[i + 1:]
                    if a[0] < b[1] and a[1] > b[0] and a[2] < b[3] and a[3] > b[2]], f"labels overlap at {lo}-{hi}"
        assert all(r[0] >= 0 and r[1] <= pi.getViewBox().width() + 1 for r in rects)      # fully inside the plot
    after = {id(i) for i in page.ann_items}
    pool = {id(i) for i in page._text_pool} | {id(i) for lst in page._line_pool.values() for i in lst}
    assert len(pi.items) <= n_items + 4                          # nothing was piled up on the plot item
    assert (before | pool) >= after or len(after - before - pool) <= 3      # reused from the pool, not rebuilt
    pi.setXRange(0, 700, padding=0)
    page._reannotate()
    assert len([i for i in page.ann_items if isinstance(i, pg.TextItem)]) >= 3
    page.close()
    assert texts_wide >= 3


def test_dense_labels_are_hidden_when_zoomed_out_and_shorter_when_tight(qt_app):
    e = native_engine()
    page = big_page(qt_app, e)
    page.win.setCurrentText("5 ms")                              # show the whole 2.3 ms snapshot, address byte included
    e.ingest_lines([snapshot_line(synth(write(0x76, [0x65, 0x66, 0x67, 0x68])))])
    page.refresh(e)
    pi = page.plot.getPlotItem()
    texts = lambda: [i.toPlainText() for i in page.ann_items if isinstance(i, pg.TextItem)]      # noqa: E731
    pi.setXRange(0, 700, padding=0); page._reannotate()
    full = texts()
    assert any(t.startswith("ADDR 0x76") for t in full) and any(t.startswith("byte 0xEC") for t in full)
    pi.setXRange(0, 12000, padding=0); page._reannotate()          # ~90 px per byte: short labels only
    mid = texts()
    assert any(t.startswith("0x76") for t in mid) and not any(t.startswith(("byte 0x", "ADDR 0x")) for t in mid)
    pi.setXRange(0, 80000, padding=0); page._reannotate()          # ~13 px per byte (< MIN_LABEL_BYTE_PX): no per-byte text
    far = texts()
    assert len(far) < len(mid) < len(full) and far == []
    page.close()


# ------------------------------------------------------------------ pages: persistent bars, quiet curves, banner
def bench_engine():
    _b, e = run_bench(seconds=6)                                 # two masters, real per-second history
    assert len(e.perf.history) >= 3
    return e


def test_performance_page_reuses_bars_and_does_not_touch_unchanged_curves(qt_app):
    from gui.performance_monitor import PerformancePage
    e = bench_engine()
    page = PerformancePage(SimpleNamespace(engine=e))
    page.resize(1500, 1400)
    page.show()
    qt_app.processEvents()
    page.refresh(e)
    bars, rb = page.bars, page._rb
    pi = page.p_addr.getPlotItem()
    n_items = len(pi.items)
    calls = {"n": 0}
    real = page.c_ack.setData
    page.c_ack.setData = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1]
    for _ in range(6):                                           # unchanged data: zero redraws
        page.refresh(e)
    assert calls["n"] == 0
    # new data: the SAME bar items are updated in place
    e.registry.devices[0x76].reads += 5
    page.refresh(e)
    assert page.bars is bars and page._rb is rb and len(pi.items) == n_items
    assert list(rb.opts["height"])[sorted(e.registry.devices).index(0x76)] >= 5
    # one clock and no NaN anywhere in the rolling graphs
    xs = []
    for pw in page._rel_widgets:
        for it in pw.getPlotItem().listDataItems():
            x, y = it.getData()
            if x is not None and len(x):
                assert np.isfinite(y).all() and x.max() <= 0
                xs.append(x.max())
    assert xs and set(xs) == {0.0}
    page.close()


def test_dashboard_graphs_share_the_clock_and_title_is_not_reset_every_tick(qt_app):
    import gui.network_3d as network
    from unittest.mock import patch
    from gui.main_window import MainWindow
    e0 = bench_engine()
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow()
        w.engine = e0
        w.packet_model.engine = e0
        d = w.p_dash
        d.refresh(e0)
        titles = {"n": 0}
        real = d.p_util.setTitle
        d.p_util.setTitle = lambda *a, **k: (titles.__setitem__("n", titles["n"] + 1), real(*a, **k))[1]
        for _ in range(5):
            d.refresh(e0)
        assert titles["n"] == 0                                  # unchanged title is not re-laid-out
        for pw in (d.p_freq, d.p_tps, d.p_util):
            for it in pw.getPlotItem().listDataItems():
                x, y = it.getData()
                if x is not None and len(x):
                    assert np.isfinite(y).all() and x.max() <= 0
        assert d.comm_banner.row.text()
        w.close()


def test_sensor_trends_use_one_master_and_never_interleave_two(qt_app):
    from gui.sensor_monitor import SensorPage
    e = CaptureEngine()
    e.set_source("live")
    page = SensorPage(SimpleNamespace(engine=e))
    page.resize(1500, 1000)
    page.show()
    qt_app.processEvents()
    for i in range(6):                                           # Master 1 reads 20.x, Master 2 reads 25.x, alternately
        e.update_master_telemetry("2026-11-14T12:00:0%d" % i, 20.0 + i * 0.01, 1000.0, master_id=1)
        e.update_master_telemetry("2026-11-14T12:00:0%d" % i, 25.0 + i * 0.01, 1010.0, master_id=2)
        page.refresh(e)
        assert page.temp.text().startswith("20."), page.temp.text()      # never flips to the other master's value
        time.sleep(0.002)
    ys = [it.getData()[1] for it in page.p_t.getPlotItem().listDataItems() if it.getData()[1] is not None][-1]
    assert len(ys) >= 5 and ys.max() < 21 and np.isfinite(ys).all()      # ONE series: no 25.x points mixed in
    assert "Master 1 telemetry" in page.p_t.getPlotItem().titleLabel.text
    sig = page._trend_sig
    page.refresh(e)
    assert page._trend_sig == sig                                # unchanged series: curves not touched again
    page.close()


def test_sensor_trend_prefers_bus_decoded_history_and_does_not_flip_back(qt_app):
    from gui.sensor_monitor import SensorPage
    e = CaptureEngine()
    e.set_source("live")
    e.hw_now = 50e9
    e.sensors.bmp.history[:] = [(40e9 + i * 1e9, 21.0 + i * 0.1, 1000.0 + i) for i in range(5)]
    for i in range(3):
        e.update_master_telemetry("2026-11-14T12:00:00", 30.0 + i, 900.0, master_id=2)
    page = SensorPage(SimpleNamespace(engine=e))
    page.show()
    page.refresh(e)
    assert page._trend_src == ("bus",)
    for i in range(3):                                           # more telemetry must not steal the graph
        e.update_master_telemetry("2026-11-14T12:00:01", 31.0 + i, 900.0, master_id=2)
        page.refresh(e)
        assert page._trend_src == ("bus",)
    assert "bus-decoded" in page.p_t.getPlotItem().titleLabel.text
    page.close()


def test_active_communication_names_master_slave_and_operation(qt_app):
    from analysis.multi_master import active_communication
    b, e = run_bench(seconds=3)
    t2 = next(t for t in e.packets if t.master_id == 2 and t.address == 0x76)
    info = active_communication(e, t2)
    bmp = e.registry.name(0x76)[0]                 # "BME280/BMP280" until the chip ID read tells them apart
    assert f"Master 2 (Pico W) → {bmp} (0x76)" in info["plain"] and "WRITE→READ" in info["plain"]
    t1 = next(t for t in e.packets if t.master_id == 1 and t.address == 0x68)
    assert "Master 1 (Pico H) → DS3231 RTC (0x68)" in active_communication(e, t1)["plain"]
    t2.master_id, t2.master_method = None, ""
    un = active_communication(e, t2)["plain"]
    assert f"Unattributed ↔ {bmp} (0x76)" in un and "Master" not in un
    assert active_communication(CaptureEngine())["plain"] == "no communication captured yet"
    assert active_communication(e)["txn"] is e.packets[-1]


def test_banner_sits_above_the_graphs_and_only_repaints_on_change(qt_app):
    from gui.widgets import CommBanner
    b, e = run_bench(seconds=3)
    page = big_page(qt_app, e, 1500, 900)
    page.refresh(e)
    pg_top = page.plot.mapTo(page, page.plot.rect().topLeft()).y()
    banner_bottom = page.banner.mapTo(page, page.banner.rect().bottomLeft()).y()
    assert banner_bottom <= pg_top and page.banner.height() <= 34
    ban = CommBanner()
    calls = {"n": 0}
    real = ban.row.setText
    ban.row.setText = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1]
    ban.update_from(e)
    ban.update_from(e)
    ban.update_from(e)
    assert calls["n"] == 1
    page.close()
