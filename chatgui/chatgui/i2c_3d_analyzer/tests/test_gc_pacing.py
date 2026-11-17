"""Garbage-collector pacing (MainWindow.GC_FREEZE_S).

Measured on the live bench (2026-11-16): a full Python GC pass grew with the capture (0.46 s at ~30 k packets) and
blocked the analyzer's serial reader; Windows' usbser queues only ~25 kB (~0.5 s of the stream), so the Pico dropped
characters -> a corrupted line -> the capture was marked unreliable (Health UNKNOWN for the rest of the session).
The window therefore collects the young generations and freezes the survivors every GC_FREEZE_S, and unfreezes when a
new capture / demo / replay starts."""
import gc
from unittest.mock import patch

from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)
from tests.test_inspect_follow import live_window


def test_old_objects_are_frozen_periodically_and_released_for_a_new_session(qt_app):
    w, _ = live_window()
    w.timer.stop()
    calls = []
    with patch.object(gc, "collect", lambda gen=2: calls.append(("collect", gen)) or 0), \
            patch.object(gc, "freeze", lambda: calls.append(("freeze",))), \
            patch.object(gc, "unfreeze", lambda: calls.append(("unfreeze",))):
        w._gc_frozen_at = None
        w._pace_gc(100.0)                                   # first call only starts the clock
        w._pace_gc(100.0 + w.GC_FREEZE_S - 0.1)
        assert calls == []
        w._pace_gc(100.0 + w.GC_FREEZE_S)
        assert calls == [("collect", 1), ("freeze",)]      # young generations only: never a long full pass
        calls.clear()
        w._pace_gc(100.0 + w.GC_FREEZE_S + 1.0)
        assert calls == []                                  # at most one freeze per period
        w._playback_context = None                          # a new capture / demo / replay
        w._refresh()
        assert ("unfreeze",) in calls
    w.close()
    gc.unfreeze()


def test_a_full_collection_stays_short_with_a_large_frozen_heap():
    junk = [{"i": i, "l": [i, i + 1]} for i in range(300_000)]      # ~600 k tracked objects, like a long capture
    gc.collect(1)
    gc.freeze()
    try:
        import time
        t = time.perf_counter()
        gc.collect()
        assert time.perf_counter() - t < 0.1                  # the frozen heap is not traversed
    finally:
        gc.unfreeze()
        del junk
