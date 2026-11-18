"""Central configuration: theme colours, hardware defaults and protocol constants."""

APP_NAME = "I²C Packet Analyzer"
APP_SUBTITLE = "Real-Time Performance Monitoring System"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------- theme
COLORS = {
    "bg": "#080D19",
    "sidebar": "#101827",
    "card": "#151F32",
    "card_hi": "#1B2740",
    "border": "#24324D",
    "cyan": "#00D9FF",      # primary accent / READ
    "blue": "#5271FF",      # secondary accent / WRITE
    "green": "#36F5A2",     # active communication / ACK
    "amber": "#FFBF47",     # warnings / NACK
    "red": "#FF5364",       # errors / malformed
    "gray": "#6B7A92",      # incomplete
    "text": "#EAF2FF",
    "text2": "#91A2BA",
    "sda": "#00D9FF",
    "scl": "#FFBF47",
    "demo": "#FF8A3D",
}

# ---------------------------------------------------------------- hardware
# What THIS bench is wired as. These are configured names, shown as
# "Configured" until bus traffic confirms them (e.g. BMP280 chip-ID read).
CONFIGURED_DEVICES = {
    0x68: "DS3231 RTC",
    0x76: "BME280/BMP280",          # chip ID 0xD0: 0x60 = BME280, 0x58 = BMP280 (confirmed from the bus)
    0x3C: "OLED (SSD1306)",
}
OLED_ADDRESS_DEFAULT = 0x3C
MASTER_LABEL = "Raspberry Pi Pico 1 · Master 1"
ANALYZER_LABEL = "Raspberry Pi Pico 2 · Analyzer"
ANALYZER_PORT_HINT = "COM28"         # pre-selected when present; any port can be chosen
# USB serial numbers of this bench's boards.  Windows renumbers COM ports whenever a board is
# re-flashed or moved to another USB socket (the Pico W went COM33 -> COM34), so the GUI selects
# ports by serial number first and falls back to the COM hint.
ANALYZER_SERIAL = "50443405B828B41C"
# Multi-master bench: two controllers share SDA/SCL (see firmware I2C.c, MASTER_ID 1/2).
# "owns" = target addresses only that master ever addresses (firmware rule), used to label
# packets as "ownership (configured)" when no controller telemetry matched them.  Empty since firmware 2.1: Master 2
# also writes the OLED 0x3C (one command per cycle), so every OLED packet is attributed by telemetry only.
MASTERS = {
    # color = controller->target (write) animation, color_read = target->controller (read)
    1: dict(label="Master 1", board="Pico H", pico="Pico 1", port_hint="COM32", serial="5044505930DCBA1C",
            owns=(), color="#5271FF", color_read="#00D9FF"),
    2: dict(label="Master 2", board="Pico W", pico="Pico 3", port_hint="COM34", serial="E661640843857723",
            owns=(), color="#FF4FC3", color_read="#FFB3E6"),
}
# Packet ↔ controller-telemetry matching window (analyzer START vs controller transfer start).
ATTRIBUTION_TOLERANCE_NS = 400_000
NOMINAL_I2C_HZ = 100_000  # configured on the master - NEVER shown as a measurement
# Controller SCL settings offered in the GUI (applied by the CONTROLLER firmware only).
# Limits: DS3231 400 kHz max (Fast-mode), SSD1306 400 kHz max (2.5 us clock cycle),
# BMP280 3.4 MHz, RP2040 1 MHz -> UM10204 Standard-mode 100 kHz and Fast-mode 400 kHz.
# The passive analyzer samples at 2 MS/s: 20 samples per cycle at 100 kHz, 5 at 400 kHz
# (per-cycle values quantised to 0.5 us; use packet/rolling means at 400 kHz).
CONTROLLER_SCL_OPTIONS = [(100_000, "100 kHz · Standard-mode"), (400_000, "400 kHz · Fast-mode")]

# ---------------------------------------------------------------- protocol
PROTOCOL_VERSION = 1
SERIAL_BAUD = 115200          # ignored by USB CDC, kept for UART bridges
SERIAL_READ_TIMEOUT_S = 0.05
HELLO_TIMEOUT_S = 2.0

# ---------------------------------------------------------------- analysis
TXN_IDLE_TIMEOUT_NS = 20_000_000     # open transaction with no edges for 20 ms -> incomplete
ROLLING_WINDOW_S = 1.0
HISTORY_POINTS = 300                 # seconds of graph history
MAX_PACKETS_IN_MEMORY = 50_000
MAX_EDGES_IN_MEMORY = 400_000
MAX_EVENTS_IN_MEMORY = 10_000
GUI_REFRESH_MS = 100

# ---------------------------------------------------------------- theme presets
# COLORS (top of this file) is THE live palette: every module does `from config import COLORS as C` and reads it at use
# time, so gui.theme.apply_theme() switches themes by mutating that one dict IN PLACE (COLORS.update(THEMES[name])).
# A preset therefore holds EVERY token: the base palette plus derived surface / state tokens (header, tables, inputs,
# pills, banners, disabled/primary/danger buttons ...) and the master wire colours.  All colour values are '#RRGGBB'
# strings, except `shadow_a` (card drop-shadow alpha, 0-255) and `grid_a` (plot grid opacity), which are numbers.
# Inside a preset two tokens share a hex string ONLY when they are meant to be the same colour (sda/cyan, scl/amber,
# header/status, ...); near-identical whites are therefore nudged by 1-2 levels.  That makes the hex -> hex translation
# between two presets unambiguous, which is how the live re-theme converts colours cached by widgets (inline style
# sheets, pens, painted colours) without every widget having to know about themes.
THEME_DEFAULT = "dark"
THEME_LABELS = {"dark": "Dark (default)", "presentation": "Presentation (light, high contrast)"}

