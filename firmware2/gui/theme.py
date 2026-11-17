"""Application-wide Qt stylesheet and font choices."""
from config import COLORS as C

UI_FONT = '"Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'
MONO_FONT = '"Cascadia Mono", "JetBrains Mono", Consolas, "DejaVu Sans Mono", monospace'

STYLESHEET = f"""
* {{ font-family: {UI_FONT}; color: {C['text']}; }}
QMainWindow, QWidget#root {{ background: {C['bg']}; }}
QWidget {{ font-size: 13px; }}
QToolTip {{ background: {C['card_hi']}; color: {C['text']}; border: 1px solid {C['cyan']}; padding: 6px 8px; border-radius: 6px; }}

/* ---------- sidebar */
QFrame#sidebar {{ background: {C['sidebar']}; border-right: 1px solid {C['border']}; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ color: {C['text2']}; padding: 10px 12px; margin: 2px 8px; border-radius: 9px; }}
QListWidget#nav::item:hover {{ background: #172238; color: {C['text']}; }}
QListWidget#nav::item:selected {{ background: #12304A; color: {C['cyan']}; border-left: 3px solid {C['cyan']}; }}

/* ---------- header / toolbar */
QFrame#header {{ background: #0B1222; border-bottom: 1px solid {C['border']}; }}
QLabel#appTitle {{ font-size: 18px; font-weight: 600; letter-spacing: 0.5px; }}
QLabel#appSub {{ color: {C['text2']}; font-size: 12px; }}
QLabel#muted, QLabel.muted {{ color: {C['text2']}; }}
QLabel#mono {{ font-family: {MONO_FONT}; }}

QPushButton {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 8px; padding: 7px 12px; min-height: 20px; }}
QPushButton:hover {{ border-color: {C['cyan']}; background: {C['card_hi']}; }}
QPushButton:pressed {{ background: #0E1A2C; }}
QPushButton:disabled {{ color: #4E5D75; border-color: #1A2438; background: #10182A; }}
QPushButton#primary {{ background: #0E4A5C; border-color: {C['cyan']}; color: #E6FCFF; font-weight: 600; }}
QPushButton#primary:hover {{ background: #11607A; }}
QPushButton#primary:disabled {{ background: #10182A; border-color: #1A2438; color: #4E5D75; font-weight: 600; }}
QPushButton#danger:disabled {{ background: #10182A; border-color: #1A2438; color: #4E5D75; }}
QPushButton#danger {{ background: #3A1620; border-color: #7A2A38; color: #FFC2C9; }}
QPushButton#demo:checked {{ background: #4A2A10; border-color: {C['demo']}; color: #FFD9B8; font-weight: 600; }}
QPushButton:checked {{ border-color: {C['cyan']}; background: #12304A; }}

QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{ background: #0C1424; border: 1px solid {C['border']}; border-radius: 7px; padding: 6px 8px; min-height: 20px; }}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: #3A5C8A; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {C['cyan']}; }}
QComboBox QAbstractItemView {{ background: {C['card']}; border: 1px solid {C['border']}; selection-background-color: #12304A; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #3A4E6E; background: #0C1424; }}
QCheckBox::indicator:checked {{ background: {C['cyan']}; border-color: {C['cyan']}; }}

/* ---------- cards */
QFrame#card {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 14px; }}
QFrame#card:hover {{ border-color: #2F4468; }}
QLabel#cardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#cardHint {{ color: {C['text2']}; font-size: 12px; }}
QLabel#metricValue {{ font-family: {MONO_FONT}; font-size: 28px; font-weight: 600; }}
QLabel#metricUnit {{ color: {C['text2']}; font-size: 13px; }}
QLabel#metricName {{ color: {C['text2']}; font-size: 12px; letter-spacing: 0.6px; }}
QLabel#bigValue {{ font-family: {MONO_FONT}; font-size: 40px; font-weight: 600; }}
QLabel#clock {{ font-family: {MONO_FONT}; font-size: 54px; font-weight: 600; color: {C['cyan']}; }}

/* ---------- tables */
QTableView {{ background: #0D1526; alternate-background-color: #111A2D; border: 1px solid {C['border']}; border-radius: 10px;
              gridline-color: transparent; selection-background-color: #173A5A; selection-color: {C['text']}; }}
QHeaderView::section {{ background: #131D31; color: {C['text2']}; border: none; border-bottom: 1px solid {C['border']};
                        padding: 7px 8px; font-weight: 600; font-size: 12px; }}
QHeaderView {{ background: #131D31; border: none; }}
QTableCornerButton::section {{ background: #131D31; border: none; }}

/* ---------- misc */
QScrollArea {{ background: transparent; border: none; }}
QWidget#scrollBody {{ background: {C['bg']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #26365A; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #33507F; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #26365A; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: #1C2A44; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {C['text2']}; padding: 8px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {C['cyan']}; border-bottom-color: {C['cyan']}; }}
QPlainTextEdit, QTextEdit {{ background: #0C1424; border: 1px solid {C['border']}; border-radius: 8px; font-family: {MONO_FONT}; }}
QLabel#bannerDemo {{ background: #3D2208; color: #FFD2A6; border-bottom: 1px solid {C['demo']}; font-weight: 700;
                     letter-spacing: 1.5px; padding: 6px; }}
QLabel#bannerReplay {{ background: #1F1A3D; color: #D4CCFF; border-bottom: 1px solid #6A5BFF; font-weight: 700;
                       letter-spacing: 1.5px; padding: 6px; }}
QLabel#bannerWarn {{ background: #2E2408; color: #FFE1A0; border-bottom: 1px solid {C['amber']}; padding: 6px; }}
"""
