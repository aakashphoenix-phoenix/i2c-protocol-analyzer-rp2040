"""Per-phase NACK attribution on REPLAYED analyzer-format records (not hardware).

The 'scan' vectors reproduce the bus pattern observed live on 2026-11-09 from the
flashed controller (bus scan of 0x08..0x77 every 5 s, 4 devices present); the
'after' vectors are the same schedule with the scan disabled (SCAN_PERIOD_MS 0).
"""
import json
from unittest.mock import patch

from tests.test_hardware_pipeline import qt_app
from tests.test_native_firmware import HELLO, seg
from tools.nack_phase_report import phase_table, summarise, trace_lines

PRESENT = {0x3C: 0x00, 0x57: 0xFF, 0x68: 0x05, 0x76: 0x58}


def native(addr, direction, data, acks, address_ack, seq, t_us, start="START", end="STOP"):
    d = seg(seq, t_us, t_us + 100, start, end, addr, direction, data, acks)
    d["address_ack"] = address_ack
    return d


def scan_records(seconds=60, scan=True):
    """Old-firmware scan probes: 1-byte READ to every address 0x08..0x77 every 5 s."""
    recs, seq = [], 1
    for k in range(int(seconds // 5)):
        if not scan:
            break
        t = k * 5_000_000
        for a in range(0x08, 0x78):
            if a in PRESENT:   # target ACKs; controller reads 1 byte then NACKs it (end of read)
                recs.append(native(a, "read", [PRESENT[a]], [False], True, seq, t))
            else:              # nobody pulls SDA low on the 9th clock
                recs.append(native(a, "read", [], [], False, seq, t))
            seq += 1
            t += 300
    return [json.dumps(HELLO).encode()] + [json.dumps(r).encode() for r in recs]


def engine_for(lines):
    from analysis.capture_engine import CaptureEngine
    e = CaptureEngine(); e.set_source("replay"); e.ingest_lines(lines)
    return e


def test_observed_pattern_eeprom_acks_every_probe_nacks_are_controller_end_of_read():
    e = engine_for(scan_records(60))
    c = phase_table(e.packets, 0x57)[0x57]
    s = summarise(c)
    assert s["transactions"] == 12 and s["acks"] == 12 and s["nacks"] == 12      # the "12/12" look
    assert c["addr-R:ACK"] == 12                                                   # EEPROM answered every time
    assert c["read[final]:NACK(controller end-of-read, normal)"] == 12            # driven by the controller
    assert s["unexpected"] == 0 and not any("target absent" in k for k in c)
    row = trace_lines(e.packets, 0x57, 1)[0]
    assert "S AF(R) A FFN P" in row                                               # 0x57<<1|1 = 0xAF


def test_observed_pattern_bus_wide_unexpected_nacks_are_the_scan_absent_addresses():
    e = engine_for(scan_records(60))
    allc = sum(phase_table(e.packets).values(), __import__("collections").Counter())
    s = summarise(allc)
    assert s["unexpected"] == 12 * 108 and s["nacks"] == 12 * 108 + 12 * 4       # 108 absent + 4 end-of-read per scan
    assert e.health_summary()["counts"]["unexpected_nacks"] == 12 * 108


def test_after_scan_disabled_same_procedure_eeprom_has_no_traffic():
    e = engine_for(scan_records(60, scan=False))
    assert phase_table(e.packets, 0x57) == {}                                     # nothing addresses 0x57 any more
    assert e.health_summary()["counts"]["unexpected_nacks"] == 0


def test_real_eeprom_write_cycle_nack_would_be_attributed_to_target():
    # AT24C32 write: S A0 + mem hi + mem lo + data, P; during its internal write
    # cycle (tWR) it does not acknowledge its address (ACK polling) — a TARGET NACK.
    lines = [json.dumps(HELLO).encode(),
             json.dumps(native(0x57, "write", [0x00, 0x10, 0x42], [True, True, True], True, 1, 0)).encode(),
             json.dumps(native(0x57, "write", [], [], False, 2, 1_000)).encode(),
             json.dumps(native(0x57, "write", [0x00, 0x10], [True, True], True, 3, 11_000, end="RESTART")).encode(),
             json.dumps(native(0x57, "read", [0x42], [False], True, 4, 11_200, start="RESTART")).encode()]
    c = phase_table(engine_for(lines).packets, 0x57)[0x57]
    assert c["addr-W:NACK(target absent/busy)"] == 1
    assert c["write[0]:ACK(target)"] == 2 and c["write[1]:ACK(target)"] == 2 and c["write[>=2]:ACK(target)"] == 1
    assert c["read[final]:NACK(controller end-of-read, normal)"] == 1
    assert summarise(c)["unexpected"] == 1


def test_gui_shows_eeprom_as_eeprom_and_attributes_its_nacks(qt_app):
    import gui.network_3d as network
    from gui.main_window import MainWindow
    assert network.kind_for(0x57, "AT24C32 EEPROM (RTC module)") == "eeprom"     # was "rtc" (drawn as DS3231)
    assert network.kind_for(0x68, "DS3231 RTC") == "rtc"
    with patch.object(network, "HAVE_GL", False), patch("gui.main_window.list_ports", return_value=[]):
        w = MainWindow(); w.engine.set_source("replay"); w.engine.ingest_lines(scan_records(60))
        w.p_net._select(0x57)
        text = w.p_net.info.text()
        assert "ACK / NACK (raw)" in text and "12 / 12" in text
        assert "end-of-read (sent by controller)</td><td><b>12" in text
        assert "address (target silent)</td><td><b>0" in text
        assert network.short_name(w.p_net.net.model.slaves[0x57]) == "AT24C32 EEPROM"
        w.close()