_DARK_DERIVED = {
    "header_bg": "#0B1222", "status_bg": "#0B1222", "table_bg": "#0D1526", "table_alt": "#111A2D", "head_sec": "#131D31",
    "hover": "#172238", "sel": "#12304A", "input_bg": "#0C1424", "panel_bg": "#0A1020", "pressed": "#0E1A2C",
    "primary_bg": "#0E4A5C", "primary_hi": "#11607A", "primary_text": "#E6FCFF",
    "danger_bg": "#3A1620", "danger_bd": "#7A2A38", "danger_text": "#FFC2C9",
    "demo_bg": "#3D2208", "demo_text": "#FFD2A6", "demo_btn_bg": "#4A2A10", "demo_btn_text": "#FFD9B8",
    "replay": "#8C7BFF", "replay_bg": "#1F1A3D", "replay_text": "#D4CCFF", "replay_bd": "#6A5BFF",
    "warn_bg": "#2E2408", "warn_text": "#FFE1A0",
    "dis_text": "#4E5D75", "dis_bg": "#10182A", "dis_bd": "#1A2438",
    "scroll": "#26365A", "scroll_hi": "#33507F", "border_hi": "#3A5C8A", "card_hover": "#2F4468", "check_bd": "#3A4E6E",
    "table_sel": "#173A5A", "split_hover": "#1C2A44", "plot_deep": "#070B14",
    "gnd": "#8A96AA", "usb": "#C58CFF", "unattr": "#9FB0CC",
    # ACK / NACK of the 3D diagram: pure green vs pure red, far from every master colour (M2 is magenta)
    "ack": "#39FF6A", "nack": "#FF2B2B",
    "shadow_a": 150, "grid_a": 0.12,
}
_PRESENTATION = {
    # base palette: dark ink #0F172A on white / near-white, crisp slate borders, saturated accents (all >= 4.5:1 on white)
    "bg": "#F3F6FA", "sidebar": "#FFFFFF", "card": "#FEFEFE", "card_hi": "#E8EEF5", "border": "#8596AD",
    "cyan": "#085870", "blue": "#1D4ED8", "green": "#047857", "amber": "#963E06", "red": "#B91C1C", "gray": "#475569",
    "text": "#0F172A", "text2": "#334155", "sda": "#085870", "scl": "#963E06", "demo": "#C2410C",
    # derived surfaces / states (matching tinted pill, banner, table, header tokens)
    "header_bg": "#F8FAFC", "status_bg": "#F8FAFC", "table_bg": "#FDFDFD", "table_alt": "#F0F4F8", "head_sec": "#E2E8F0",
    "hover": "#E1E7EF", "sel": "#DBEAFE", "input_bg": "#FCFCFD", "panel_bg": "#F7F9FB", "pressed": "#CBD5E1",
    "primary_bg": "#075985", "primary_hi": "#0C4A6E", "primary_text": "#FFFFFE",
    "danger_bg": "#FEE2E2", "danger_bd": "#DC2626", "danger_text": "#7F1D1D",
    "demo_bg": "#FFEDD5", "demo_text": "#7C2D12", "demo_btn_bg": "#FED7AA", "demo_btn_text": "#431407",
    "replay": "#6D28D9", "replay_bg": "#EDE9FE", "replay_text": "#4C1D95", "replay_bd": "#7C3AED",
    "warn_bg": "#FEF3C7", "warn_text": "#78350F",
    "dis_text": "#94A3C0", "dis_bg": "#EFF3F7", "dis_bd": "#CAD4E0",
    "scroll": "#A5B4CB", "scroll_hi": "#64748B", "border_hi": "#56657D", "card_hover": "#7488A6", "check_bd": "#6B7B93",
    "table_sel": "#BFDBFE", "split_hover": "#D5DEEA", "plot_deep": "#F6F8FA",
    "gnd": "#3D4B61", "usb": "#7B1FA2", "unattr": "#4B5E78",
    "ack": "#0A7A2F", "nack": "#D00000",
    "shadow_a": 45, "grid_a": 0.35,
    # master wire colours (config.MASTERS is updated from these on every theme switch)
    "m1": "#1D4ED8", "m1_read": "#085870", "m2": "#9D174D", "m2_read": "#C81E6A",
}
THEMES = {
    "dark": {**COLORS, **_DARK_DERIVED,
             "m1": MASTERS[1]["color"], "m1_read": MASTERS[1]["color_read"],
             "m2": MASTERS[2]["color"], "m2_read": MASTERS[2]["color_read"]},
    "presentation": _PRESENTATION,
}
COLORS.update(THEMES["dark"])          # the live palette starts as the default theme (every derived token present)
