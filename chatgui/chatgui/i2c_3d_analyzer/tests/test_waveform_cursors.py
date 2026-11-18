"""Digital Waveform cursors A / B: placed once, never reset by new packets / sweeps / redraws; full A, B and delta
readout (capture time, SCL cycle at each cursor, 1/dt); never clipped; the header banner names cursor A's packet.

Traffic: the scripted presentation bus with the measured bench timing (SIMULATED)."""
from types import SimpleNamespace

import pytest

from analysis.capture_engine import CaptureEngine
from sources.presentation_source import PresentationBus
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)


def bench_engine(n=6):
    e = CaptureEngine(); e.set_source("demo")
    bus = PresentationBus()
    e.ingest_lines([bus.hello()])
    for _ in range(n):
        e.ingest_lines(bus.next_transfer()[0])
    return e, bus


@pytest.fixture
def page(qt_app):
    from gui.waveform_viewer import WaveformPage
    e, bus = bench_engine()
    p = WaveformPage(SimpleNamespace(engine=e))
    p.resize(1500, 900); p.show(); qt_app.processEvents()
    p.bus = bus
    yield p
    p.close()


def test_cursors_are_placed_once_and_never_reset(page, qt_app):
    e = page.ctx.engine
    page.show_packet(e.packets[1])                                   # first draw: the one-time default placement
    page.ca.setValue(25.0); page.cb.setValue(42.0)                   # the user places A and B
    for t in e.packets[2:]:                                          # other packets selected / fitted
        page.show_packet(t)
        assert (page.ca.value(), page.cb.value()) == (25.0, 42.0)
    page.follow.setChecked(True)                                     # live follow: new packets keep arriving
    for _ in range(4):
        e.ingest_lines(page.bus.next_transfer()[0])
        page._last = 0; page.last_draw = 0
        page.refresh(e)
        page._redraw(force=True)
        assert (page.ca.value(), page.cb.value()) == (25.0, 42.0)
    page._reset_sweep()                                              # trigger / window change
    assert (page.ca.value(), page.cb.value()) == (25.0, 42.0)


def test_fit_packet_brings_cursors_back_only_when_they_are_off_screen(page):
    e = page.ctx.engine
    page.show_packet(e.packets[1])
    page.ca.setValue(30.0); page.cb.setValue(60.0)
    page.fit.click()                                                 # inside the view: untouched
    assert (page.ca.value(), page.cb.value()) == (30.0, 60.0)
    page.ca.setValue(1e6)                                            # far outside the visible range
    page.fit.click()
    span = page.plot.getPlotItem().viewRange()[0][1]
    assert 0 <= page.ca.value() <= span and 0 <= page.cb.value() <= span


def test_readout_has_a_b_and_delta_with_formulas(page):
    from analysis.scl_timing import cycles
    e = page.ctx.engine
    t = e.packets[3]                                                 # BMP280 ADC burst read: many SCL cycles
    page.show_packet(t)
    cyc = list(cycles(t))
    (a0, a1, pa), (b0, b1, pb) = cyc[2], cyc[20]
    page.ca.setValue((a0 + 1000 - page.origin) / 1e3)
    page.cb.setValue((b0 + 1000 - page.origin) / 1e3)
    m = page.cursor_measurement()
    assert m["cycle_a"]["period_ns"] == pa and m["cycle_b"]["period_ns"] == pb
    assert m["cycle_a"]["txn_id"] == m["cycle_b"]["txn_id"] == t.id
    for c in (m["cycle_a"], m["cycle_b"]):
        assert c["high_ns"] + c["low_ns"] == c["period_ns"]
        assert c["duty_pct"] == pytest.approx(100 * c["high_ns"] / c["period_ns"])
        assert 92e3 <= c["frequency_hz"] <= 97e3                     # measured bench clock, not an ideal 100 kHz
    assert m["dt_ns"] == pytest.approx(abs(m["b_ns"] - m["a_ns"])) and m["freq_hz"] == pytest.approx(1e9 / m["dt_ns"])
    lines = page.cursor_lbl.text().split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("Cursor A: x_A = ") and "capture time" in lines[0] and "T_A = rise→next rise" in lines[0]
    assert "f_A = 1/T_A" in lines[0] and "HIGH" in lines[0] and "LOW" in lines[0] and "duty" in lines[0]
    assert lines[1].startswith("Cursor B: x_B = ") and "T_B = rise→next rise" in lines[1] and "f_B = 1/T_B" in lines[1]
    assert f"packet #{t.id}" in lines[0] and f"packet #{t.id}" in lines[1]
    assert lines[2].startswith("Δ (A ↔ B): Δt = |t_B − t_A| = ")
    assert f"Capture Frequency (1/Δt) = 1 / {m['dt_ns'] / 1e3:,.3f} µs = {m['freq_hz'] / 1e3:.3f} kHz" in lines[2]


def test_readout_is_never_clipped(page, qt_app):
    e = page.ctx.engine
    page.show_packet(e.packets[3])
    for w in (1500, 1000, 760):
        page.resize(w, 900)
        for _ in range(4):
            qt_app.processEvents()
        for lb in (page.info, page.cursor_lbl):
            need = lb.heightForWidth(lb.width())
            assert lb.height() >= need, (w, lb.height(), need)          # every wrapped line fits
        assert page.plot.height() >= 300                               # the waveform keeps its own space


def test_banner_names_the_packet_under_cursor_a(page):
    e = page.ctx.engine
    page.follow.setChecked(True)
    t0, t1 = e.packets[1].t_start - 50_000, e.packets[2].t_end + 50_000    # a window holding two packets
    page._plan = dict(t0=t0, t1=t1, key=None, label="test window")
    page._redraw(force=True)
    for t in (e.packets[1], e.packets[2]):
        page.ca.setValue(((t.t_start + t.t_end) / 2 - page.origin) / 1e3)
        assert page._banner_txn(e) is t
        assert f"#{t.id}" in page.banner._last                    # the header shows cursor A's packet number
