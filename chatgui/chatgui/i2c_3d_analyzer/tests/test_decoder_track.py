"""Saleae / PulseView-style decoder track on the Digital Waveform: boxes on exact clock timestamps, colours, adaptive text,
no overlaps, clear of SCL / SDA / the A/B cursors, tooltips.  Real firmware-decoded traffic at 100 and 400 kHz."""
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPointF, QPointF as P, QRectF, Qt

import config
from config import COLORS as C
from tests.test_edge_stream import (_destroy_pages_after_each_test, bmp_read, qt_app, rate_engine, rate_exe,   # noqa: F401
                                    shown_page)
from tests.helpers import write

KINDS_BMP_READ = ["S", "addr", "ack", "data", "ack", "Sr", "addr", "ack", "data", "ack", "data", "ack", "data", "nack", "P"]


def track_page(qt_app, rate_exe, khz=400, build=bmp_read, w=1500, h=820):
    e = rate_engine(rate_exe, khz, build)
    page = shown_page(qt_app, e, w=w, h=h, track=True)
    page.grab()
    qt_app.processEvents()
    return e, page


def boxes(page):
    return page.decoder_track.layout()


def overlaps(bs):
    return [(a["kind"], b["kind"]) for i, a in enumerate(bs) for b in bs[i + 1:] if a["rect"].intersects(b["rect"])]


def set_view(page, x0, x1):
    page.plot.getPlotItem().setXRange(x0, x1, padding=0)
    page._reannotate()


@pytest.mark.parametrize("khz", [100, 400])
def test_track_has_every_element_in_order_on_the_exact_clock_timestamps(qt_app, rate_exe, khz):
    e, page = track_page(qt_app, rate_exe, khz)
    bs = boxes(page)
    assert [b["kind"] for b in bs] == KINDS_BMP_READ
    txn = e.waveform_transactions[-1]
    vb = page.plot.getPlotItem().getViewBox()
    x_of = lambda ns: vb.mapViewToScene(QPointF((ns - page.origin) / 1e3, 0)).x()          # noqa: E731
    by_kind = {}
    for b in bs:
        by_kind.setdefault(b["kind"], []).append(b)
    first_addr = by_kind["addr"][0]
    byte0 = txn.bytes[0]
    assert abs(first_addr["rect"].left() - x_of(byte0.bit_times[0])) < 0.6               # box starts on the 1st SCL clock
    pitch = (byte0.bit_times[7] - byte0.bit_times[0]) / 7
    assert abs(first_addr["seg"]["x1"] - (byte0.bit_times[7] + pitch - page.origin) / 1e3) < 1e-6   # ends after the 8th clock
    ack0 = by_kind["ack"][0]
    assert abs(ack0["rect"].left() - x_of(byte0.t_ack)) < 0.6                               # ACK on the 9th clock
    s_ns = next(tt for tt, k in txn.events if k == "S")
    assert abs(by_kind["S"][0]["rect"].left() - x_of(s_ns)) < 0.6                           # START at its event time
    p_ns = next(tt for tt, k in txn.events if k == "P")
    assert abs(by_kind["P"][0]["rect"].left() - x_of(p_ns)) < 0.6
    for b in bs:                                                                           # every data byte has its own box
        assert b["rect"].width() > 1.0


def test_write_blue_read_cyan_ack_green_nack_red(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe)
    txn = e.waveform_transactions[-1]
    bs = boxes(page)
    col = lambda kind, i=0: [b for b in bs if b["kind"] == kind][i]["color"]               # noqa: E731
    assert col("addr", 0) == C["blue"] and col("data", 0) == C["blue"]                    # write frame
    assert col("addr", 1) == C["cyan"] and all(b["color"] == C["cyan"] for b in bs if b["kind"] == "data" and b is not bs[3])
    assert col("data", 1) == C["cyan"] and col("data", 3) == C["cyan"]                    # read data: target -> controller
    assert all(b["color"] == C["green"] for b in bs if b["kind"] == "ack")
    assert [b["color"] for b in bs if b["kind"] == "nack"] == [C["red"]]
    assert col("S") == C["amber"] and col("Sr") == C["amber"] and col("P") == C["gray"]
    assert txn.frames[0].rw == "W" and txn.frames[1].rw == "R"


