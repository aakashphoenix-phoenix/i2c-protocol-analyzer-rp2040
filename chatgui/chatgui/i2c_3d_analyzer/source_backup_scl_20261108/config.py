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
    0x76: "BMP280",
    0x3C: "OLED (SSD1306)",
}
OLED_ADDRESS_DEFAULT = 0x3C
MASTER_LABEL = "Raspberry Pi Pico 1 · Master"
ANALYZER_LABEL = "Raspberry Pi Pico 2 · Analyzer"
NOMINAL_I2C_HZ = 100_000  # configured on the master - NEVER shown as a measurement

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
