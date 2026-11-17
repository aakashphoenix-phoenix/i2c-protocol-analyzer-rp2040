"""Address -> device naming and per-address activity statistics.

A passive analyzer only sees addresses. Names therefore carry a confidence:
  Confirmed   an ID register value observed on the bus matched (e.g. BMP280 0xD0 = 0x58)
  Configured  the user told the program what is wired at this address
  Likely      known address of a common part, not verified
  Unknown     address not in the database
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config

# address -> candidate parts (common hobby modules)
KNOWN_ADDRESSES = {
    0x3C: "SSD1306/SH1106 OLED", 0x3D: "SSD1306 OLED",
    0x68: "DS3231 / DS1307 RTC or MPU-6050", 0x69: "MPU-6050 (AD0 high)",
    0x76: "BMP280 / BME280", 0x77: "BMP280 / BME280 / BMP180",
    0x57: "AT24C32 EEPROM (RTC module)", 0x50: "AT24Cxx EEPROM",
    0x23: "BH1750 light", 0x5C: "BH1750 light",
    0x27: "PCF8574 LCD backpack", 0x3F: "PCF8574A LCD backpack",
    0x38: "AHT10/AHT20", 0x40: "INA219 / HTU21D", 0x44: "SHT3x", 0x48: "ADS1115 / PCF8591",
    0x29: "VL53L0X", 0x1E: "HMC5883L", 0x0D: "QMC5883L",
}

# (address, register) -> {value: name}  used to confirm identities from observed reads
ID_REGISTERS = {
    (0x76, 0xD0): {0x58: "BMP280", 0x60: "BME280", 0x55: "BMP180"},
    (0x77, 0xD0): {0x58: "BMP280", 0x60: "BME280", 0x55: "BMP180"},
    (0x68, 0x75): {0x68: "MPU-6050", 0x70: "MPU-6500", 0x71: "MPU-9250"},
}


@dataclass
class DeviceStats:
    address: int
    ten_bit: bool = False
    transactions: int = 0
    reads: int = 0
    writes: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    acks: int = 0
    nacks: int = 0
    addr_nacks: int = 0
    first_seen: float | None = None
    last_seen: float | None = None
    last_ack_seen: float | None = None
    confirmed_name: str | None = None
    confirm_detail: str = ""
    recent: list = field(default_factory=list)  # start times in the last window

    @property
    def total_bytes(self):
        return self.bytes_read + self.bytes_written


class DeviceRegistry:
    def __init__(self):
        self.devices: dict[int, DeviceStats] = {}
        self.configured: dict[int, str] = dict(config.CONFIGURED_DEVICES)

    def clear(self):
        self.devices.clear()

    def set_oled_address(self, addr: int):
        self.configured = {a: n for a, n in self.configured.items() if "OLED" not in n}
        self.configured[addr] = "OLED (SSD1306)"

    def name(self, addr: int | None) -> tuple[str, str]:
        """(display name, confidence)"""
        if addr is None:
            return "—", ""
        d = self.devices.get(addr)
        if d and d.confirmed_name:
            return d.confirmed_name, "Confirmed"
        if addr in self.configured:
            return self.configured[addr], "Configured"
        if addr in KNOWN_ADDRESSES:
            return KNOWN_ADDRESSES[addr], "Likely"
        return f"Unknown device @0x{addr:02X}", "Unknown"

    def observe(self, txn) -> list:
        """Update statistics from a completed transaction. Returns new-address notices."""
        notices = []
        for f in txn.frames:
            if f.addr is None:
                continue
            d = self.devices.get(f.addr)
            if d is None:
                d = self.devices[f.addr] = DeviceStats(f.addr, ten_bit=f.ten_bit, first_seen=txn.t_start)
                if f.addr_ack:
                    notices.append((txn.t_start, "info", "New address",
                                    f"0x{f.addr:02X} first seen ({self.name(f.addr)[0]})", f.addr))
            d.last_seen = txn.t_start
            if f.addr_ack:
                d.last_ack_seen = txn.t_start
            else:
                d.addr_nacks += 1
            if f.rw == "R":
                d.reads += 1
                d.bytes_read += len(f.data)
            elif f.rw == "W":
                d.writes += 1
                d.bytes_written += len(f.data)
            d.acks += int(bool(f.addr_ack)) + sum(1 for a in f.acks if a)
            d.nacks += int(f.addr_ack is False) + sum(1 for a in f.acks if not a)
        addr = txn.address
        if addr is not None and addr in self.devices:
            d = self.devices[addr]
            d.transactions += 1
            d.recent.append(txn.t_start)
        return notices

    def node_list(self, now_ns: float):
        """Addresses that have ACKed at least once -> nodes for topology views.
        Addresses that never ACKed (probes of empty addresses) are not drawn as devices."""
        return [(a, *self.name(a), self.status(a, now_ns)) for a in sorted(self.devices)
                if self.devices[a].last_ack_seen is not None]

    def confirm(self, addr: int, reg: int, value: int):
        table = ID_REGISTERS.get((addr, reg))
        if not table or addr not in self.devices:
            return None
        name = table.get(value)
        d = self.devices[addr]
        if name and d.confirmed_name != name:
            d.confirmed_name = name
            d.confirm_detail = f"reg 0x{reg:02X} = 0x{value:02X}"
            return name
        return None

    def rate(self, addr: int, now_ns: float, window_s: float = 5.0) -> float:
        d = self.devices.get(addr)
        if not d:
            return 0.0
        lo = now_ns - window_s * 1e9
        d.recent = [t for t in d.recent if t >= lo]
        return len(d.recent) / window_s

    def status(self, addr: int, now_ns: float) -> str:
        d = self.devices.get(addr)
        if not d or d.last_seen is None:
            return "Not seen"
        if d.last_ack_seen is None:
            return "No device (NACK)"
        age = (now_ns - d.last_ack_seen) / 1e9
        if age < 3:
            return "Active"
        if d.last_seen > d.last_ack_seen:
            return "Not responding"
        return "Idle"