def test_text_adapts_to_the_box_width(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe)
    word = lambda kind, i=0: [b for b in boxes(page) if b["kind"] == kind][i]["text"]      # noqa: E731
    set_view(page, 0, 60)                                                                  # zoomed in: a clock is ~60 px wide
    assert word("data", 0) == "DATA 0xF7" and word("addr", 0) == "ADDR 0x76 W"
    assert word("ack", 0) == "ACK" and word("S") == "S"
    set_view(page, 0, 150)                                                                 # a clock ~24 px: room for 'A', not for 'ACK'
    assert word("ack", 0) == "A" and word("data", 0) == "DATA 0xF7"
    set_view(page, 0, 250)                                                                 # the Auto window: byte ~110 px
    assert word("data", 0) == "DATA 0xF7" and word("ack", 0) in ("A", "")                  # a 14 px clock: 'A' (font permitting)
    set_view(page, 0, 700)                                                                 # zoomed out: byte ~40 px
    assert word("data", 0) == "F7" and word("addr", 0) in ("0x76 W", "0x76", "W")          # shortened step by step
    set_view(page, 0, 4000)                                                                # far out: coloured bars, no text
    bs = boxes(page)
    assert bs and all(b["text"] == "" for b in bs if b["kind"] in ("data", "addr", "ack"))
    assert all(b["rect"].width() >= 3.0 for b in bs)                                       # still visible as bars


def test_nack_wording_shows_end_of_read_when_it_fits(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe)
    set_view(page, 150, 215)                                                               # the last read byte + its NACK, zoomed in
    txt = [b["text"] for b in boxes(page) if b["kind"] == "nack"]
    assert txt and txt[0].startswith("NACK")                                               # 'NACK (end)' / 'NACK' - never green


@pytest.mark.parametrize("khz,span", [(400, 90), (400, 250), (400, 600), (400, 1500), (100, 1000), (100, 3000), (100, 12000)])
def test_no_overlaps_and_clear_of_scl_sda_and_cursors_at_every_zoom(qt_app, rate_exe, khz, span):
    e, page = track_page(qt_app, rate_exe, khz)
    set_view(page, 0, span)
    page.grab()
    bs = boxes(page)
    assert overlaps(bs) == []
    tr = page.decoder_track
    vb = page.plot.getPlotItem().getViewBox()
    y_of = lambda y: vb.mapViewToScene(QPointF(0, y)).y()                                  # noqa: E731
    assert tr.band.bottom() < y_of(2.6) - 20                                               # clear above the SCL trace (top 2.6)
    assert tr.band.bottom() < y_of(1.25)                                                   # and above the bit digits / SDA
    assert tr.band.top() >= vb.sceneBoundingRect().top()                                   # inside the plot
    for b in bs:
        assert b["rect"].top() == tr.band.top() and b["rect"].height() == tr.H
    for ln in (page.ca, page.cb):                                                          # A/B letters live below the track
        r = ln.label.sceneBoundingRect()
        assert not r.intersects(tr.band), "cursor letter touches the decoder track"
        assert r.bottom() < y_of(2.6)                                                      # and above SCL
    page.close()


def test_boxes_never_overlap_each_other_even_when_the_view_is_panned_off_the_data(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe)
    for x0, x1 in ((-500, -100), (900, 1200), (-60, 40), (230, 330)):
        set_view(page, x0, x1)
        assert overlaps(boxes(page)) == []


def test_tooltips_explain_every_box(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe)
    tr = page.decoder_track
    tips = {}
    for b in boxes(page):
        tips.setdefault(b["kind"], []).append(b["tip"])
        assert tr.box_at(b["rect"].center()) is b
    assert "START" in tips["S"][0] and "SCL is HIGH" in tips["S"][0]
    assert "Repeated START" in tips["Sr"][0]
    assert "STOP" in tips["P"][0]
    assert "ADDRESS 0x76" in tips["addr"][0] and "WRITE" in tips["addr"][0] and "0xEC = 0x76<<1 | 0" in tips["addr"][0]
    assert "READ" in tips["addr"][1] and "0xED = 0x76<<1 | 1" in tips["addr"][1]
    assert "DATA 0xF7" in tips["data"][0] and "controller → target" in tips["data"][0]
    assert "DATA 0x51" in tips["data"][1] and "target → controller" in tips["data"][1]
    assert all(t.startswith("ACK") for t in tips["ack"])
    assert "controller (read data)" in tips["ack"][3] and "by the target" in tips["ack"][0]   # controller acks read data
    assert "normal end of read" in tips["nack"][0]                                         # the end-of-read NACK is explained
    assert tr.box_at(QPointF(-5, -5)) is None


