"""I²C Packet Analyzer and Real-Time Performance Monitoring System — entry point.

    python main.py            start in Real Hardware Mode
    python main.py --demo     start with Demo Mode on (simulated, clearly labelled)
"""
import os
import logging
import sys

# Allow running from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Optional project-local copy of the declared PyOpenGL dependency.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime_dependencies"))


def main():
    logging.basicConfig(level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont, QGuiApplication, QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    fmt = QSurfaceFormat()
    fmt.setSamples(4)                    # anti-aliased 3D edges
    fmt.setDepthBufferSize(24)
    QSurfaceFormat.setDefaultFormat(fmt)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("I2C Packet Analyzer")
    app.setStyle("Fusion")
    f = QFont("Segoe UI", 10)
    app.setFont(f)

    from gui.theme import STYLESHEET
    app.setStyleSheet(STYLESHEET)
    from gui.main_window import MainWindow

    w = MainWindow()
    screen = app.primaryScreen().availableGeometry()
    if screen.width() <= 1600 or screen.height() <= 900:
        w.showMaximized()
    else:
        w.resize(1680, 980)
        w.show()
    if "--demo" in sys.argv:
        w.btn_demo.setChecked(True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
