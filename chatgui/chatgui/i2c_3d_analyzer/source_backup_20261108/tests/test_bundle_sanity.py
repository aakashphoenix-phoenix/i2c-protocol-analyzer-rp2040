"""Offline source-level contract checks for supplied C firmware and GUI."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_firmware_starts_only_on_host_command():
    s = (ROOT / 'firmware/native_rp2040_analyzer/packet_analyzer.c').read_text()
    assert 'requested_run = false' in s
    assert 'paused_for_overrun = true' in s
    assert 'processing_buffer == next' in s
    assert '"START"' in s and '"STOP"' in s

def test_gui_waits_for_handshake_before_start():
    s = (ROOT / 'gui/main_window.py').read_text()
    assert 'connected and self.caps is not None and not self.capturing' in s