def test_hover_highlights_the_box_and_the_item_lets_mouse_events_through(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe)
    tr = page.decoder_track
    assert tr.acceptedMouseButtons() == Qt.MouseButton.NoButton                           # pan / zoom / cursor drag unaffected
    target = next(b for b in boxes(page) if b["kind"] == "data")
    ev = SimpleNamespace(pos=lambda: target["rect"].center(), screenPos=lambda: QPointF(10, 10).toPoint())
    tr.hoverMoveEvent(ev)
    assert tr.hover is target
    tr.hoverMoveEvent(SimpleNamespace(pos=lambda: QPointF(-5, -5), screenPos=lambda: QPointF(10, 10).toPoint()))
    assert tr.hover is None
    tr.hoverMoveEvent(ev)
    tr.hoverLeaveEvent(None)
    assert tr.hover is None


def test_real_mouse_move_over_the_track_hovers_a_box(qt_app, rate_exe):
    from PyQt6.QtTest import QTest
    e, page = track_page(qt_app, rate_exe)
    tr = page.decoder_track
    target = next(b for b in boxes(page) if b["kind"] == "data")
    view = page.plot
    vp = view.viewport()
    pt = view.mapFromScene(target["rect"].center())
    QTest.mouseMove(vp, pt)
    QTest.mouseMove(vp, pt + QPointF(1, 0).toPoint())
    for _ in range(10):
        qt_app.processEvents()
    assert tr.hover is not None and tr.hover["kind"] == "data"


def test_decoder_track_switch_restores_the_classic_layout(qt_app, rate_exe):
    import pyqtgraph as pg
    e, page = track_page(qt_app, rate_exe)
    pi = page.plot.getPlotItem()
    assert page.track.isChecked() and pi.viewRange()[1][1] == pytest.approx(4.6)
    assert page.decoder_track.isVisible() and page._cursor_fracs == (0.74, 0.68)
    assert not [i for i in page.ann_items if isinstance(i, pg.TextItem) and i.toPlainText().startswith(("ADDR", "DATA", "ACK"))]
    page.track.setChecked(False)                                                           # classic: text rows, old y-range
    qt_app.processEvents()
    assert pi.viewRange()[1][1] == pytest.approx(4.0) and page._cursor_fracs == (0.97, 0.86)
    assert not page.decoder_track.isVisible() and page.decoder_track.segs == []
    texts = [i.toPlainText() for i in page.ann_items if isinstance(i, pg.TextItem)]
    assert any(t.startswith("ADDR 0x76") for t in texts) and any(t.startswith("DATA 0xF7") for t in texts)
    page.track.setChecked(True)
    qt_app.processEvents()
    assert pi.viewRange()[1][1] == pytest.approx(4.6) and boxes(page) and page.decoder_track.isVisible()
    page.ann.setChecked(False)                                                             # Annotations off clears the track too
    qt_app.processEvents()
    assert page.decoder_track.segs == []
    page.ann.setChecked(True)
    assert boxes(page)


def test_bit_digits_stay_between_sda_and_scl_with_the_track_on(qt_app, rate_exe):
    import pyqtgraph as pg
    e, page = track_page(qt_app, rate_exe)
    digits = [i for i in page.ann_items if isinstance(i, pg.TextItem) and i.toPlainText() in ("0", "1")]
    assert len(digits) >= 8 * 5
    tr = page.decoder_track
    assert all(not d.sceneBoundingRect().intersects(tr.band) for d in digits)


def test_address_nack_is_a_red_box_with_its_reason(qt_app, rate_exe):
    e, page = track_page(qt_app, rate_exe, 400, write(0x76, [], nack_addr=True))
    bs = boxes(page)
    kinds = [b["kind"] for b in bs]
    assert kinds == ["S", "addr", "nack", "P"]
    nack = bs[2]
    assert nack["color"] == C["red"] and "not acknowledged" in nack["tip"]
    assert overlaps(bs) == []
