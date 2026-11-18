"""Packet Proof (one packet, all evidence), the cross-check against references, signal export and 'Unknown' masters."""
import os
from unittest.mock import patch

import pytest

from analysis.cross_check import (compare_with_reference, known_content, known_transactions, parse_saleae_csv,
                                  parse_sigrok_text)
from analysis.multi_master import attribution_detail, attribution_text
from analysis.packet_proof import UM10204, _judge, build_proof
from storage.signal_export import _clean, read_vcd, write_edges_csv, write_vcd
from tests.test_bench_simulation import run_bench
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def bench():
    _g, e, _ctl = run_bench(3.0, seed=5)
    return e


def newest(e, addr, rw=None):
    return next(t for t in reversed(e.packets) if t.address == addr and t.status == "ok"
                and (rw is None or t.operation == rw))


@pytest.mark.parametrize("addr,op,nbytes", [(0x76, "WRITE→READ", 1 + 1 + 1 + 6), (0x68, "WRITE→READ", 1 + 1 + 1 + 16),
                                            (0x3C, "WRITE", None)])
def test_a_packet_is_proven_bit_by_bit_from_its_raw_edges(bench, addr, op, nbytes):
    t = newest(bench, addr, op)
    p = build_proof(bench, t)
    assert p.raw_available and p.agree and p.verdict == "PROVEN", [(c.name, c.ok, c.detail) for c in p.checks]
    if nbytes:
        assert len(p.bits) == 9 * nbytes                       # 8 data bits + ACK per byte, address bytes included
    assert all(b["sda"] == b["decoded"] for b in p.bits)
    addr_bits = [b for b in p.bits if b["kind"] == "addr"][:8]
    assert [b["role"] for b in addr_bits] == ["A6", "A5", "A4", "A3", "A2", "A1", "A0", "R/W"]
    assert int("".join(str(b["decoded"]) for b in addr_bits[:7]), 2) == addr
    assert {b["driver"] for b in p.bits if b["kind"] == "addr"} == {"controller"}
    assert [c["kind"] for c in p.conditions][0] == "START" and p.conditions[-1]["kind"] == "STOP"
    assert all(c["ok"] for c in p.conditions)
    assert p.spec_mode == "Standard-mode" and all(c.ok for c in p.spec)
    assert p.master["report"] is not None and p.master["id"] == t.master_id
    if op == "WRITE→READ":
        reads = [b for b in p.bits if b["kind"] == "data" and b["driver"] == "target"]
        assert reads                                                   # the target drives SDA for read data
        last = [a for a in p.acks if a["ack"] is False]
        assert len(last) == 1 and "(expected" in last[0]["meaning"]   # end-of-read NACK, not a fault


def test_bmp280_burst_read_is_explained_down_to_degrees(bench):
    t = next(t for t in reversed(bench.packets) if t.address == 0x76 and t.frames[0].data[:1] == [0xF7])
    p = build_proof(bench, t)
    txt = "\n".join(p.meaning)
    assert "press_msb" in txt and "raw ADC" in txt and "°C" in txt and "hPa" in txt


def test_a_corrupted_stored_packet_fails_the_proof(bench):
    t = newest(bench, 0x76, "WRITE→READ")
    saved = list(t.frames[1].data)
    try:
        t.frames[1].data[0] ^= 0x10
        p = build_proof(bench, t)
        assert p.agree is False and p.verdict == "FAIL"
    finally:
        t.frames[1].data[:] = saved


def test_raw_edges_gone_is_reported_not_invented(bench):
    t = bench.packets[0]
    old = bench.edge_gaps
    bench.edge_gaps = old + [t.t_start + 1]
    try:
        p = build_proof(bench, t)
        assert not p.raw_available and not p.bits and p.verdict != "PROVEN"
        assert "gap" in p.raw_reason
    finally:
        bench.edge_gaps = old


def test_timing_judgement_respects_the_capture_resolution():
    lim = UM10204["Standard-mode"]["t_low"]
    assert _judge("t_low", lim + 300, lim, 100).ok is True
    assert _judge("t_low", lim - 300, lim, 100).ok is False
    assert _judge("t_low", lim + 50, lim, 100).ok is None             # within one sample: not decidable
    assert _judge("t_low", None, lim, 100).ok is None


