"""Tests for protocol/analysis can run without a display or PyQt6 install.

These are NOT GUI tests. When PyQt6 is installed it is imported normally.
"""
import importlib.util
import sys
import types

if importlib.util.find_spec("PyQt6") is None:
    qt = types.ModuleType("PyQt6")
    core = types.ModuleType("PyQt6.QtCore")
    core.QThread = type("QThread", (), {})
    core.pyqtSignal = lambda *args, **kwargs: None
    qt.QtCore = core
    sys.modules["PyQt6"] = qt
    sys.modules["PyQt6.QtCore"] = core
else:
    import gc

    import pytest

    @pytest.fixture(autouse=True)
    def _in_thread_analyzer_reader(monkeypatch):
        """Tests drive SerialWorker with patched serial.Serial doubles, which a separate reader process cannot see: they
        use the in-thread reader.  tests/test_serial_reader_process.py switches the real reader process back on."""
        from communication.serial_manager import SerialWorker
        monkeypatch.setattr(SerialWorker, "USE_READER_PROCESS", False)
        yield

    @pytest.fixture(autouse=True)
    def _deterministic_qt_teardown():
        """Destroy the Qt objects a test dropped at a SAFE point: right after the test.

        Many tests build a whole MainWindow, close it and let it go.  Left to the cyclic garbage collector, those windows
        were destroyed at an arbitrary allocation inside a LATER test - once in the middle of
        test_capture_history_ui's findChildren(QWidget) walk, which crashed the run with a Windows access violation
        (~1 run in 6).  Collecting here, then flushing Qt's deferred deletes, makes every test start from a clean heap.
        (The shipped GUI has exactly one window for its whole life: this concerns the test process only.)"""
        yield
        gc.collect()
        try:
            from PyQt6.QtCore import QCoreApplication, QEvent
            if QCoreApplication.instance() is not None:
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        except Exception:
            pass
