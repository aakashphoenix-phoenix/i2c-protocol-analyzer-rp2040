"""Check GUI dependencies and bundled firmware sources before connecting hardware."""
from pathlib import Path
from importlib.util import find_spec

root = Path(__file__).resolve().parents[1]
libs = {"PyQt6": "PyQt6", "PyQtGraph": "pyqtgraph", "PyOpenGL": "OpenGL",
        "NumPy": "numpy", "PySerial": "serial"}
print("I2C 3D GUI setup check")
missing = []
for label, mod in libs.items():
    ok = find_spec(mod) is not None
    print(("OK " if ok else "MISSING ") + label)
    if not ok: missing.append(label)
for path in ("main.py", "communication/protocol_parser.py", "analysis/capture_engine.py",
             "firmware/native_rp2040_analyzer/packet_analyzer.c",
             "firmware/native_rp2040_analyzer/capture.pio",
             "firmware/native_rp2040_analyzer/CMakeLists.txt"):
    present = (root/path).is_file()
    print(("OK " if present else "MISSING ") + path)
    if not present: missing.append(path)
if missing:
    print("\nInstall missing Python packages or check extraction. See CONNECT_PICOS_WINDOWS.md.")
    raise SystemExit(1)
print("\nFiles and Python packages present. This does not verify USB hardware or firmware compilation.")