def test_unknown_master_is_labelled_unknown_never_guessed():
    from tests.test_multi_master import Bench, live_engine, two_master_traffic
    b = Bench()
    two_master_traffic(b, seconds=1)
    e = live_engine()
    b.feed(e, ports=())
    t = e.packets[0]
    assert t.master_id is None and attribution_text(t) == "Unknown"
    assert attribution_detail(t, e.masters).startswith("Master UNKNOWN (not validated)")
    p = build_proof(e, t)
    assert p.master["name"] == "UNKNOWN"
    assert next(c for c in p.checks if c.name.startswith("Master")).ok is None


def test_known_transactions_and_content(bench):
    rows, unknown = known_transactions(bench)
    assert unknown == 0 and all(r["missing"] == 0 and r["matched"] > 0 for r in rows)
    k = known_content(bench)
    assert k["chip_id_reads"] and k["chip_id_ok"] == k["chip_id_reads"]
    assert k["rtc_reads"] and k["rtc_bcd_ok"] == k["rtc_reads"]


def _sigrok_lines(packets, corrupt=None):
    out = []
    for i, t in enumerate(packets):
        for fi, f in enumerate(t.frames):
            out.append("i2c-1: Start repeat" if fi else "i2c-1: Start")
            out.append(f"i2c-1: Address {'read' if f.rw == 'R' else 'write'}: {f.addr:02X}")
            out.append("i2c-1: ACK" if f.addr_ack else "i2c-1: NACK")
            for j, (d, a) in enumerate(zip(f.data, f.acks)):
                if corrupt == (i, fi, j):
                    d ^= 1
                out.append(f"{100 + j}-{109 + j} i2c-1: Data {'read' if f.rw == 'R' else 'write'}: {d:02X}")
                out.append("i2c-1: ACK" if a else "i2c-1: NACK")
        out.append("i2c-1: Stop")
    return "\n".join(out)


def test_reference_analyzer_decode_is_aligned_and_compared(bench):
    window = bench.packets[40:70]
    ref = parse_sigrok_text(_sigrok_lines(window))
    r = compare_with_reference(bench, ref)
    assert r["reference"] == 30 and r["matched"] == 30 and r["start_packet"] == window[0].id
    bad = parse_sigrok_text(_sigrok_lines(window, corrupt=(5, 0, 0)))
    r = compare_with_reference(bench, bad)
    assert r["matched"] == 29 and r["mismatches"]


def test_saleae_logic2_csv_is_parsed(bench):
    t = newest(bench, 0x76, "WRITE→READ")
    rows = ['name,type,start_time,duration,"ack","address","read","data"']
    for fi, f in enumerate(t.frames):
        rows.append('I2C,"start",0.1,0.0,,,,')
        rows.append(f'I2C,"address",0.1,0.0,{str(f.addr_ack).lower()},0x{f.addr:02X},{str(f.rw == "R").lower()},')
        for d, a in zip(f.data, f.acks):
            rows.append(f'I2C,"data",0.1,0.0,{str(a).lower()},,,0x{d:02X}')
    rows.append('I2C,"stop",0.1,0.0,,,,')
    ref = parse_saleae_csv("\n".join(rows))
    assert len(ref) == 1 and [f["start"] for f in ref[0]["frames"]] == ["S", "Sr"]
    from analysis.cross_check import _key, packet_key
    assert _key(ref[0]["frames"]) == packet_key(t)


def test_vcd_and_csv_export_are_the_captured_edges_exactly(bench, tmp_path):
    t = newest(bench, 0x68)
    ts, ss = bench.edges_between(t.t_start - 20_000, t.t_end + 20_000)
    sm = list(zip(ts, ss))
    n = write_vcd(tmp_path / "x.vcd", sm, bench.tick_ns)
    clean = _clean(sm)
    assert n == len(clean) - 1
    assert read_vcd(tmp_path / "x.vcd") == [(round(a - clean[0][0]), b) for a, b in clean]
    assert write_edges_csv(tmp_path / "x.csv", sm) == len(clean)
    assert (tmp_path / "x.csv").read_text().splitlines()[0] == "capture_time_ns,t_from_first_ns,SDA,SCL"


