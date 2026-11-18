"""Save -> reload must reproduce identical packets and timestamps."""
from analysis.capture_engine import CaptureEngine
from sources.demo_source import DemoGenerator
from storage.capture_storage import export_packets_csv, load_capture, save_capture


def _engine_with_demo(seconds=3):
    g = DemoGenerator(seed=7)
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines([g.hello()])
    for k in range(1, int(seconds / 0.05) + 1):
        e.ingest_lines(g.lines_until(k * 0.05))
    return e


def test_roundtrip_identical(tmp_path):
    e = _engine_with_demo()
    p = tmp_path / "x.i2ccap"
    save_capture(p, e)
    meta, lines = load_capture(p)
    assert meta["simulated"] is True and meta["analyzer"]["tick_ns"] == 100
    r = CaptureEngine()
    r.set_source("replay")
    r.ingest_lines(lines)
    a = [(t.t_start, t.t_end, t.address, t.data_hex(), t.status) for t in e.packets]
    b = [(t.t_start, t.t_end, t.address, t.data_hex(), t.status) for t in r.packets]
    assert a == b and len(a) > 50
    assert r.perf.compute().freq_avg_hz == e.perf.compute().freq_avg_hz


def test_demo_full_pipeline_sane():
    e = _engine_with_demo(5)
    s = e.perf.compute()
    assert s.malformed == 0 and s.incomplete == 0
    assert 99_000 < s.freq_avg_hz < 101_000
    assert e.registry.name(0x76) == ("BMP280", "Confirmed")
    assert e.sensors.rtc.time is not None and e.sensors.bmp.temperature is not None
    assert e.sensors.oled.coverage == 1.0


def test_csv_export(tmp_path):
    e = _engine_with_demo(1)
    p = tmp_path / "p.csv"
    n = export_packets_csv(p, e)
    text = p.read_text(encoding="utf-8")
    assert n == len(e.packets) and text.count("\n") == n + 1 and ",yes" in text
