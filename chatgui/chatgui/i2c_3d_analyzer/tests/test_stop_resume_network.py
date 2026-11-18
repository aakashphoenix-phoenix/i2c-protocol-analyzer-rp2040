"""Top Stop button: the 3D network finishes the transaction on display (every stage + every dot) and then freezes;
Connect all after Stop flushes everything from before Stop and restarts from fresh live data.

Replayed analyzer-format records (tools/repro_inspect_freeze.Bus) - not a hardware test."""
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)
from tests.test_inspect_follow import Bus, live_window


def _step(w, dt=0.25):
    """One GUI refresh `dt` seconds later (playback clock + dot animation of the visible diagrams)."""
    w._playback_wall -= dt
    w._refresh()
    for n in w._nets():
        n._tick(dt)


def _start_following(w, bus):
    w.show()
    w.nav.setCurrentRow(1)                                   # the 3D I2C Network page
    w._on_lines(bus.until(3_000_000))
    for _ in range(3):
        _step(w, 0.2)
    assert w.playback.txn is not None and not w.playback.finished
    return w.playback.txn


def test_stop_finishes_the_current_transaction_and_its_dots_then_freezes(qt_app):
    w, sent = live_window()
    bus = Bus()
    txn = _start_following(w, bus)
    w._on_lines(bus.until(5_000_000))                       # more packets are queued behind the one on display
    _step(w, 0.05)
    assert len(w._follow_queue) or w._follow_latest_txn is not None
    w.btn_stop.setEnabled(True)
    w.btn_stop.click()
    assert sent[-1] == "stop" and not w.capturing
    assert w.net_run_state == "draining" and w.btn_stop.text() == "■ Stopping…" and not w.btn_stop.isEnabled()
    assert len(w._follow_queue) == 0                         # queued packets never start
    assert "STOPPING" in w.p_net.net.hud_row1.text()
    seen = set()
    for _ in range(400):
        _step(w)
        seen.add(id(w.playback.txn))
        if w.net_run_state == "stopped":
            break
    assert w.net_run_state == "stopped"
    assert seen == {id(txn)} and w.playback.txn is txn       # only the in-flight packet was played to its end
    assert w.playback.finished and not w._dots_in_flight()   # ... every stage shown and every dot arrived
    assert "STOPPED" in w.p_net.net.hud_row1.text() and w.btn_stop.text() == "■ Stop"
    # frozen: later refreshes start nothing and spawn no dot
    arrived = w.p_net.net.particles_arrived
    for _ in range(20):
        _step(w, 0.5)
    assert w.playback.txn is txn and not w.p_net.net.particles and w.p_net.net.particles_arrived == arrived
    w.close()


def test_stop_while_idle_stops_at_once(qt_app):
    w, sent = live_window()
    w.stop_capture()
    assert w.net_run_state == "stopped" and sent == ["stop"]
    w.close()


def test_connect_all_after_stop_resumes_from_fresh_live_data_only(qt_app):
    w, sent = live_window()
    bus = Bus()
    _start_following(w, bus)
    old_packets = list(w.engine.packets)
    w.stop_capture()
    for _ in range(400):
        _step(w)
        if w.net_run_state == "stopped":
            break
    assert w.net_run_state == "stopped"
    w._on_lines(bus.until(6_000_000))                        # analyzer lines after Stop: not capturing -> dropped
    # everything is still connected: the button resumes instead of disconnecting
    w.port.currentData = lambda: "COM28"
    for c in w.master_ports.values():
        c.currentData = lambda: "COM3x"
    w._master_running = lambda mid: True
    w._set_state()
    assert w.btn_connect_all.text() == "Connect all"
    w.connect_all()
    assert w.capturing and sent[-1] == "start" and w.net_run_state == "running"
    assert w.engine.packets == [] and len(w._follow_queue) == 0 and w.playback.txn is None
    assert "STOPPED" not in w.p_net.net.hud_row1.text()
    w._on_lines(bus.until(9_000_000))                        # fresh live data
    for _ in range(3):
        _step(w, 0.2)
    assert w.playback.txn is not None and all(w.playback.txn is not t for t in old_packets)
    assert w.playback.txn in w.engine.packets
    assert w.btn_connect_all.text() == "Disconnect all"
    w.close()