def test_proof_and_cross_check_windows_from_the_packet_analyzer(qt_app, tmp_path):
    import gui.network_3d as network
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        from gui.main_window import MainWindow
        w = MainWindow()
    try:
        w.timer.stop()
        _g, e, _c = run_bench(2.0, seed=9)
        w.engine = e
        w.p_pk.ctx.engine = e
        dlg = w.p_pk.open_proof(newest(e, 0x76, "WRITE→READ"))
        assert dlg.proof.verdict == "PROVEN" and "PROVEN" in dlg.head.text()
        assert "Bit by bit" in dlg.text.toHtml() and dlg.btn_newest is None
        path = dlg.save_report(str(tmp_path / "proof.html"))
        html_txt = open(path, encoding="utf-8").read()
        assert "data:image/png;base64," in html_txt and "Packet Proof" in html_txt and "UM10204" in html_txt
        assert dlg._export("vcd", str(tmp_path / "p.vcd")) and os.path.getsize(tmp_path / "p.vcd") > 100
        dlg.close()
        cc = w.p_pk.open_cross_check()
        assert "Known transactions" in cc.text.toHtml()
        ref = tmp_path / "ref.txt"
        ref.write_text(_sigrok_lines(e.packets[10:20]), encoding="utf-8")
        _p, fmt, r = cc.load_reference(str(ref))
        assert fmt.startswith("sigrok") and r["matched"] == 10
        cc.close()
    finally:
        w.close()


def test_a_genuine_bme280_chip_id_is_recognised_as_well_as_a_bmp280(bench):
    """Register 0xD0: 0x60 = BME280 (temperature, pressure, humidity), 0x58 = BMP280 (no humidity)."""
    t = next(t for t in reversed(bench.packets) if t.address == 0x76 and t.frames[0].data[:1] == [0xD0]
             and len(t.frames) == 2)
    saved = t.frames[1].data[0]
    try:
        for cid, name in ((0x60, "BME280"), (0x58, "BMP280")):
            t.frames[1].data[0] = cid
            k = known_content(bench)
            assert k["chip_id_ok"] == k["chip_id_reads"] and name in k["chip"]
            assert any(f"chip ID read 0x{cid:02X} → {name}" in line for line in build_proof(bench, t).meaning)
        t.frames[1].data[0] = 0x55                                     # BMP180: not this bench's sensor
        assert known_content(bench)["chip_id_ok"] < known_content(bench)["chip_id_reads"]
    finally:
        t.frames[1].data[0] = saved


def test_oled_live_text_write_is_explained_and_its_text_read_back():
    """Controller 2.6-live: one OLED write = [0x80, cmd] window commands + 0x40 + three text lines; Packet Proof lists
    the commands and reads the text back from the pixel columns (firmware glyph variants for ':' and '.')."""
    from analysis.packet_proof import oled_text_write_meaning
    from sources.demo_source import text_columns

    def fw(text):
        out = []
        for ch in text:
            out += {":": [0, 0, 0x36, 0, 0, 0], ".": [0, 0, 0x60, 0, 0, 0]}.get(ch, text_columns(ch))
        return out + [0] * (78 - len(out))
    hdr = [0x80, 0x20, 0x80, 0, 0x80, 0x21, 0x80, 0, 0x80, 77, 0x80, 0x22, 0x80, 0, 0x80, 2, 0x40]
    lines = oled_text_write_meaning(hdr + fw("M1 09:05:59") + fw("T 24.80 C") + fw("P 1009.12 HPA"))
    text = "\n".join(lines)
    assert "column window 0..77" in text and "page window 0..2" in text and "horizontal" in text
    assert 'page 0: text "M1 09:05:59"' in text and 'page 1: text "T 24.80 C"' in text
    assert 'page 2: text "P 1009.12 HPA"' in text
