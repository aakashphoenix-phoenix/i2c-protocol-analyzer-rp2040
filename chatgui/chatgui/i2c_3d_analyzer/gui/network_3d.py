"""Interactive I²C network view (large 3D page and compact dashboard box).

One shared schematic layout drives both renderers:
  * 3D  - pyqtgraph.opengl on QOpenGLWidget, photo textures of the bench parts;
  * 2D  - QPainter fallback when OpenGL is unavailable/disabled.

Physical roles (verified from firmware/native_rp2040_master/I2C.c and
firmware/native_rp2040_analyzer/packet_analyzer.c):
  * Pico 1  = Master 1 (Pico H), i2c0 SDA=GP4 SCL=GP5, 100 kHz; drives the OLED.
  * Pico 3  = Master 2 (Pico W), same pins and bus; reads RTC/BMP280 on its own
              staggered schedule.  Drawn above the rails at the far end, with
              solid feeders (it drives the bus) and its own USB link.
              Which master drove a packet comes from controller telemetry
              (analysis.multi_master), never from the two-wire capture alone.
  * targets = DS3231 0x68, BMP280 0x76, SSD1306 0x3C (configured in firmware).
  * Pico 2  = passive analyzer, PIO inputs GP2=SDA GP3=SCL with pulls disabled;
              it never drives the bus, has no address and never ACKs.  It sends
              JSON-lines records to the PC over USB CDC.
Physical wiring that firmware cannot prove (common GND, target VCC, pull-ups)
is labelled as unverified.

Data flow: Master 1 (Pico 1) and Master 2 (Pico 3) each TX (controller -> target) and RX (target -> controller) with
the DS3231 / BMP280 / OLED on the shared bus; the dot starts at the board that owns the packet (own colours per master and
direction).  A packet whose master is unknown is drawn on the shared bus only.  The analyzer (Pico 2) is passive: it is
never the start or the end of a data dot, only dashed grey taps and a dim "observing" outline.

Animation overlays are driven ONLY by decoded transaction records handed in by
the capture engine (playback stages); nothing animates on its own.  Arrows show
which party transmits the current bit/byte — SDA itself is a bidirectional
open-drain wire, and a two-wire capture cannot prove which device pulled it LOW.
"""
from __future__ import annotations

import math
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap, QPolygonF, QTextDocument,
                         QTransform, QVector3D)
from PyQt6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QToolTip,
                             QVBoxLayout, QWidget)

import config
from config import ANALYZER_LABEL, COLORS as C, MASTER_LABEL
from gui.theme import on_change, remap_text, theme_serial
from gui.widgets import MOTION, ElidedLabel, FlowLayout, HudRow
from analysis.playback import capture_clock_text, format_capture_ns

try:
    import pyqtgraph.opengl as gl
    from pyqtgraph.opengl import MeshData
    HAVE_GL = True
except Exception:          # PyOpenGL missing or broken driver
    HAVE_GL = False

# ----------------------------------------------------------------------------- layout constants
CTRL_X, CTRL_Y = -10.2, 0.0
RAIL_X0 = -8.7
RAIL_Y = {"SDA": 1.1, "SCL": 0.0, "GND": -1.1}
RAIL_Z = 0.25
TARGET_Y = -5.0
TARGET_X0 = -5.4
TARGET_DX = 6.6        # roomy node spacing: titles/addresses never touch their neighbours
ANALYZER_Y = 5.7
PC_SIZE = (3.0, 2.0)
USB2_LANE_DEFAULT = 2.6      # scene units above the Pico row (2D fits it in pixels)
DROP_OFF = {"SDA": -0.5, "SCL": 0.0, "GND": 0.5}
TAP_OFF = {"SDA": -0.6, "SCL": 0.0, "GND": 0.6}
# Kept for backwards compatibility with older imports.
SLAVE_Y = TARGET_Y
SDA_OFF, SCL_OFF = RAIL_Y["SDA"], RAIL_Y["SCL"]

LINE_COL = {"SDA": C["sda"], "SCL": C["scl"], "GND": C["gnd"], "USB": C["usb"]}
UNATTR_COL = C["unattr"]                # packet whose master is unknown: neutral, never a guessed master colour
OBS_COL = C["gray"]                     # the passive analyzer OBSERVING the bus (taps, outline): never a data transfer
STATE_COL = {"HEALTHY": C["green"], "CONTENDED": C["cyan"], "DEGRADED": C["red"], "UNKNOWN": C["amber"],
             "NOT CONNECTED": C["red"]}


def master_color(mid, rw="W"):
    """Animation colour of a master: write (C→T) uses .color, read (T→C) .color_read."""
    m = config.MASTERS.get(mid)
    if m is None:
        return UNATTR_COL
    return m["color_read"] if rw == "R" else m["color"]


M_KEY = {1: "master", 2: "master2"}      # scene keys of the controllers (master id -> key)
M_ID = {v: k for k, v in M_KEY.items()}
ARB_COL = C["amber"]                    # arbitration / bus contention
CT_COL = C["blue"]          # controller -> target transmission
TC_COL = C["cyan"]          # target -> controller transmission
ACK_COL = C["ack"]           # dedicated high-contrast tokens (config.THEMES): never a master / wire colour
NACK_COL = C["nack"]


def _load_palette():
    """Re-read the module-level colours from the live palette (gui.theme calls this after every theme switch)."""
    global UNATTR_COL, OBS_COL, ARB_COL, CT_COL, TC_COL, ACK_COL, NACK_COL
    LINE_COL.update(SDA=C["sda"], SCL=C["scl"], GND=C["gnd"], USB=C["usb"])
    STATE_COL.update({"HEALTHY": C["green"], "CONTENDED": C["cyan"], "DEGRADED": C["red"], "UNKNOWN": C["amber"],
                      "NOT CONNECTED": C["red"]})
    UNATTR_COL, OBS_COL, ARB_COL, CT_COL, TC_COL = C["unattr"], C["gray"], C["amber"], C["blue"], C["cyan"]
    ACK_COL, NACK_COL = C["ack"], C["nack"]


on_change(_load_palette)


def overlay_q(alpha):
    """Label / badge backdrop drawn over the diagram: the page background colour with the given alpha (0-255)."""
    q = QColor(C["bg"])
    q.setAlpha(alpha)
    return q


def overlay_css(alpha):
    q = QColor(C["bg"])
    return f"rgba({q.red()},{q.green()},{q.blue()},{alpha})"


def rgba(hex_color, a=1.0, k=1.0):
    q = QColor(hex_color)
    return (min(1, q.redF() * k), min(1, q.greenF() * k), min(1, q.blueF() * k), a)


DOT_LAYER_DZ = 0.02         # each dot layer sits this much nearer the camera than the one drawn before it (see below)
DOT_DEPTH_VALUE = 100       # GL draw order: the dot layers are drawn AFTER every wire, board texture and stage overlay


def particle_render_data(particles, z=0.1):
    """Arrays for the four 3D scatter layers of the moving dots (pure numpy: testable without an OpenGL context).

    halo  large, faint disc of the dot colour         trail  short comet tail along the polyline behind the dot
    core  the dot itself, solid colour                spark  small light centre (the 'bright core')
    Every layer is (pos Nx3, rgba Nx4, size N) and is drawn with ordinary 'translucent' blending, so the dots read on the
    dark AND on the light presentation background (additive blending vanishes on white).

    pyqtgraph's 'translucent' preset keeps GL_DEPTH_TEST on (default GL_LESS) and writes depth, so two layers at the SAME z
    would reject each other's fragments (the core / bright centre inside the halo).  Each layer is therefore lifted by
    DOT_LAYER_DZ over the previous one, in drawing order halo < trail < core < spark."""
    n = len(particles)
    if not n:
        return None
    pos = np.array([p.pos() for p in particles], dtype=float)
    pos[:, 2] += z
    core_pos, spark_pos = pos.copy(), pos.copy()
    core_pos[:, 2] += 2 * DOT_LAYER_DZ
    spark_pos[:, 2] += 3 * DOT_LAYER_DZ
    base = np.array([rgba(p.color) for p in particles], dtype=float)
    big = np.array([1.35 if getattr(p, "mark", None) else 1.0 for p in particles], dtype=float)   # ACK / NACK reply dots
    halo = base.copy()
    halo[:, 3] = 0.30
    spark = base.copy()
    spark[:, :3] = base[:, :3] * 0.35 + 0.65                       # towards white: a bright centre on any colour
    spark[:, 3] = 0.95
    tpos, tcol, tsize = [], [], []
    for p in particles:
        step = p.total * TRAIL_STEP
        for k in range(1, TRAIL_POINTS + 1):
            s = p.s - k * step
            if s < 0:
                break
            q = np.array(p.pos_at(s), dtype=float)
            q[2] += z + DOT_LAYER_DZ
            fade = 1.0 - k / (TRAIL_POINTS + 1)
            c = rgba(p.color)
            tpos.append(q)
            tcol.append((c[0], c[1], c[2], 0.65 * fade))
            tsize.append(4.0 + 8.0 * fade)
    trail = (np.array(tpos, dtype=float).reshape(-1, 3), np.array(tcol, dtype=float).reshape(-1, 4),
             np.array(tsize, dtype=float))
    return dict(halo=(pos, halo, 34.0 * big), trail=trail, core=(core_pos, base, 14.0 * big),
                spark=(spark_pos, spark, 6.0 * big))


def kind_for(addr, name):
    n = (name or "").lower()
    # Checked first: the RTC module's EEPROM is named "AT24C32 EEPROM (RTC module)",
    # which must not be drawn/labelled as the DS3231 itself.
    if "eeprom" in n or "at24" in n:
        return "eeprom"
    if "oled" in n or "ssd1306" in n:
        return "oled"
    if "ds3231" in n or "rtc" in n:
        return "rtc"
    if "bmp" in n or "bme" in n:
        return "bmp"
    return "unknown"


KIND_STYLE = {   # board footprint (x, y) in scene units, pcb colour
    "pico": ((2.1, 5.1), "#1E7A46"),
    "rtc": ((2.2, 3.4), "#1F4FA3"),
    "bmp": ((2.0, 2.6), "#5E2C8C"),
    "oled": ((2.7, 2.8), "#123C70"),
    "eeprom": ((2.0, 1.6), "#2F4A6E"),      # no photo asset: drawn as a plain board, not a fake one
    "unknown": ((2.0, 1.6), "#4A566B"),
}

# Module header labels read from the supplied component photos (silkscreen).
HEADER_PINS = {
    "rtc": "HW-084 header: GND VCC SDA SCL SQW 32K",
    "bmp": "GY-BME/PM280 header: VCC GND SCL SDA CSB SDO",
    "oled": "0.96\" I²C module, 4-pin header",
}


@lru_cache(maxsize=32)
def hardware_image(kind, horizontal=False):
    """Decode each transparent photographic asset once for all network views."""
    image = QImage(str(Path(__file__).resolve().parents[1] / "assets" / "hardware" / f"{kind}.png"))
    if horizontal and not image.isNull():
        image = image.transformed(QTransform().rotate(-90), Qt.TransformationMode.SmoothTransformation)
    return image.convertToFormat(QImage.Format.Format_RGBA8888)


@lru_cache(maxsize=32)
def hardware_texture(kind, horizontal=False):
    image = hardware_image(kind, horizontal)
    if image.isNull():
        return None
    data = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8)
    data = data.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4]
    return np.ascontiguousarray(data.reshape(image.height(), image.width(), 4)[::-1].transpose(1, 0, 2))


@lru_cache(maxsize=64)
def board_size(kind, horizontal=False):
    """Footprint fitted to the photo's real aspect ratio (no stretching)."""
    (sx, sy), _ = KIND_STYLE[kind]
    if horizontal:
        sx, sy = sy, sx
    image = hardware_image(kind, horizontal)
    if image.isNull():
        return sx, sy
    scale = min(sx / image.width(), sy / image.height())
    return image.width() * scale, image.height() * scale


def display_name(device):
    if device["kind"] == "bmp" and device["conf"] != "Confirmed":
        return "BME280/BMP280 (Unverified)"
    return device["name"]


def short_name(device):
    return {"rtc": "DS3231 RTC", "bmp": "BMP280" if device["conf"] == "Confirmed" else "BMP/BME280",
            "oled": "SSD1306 OLED", "eeprom": "AT24C32 EEPROM"}.get(device["kind"], device["name"][:14] or "Device")


# The 2D diagram's label layout collapses to ~1/4 of its size when the view is shorter than ~268 px (measured: scale 4-6
# below, 12+ above), so the large view never gets less than this - the page scrolls instead of shrinking the diagram.
VIEW_MIN_H = 272
TRAVEL_FRACTION = 0.8       # a stage's dot crosses its whole wire within this share of the stage; the rest is dwell
MIN_TRAVEL_S = 0.20         # ...but never faster than ~6 frames of the 33 ms dot timer: a long wire (Master 2 -> RTC is 24 scene
#                             units) crossed in 2-3 frames at 2x follow pace reads as 'the dot never appeared'.  Dots are not
#                             cleared between stages, so a floored dot simply overlaps the next stage's.
MAX_PARTICLES = 120         # dots alive at once; beyond that the OLDEST goes (a new stage's dot is never the one refused)
STEP_CAP = 0.2              # one animation frame moves a dot at most this share of its wire (stall -> lag, never teleport)
TRAIL_POINTS = 5            # comet tail: points behind every moving dot (3D scatter layers and the 2D painter)
TRAIL_STEP = 0.035          # distance between tail points, as a share of the path length


class Particle:
    """One dot travelling its path (transmitter -> receiver) in `travel_s` seconds: s / total is the fraction done.
    src / dst are the scene keys of the sending and the receiving board: both outlines pulse (sender when the dot leaves,
    receiver when it ARRIVES)."""
    __slots__ = ("path", "seg_len", "total", "s", "speed", "color", "travel_s", "src", "dst", "arrived", "partial", "mark")

    def __init__(self, path, color, speed=0.55, travel_s=None, src=None, dst=None, partial=False, mark=None):
        self.src, self.dst, self.arrived = src, dst, False
        self.mark = mark                    # 'ack' / 'nack' for an acknowledge-slot dot (drawn larger, with its own symbol)
        self.partial = partial              # a deliberately truncated pulse (lost arbitration): it never 'arrives'
        self.path = np.asarray(path, dtype=float)
        d = np.diff(self.path, axis=0)
        self.seg_len = np.sqrt((d ** 2).sum(axis=1))
        self.total = float(self.seg_len.sum()) or 1.0
        self.s = 0.0
        self.speed = speed
        self.color = color
        self.travel_s = float(travel_s) if travel_s else 1.0 / (speed * 2.2)

    @property
    def done(self):
        return self.s >= self.total

    def pos(self):
        return self.pos_at(self.s)

    def pos_at(self, s):
        """Point `s` scene units along the path (clamped to the path)."""
        s = max(0.0, s)
        for i, L in enumerate(self.seg_len):
            if s <= L or i == len(self.seg_len) - 1:
                f = 0 if L == 0 else min(1.0, s / L)
                return self.path[i] + f * (self.path[i + 1] - self.path[i])
            s -= L
        return self.path[-1]


class NodeModel:
    """Shared schematic layout for both renderers (scene units, z = up)."""

    def __init__(self):
        self.slaves: dict[int, dict] = {}     # addr -> {name, conf, status, x, kind}
        self.order: list[int] = []
        self.masters = [m for m in config.MASTERS if m in M_KEY]
        self.usb2_lane = None       # scene y of the USB lane; Topology2D._fit puts it 9 px above every caption

    def update(self, devices):
        """devices: list of (addr, name, confidence, status).  Returns "layout" when a board was added or changed its kind
        (the scene must be rebuilt), "text" when only a caption changed (name / confidence / status), else False."""
        changed = False
        layout = False
        for addr, name, conf, status in devices:
            if addr not in self.slaves:
                self.slaves[addr] = {"name": name, "conf": conf, "status": status, "kind": kind_for(addr, name)}
                changed = layout = True
            else:
                s = self.slaves[addr]
                if s["name"] != name or s["status"] != status or s["conf"] != conf:
                    changed = True
                if s["name"] != name:
                    kind = kind_for(addr, name)
                    layout = layout or kind != s["kind"]
                    s["kind"] = kind
                s.update(name=name, conf=conf, status=status)
        if layout:
            # configured devices first in a fixed order, then discovered ones by address
            fixed = [a for a in (0x68, 0x76, 0x77, 0x3C, 0x3D) if a in self.slaves]
            rest = sorted(a for a in self.slaves if a not in fixed)
            self.order = fixed + rest
            for i, a in enumerate(self.order):
                self.slaves[a]["x"] = TARGET_X0 + i * TARGET_DX
        return "layout" if layout else ("text" if changed else False)

    # ---------------------------------------------------------------- geometry
    @property
    def rail_end(self):
        last = TARGET_X0 + TARGET_DX * max(len(self.order) - 1, 2)
        if self.two_masters:                     # rails continue to Master 2's feeders
            return max(last + 2.0, self.master2_x() + DROP_OFF["GND"] + 1.0)
        return last + 2.0

    @property
    def two_masters(self):
        return 2 in self.masters

    def master2_x(self):
        # Above the rails, half a slot beyond the last target: its feeders never line up with a
        # target's drop wires (which would read as a direct connection), and it stays clear of
        # the analyzer and of the rail labels.
        return TARGET_X0 + TARGET_DX * (max(len(self.order) - 1, 2) + 0.5)

    def feed_x(self, line):
        return self.master2_x() + DROP_OFF[line]

    def master2_bottom(self):
        return ANALYZER_Y - board_size("pico", True)[1] / 2

    def feed_path(self, line):
        """Master 2 pin -> rail (solid: Master 2 drives SCL and SDA)."""
        x = self.feed_x(line)
        return [(x, self.master2_bottom(), RAIL_Z), (x, RAIL_Y[line], RAIL_Z)]

    def usb2_columns(self):
        """Scene x of the Master 2 USB riser (mid-gap between the analyzer and Master 2) and of its descent
        (just left of the PC).  Fixed geometry: captions are placed around these columns, not the reverse."""
        sx, _ = board_size("pico", True)
        px, _ = self.pc_center()
        riser = ((self.analyzer_x() + sx / 2) + (self.master2_x() - sx / 2)) / 2
        return riser, px - PC_SIZE[0] / 2 - 0.9

    def master2_usb_path(self):
        """Master 2 USB -> PC.  Leaves the board on its connector side (left edge), rises in the gap between the
        analyzer and Master 2, runs ABOVE every top caption (usb2_lane, fitted in pixels by the 2D view) and
        comes down the PC's left side, so it never passes behind the analyzer / PC / Master 2 captions."""
        sx, sy = board_size("pico", True)
        px, py = self.pc_center()
        rx, lx = self.usb2_columns()
        lane = self.usb2_lane if self.usb2_lane is not None else ANALYZER_Y + sy / 2 + USB2_LANE_DEFAULT
        return [(self.master2_x() - sx / 2, ANALYZER_Y, RAIL_Z), (rx, ANALYZER_Y, RAIL_Z), (rx, lane, RAIL_Z),
                (lx, lane, RAIL_Z), (lx, py, RAIL_Z), (px - PC_SIZE[0] / 2, py, RAIL_Z)]

    def analyzer_x(self):
        # Between the first two target drops so the passive taps never sit on a target's wires.
        return TARGET_X0 + TARGET_DX / 2

    def pc_center(self):
        return (CTRL_X, ANALYZER_Y)

    def target_size(self, addr):
        return board_size(self.slaves[addr]["kind"], False)

    def target_top(self, addr):
        return TARGET_Y + self.target_size(addr)[1] / 2

    def drop_x(self, addr, line):
        return self.slaves[addr]["x"] + DROP_OFF[line]

    def tap_x(self, line):
        return self.analyzer_x() + TAP_OFF[line]

    def resolve_master(self, mid):
        """Controller id (1 / 2) that drives a packet, or None when the bus owner is genuinely unknown.

        With a single configured controller every packet is that controller's (nobody else can drive the bus).  With two
        masters an unattributed packet stays unknown: it is never given a guessed master - but it is NOT drawn as
        coming from the passive analyzer either (path_to)."""
        if mid in (1, 2) and mid in self.masters:
            return mid
        if mid is None and len(self.masters) <= 1:
            return 1
        return None

    def controller_key(self, mid):
        """Scene key ('master' / 'master2') of the controller board that drives a packet, None if unknown."""
        return M_KEY.get(self.resolve_master(mid))

    def path_to(self, addr, line="SDA", master=1):
        """Controller pin -> rail -> target pin, along one bus line.

        master: 1 / 2 = the attributed controller (Master 1 leaves Pico 1's pin, Master 2 its own feeder);
        None = bus owner unknown: only the shared-bus part (rail junction -> target) is drawn.  It never starts at the
        analyzer tap: the analyzer is passive, it transmits to / receives from NO target."""
        if not isinstance(line, str):           # legacy offset argument
            line = "SDA"
        y = RAIL_Y[line]
        x = self.drop_x(addr, line)
        end = [(x, y, RAIL_Z), (x, self.target_top(addr), RAIL_Z)]
        if master is None:
            master = self.resolve_master(None)          # a single configured controller owns every packet
        if master == 2 and self.two_masters:
            return self.feed_path(line) + end
        if master is None:
            return end
        # Master 1 leaves its board at the pin edge, where the visible rail starts (CTRL_X + half the board width)
        return [(CTRL_X + board_size("pico", False)[0] / 2, y, RAIL_Z)] + end

    def tap_path(self, line):
        x = self.tap_x(line)
        _, sy = board_size("pico", True)
        return [(x, RAIL_Y[line], RAIL_Z), (x, ANALYZER_Y - sy / 2, RAIL_Z)]

    def usb_path(self):
        sx, _ = board_size("pico", True)
        px, py = self.pc_center()
        return [(self.analyzer_x() - sx / 2, ANALYZER_Y, RAIL_Z), (px + PC_SIZE[0] / 2, py, RAIL_Z)]

    def controller_usb_path(self):
        _, sy = board_size("pico", False)
        px, py = self.pc_center()
        return [(CTRL_X, CTRL_Y + sy / 2, RAIL_Z), (px, py - PC_SIZE[1] / 2, RAIL_Z)]

    def segments(self):
        """(name, points, colour, dashed) for every schematic connection."""
        out = []
        _, csy = board_size("pico", False)
        csx = board_size("pico", False)[0]
        for line, y in RAIL_Y.items():
            out.append((f"rail:{line}", [(CTRL_X + csx / 2, y, RAIL_Z), (self.rail_end, y, RAIL_Z)],
                        LINE_COL[line], False))
            for a in self.order:
                x = self.drop_x(a, line)
                out.append((f"drop:{a}:{line}", [(x, y, RAIL_Z), (x, self.target_top(a), RAIL_Z)],
                            LINE_COL[line], False))
            out.append((f"tap:{line}", self.tap_path(line), LINE_COL[line], True))
        out.append(("usb:analyzer", self.usb_path(), LINE_COL["USB"], False))
        out.append(("usb:controller", self.controller_usb_path(), LINE_COL["USB"], True))
        if self.two_masters:
            for line in RAIL_Y:
                out.append((f"feed:master2:{line}", self.feed_path(line), LINE_COL[line], False))
            out.append(("usb:master2", self.master2_usb_path(), LINE_COL["USB"], True))
        return out

    def junctions(self):
        pts = []
        for line, y in RAIL_Y.items():
            pts += [(self.drop_x(a, line), y, RAIL_Z) for a in self.order]
            pts.append((self.tap_x(line), y, RAIL_Z))
            if self.two_masters:
                pts.append((self.feed_x(line), y, RAIL_Z))
        return pts

    def rects(self):
        """key -> (cx, cy, sx, sy) for boards and the PC."""
        r = {"master": (CTRL_X, CTRL_Y, *board_size("pico", False)),
             "analyzer": (self.analyzer_x(), ANALYZER_Y, *board_size("pico", True)),
             "pc": (*self.pc_center(), *PC_SIZE)}
        if self.two_masters:
            r["master2"] = (self.master2_x(), ANALYZER_Y, *board_size("pico", True))
        for a in self.order:
            r[a] = (self.slaves[a]["x"], TARGET_Y, *self.target_size(a))
        return r

    def bounds_points(self):
        pts = []
        for cx, cy, sx, sy in self.rects().values():
            pts += [(cx + dx * sx / 2, cy + dy * sy / 2, RAIL_Z) for dx in (-1, 1) for dy in (-1, 1)]
        for _, seg, _, _ in self.segments():
            pts += list(seg)
        return pts

    def label_specs(self, compact, level=0):
        """(key, text, anchor(x,y,z), placement, colour, bold, priority).

        placement: below / above / right / left of the anchor point.
        level 0 = full text; 1 = short text (compact box); 2 = minimal text for
        very small boxes.  Roles, addresses and pins are kept at every level.
        """
        compact = compact or level >= 1
        tiny = level >= 2
        specs = []
        r = self.rects()
        cx, cy, sx, sy = r["master"]
        two = self.two_masters
        m1 = ("Pico 1 · MASTER 1 (Pico H)" if two else "Pico 1 · CONTROLLER") if not compact else (
            ("Pico 1 · Master 1" if two else "Pico 1 · controller") if not tiny else ("M1 Pico 1" if two else "Pico 1 ctrl"))
        specs.append(("master", m1, (cx, cy - sy / 2, RAIL_Z), "below", config.MASTERS.get(1, {}).get("color", C["text"])
                      if two else C["text"], True, 0))
        specs.append(("master:sub", "SDA GP4 · SCL GP5 · drives SCL" if not compact else "GP4 SDA · GP5 SCL" if not tiny else "GP4/GP5",
                      (cx, cy - sy / 2, RAIL_Z), "below2", C["text2"], False, 1))
        if two:
            mx, my, msx, msy = r["master2"]
            col2 = config.MASTERS[2].get("color", C["text"])
            if not compact:
                specs.append(("master2", "Pico 3 · MASTER 2 (Pico W)", (mx, my + msy / 2, RAIL_Z), "above2",
                              col2, True, 1))
                specs.append(("master2:sub", "GP4 SDA · GP5 SCL · drives SCL", (mx, my + msy / 2, RAIL_Z), "above",
                              C["text2"], False, 0))
            else:                                # one row in small boxes (pins are in the tooltip)
                specs.append(("master2", "Pico 3 · Master 2" if not tiny else "M2 Pico 3",
                              (mx, my + msy / 2, RAIL_Z), "above", col2, True, 1))
        ax, ay, asx, asy = r["analyzer"]
        specs.append(("analyzer", "Pico 2 · PASSIVE ANALYZER (no address, never drives)" if not compact
                      else "Pico 2 · passive analyzer" if not tiny else "Pico 2 analyzer", (ax, ay + asy / 2, RAIL_Z), "above2", C["text"], True, 1))
        specs.append(("analyzer:sub", "taps GP2 SDA · GP3 SCL · GND" if not compact else "GP2 SDA · GP3 SCL" if not tiny else "GP2/GP3 · passive",
                      (ax, ay + asy / 2, RAIL_Z), "above", C["text2"], False, 0))
        px, py, psx, psy = r["pc"]
        specs.append(("pc", "PC · this GUI", (px, py + psy / 2, RAIL_Z), "above", C["text"], True, 0))
        u0, u1 = self.usb_path()
        specs.append(("usb", "USB CDC: analyzer records → PC" if not compact else "USB records → PC" if not tiny else "USB",
                      ((u0[0] + u1[0]) / 2, u0[1], RAIL_Z), "above", LINE_COL["USB"], False, 2))
        c0, c1 = self.controller_usb_path()
        if not compact:
            specs.append(("usb:ctrl", "USB (power / optional telemetry)",
                          (c0[0], (c0[1] + c1[1]) / 2, RAIL_Z), "right", LINE_COL["USB"], False, 3))
        for line, y in RAIL_Y.items():
            text = line if line != "GND" else ("GND (common · unverified)" if not compact else "GND")
            specs.append((f"rail:{line}", text, (self.rail_end, y, RAIL_Z), "right", LINE_COL[line], True, 1))
        for a in self.order:
            d = self.slaves[a]
            tx, ty, tsx, tsy = r[a]
            title = display_name(d) if not compact else short_name(d) if not tiny else short_name(d).split()[0]
            specs.append((a, title, (tx, ty - tsy / 2, RAIL_Z), "below", C["text"], True, 0))
            conf_col = {"Confirmed": C["green"], "Configured": C["cyan"], "Likely": C["amber"]}.get(d["conf"], C["red"])
            sub = (f"0x{a:02X} · {d['conf']} · {d['status']}" if not compact else
                   f"0x{a:02X} · {d['status']}" if not tiny else f"0x{a:02X}")
            specs.append((f"{a}:sub", sub, (tx, ty - tsy / 2, RAIL_Z), "below2", conf_col, False, 1))
        return specs


def _free_spot(r, taken, view_w, view_h, margin):
    """Nearest position for rect r that overlaps nothing in `taken` and stays inside the view."""
    step_y, step_x = r.height() + 1, r.width() / 2 + 2
    for k in range(1, 9):
        for dy in (0, k * step_y, -k * step_y):
            for dx in (0, step_x, -step_x, 2 * step_x, -2 * step_x):
                c = QRectF(r)
                c.translate(dx, dy)
                inside = (c.left() >= margin and c.top() >= margin and c.right() <= view_w - margin
                          and c.bottom() <= view_h - margin)
                if inside and not any(t.intersects(c) for t in taken):
                    return c
    return r


def place_labels(specs, project, sizes, view_w, view_h, margin=3, obstacles=()):
    """Pixel rectangles for every label: priority order, overlap avoidance, clamping.

    project(point3) -> QPointF; sizes: key -> (w, h); obstacles: pixel rects (boards, wires) a label
    must not cover.  Returns key -> QRectF.
    """
    placed = {}
    taken = list(obstacles)
    gap = 4
    place_labels.displaced = set()
    for key, _text, anchor, where, _col, _bold, _prio in sorted(specs, key=lambda s: s[6]):
        w, h = sizes[key]
        p = project(anchor)
        if where == "below":
            r = QRectF(p.x() - w / 2, p.y() + gap, w, h)
        elif where == "below2":
            parent = key.rsplit(":", 1)[0]
            parent = int(parent) if parent.isdigit() else parent
            base = placed.get(parent)
            top = base.bottom() + 1 if base is not None else p.y() + gap + h
            r = QRectF(p.x() - w / 2, top, w, h)
        elif where == "above":
            r = QRectF(p.x() - w / 2, p.y() - gap - h, w, h)
        elif where == "above2":
            sub = placed.get(f"{key}:sub")
            bottom = sub.top() - 1 if sub is not None else p.y() - gap - h
            r = QRectF(p.x() - w / 2, bottom - h, w, h)
        elif where == "right":
            r = QRectF(p.x() + gap, p.y() - h / 2, w, h)
        else:
            r = QRectF(p.x() - gap - w, p.y() - h / 2, w, h)
        # resolve overlaps by moving away from the anchor along the placement axis
        for _ in range(6):
            hit = next((t for t in taken if t.intersects(r)), None)
            if hit is None:
                break
            place_labels.displaced.add(key)
            if hit.height() > 3 * r.height() and hit.height() > 4 * hit.width():
                # a tall thin obstacle (a wire column): step sideways round it; moving up/down never clears it
                if abs(r.center().x() - hit.left()) <= abs(hit.right() - r.center().x()):
                    r.moveRight(hit.left() - 1)
                else:
                    r.moveLeft(hit.right() + 1)
            elif where in ("above", "above2"):
                r.moveBottom(hit.top() - 1)
            elif where in ("right", "left"):
                r.moveTop(hit.bottom() + 1)
            else:
                r.moveTop(hit.bottom() + 1)
        # clamp inside the view
        r.moveLeft(min(max(margin, r.left()), max(margin, view_w - margin - w)))
        r.moveTop(min(max(margin, r.top()), max(margin, view_h - margin - h)))
        if any(t.intersects(r) for t in taken):          # clamping can push it back onto a neighbour
            r = _free_spot(r, taken, view_w, view_h, margin)
            place_labels.displaced.add(key)
        placed[key] = r
        taken.append(r)
    return placed


def label_font(compact, pt=None):
    return QFont("Segoe UI", pt if pt else (8 if compact else 10))


def controller_name(txn):
    """'Pico 1 (controller) · Master 1' for an attributed packet, else an explicit 'unattributed'."""
    mid = getattr(txn, "master_id", None) if txn is not None else None
    m = config.MASTERS.get(mid)
    if m is None:
        return "controller (unattributed)"
    return f"{m['pico']} (controller) · {m['label']}"


def _master_short(txn):
    """'Master 1' / 'Master 2' for an attributed packet, 'bus owner unknown' otherwise (never the analyzer)."""
    m = config.MASTERS.get(getattr(txn, "master_id", None) if txn is not None else None)
    return m["label"] if m else "bus owner unknown"


def flow_tag(stage, txn):
    """Short TX / RX tag of one stage from the MASTER's point of view: 'M1 TX' (master 1 sends to the target),
    'M2 RX' (master 2 receives from the target), 'bus TX' when the bus owner is unknown."""
    if stage is None:
        return ""
    mid = getattr(txn, "master_id", None) if txn is not None else None
    who = f"M{mid}" if mid in config.MASTERS else "bus"
    if getattr(stage, "kind", "") == "cond":
        return f"{who} TX"
    return {"W": f"{who} TX", "R": f"{who} RX"}.get(stage.direction or "", "")


def stage_description(stage, txn, names):
    """Plain-language transmitter/receiver text for one decoded protocol stage."""
    if stage is None:
        return "", None, None
    dev = names.get(stage.address, f"0x{stage.address:02X}" if stage.address is not None else "bus")
    kind = getattr(stage, "kind", "")
    ctrl = controller_name(txn)
    mshort = _master_short(txn)
    if kind == "cond" or stage.direction is None:
        return f"{mshort} TX: controller generates bus condition (analyzer only observes)", None, None
    if kind == "ack":
        responder = mshort if stage.direction == "W" else f"target {dev}"
        if stage.ack:
            return f"ACK: SDA LOW at 9th clock (expected from {responder})", stage.direction, ACK_COL
        note = getattr(stage, "note", "") or "SDA HIGH at 9th clock"
        return f"NACK: {note}", stage.direction, NACK_COL       # drawn along the same wire the ACK would have used
    if stage.direction == "W":
        head, tail = (ctrl.split(" · ", 1) + [""])[:2]
        return f"TX {head} → {dev}" + (f" · {tail}" if tail else ""), "W", CT_COL
    return f"RX {dev} → {ctrl}", "R", TC_COL


def box_mesh(sx, sy, sz):
    x, y, z = sx / 2, sy / 2, sz
    v = np.array([[-x, -y, 0], [x, -y, 0], [x, y, 0], [-x, y, 0],
                  [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]], dtype=float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return MeshData(vertexes=v, faces=f)


DEFAULT_CAMERA = dict(elevation=62, azimuth=-90, fov=60)


# ============================================================================ 3D
if HAVE_GL:
    class _GLView(gl.GLViewWidget):
        hovered = pyqtSignal(object, QPointF)
        clicked = pyqtSignal(object)
        resized = pyqtSignal()

        def __init__(self, owner):
            super().__init__()
            self.owner = owner
            self._press = None
            self.user_view = False          # True once the user rotates/zooms
            self.setMouseTracking(True)

        _fail_streak = 0
        _capability_checked = False

        def _gl_capable(self):
            """A context can report OpenGL >= 2.1 yet still have no real shader
            pipeline (e.g. a software/basic-display fallback) — the version string
            alone is not enough.  Probe the actual functions pyqtgraph's 3D items
            need, with the context already current (Qt made it current for us)."""
            try:
                from OpenGL import GL
                return bool(GL.glCreateShader) and bool(GL.glBlendFuncSeparate)
            except Exception:
                return False

        def _request_fallback(self, reason):
            """Tearing down this widget (reparenting/deleting it) must never happen while
            Qt's C++ side is still inside OUR paintGL() for it — that reentrancy crashes
            the process. Defer the actual swap to the next event-loop tick."""
            if getattr(self, "_fallback_requested", False):
                return
            self._fallback_requested = True
            owner = self.owner
            QTimer.singleShot(0, lambda: owner._gl_unavailable(reason))

        def paintGL(self):
            if not self._capability_checked:
                self._capability_checked = True
                if not self._gl_capable():
                    self._request_fallback("this GPU/driver has no OpenGL shader support "
                                           "(glCreateShader unavailable) — 3D parts cannot draw")
                    return
            if getattr(self, "_fallback_requested", False):
                return                        # fallback already scheduled; do not paint further
            try:
                super().paintGL()
                self._fail_streak = 0
            except Exception as exc:          # OpenGL.error.GLError on a context not ready yet
                self._fail_streak += 1
                if not getattr(self, "_gl_error_logged", False):
                    import logging
                    logging.getLogger(__name__).warning("OpenGL paint skipped: %s", str(exc).splitlines()[0])
                    self._gl_error_logged = True
                if self._fail_streak >= 5:    # transient once is tolerated; persistent means give up cleanly
                    self._request_fallback(f"OpenGL repeatedly failed to paint ({str(exc).splitlines()[0]})")
                    return
            self.owner._position_labels()

        def project(self, p):
            viewport = (0, 0, self.width(), self.height())
            m = self.projectionMatrix(viewport, viewport) * self.viewMatrix()
            v = m.map(QVector3D(*p))
            return QPointF((v.x() + 1) / 2 * self.width(), (1 - v.y()) / 2 * self.height())

        def pick(self, pos):
            best, bd = None, 46.0
            for key, p in self.owner.anchor_points().items():
                sp = self.project(p)
                d = math.hypot(sp.x() - pos.x(), sp.y() - pos.y())
                if d < bd:
                    best, bd = key, d
            return best

        def resizeEvent(self, ev):
            super().resizeEvent(ev)
            self.resized.emit()

        def mousePressEvent(self, ev):
            self._press = ev.position()
            super().mousePressEvent(ev)

        def mouseReleaseEvent(self, ev):
            super().mouseReleaseEvent(ev)
            if self._press is not None and (ev.position() - self._press).manhattanLength() < 5:
                self.clicked.emit(self.pick(ev.position()))
            self._press = None

        def mouseMoveEvent(self, ev):
            if ev.buttons():
                self.user_view = True
            super().mouseMoveEvent(ev)
            if not ev.buttons():
                self.hovered.emit(self.pick(ev.position()), ev.globalPosition())

        def wheelEvent(self, ev):
            """Zoom only with Ctrl + wheel: a plain wheel turn (scrolling the page, a touchpad brushed while hovering)
            must never change the fitted view by accident.  Ctrl + wheel changes the camera distance (not the field of
            view, which is what pyqtgraph does with Ctrl)."""
            if not (ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
                ev.ignore()
                return
            delta = ev.angleDelta().y() or ev.angleDelta().x()
            if delta:
                self.user_view = True
                self.opts["distance"] *= 0.999 ** delta
                self.update()
            ev.accept()


class Network3D(QWidget):
    node_clicked = pyqtSignal(object)      # address int, "master", "analyzer", "pc" or None
    renderer_changed = pyqtSignal()        # emitted once if a 3D view falls back to 2D at runtime

    def __init__(self, prefer_3d=True, compact=False, parent=None):
        super().__init__(parent)
        self._playback_paused = False
        self.playback_speed = 1.0
        self._stage_key = None
        self._stage = None
        self._stage_txn = None
        self.compact = compact
        self.model = NodeModel()
        self.particles: list = []
        self.particles_arrived = 0          # dots that reached 100 % of their wire (diagnostics / verification)
        self.particles_cut = 0              # dots removed before arriving (must stay 0 within a packet)
        self._travel_s = 0.35               # seconds a stage dot needs for its wire (from the playback pace)
        self._stage_pkt = None              # id of the packet whose stages are being shown
        self._stage_idx = None
        self._tick_wall = None
        self.flash: dict = {}
        self.flash_color: dict = {}         # key -> colour of the current flash (default cyan)
        self.owner = None                   # scene key of the master owning the current packet
        self.auto_rotate = False
        self.active = True
        self.selected = None
        self.focus = None                   # device chosen by the user (filter); amber ring
        self._focus_items: list = []
        self._last_spawn = {}
        self._labels: dict = {}             # key -> QLabel (scene labels)
        self._label_rects: dict = {}
        self._stage_items: list = []
        self.use_gl = HAVE_GL and prefer_3d
        self.label_level = 1 if compact else 0
        self.model.update(self._configured_nodes([]))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3 if compact else 6)

        hud_font = "font-size:8pt;" if compact else ""
        from gui.phase_panel import PhasePanel
        self.phase_panel = PhasePanel(compact)          # '[Master 2 (Pico 3) → OLED 0x3C · WRITE] Phase 3/6: Address ACK'
        lay.addWidget(self.phase_panel)
        self.capture_clock = QLabel('Capture time: UNKNOWN')
        self.capture_clock.setWordWrap(True)
        self.capture_clock.setObjectName('mono')
        self.capture_clock.setStyleSheet('color:' + C['text2'] + ';' + hud_font)
        self.rtc_label = QLabel("DS3231 RTC: UNAVAILABLE — no valid RTC record")
        self.rtc_label.setWordWrap(True)
        self.rtc_label.setStyleSheet(f"color:{C['text2']};" + hud_font)
        self.transaction_overlay = QLabel("IDLE — no decoded transaction")
        self.transaction_overlay.setWordWrap(True)
        self.transaction_overlay.setStyleSheet(hud_font)
        self.health_label = QLabel("Bus health: UNKNOWN")
        self.health_label.setWordWrap(True)
        self.health_label.setStyleSheet(f"color:{C['text2']};" + hud_font)
        # HUD: two one-line summary rows so the 3D canvas gets the height.  The verbose texts are still
        # produced (below, hidden) and shown in full as the rows' tooltips.
        for lb in (self.capture_clock, self.rtc_label, self.transaction_overlay, self.health_label):
            lb.setParent(self)
            lb.hide()
        self.hud_row1, self.hud_row2 = HudRow(), HudRow(mono=True)     # row 2 = clocks / counters: fixed-pitch digits
        for lb in (self.hud_row1, self.hud_row2):          # long text is elided with "…"; the tooltip has everything
            lb.setStyleSheet(hud_font)
            lay.addWidget(lb)
        self.hud_row1.setText("IDLE — no decoded transaction")
        if not compact:
            # the phase banner above replaces row 1 on the full-size view; it stays hidden for good (showing / hiding it
            # per stage resized the OpenGL view and re-fitted the whole scene: ~0.8 s stalls that slowed every dot)
            self.hud_row1.hide()
        self._rtc_short = self._health_short = ""
        self.run_state = "running"                        # running | draining | stopped (MainWindow Stop / resume)
        self._run_txn = None
        self._arb_note = None                             # (text, until) shown in row 1 after a lost arbitration
        self._last_arb_spawn = 0.0

        self._lay = lay
        self._view_row = lay.count()        # index self.view occupies, for a later swap
        self._fallback_reason = None
        self._gl_fallback_done = False
        if self.use_gl:
            self.view = _GLView(self)
            self.view.setBackgroundColor(C["bg"])
            self.view.hovered.connect(self._hover)
            self.view.clicked.connect(self.node_clicked.emit)
            self.view.resized.connect(self._auto_fit)
            self.view.setMinimumHeight(170 if compact else VIEW_MIN_H)
            lay.addWidget(self.view, 1)
            self._items: dict = {}
            self._dynamic: list = []
            self._build_static()
            self.reset_view()
        else:
            self.view = Topology2D(self)
            self.view.clicked.connect(self.node_clicked.emit)
            self.view.setMinimumHeight(170 if compact else VIEW_MIN_H)
            lay.addWidget(self.view, 1)

        legend = FlowLayout(hspace=8 if compact else 14, vspace=2)       # wraps to a second row when narrow
        self.btn_reset = None
        if compact:                       # Reset view on the LEFT, first in the control row (always easy to reach)
            self.btn_reset = QPushButton("Reset view")
            self.btn_reset.setToolTip("Restore the startup view of this diagram (camera, zoom, labels)")
            self.btn_reset.clicked.connect(self.reset_view)
            legend.addWidget(self.btn_reset)
        self._legend_items = []
        for txt, _col in self._legend_entries():
            lb = QLabel()
            lb.setObjectName("cardHint")
            if compact:
                lb.setStyleSheet("font-size:8pt;")
            legend.addWidget(lb)
            self._legend_items.append((txt, lb))
        self._refresh_legend()
        lay.addLayout(legend)
        note = ("Arrows show which party transmits the current bit/byte (C = controller, T = target); the path "
                "starts at the master the packet is attributed to (controller telemetry: Master 1 or Master 2); an "
                "unattributed packet is drawn on the shared bus only. The analyzer (Pico 2) never transmits to a target. Amber flashes = an arbitration loss reported by a master (retried, not a NACK). "
                "SDA is a bidirectional open-drain wire; the passive analyzer only observes.")
        # (the explanatory note is the widget tooltip: the 3D page needs every pixel of height for the stage bar)
        self.setToolTip(note)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    @staticmethod
    def _legend_entries():
        m1, m2 = config.MASTERS.get(1, {}), config.MASTERS.get(2, {})
        return (("SDA", C["sda"]), ("SCL", C["scl"]), ("GND", LINE_COL["GND"]), ("USB", LINE_COL["USB"]),
                ("M1 C→T", m1.get("color", CT_COL)), ("M1 T→C", m1.get("color_read", TC_COL)),
                ("M2 C→T", m2.get("color", CT_COL)), ("M2 T→C", m2.get("color_read", TC_COL)),
                ("ACK", ACK_COL), ("NACK", NACK_COL), ("ARB lost", ARB_COL), ("unattributed (bus)", UNATTR_COL),
                ("analyzer observes", OBS_COL))

    def _refresh_legend(self):
        for (txt, col), (_, lb) in zip(self._legend_entries(), self._legend_items):
            lb.setText(f"<span style='color:{col}'>━━</span> {txt}")

    def retheme(self):
        """Theme switch (called by gui.theme.retheme_widgets): clear colour of the 3D view + rebuilt scene / 2D layers."""
        self._refresh_legend()
        self.phase_panel.retheme()
        for q in self.particles:                    # dots in flight follow the new palette (they keep flying: never cut)
            q.color = remap_text(q.color)
        if self.use_gl:
            self.view.setBackgroundColor(C["bg"])
            self._rebuild_dynamic()                 # wires / outlines read the (already reloaded) module palette
        else:
            self.view._fit_cache = None
            self.view._layer_cache = None
            self.view._stage_path_key = None
            if self._stage is not None:
                self._set_stage(self._stage, self._stage_txn)
            self.view.update()                      # Topology2D layer cache is keyed by theme_serial()
        # (the stage key is kept: _rebuild_dynamic / _set_stage above already re-applied the stage, and resetting it made the
        #  next show_stage() drop the dots in flight and respawn the stage's dot from the start)

    # ------------------------------------------------------------------ API
    @property
    def is_3d(self):
        return self.use_gl

    def set_active(self, on: bool):
        on = bool(on)
        if on == self.active:
            return
        self.active = on
        self._tick_wall = None
        if on:
            self._stage_key = None          # restored: the next show_stage() re-shows the current stage and spawns its dot
        else:
            self._drop_particles()          # nothing ticks an inactive view: its dots would freeze and burst out on restore

    @property
    def playback_paused(self):
        return self._playback_paused

    @playback_paused.setter
    def playback_paused(self, on):
        on = bool(on)
        if on == self._playback_paused:
            return
        self._playback_paused = on
        self._tick_wall = None
        if not on:
            self._stage_key = None          # resumed: the stage shown while paused gets its dot (none was spawned frozen)

    def set_run_state(self, state, txn_id=None):
        """Lifecycle shown in the HUD: 'draining' = Stop pressed, the current packet finishes; 'stopped' = frozen."""
        self.run_state, self._run_txn = state, txn_id

    def _run_banner(self):
        if self.run_state == "draining":
            return (f"<span style='color:{C['amber']}'><b>■ STOPPING</b> — finishing packet #{self._run_txn} "
                    f"(ADDR · DATA · ACK/NACK), no new packets</span>")
        if self.run_state == "stopped":
            return (f"<span style='color:{C['red']}'><b>■ STOPPED</b></span> — last transaction completed · "
                    f"Connect all / Start capture resumes with fresh data")
        return ""

    def _animating(self):
        """True while dots are ticked (a hidden / minimised view is not: spawning there only piles up frozen dots)."""
        return self.active and self.isVisible()

    def _can_spawn(self):
        """A new stage dot is only worth spawning where something will move it: not hidden, minimised or paused."""
        return self._animating() and not self._playback_paused

    def _can_pulse(self):
        """Arbitration / legacy pulses: not while minimised or paused.  (A view that is merely hidden may still queue one;
        showEvent() drops whatever is left, so it can never resume as a ghost.)"""
        return self.active and not self._playback_paused

    def hideEvent(self, e):
        self._drop_particles()              # a tab switch: frozen dots must not resume as ghosts of an old stage
        self._tick_wall = None
        super().hideEvent(e)

    def showEvent(self, e):
        self._drop_particles()              # anything queued while hidden (arbitration pulses) must not resume as a ghost
        self._tick_wall = None              # no catch-up jump for the time spent hidden
        self._stage_key = None              # the next show_stage() re-shows the current stage and spawns its dot
        super().showEvent(e)

    def set_auto_rotate(self, on: bool):
        self.auto_rotate = on
        if on and self.use_gl:
            self.view.user_view = True

    def _gl_unavailable(self, reason: str):
        """Runtime fallback: this session's OpenGL context cannot actually draw the
        3D scene (missing shader support, or paint kept failing). Swap to the same
        data drawn by the already-working 2D renderer instead of leaving a blank
        box, and say why, once, so this is never a silent/unexplained black screen."""
        if self._gl_fallback_done:
            return
        self._gl_fallback_done = True
        import logging
        logging.getLogger(__name__).warning("Falling back to 2D topology view: %s", reason)
        old_view = self.view
        self.timer.stop()                   # stop driving GL-only animation state
        self._lay.removeWidget(old_view)
        old_view.setParent(None)
        old_view.deleteLater()
        for lb in list(self._labels.values()):
            lb.setParent(None)
            lb.deleteLater()
        self._labels.clear()
        self._label_rects.clear()
        # _stage_label was a child of the old GL view too (destroyed with it);
        # dropping the reference makes _set_stage treat it as "no label widget",
        # exactly like a Network3D that was 2D from construction.
        self._stage_label = None
        self.particles.clear()
        self._stage_key = None              # the 2D view re-shows the stage on display (and its dot) on the next refresh
        self._dynamic = []
        self._items = {}
        self._boards = {}
        self._focus_items = []
        self._stage_items = []
        self.use_gl = False
        self.view = Topology2D(self)
        self.view.clicked.connect(self.node_clicked.emit)
        self.view.setMinimumHeight(170 if self.compact else 240)
        self._lay.insertWidget(self._view_row, self.view, 1)
        # Drawn ON the canvas by Topology2D.paintEvent (like every other label there),
        # never as a layout row — it must not be able to shrink the diagram's space.
        self._fallback_reason = reason
        self.view.setToolTip(f"Switched to 2D: {reason}. Try updating your graphics driver, or avoid Remote "
                             "Desktop, then restart the app.")
        self.timer.start(33)
        self.view.update()
        self.renderer_changed.emit()

    @staticmethod
    def _configured_nodes(devices):
        """Configured targets stay drawn (as 'Not yet observed') until traffic proves them."""
        seen = {d[0] for d in devices}
        extra = [(a, n, "Configured", "Not yet observed") for a, n in config.CONFIGURED_DEVICES.items()
                 if a not in seen]
        return list(devices) + extra

    def set_selected(self, key):
        if key == self.selected:
            return
        self.selected = key
        if self.use_gl:
            for k, outline in getattr(self, "_boards", {}).items():
                outline.setData(color=rgba(C["cyan"], 0.9 if k == key else 0.0))
        else:
            self.view.update()

    def set_focus(self, addr):
        """Persistent highlight of the device the user filtered on (independent of playback)."""
        self.focus = addr if isinstance(addr, int) else None
        if self.use_gl:
            self._draw_focus()
        else:
            self.view.update()

    def _draw_focus(self):
        for it in self._focus_items:
            self.view.removeItem(it)
        self._focus_items = []
        r = self.model.rects().get(self.focus)
        if r is None:
            return
        x, y, sx, sy = r
        hx, hy, z = sx / 2 + 0.28, sy / 2 + 0.28, 0.3
        ring = np.array([[x-hx, y-hy, z], [x+hx, y-hy, z], [x+hx, y+hy, z], [x-hx, y+hy, z], [x-hx, y-hy, z]])
        self._line(ring, C["amber"], 3.5, 1.0, dynamic=False, store=self._focus_items)

    def set_devices(self, devices):
        changed = self.model.update(self._configured_nodes(devices))
        if changed == "layout":
            if self.use_gl:
                self._rebuild_dynamic()
                self._auto_fit()
        elif changed == "text" and self.use_gl:
            # only a caption changed ('Configured' -> 'Confirmed' / 'Active'): refresh the captions in place - the full
            # rebuild + refit (~1-2 s on the GUI thread) stalled the animation in the middle of a transfer
            self._label_widgets(self.view)
            self._position_labels()
        if not self.use_gl:
            self.view.update()

    def _drop_particles(self):
        """Remove every dot (a different packet, a new capture, a re-attributed stage); count the ones cut short."""
        self.particles_cut += sum(1 for q in self.particles if not q.done)
        self.particles.clear()
        self._blank_dot_layers()

    def _blank_dot_layers(self):
        """3D: the scatter layers keep the last uploaded dots until _tick() feeds them again - and _tick() does not run for a
        hidden / inactive / paused view - so a dropped dot must be wiped from the GPU buffers at once, not left as a ghost."""
        if not self.use_gl:
            return
        for name in ("scatter_halo", "scatter_trail", "scatter", "scatter_spark"):
            item = getattr(self, name, None)
            if item is not None:
                item.setData(pos=np.zeros((1, 3)), size=0.0)

    def _spawn_stage_particle(self, stage, txn):
        """One dot on SDA for this stage along its true direction (sender -> receiver), full wire, in its own colour:
        START / Repeated START / STOP: controller -> target · ADDR / R/W / DATA write: controller -> target ·
        DATA read: target -> controller · ACK and NACK: from whoever is expected to answer back to the sender."""
        path, col, _nack = self._stage_paths(stage)
        addr = stage.address if stage.address is not None else getattr(txn, "address", None)
        ends = self._stage_endpoints(stage, txn)
        if addr is not None:
            self.flash[addr] = 1.0
        if ends is not None and ends[0] is not None:            # the sending board lights up as the dot leaves it ...
            self.flash[ends[0]] = 1.0
            self.flash_color[ends[0]] = col or C["cyan"]
        if getattr(stage, "kind", "") == "rw":
            # the R/W bit is the last bit of the ADDR byte whose dot just crossed: no second, overlapping dot - only the
            # badge ('ADDR/WRITE W=0 ▶') and both boards pulse, so the direction of the address stays unmistakable
            if ends is not None and ends[1] is not None:
                self.flash[ends[1]] = 1.0
                self.flash_color[ends[1]] = col or C["cyan"]
            return
        if path is None or not MOTION["animations"] or not self._can_spawn():
            return                      # (a hidden / paused view is never ticked: its dots would pile up and burst out on show)
        arb = (getattr(txn, "presentation", None) or {}).get("arbitration")
        if arb and getattr(stage, "kind", "") == "cond" and stage.text == "START" and addr in self.model.slaves:
            self._presentation_arbitration(arb, addr)
        src, dst = ends if ends is not None else (None, None)
        self._make_room()
        mark = ("ack" if stage.ack else "nack") if getattr(stage, "kind", "") == "ack" else None
        self.particles.append(Particle(path, col or C["cyan"], travel_s=self._travel_s, src=src, dst=dst, mark=mark))

    def _presentation_arbitration(self, arb, addr):
        """Phase 1 of a contended transfer: BOTH masters START; the loser's amber pulse stops short of the target (it
        stops driving the bus when it reads back a 0 it did not send) and its board flashes amber; the winner goes on."""
        loser = M_KEY.get(arb.get("loser"))
        if loser in self.model.rects():
            self.flash[loser] = 1.0
            self.flash_color[loser] = ARB_COL
        self._arb_note = (f"ARBITRATION: {config.MASTERS.get(arb.get('loser'), {}).get('label', 'M?')} lost to "
                          f"{config.MASTERS.get(arb.get('winner'), {}).get('label', 'M?')} — waits for STOP (not a NACK)",
                          time.monotonic() + 6.0)
        path = self.model.path_to(addr, "SDA", master=arb.get("loser"))
        self._make_room()
        self.particles.append(Particle(self._truncate_path(path, 0.45), ARB_COL, travel_s=self._travel_s,
                                       src=loser, partial=True))

    def finish_dots(self):
        """A manual phase step: every dot still travelling completes at once (it reaches its receiver, which lights up)
        before the next phase starts, so two phases never animate together.  Not counted as cut."""
        for q in self.particles:
            if not q.done:
                q.s = q.total
                if not q.partial:
                    self.particles_arrived += 1
                if q.dst is not None:
                    self.flash[q.dst] = 1.0
                    self.flash_color[q.dst] = q.color
        self.particles.clear()
        self._blank_dot_layers()

    def _make_room(self):
        """Never starve a new stage's dot: when MAX_PARTICLES are alive the oldest one(s) are retired instead."""
        over = len(self.particles) - MAX_PARTICLES + 1
        if over > 0:
            self.particles_cut += sum(1 for q in self.particles[:over] if not q.done)
            del self.particles[:over]

    def _stage_endpoints(self, stage, txn):
        """(sending key, receiving key) of a stage's dot: the attributed master's board (None when the packet is unattributed -
        a master is never guessed, and the passive analyzer never stands in for it) and the target's address, in the
        direction the bit really flows."""
        addr = stage.address if stage.address is not None else getattr(txn, "address", None)
        if addr not in self.model.slaves:
            return None
        if getattr(stage, "kind", "") == "cond":
            if stage.text not in ("START", "Repeated START", "STOP"):
                return None
            direction = "W"
        else:
            _, direction, _ = stage_description(stage, txn, self._device_names())
        if direction is None:
            return None
        master = self.model.controller_key(getattr(txn, "master_id", None))     # None: bus owner unknown (never the analyzer)
        return (master, addr) if direction == "W" else (addr, master)

    def pulse(self, addr, rw: str, nack: bool, color=None):
        """Spawn a direction particle on SDA for a decoded stage (no free-running animation)."""
        import time as _t
        now = _t.monotonic()
        last = self._last_spawn.get(addr, 0)
        if addr not in self.model.slaves or not MOTION["animations"] or (now - last < 0.35 and not nack):
            self.flash[addr] = 1.0
            return
        self._last_spawn[addr] = now
        col = color or (NACK_COL if nack else (TC_COL if rw == "R" else CT_COL))
        speed = 0.9 if MOTION["reduced"] else 0.55
        if not nack and self._can_pulse():
            path = self.model.path_to(addr, "SDA", master=M_ID.get(self.owner))
            if rw == "R":
                path = path[::-1]
            self._make_room()
            self.particles.append(Particle(path, col, speed,
                                           travel_s=1.0 / (speed * 2.2 * max(self.playback_speed, 0.05))))
        self.flash[addr] = 1.0
        self._flash_observer()
        self._flash_owner()

    def _flash_observer(self, level=0.6):
        """The passive analyzer only OBSERVES: a dim, neutral outline - never the colour of a transmitter."""
        self.flash["analyzer"] = max(self.flash.get("analyzer", 0), level)
        self.flash_color["analyzer"] = OBS_COL

    def _flash_owner(self, level=1.0):
        if self.owner is not None:
            self.flash[self.owner] = level
            self.flash_color.pop(self.owner, None)
        # unattributed: NO board is highlighted as the transmitter (the analyzer is not one)

    def show_contention(self, events):
        """Flash the master that LOST arbitration (amber) and the winner's board, if known."""
        now = time.monotonic()
        for ev in events:
            loser = M_KEY.get(ev.master_id)
            if loser in self.model.rects():
                self.flash[loser] = 1.0
                self.flash_color[loser] = ARB_COL
            win = M_KEY.get(getattr(ev.winner, "master_id", None))
            if win in self.model.rects():
                self.flash[win] = max(self.flash.get(win, 0), 0.8)
                self.flash_color.pop(win, None)
            names = {1: "M1", 2: "M2"}
            other = 2 if ev.master_id == 1 else 1
            self._arb_note = (f"ARBITRATION: {names.get(ev.master_id, '?')} lost to {names.get(other, '?')} at "
                              f"0x{ev.addr:02X}, retried (not a NACK)", now + 3.0)
            # an amber pulse runs from the loser towards the target and stops short: its START never completed
            busy = any(not q.done and not q.partial for q in self.particles)   # one activity on the wire at a time
            if (MOTION["animations"] and ev.addr in self.model.slaves and self._can_pulse() and not busy
                    and now - self._last_arb_spawn > 0.25):
                self._last_arb_spawn = now
                path = self.model.path_to(ev.addr, "SDA", master=ev.master_id)
                self._make_room()
                self.particles.append(Particle(self._truncate_path(path, 0.45), ARB_COL, 0.7,
                                               travel_s=max(MIN_TRAVEL_S, 1.0 / (0.7 * 2.2 * max(self.playback_speed, 0.05))),
                                               partial=True))
        if events and not self.use_gl:
            self.view.update()

    @staticmethod
    def _truncate_path(path, frac):
        pts = [np.asarray(q, float) for q in path]
        lens = [float(np.linalg.norm(b - a)) for a, b in zip(pts, pts[1:])]
        limit, out, acc = frac * sum(lens), [pts[0]], 0.0
        for a, b, L in zip(pts, pts[1:], lens):
            if acc + L >= limit:
                out.append(a + (b - a) * ((limit - acc) / (L or 1)))
                break
            out.append(b)
            acc += L
        return out if len(out) > 1 else pts[:2]

    # ------------------------------------------------------------------ HUD text
    def _refresh_hud(self, engine, txn=None, stage=None, ack_html="", stage_col=None, rw="", owner_short=""):
        """Two compact rows: (1) what is happening on the bus now, (2) clocks / health / masters."""
        now = time.monotonic()
        parts = []
        banner = self._run_banner()
        if banner:
            parts.append(banner)
        if self._arb_note and now < self._arb_note[1]:
            parts.append(f"<span style='color:{ARB_COL}'><b>⚡ {self._arb_note[0]}</b></span>")
        if stage is None:
            parts.append("IDLE — no selected packet")
        else:
            if txn.demo:
                src, scol = "SIMULATED", C["demo"]
            elif txn.source == "replay":
                src, scol = "REPLAY", C["replay"]
            elif engine.measurement_trust()[0]:
                src, scol = "LIVE", C["green"]
            else:
                src, scol = "RECORDED", C["text2"]
            a = stage.address if stage.address is not None else txn.address
            dev = engine.registry.name(a)[0] if a is not None else "—"
            mid = getattr(txn, "master_id", None)
            if mid in config.MASTERS:
                who = (f"<span style='color:{master_color(mid, stage.direction or 'W')}'><b>"
                       f"{config.MASTERS[mid]['label']}</b></span>")
            else:
                who = f"<span style='color:{C['amber']}'>unattributed (bus only)</span>"
            direction = {"W": "C→T", "R": "T→C"}.get(stage.direction or "", "")
            tag = flow_tag(stage, txn)
            if tag and direction:
                direction = f"<b style='color:{stage_col}'>{tag}</b> {direction}"
            # the same concise, arrow-annotated text as the badge: 'ADDR 0x68 W=0 ▶' · 'DATA 0x5A ◀' · 'ACK ◀' · 'NACK (end) ▶'
            # (an ACK / NACK stage already says so: no second '· ACK' behind it)
            what = self._stage_badge_text(stage, txn)
            tail = "" if getattr(stage, "kind", "") == "ack" else ack_html
            parts.append(f"<b style='color:{scol}'>{src}</b> · #{txn.id} · "
                         f"{'0x%02X' % a if a is not None else '—'} {dev} · {rw} · "
                         f"<span style='color:{stage_col}'><b>{what}</b></span>{tail}"
                         f"{' · ' + direction if direction else ''} · {who}")
        self.hud_row1.setText("  ·  ".join(parts))
        clock = ((capture_clock_text(engine).splitlines() or [""])[0].replace("Capture time: ", "t=")
                 .replace("Simulation time: ", "sim t=").replace(" (capture clock)", ""))
        pills = []
        for r in engine.masters.rows():
            col = STATE_COL.get(r["state"], C["text2"])
            pills.append(f"<span style='color:{master_color(r['master_id'])}'>{config.MASTERS[r['master_id']]['label']}"
                         f"</span> <span style='color:{col}'>● {r['state']}</span>")
        pills.append(f"<span style='color:{OBS_COL}'>Pico 2 analyzer ◌ passive tap (observes, never transmits)</span>")
        self.hud_row2.setText("  ·  ".join(x for x in (clock, self._rtc_short, self._health_short, "  ".join(pills)) if x))
        tip = "<br>".join(x for x in (self.transaction_overlay.text(), self.capture_clock.text().replace("\n", "<br>"),
                                      self.rtc_label.text(), self.health_label.text()) if x)
        self.hud_row1.setToolTip(tip)
        self.hud_row2.setToolTip(tip)

    def _device_names(self):
        return {a: f"{short_name(d)} 0x{a:02X}" for a, d in self.model.slaves.items()}

    def show_stage(self, playback, engine):
        self.capture_clock.setText(capture_clock_text(engine))
        self._show_rtc(engine)
        self._show_health(engine)
        stage = playback.current
        self.playback_speed = playback.speed
        idx = playback.stage_index
        # A dot crosses its wire in TRAVEL_FRACTION of the stage's real duration (stage_seconds is per 1x; speed scales it)
        wall = getattr(playback, "stage_wall_seconds", None)
        self._travel_s = max(MIN_TRAVEL_S, TRAVEL_FRACTION * (wall(idx) if wall is not None else
                                                             playback.stage_seconds() / max(playback.speed, 0.05)))
        held = bool(getattr(playback, "held", False))
        note = self._arb_note[0] if (self._arb_note and time.monotonic() < self._arb_note[1]) else ""
        self.phase_panel.show_playback(playback if stage is not None else None, self.run_state, held, note)

        if stage is None:
            self.transaction_overlay.setText('IDLE / UNKNOWN — no selected capture')
            self._refresh_hud(engine)
            self._drop_particles()
            self._stage_key = self._stage_pkt = self._stage_idx = None
            self._set_stage(None, None)
            return
        txn = playback.txn
        arb = (getattr(txn, "presentation", None) or {}).get("arbitration")
        if arb and idx == 0:                          # the losing master stays outlined amber for the whole phase 1
            loser = M_KEY.get(arb.get("loser"))
            if loser in self.model.rects():
                self.flash[loser] = max(self.flash.get(loser, 0.0), 0.95)
                self.flash_color[loser] = ARB_COL
        source = ('SIMULATED — NOT VERIFIED HARDWARE (demo)' if txn.demo else
                  'REPLAY — recorded capture' if txn.source == 'replay' else
                  'LIVE — HARDWARE CAPTURE' if engine.measurement_trust()[0] else 'RECORDED — live link disconnected/unverified')
        mode = (f"PINNED packet #{txn.id} — press “Follow latest” to resume live" if playback.pinned
                else "FOLLOWING live traffic")
        source = f"{source} · {mode}"
        from analysis.multi_master import attribution_detail
        self.owner = self.model.controller_key(getattr(txn, "master_id", None))
        owner_txt = f"bus owner: {attribution_detail(txn, engine.masters)}"
        who, _, col = stage_description(stage, txn, self._device_names())
        mid = getattr(txn, "master_id", None)
        if col in (CT_COL, TC_COL):               # data/address stage: colour = the master that drives the bus
            col = master_color(self.model.resolve_master(mid), stage.direction)
        stamp = (f'event t={format_capture_ns(stage.timestamp)}' if stage.timestamp is not None
                 else 'byte timestamp unavailable (segment record only)')
        levels = 'SCL/SDA levels unavailable'
        if stage.timestamp is not None and engine.edges and engine.edges[0][0] <= stage.timestamp <= engine.edges[-1][0]:
            ts, states = engine.edges_between(stage.timestamp, stage.timestamp)
            observed = [int(v) for tt, v in zip(ts, states) if tt <= stage.timestamp]
            if observed:
                levels = f"sampled SCL={(observed[-1]>>1)&1} SDA={observed[-1]&1}"
        col = col or C['cyan']
        if stage.ack is True:
            col = ACK_COL
        elif stage.ack is False:
            col = NACK_COL
        a = stage.address if stage.address is not None else txn.address
        if a is not None:
            nm, conf = engine.registry.name(a)
            target = nm if conf in ("Confirmed", "Configured", "Likely") else "unknown"
            addr_txt = f"0x{a:02X} (7-bit) · target {target}"
        else:
            addr_txt = "address — · target unknown"
        rw = {"R": "READ", "W": "WRITE"}.get(stage.frame_rw or "", txn.operation)
        hz = txn.timing.get("frequency_hz")
        scl_text = (f"SCL measured {hz / 1e3:.2f} kHz ({txn.timing.get('frequency_scope', 'scope unknown')}, "
                    f"{txn.timing.get('frequency_source', '')})" if hz else "SCL measured UNKNOWN")
        byte = f"0x{stage.byte:02X}" if stage.byte is not None else "—"
        ack = ""
        if stage.ack is True:
            ack = f" · <span style='color:{ACK_COL}'><b>ACK</b></span>"
        elif stage.ack is False:
            ack = f" · <span style='color:{NACK_COL}'><b>NACK</b></span>"
        self.transaction_overlay.setText(
            f"<b>{source}</b><br>Packet #{txn.id} · {addr_txt} · {rw} · byte {byte}{ack} · "
            f"stage {idx+1}/{len(playback.events)}: "
            f"<span style='color:{col}'><b>{stage.text}</b></span> · {who}<br>"
            f"{stamp} · {levels} · {scl_text} · roles inferred from protocol<br>{owner_txt}")
        self._refresh_hud(engine, txn, stage, ack, col, rw)
        key = (id(txn), idx, getattr(txn, "master_id", None))
        if key != self._stage_key:
            # Dots are NOT cleared between stages, nor when the next packet starts: each one runs its full wire to the
            # receiver, even if the next stage / packet has already begun (the STOP dot of a packet is still flying when
            # playback moves on; on a busy GUI thread it can take seconds).  They are dropped only for a new capture
            # (stage None, above) or when the widget is hidden (nothing ticks the dots there, they would pile up).
            prev = self._stage_key
            # Controller telemetry attributes a packet while it is already playing: the stage's path / badge move to the
            # master's wire at once, but the dot already in flight FINISHES (dropping it and spawning another made a dot
            # vanish halfway and reappear elsewhere - seen on the first packet after connecting).
            attribution_only = prev is not None and prev[:2] == key[:2] and prev[2] != key[2]
            if not self._animating() or (not attribution_only and id(txn) == self._stage_pkt and idx == self._stage_idx):
                self._drop_particles()
            self._stage_key, self._stage_pkt, self._stage_idx = key, id(txn), idx
            self.set_selected(stage.address)
            self._flash_owner()
            self._flash_observer()
            self._set_stage(stage, txn)
            # The dot already flying keeps going (above), but it was spawned on the OLD path: the analyzer-tap stub of an
            # unattributed packet (only the tap -> target part of the wire) or the other master's wire.  A packet that is
            # now attributed to a master (None -> M, M1 -> M2) also gets its stage dot on THAT master's full wire.
            if (not attribution_only or key[2] is not None) and (self.run_state != "stopped" or playback.pinned):
                self._spawn_stage_particle(stage, txn)

    def _show_rtc(self, engine):
        r = engine.rtc_status() if hasattr(engine, "rtc_status") else dict(state="UNAVAILABLE", text="—", source="")
        self._rtc_short = (f"RTC <span style='color:{C['amber']}'>unavailable</span>" if r["state"] == "UNAVAILABLE" else
                           f"RTC {r['text']} [{r['state']}]")
        if r["state"] == "UNAVAILABLE":
            self.rtc_label.setText(("RTC: <span style='color:%s'>UNAVAILABLE</span> — no valid record" if self.compact else
                                    "DS3231 RTC: <span style='color:%s'>UNAVAILABLE</span> — no valid bus-decoded "
                                    "RTC record or master telemetry (PC clock is never shown as RTC)") % C["amber"])
            return
        age = f" · {r['age_s']:.1f} s old" if r.get("age_s") is not None else ""
        at = f" · read at t={format_capture_ns(r['t_ns'])}" if r.get("t_ns") is not None else ""
        col = C["red"] if r["state"] == "INVALID" else C["amber"] if r["state"] == "STALE" else (C["demo"] if "SIMULATED" in r["source"] else C["green"])
        if self.compact:
            src = "SIMULATED demo record" if "SIMULATED" in r["source"] else (
                "master telemetry" if "telemetry" in r["source"] else "bus-decoded 0x68")
            self.rtc_label.setText(f"RTC <span style='color:{col}'><b>{r['text']}</b> [{r['state']}]</span> · {src}{age}")
            self.rtc_label.setToolTip(f"{r['source']}{at}{age}")
            return
        self.rtc_label.setText(f"DS3231 RTC <span style='color:{col}'><b>{r['text']}</b> [{r['state']}]</span>"
                               f" · {r['source']}{at}{age}")

    def _show_health(self, engine):
        if not hasattr(engine, "health_summary"):
            return
        h = engine.health_summary()
        c = h["counts"]
        n = c["opportunities"]
        rates = (f"raw {c['raw_nack_pct']:.2f}% · unexpected {c['unexpected_nack_pct']:.2f}%" if n else "rates —")
        col = {"HEALTHY": C["green"], "DEGRADED": C["red"]}.get(h["state"], C["amber"])
        sim = (f" · <span style='color:{C['demo']}'>SIMULATED policy: {h['simulated_state']} (not verified)</span>"
               if h["simulated"] else "")
        mm = h.get("multi_master") or {}
        self._health_short = ((f"<span style='color:{C['demo']}'><b>SIMULATED</b></span> · " if h["simulated"] else "")
                              + f"Health <span style='color:{col}'><b>{h['state']}</b></span> · ACK {c['acks']} · NACK "
                              f"{c['raw_nacks']} (unexp <span style='color:{C['red'] if c['unexpected_nacks'] else C['text2']}'>"
                              f"{c['unexpected_nacks']}</span>)"
                              + (f" · <span style='color:{ARB_COL}'>arb lost {mm.get('arb_lost_60s', 0)}/60 s (not NACKs)</span>"
                                 if mm else ""))
        arb = (f" · <span style='color:{ARB_COL}'>arbitration 60 s: lost {mm.get('arb_lost_60s', 0)}, gave up "
               f"{mm.get('arb_gave_up_60s', 0)} (not NACKs)</span>") if mm else ""
        if self.compact:
            self.health_label.setText(
                f"Health <span style='color:{col}'><b>{h['state']}</b></span>"
                + (f" <span style='color:{C['demo']}'>(SIMULATED: {h['simulated_state']})</span>" if h["simulated"] else "")
                + f" · 60 s · ACK {c['acks']} · NACK {c['raw_nacks']} (end-of-read {c['expected_read_nacks']}, "
                f"unexpected <span style='color:{C['red'] if c['unexpected_nacks'] else C['text2']}'>{c['unexpected_nacks']}</span>)"
                f" · {rates}{arb}")
            return
        self.health_label.setText(
            f"Bus health <span style='color:{col}'><b>{h['state']}</b></span>{sim} · {h['window']}: "
            f"slots {n} · ACK {c['acks']} · NACK raw {c['raw_nacks']} (end-of-read {c['expected_read_nacks']}, "
            f"unexpected <span style='color:{C['red'] if c['unexpected_nacks'] else C['text2']}'>{c['unexpected_nacks']}</span>) · {rates}{arb}")

    def anchor_points(self):
        r = self.model.rects()
        return {k: (v[0], v[1], 0.6) for k, v in r.items()}

    def describe(self, key, engine=None):
        if key in ("master", "master2"):
            mid = M_ID[key]
            m = config.MASTERS.get(mid, {})
            role = ("owns the SSD1306 OLED, reads RTC + BMP280 (200 ms / 1 s)" if mid == 1 else
                    "reads RTC + BMP280 on a staggered schedule (330 ms / 1.1 s, +97 ms); never drives the OLED")
            text = (f"<b>{m.get('pico', 'Pico')} · {m.get('label', key)} ({m.get('board', '')})</b><br>"
                    f"I²C controller (RP2040, i2c0) · 100 kHz configured · SDA GP4 · SCL GP5 (firmware I2C.c, "
                    f"MASTER_ID {mid})<br>{role}<br>Multi-master: waits for bus idle, retries after arbitration "
                    f"loss with back-off, recovers the bus only if a line is stuck LOW with no clock<br>"
                    f"USB {m.get('port_hint', '')} (usual): telemetry, arbitration reports, SCL/TIME commands")
            if engine is not None:
                r = next((x for x in engine.masters.rows() if x["master_id"] == mid), None)
                if r is not None:
                    st = r["stats"]
                    text += (f"<br><br><b>Status: {r['state']}</b> — {r['detail']}<br>Port {r['port']} · "
                             f"{'connected' if r['connected'] else 'not connected'} · alignment: {r['clock']}")
                    if r["have_stats"]:
                        text += (f"<br>Transfers ok {st['txn_ok']:,} · address NACK {st['addr_nack']} · data NACK "
                                 f"{st['data_nack']} · arbitration lost {st['arb_lost']} (retries {st['retries']}, "
                                 f"gave up {st['gave_up']}) · bus-busy timeouts {st['busy_timeouts']} · timeouts "
                                 f"{st['timeouts']} · recoveries {st['recoveries']}")
                    text += f"<br>Bus packets attributed to it: {r['bus_packets']:,}"
            return text
        if key == "analyzer":
            return (f"<b>{ANALYZER_LABEL}</b><br>Passive tap: PIO inputs GP2 (SDA) / GP3 (SCL), pulls disabled<br>"
                    "Never drives SDA/SCL · no I²C address · never ACKs<br>"
                    "Sees the wired-AND bus only: it cannot tell which master drove a packet and never sees an "
                    "arbitration loser<br>"
                    "Sends JSON-lines records over USB CDC to the PC<br>Common GND with the bus required (physically unverified)")
        if key == "pc":
            return "<b>PC</b><br>Runs this GUI · receives analyzer records over USB CDC (COM port chosen at runtime)"
        s = self.model.slaves.get(key)
        if not s:
            return ""
        header = HEADER_PINS.get(s["kind"], "")
        extra = "<br>Module also carries an AT24C32 EEPROM (not accessed by firmware)" if s["kind"] == "rtc" else ""
        return (f"<b>{display_name(s)}</b><br>Target address 0x{key:02X} · name {s['conf'].lower()}<br>"
                f"Status: {s['status']}<br>{header}<br>VCC 3.3 V from ONE source (Pico 1 3V3) — never join two Picos' 3V3 "
                f"outputs; wiring NOT verified{extra}"
                "<br><i>click to filter packets</i>")

    def _hover(self, key, gpos):
        if key is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(gpos.toPoint(), self.describe(key), self)

    # ------------------------------------------------------------------ labels (shared)
    def _label_widgets(self, parent):
        specs = self.model.label_specs(self.compact, self.label_level)
        wanted = {s[0] for s in specs}
        for k in list(self._labels):
            if k not in wanted:
                gone = self._labels.pop(k)
                gone.hide()                  # deleteLater() only runs when control returns to the event loop: until then
                gone.deleteLater()           # an untracked caption stayed on screen (seen at (0,0) of the 3D view)
        font = label_font(self.compact)
        for key, text, _, _, col, bold, _ in specs:
            lb = self._labels.get(key)
            if lb is None:
                lb = QLabel(parent)
                lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                self._labels[key] = lb
            f = QFont(font)
            f.setBold(bold)
            lb.setFont(f)
            lb.setText(text)
            lb.setStyleSheet(f"color:{col}; background: {overlay_css(225)}; padding:0px 3px; "
                             f"font-size:{f.pointSize()}pt; font-weight:{'bold' if bold else 'normal'};")
            lb.adjustSize()
            lb.show()
        if self._stage_label is None:
            self._stage_label = QLabel(parent)
            self._stage_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._stage_label.hide()
        return specs

    _stage_label = None
    label_level = 0
    stage_text = ""
    stage_color = None

    def _sizes(self, specs):
        return {s[0]: (self._labels[s[0]].width(), self._labels[s[0]].height()) for s in specs}

    def _position_labels(self):
        if not self._labels:
            return
        specs = self.model.label_specs(self.compact, self.label_level)
        if any(s[0] not in self._labels for s in specs):
            specs = self._label_widgets(self.view)
        rects = place_labels(specs, self.view.project, self._sizes(specs), self.view.width(), self.view.height())
        self._label_rects = rects
        for k, r in rects.items():
            self._labels[k].move(round(r.left()), round(r.top()))
        self._position_stage_label()

    def place_badge(self, w, h, project, vw, vh, label_rects):
        """Slot for the packet badge that touches the fewest bus lines, boards and labels.

        A grid of slots over the whole view is scored (sampled wire points and boards underneath it, labels
        weighted heavily; edges and corners preferred on ties).  The choice is cached per (size, labels) so
        animation frames do not recompute it."""
        w_text = w
        w = -(-w // 48) * 48          # score a slot at least as wide as the text, in 48 px steps: the scoring loop costs
        #                               ~30 ms, and every stage has a slightly different text width (it ran on EVERY stage)
        key = (w, h, vw, vh, tuple((round(r.x()), round(r.y())) for r in label_rects), self.model.usb2_lane)
        slots = getattr(self, "_badge_slots", None)
        if slots is None:
            slots = self._badge_slots = {}
        hit = slots.get(key)
        if hit is not None:
            return QRectF(hit.x(), hit.y(), w_text, h)
        pad = 3
        pts = []
        for _n, seg, _c, _d in self.model.segments():
            for a, b in zip(seg, seg[1:]):
                pa, pb = project(a), project(b)
                n = max(2, int(math.hypot(pb.x() - pa.x(), pb.y() - pa.y()) / 5))
                pts += [(pa.x() + (pb.x() - pa.x()) * i / n, pa.y() + (pb.y() - pa.y()) * i / n) for i in range(n + 1)]
        P = np.asarray(pts, float)
        boards = []
        for cx, cy, sx, sy in self.model.rects().values():
            q = [project((cx + dx * sx / 2, cy + dy * sy / 2, RAIL_Z)) for dx in (-1, 1) for dy in (-1, 1)]
            boards.append(QRectF(QPointF(min(v.x() for v in q), min(v.y() for v in q)),
                                 QPointF(max(v.x() for v in q), max(v.y() for v in q))))
        xs = sorted({pad, vw - w - pad, (vw - w) // 2} | set(range(pad, max(pad + 1, vw - w - pad), max(24, w // 4))))
        ys = sorted({pad, vh - h - pad} | set(range(pad, max(pad + 1, vh - h - pad), max(18, h + 2))))
        best, best_score = QRectF(pad, pad, w, h), None
        for y in ys:
            for x in xs:
                r = QRectF(x, y, w, h)
                rr = r.adjusted(-3, -3, 3, 3)
                lines = int(np.count_nonzero((P[:, 0] >= rr.left()) & (P[:, 0] <= rr.right())
                                             & (P[:, 1] >= rr.top()) & (P[:, 1] <= rr.bottom())))
                score = (lines + 6 * sum(1 for bd in boards if bd.intersects(rr))
                         + 60 * sum(1 for lb in label_rects if lb.intersects(rr))
                         + 0.002 * (min(x, vw - w - x) + min(y, vh - h - y)))        # tie-break: towards the edges
                if best_score is None or score < best_score:
                    best, best_score = r, score
        if len(slots) > 48:
            slots.clear()
        slots[key] = QRectF(best)                 # one placement per (width bucket, size, labels): every stage reuses one
        return QRectF(best.x(), best.y(), w_text, h)

    def _position_stage_label(self):
        lb = self._stage_label
        if lb is None or not lb.isVisible() or self._stage is None:
            return
        w, h = lb.width(), lb.height()
        # a corner/edge slot clear of bus lines, boards and labels (it used to sit on the SDA rail)
        r = self.place_badge(w, h, self.view.project, self.view.width(), self.view.height(),
                             list(self._label_rects.values()))
        lb.move(round(r.left()), round(r.top()))

    # ------------------------------------------------------------------ fitting
    def _scene_fits(self):
        """True when every board corner, line end and label rect lies inside the view."""
        w, h = self.view.width(), self.view.height()
        m = 4
        for p in self.model.bounds_points():
            q = self.view.project(p)
            if not (m <= q.x() <= w - m and m <= q.y() <= h - m):
                return False
        specs = self.model.label_specs(self.compact, self.label_level)
        sizes = self._sizes(specs)
        rects = place_labels(specs, self.view.project, sizes, 10 ** 6, 10 ** 6, margin=-10 ** 6)
        return all(r.left() >= 4 and r.top() >= 4 and r.right() <= w - 4 and r.bottom() <= h - 4
                   for r in rects.values())

    def _labels_overlap(self):
        specs = self.model.label_specs(self.compact, self.label_level)
        rects = list(place_labels(specs, self.view.project, self._sizes(specs), 10 ** 6, 10 ** 6,
                                  margin=-10 ** 6).values())
        displaced = set(place_labels.displaced)
        return bool(displaced) or any(a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:])

    def _content_center_px(self):
        xs, ys = [], []
        for p in self.model.bounds_points():
            q = self.view.project(p)
            xs.append(q.x()); ys.append(q.y())
        specs = self.model.label_specs(self.compact, self.label_level)
        rects = place_labels(specs, self.view.project, self._sizes(specs), 10 ** 6, 10 ** 6, margin=-10 ** 6)
        for r in rects.values():
            xs += [r.left(), r.right()]; ys += [r.top(), r.bottom()]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def fit_view(self):
        """Fit the whole topology and its labels; shorten label text in small boxes."""
        if not self.use_gl:
            self.view.update()
            return
        v = self.view
        if v.width() < 50 or v.height() < 50:
            return
        start = 1 if self.compact else 0
        for level in range(start, 3):
            self.label_level = level
            self._fit_at_level()
            if self._scene_fits() and not self._labels_overlap():
                break
        v.update()

    def _points_fit(self, margin=4):
        w, h = self.view.width(), self.view.height()
        for p in self.model.bounds_points():
            q = self.view.project(p)
            if not (margin <= q.x() <= w - margin and margin <= q.y() <= h - margin):
                return False
        return True

    def _recenter(self, span_x, span_y):
        """Shift the camera centre so the projected content (boards + labels) is centred."""
        v = self.view
        c = v.opts["center"]
        cx, cy = c.x(), c.y()
        px, py = self._content_center_px()
        dx, dy = px - v.width() / 2, py - v.height() / 2
        a = v.project((cx, cy, 0)); b = v.project((cx + 1, cy, 0)); d = v.project((cx, cy + 1, 0))
        ux = max(1e-3, abs(b.x() - a.x())); uy = max(1e-3, abs(d.y() - a.y()))
        cx += max(-span_x / 4, min(span_x / 4, dx / ux))
        cy -= max(-span_y / 4, min(span_y / 4, dy / uy))
        v.opts["center"] = QVector3D(cx, cy, 0)

    def _fit_at_level(self):
        v = self.view
        self._label_widgets(v)
        pts = np.array(self.model.bounds_points())
        span_x = pts[:, 0].max() - pts[:, 0].min()
        span_y = pts[:, 1].max() - pts[:, 1].min()
        v.opts["center"] = QVector3D((pts[:, 0].min() + pts[:, 0].max()) / 2,
                                     (pts[:, 1].min() + pts[:, 1].max()) / 2, 0)
        v.opts["elevation"] = DEFAULT_CAMERA["elevation"]
        v.opts["azimuth"] = DEFAULT_CAMERA["azimuth"]
        # 1) largest view where all boards and wires fit (monotonic in distance)
        lo, hi = 2.0, 2000.0
        for _ in range(40):
            mid = (lo + hi) / 2
            v.opts["distance"] = mid
            if self._points_fit():
                hi = mid
            else:
                lo = mid
        v.opts["distance"] = hi
        # 2) back off gradually until the pixel-sized labels fit too, re-centring each step
        for _ in range(80):
            self._recenter(span_x, span_y)
            if self._scene_fits():
                break
            v.opts["distance"] *= 1.04
        # 3) tighten again while everything still fits
        for _ in range(30):
            d = v.opts["distance"]
            v.opts["distance"] = d * 0.985
            self._recenter(span_x, span_y)
            if not self._scene_fits():
                v.opts["distance"] = d
                self._recenter(span_x, span_y)
                break
        if not self._scene_fits():
            v.opts["distance"] *= 1.02

    _initial_camera = None          # the startup view, saved on the first fit while the diagram is on screen

    def _save_initial_camera(self):
        v = self.view
        if self._initial_camera is not None or not v.isVisible() or v.width() < 200 or v.height() < 150:
            return
        c = v.opts["center"]
        self._initial_camera = dict(center=QVector3D(c.x(), c.y(), c.z()), distance=v.opts["distance"],
                                    elevation=v.opts["elevation"], azimuth=v.opts["azimuth"],
                                    fov=v.opts.get("fov", DEFAULT_CAMERA["fov"]), label_level=self.label_level,
                                    size=(v.width(), v.height()))

    def _apply_baseline(self):
        """The startup view: the saved startup camera when the view has its startup size (and the scene still fits
        it), else the same deterministic fit the startup used."""
        v = self.view
        init = self._initial_camera
        if init is not None and init["size"] == (v.width(), v.height()):
            c = init["center"]
            v.opts.update(center=QVector3D(c.x(), c.y(), c.z()), distance=init["distance"],
                          elevation=init["elevation"], azimuth=init["azimuth"], fov=init["fov"])
            self.label_level = init["label_level"]
            self._label_widgets(v)
            if self._scene_fits():
                v.update()
                return
        v.opts["fov"] = DEFAULT_CAMERA["fov"]
        self.fit_view()
        self._save_initial_camera()

    def reset_view(self):
        """Restore the startup view of THIS diagram (compact and large views are independent): camera centre,
        distance, elevation 62 / azimuth -90, fov 60, label level, label and badge positions; auto-rotate off."""
        self.auto_rotate = False
        if self.use_gl:
            self.view.user_view = False
            self._apply_baseline()
            self._badge_slots = {}
            self._position_labels()
            self._position_stage_label()
        else:
            self.view._fit_cache = None
            self.view.update()

    def _auto_fit(self):
        if self.use_gl and not self.view.user_view:
            self._apply_baseline()

    # ------------------------------------------------------------------ 3D scene
    def _add(self, item, dynamic=False, store=None):
        self.view.addItem(item)
        if dynamic:
            self._dynamic.append(item)
        if store is not None:
            store.append(item)
        return item

    def _board(self, kind, x, y, highlight=False, dynamic=True, rot90=False):
        sx, sy = board_size(kind, rot90)
        image = hardware_image(kind, rot90)
        if not image.isNull():
            scale = sx / image.width()
            texture = gl.GLImageItem(hardware_texture(kind, rot90), smooth=True, glOptions="translucent")
            texture.translate(x - sx / 2, y - sy / 2, 0.22)
            texture.scale(scale, scale, 1)
            self._add(texture, dynamic)
        else:
            # Unknown discovered hardware is deliberately not given a fabricated photo.
            board = gl.GLMeshItem(meshdata=box_mesh(sx, sy, 0.16), color=rgba(KIND_STYLE[kind][1]), shader="shaded")
            board.translate(x, y, 0.05)
            self._add(board, dynamic)
        return self._outline(x, y, sx, sy, highlight)

    def _outline(self, x, y, sx, sy, highlight):
        hx, hy, z = sx / 2 + 0.08, sy / 2 + 0.08, 0.24
        outline = gl.GLLinePlotItem(pos=np.array([
            [x-hx, y-hy, z], [x+hx, y-hy, z], [x+hx, y+hy, z], [x-hx, y+hy, z], [x-hx, y-hy, z]]),
            color=rgba(C["cyan"], 0.9 if highlight else 0.0), width=2, antialias=True,
            glOptions="translucent")
        return self._add(outline, True)

    def _line(self, pts, color, width=2.0, alpha=1.0, dynamic=True, mode="line_strip", store=None):
        it = gl.GLLinePlotItem(pos=np.asarray(pts, dtype=float), color=rgba(color, alpha), width=width,
                               antialias=True, mode=mode, glOptions="translucent")
        return self._add(it, dynamic, store)

    @staticmethod
    def _dashes(p0, p1, n=12):
        pts = []
        p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
        for i in range(n):
            if i % 2 == 0:
                pts += [p0 + (p1 - p0) * i / n, p0 + (p1 - p0) * (i + 1) / n]
        return pts

    def _build_static(self):
        # four translucent layers (halo, comet trail, dot, bright centre), drawn in that order (see particle_render_data)
        layers = []
        for _ in range(4):
            item = gl.GLScatterPlotItem(pos=np.zeros((1, 3)), size=0.0, pxMode=True)
            item.setGLOptions("translucent")
            item.setDepthValue(DOT_DEPTH_VALUE)     # drawn after the wires / boards added later (stable sort = add order)
            self._add(item)
            layers.append(item)
        self.scatter_halo, self.scatter_trail, self.scatter, self.scatter_spark = layers
        self._rebuild_dynamic()

    def _pc_items(self):
        px, py = self.model.pc_center()
        sx, sy = PC_SIZE
        body = gl.GLMeshItem(meshdata=box_mesh(sx, sy, 0.12), color=rgba("#1B2740"), shader="shaded")
        body.translate(px, py, 0.05)
        self._add(body, True)
        screen = gl.GLMeshItem(meshdata=box_mesh(sx * 0.82, sy * 0.66, 0.02), color=rgba("#0E3A52"), shader="shaded")
        screen.translate(px, py + 0.1, 0.18)
        self._add(screen, True)
        return self._outline(px, py, sx, sy, self.selected == "pc")

    def _rebuild_dynamic(self):
        for it in self._dynamic:
            self.view.removeItem(it)
        self._dynamic.clear()
        self._boards = {}
        m = self.model
        for name, pts, col, dashed in m.segments():
            if dashed:
                self._line(self._dashes(pts[0], pts[-1], 14), col, 2.4, 1.0, mode="lines")
            else:
                width = 3.2 if (name.startswith("rail") or name.startswith("usb")) else 2.4
                self._line(pts, col, width, 1.0)
        dots = np.array(m.junctions(), dtype=float)
        self._add(gl.GLScatterPlotItem(pos=dots, size=8, color=rgba(C["text"], 0.95), pxMode=True), True)
        self._boards["master"] = self._board("pico", CTRL_X, CTRL_Y, self.selected == "master")
        for a in m.order:
            s = m.slaves[a]
            self._boards[a] = self._board(s["kind"], s["x"], TARGET_Y, self.selected == a)
        self._boards["analyzer"] = self._board("pico", m.analyzer_x(), ANALYZER_Y, self.selected == "analyzer", rot90=True)
        if m.two_masters:
            self._boards["master2"] = self._board("pico", m.master2_x(), ANALYZER_Y, self.selected == "master2",
                                                  rot90=True)
        self._boards["pc"] = self._pc_items()
        self._label_widgets(self.view)
        self._draw_focus()
        if self._stage is not None:
            self._set_stage(self._stage, self._stage_txn)

    # ------------------------------------------------------------------ stage overlay
    def _arrow_points(self, path, n=3, size=0.32):
        """Chevrons along a polyline, pointing in the path direction."""
        path = np.asarray(path, float)
        seg = np.diff(path, axis=0)
        lens = np.sqrt((seg ** 2).sum(axis=1))
        total = lens.sum()
        pts = []
        for k in range(1, n + 1):
            s = total * k / (n + 1)
            for i, L in enumerate(lens):
                if s <= L or i == len(lens) - 1:
                    d = seg[i] / (L or 1)
                    p = path[i] + d * min(s, L)
                    nrm = np.array([-d[1], d[0], 0.0])
                    tip = p + d * size / 2
                    back = p - d * size / 2
                    z = np.array([0, 0, 0.05])
                    pts += [back + nrm * size / 2 + z, tip + z, tip + z, back - nrm * size / 2 + z]
                    break
                s -= L
        return pts

    def _stage_paths(self, stage):
        """(sda_path oriented transmitter->receiver or None, colour, is_nack).

        Every stage with a target on the diagram has a path, a NACK included (red, from the side that should have
        answered); START / Repeated START / STOP are driven by the controller towards the target."""
        if stage is None:
            return None, None, False
        txn = self._stage_txn
        addr = stage.address if stage.address is not None else getattr(txn, "address", None)
        if addr not in self.model.slaves:
            return None, None, False
        _, direction, col = stage_description(stage, txn, self._device_names())
        mid = self.model.resolve_master(getattr(txn, "master_id", None))      # None: unknown owner -> neutral colour
        if getattr(stage, "kind", "") == "cond":
            if stage.text not in ("START", "Repeated START", "STOP"):
                return None, None, False                      # "TRUNCATED - last verified event": nothing was driven
            direction, col = "W", master_color(mid, "W")
        elif col in (CT_COL, TC_COL):
            col = master_color(mid, direction)
        nack = stage.ack is False
        if direction is None:
            return None, col, nack
        path = self.model.path_to(addr, "SDA", master=mid)
        if direction == "R":
            path = path[::-1]
        return path, col, nack

    def _stage_badge_text(self, stage, txn):
        """The badge text on the diagram: what is on the wire and which way it flows (▶ controller → target, ◀ target →
        controller).  Kept short on purpose (the badge must fit between the wires of a small panel); the full 'who sends
        to whom' sentence is in the transaction overlay.

        'ADDR 0x76 W=0 ▶' · 'READ R=1 ▶' · 'DATA 0xF7 ▶' · 'DATA 0x51 ◀' · 'ACK ◀' (target answers) · 'ACK ▶' (controller
        answers read data) · 'NACK (end) ▶' normal end of read · 'NACK (unexp.) ◀' · 'START ▶' · 'STOP ▶'."""
        text = stage.text
        kind = getattr(stage, "kind", "")
        if kind == "addr" and stage.direction == "W" and text.startswith("ADDR ") and stage.frame_rw:
            text += " " + ("R=1" if stage.frame_rw == "R" else "W=0")
        if kind == "ack" and stage.ack is False:
            note = getattr(stage, "note", "") or ""
            text = ("NACK (end)" if note.startswith("controller left SDA HIGH") else
                    "NACK (end?)" if "STOP/Sr not captured" in note else "NACK (unexp.)")
        _, direction, _ = stage_description(stage, txn, self._device_names())
        if kind == "cond" and text in ("START", "Repeated START", "STOP"):
            direction = "W"
        return text + {"W": " ▶", "R": " ◀"}.get(direction or "", "")

    def _set_stage(self, stage, txn):
        self._stage, self._stage_txn = stage, txn
        lb = self._stage_label
        if self.use_gl:
            for it in self._stage_items:
                self.view.removeItem(it)
            self._stage_items = []
        if stage is None:
            self.stage_text, self.stage_color = "", None
            if lb is not None:
                lb.hide()
            if not self.use_gl:
                self.view.update()
            return
        path, col, nack = self._stage_paths(stage)
        if self.use_gl:
            if path is not None:
                self._line(path, col, 8, 0.28, dynamic=False, store=self._stage_items)
                self._line(self._arrow_points(path, n=7, size=0.36), col, 4.0, 1.0, dynamic=False, mode="lines",
                           store=self._stage_items)
            if stage.address in self.model.slaves and getattr(stage, "kind", "") != "cond":
                scl = self.model.path_to(stage.address, "SCL", master=getattr(txn, "master_id", None))
                self._line(self._arrow_points(scl, 2, 0.26), C["scl"], 2.5, 0.9, dynamic=False, mode="lines",
                           store=self._stage_items)
            if stage.ack is True and getattr(stage, "kind", "") == "ack" and stage.address in self.model.slaves:
                x, y = self.model.drop_x(stage.address, "SDA"), RAIL_Y["SDA"]
                ring = [(x + 0.38 * math.cos(a), y + 0.38 * math.sin(a), 0.4) for a in np.linspace(0, 2 * math.pi, 17)]
                self._line(ring, ACK_COL, 4, 1.0, dynamic=False, store=self._stage_items)
            if nack and stage.address in self.model.slaves:
                x, y = self.model.drop_x(stage.address, "SDA"), RAIL_Y["SDA"]
                s = 0.35
                self._line([(x - s, y - s, 0.4), (x + s, y + s, 0.4), (x - s, y + s, 0.4), (x + s, y - s, 0.4)],
                           NACK_COL, 4, 1.0, dynamic=False, mode="lines", store=self._stage_items)
            for tline in ("SDA", "SCL"):            # the analyzer only LISTENS: dashed neutral taps, no arrowheads / dot
                tp = self.model.tap_path(tline)
                self._line(self._dashes(tp[0], tp[-1], 14), OBS_COL, 2.4, 0.9, dynamic=False, mode="lines",
                           store=self._stage_items)
        where = f"0x{stage.address:02X} · " if stage.address is not None else ""
        self.stage_text = f"{where}{self._stage_badge_text(stage, txn)}"
        self.stage_color = col or C["text"]
        if lb is not None:
            colour = self.stage_color
            lb.setText(self.stage_text)
            f = label_font(self.compact)
            f.setBold(True)
            lb.setFont(f)
            lb.setStyleSheet(f"color:{colour}; background: {overlay_css(200)}; border:1px solid {colour}; padding:0px 3px; "
                             f"font-size:{f.pointSize()}pt; font-weight:bold;")
            lb.adjustSize()
            lb.show()
            if self.use_gl:
                self._position_stage_label()
        if not self.use_gl:
            self.view.update()

    # ------------------------------------------------------------------ animation tick
    def _tick(self, dt=None):
        """Advance the dots by real time (dt in seconds; measured when None).  Every dot owns its travel time (see
        Particle), so it reaches 100 % of its wire whatever the playback speed or the frame rate."""
        if self.playback_paused:
            self._tick_wall = None            # paused: dots freeze, and resuming does not jump them
            return
        if not self.active or not self.isVisible():
            return
        now = time.monotonic()
        if dt is None:
            # A stalled GUI thread must make dots LAG, not teleport: one tick moves a dot at most ~half of its path
            # real elapsed time (a slow frame must not slow the dots down: the next phase waits for them), but a long
            # stall is capped per dot below so a dot never jumps more than STEP_CAP of its wire in one frame
            dt = 0.033 if self._tick_wall is None else min(now - self._tick_wall, 1.0)
        self._tick_wall = now
        moving = False
        if self.particles:
            moving = True
            alive = []
            for p in self.particles:
                p.s += min(dt, STEP_CAP * p.travel_s) * p.total / p.travel_s
                if p.s < p.total:
                    alive.append(p)
                else:
                    p.s = p.total                 # arrived at the receiver: 100 % of the path
                    if not p.partial:             # (an arbitration pulse stops short on purpose: not a delivered bit)
                        self.particles_arrived += 1
                    if p.dst is not None:         # the receiving board lights up as the dot ARRIVES
                        self.flash[p.dst] = 1.0
                        self.flash_color[p.dst] = p.color
            self.particles = alive
        had_flash = bool(self.flash)
        for k in list(self.flash):
            self.flash[k] -= dt * 1.8
            if self.flash[k] <= 0:
                del self.flash[k]
                self.flash_color.pop(k, None)
        if self.use_gl:
            if self.particles:
                data = particle_render_data(self.particles)
                for item, key in ((self.scatter_halo, "halo"), (self.scatter_trail, "trail"), (self.scatter, "core"),
                                  (self.scatter_spark, "spark")):
                    pos_, col_, size_ = data[key]
                    if len(pos_):
                        item.setData(pos=pos_, color=col_, size=size_)
                    else:
                        item.setData(pos=np.zeros((1, 3)), size=0.0)
            elif moving:
                for item in (self.scatter_halo, self.scatter_trail, self.scatter, self.scatter_spark):
                    item.setData(pos=np.zeros((1, 3)), size=0.0)
            for key, outline in getattr(self, "_boards", {}).items():
                f = self.flash.get(key, 0)
                if f > 0 or getattr(outline, "_lit", False):
                    if f > 0:
                        outline.setData(color=rgba(self.flash_color.get(key, C["cyan"]), min(1.0, f)))
                        outline._lit = True
                    else:
                        outline.setData(color=rgba(C["cyan"], 0.9 if self.selected == key else 0.0))
                        outline._lit = False
            if self.auto_rotate and MOTION["animations"] and not MOTION["reduced"]:
                self.view.orbit(0.25, 0)
        else:
            if moving or had_flash:
                self.view.update()


# ============================================================================ 2D fallback
class Topology2D(QWidget):
    """Same schematic layout rendered with QPainter (always fitted)."""
    clicked = pyqtSignal(object)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMouseTracking(True)
        self.setMinimumHeight(170)
        self._rects = {}
        self.label_rects = {}
        self._xf = (1.0, 0.0, 0.0)
        self.font_pt = 8 if owner.compact else 10
        self._fit_cache = None

    # ---- geometry helpers (pixels)
    def _boards_px(self):
        out = {}
        for key, (cx, cy, sx, sy) in self.owner.model.rects().items():
            out[key] = QRectF(self._map(cx - sx / 2, cy + sy / 2), self._map(cx + sx / 2, cy - sy / 2)).normalized()
        return out

    def _wires_px(self, with_usb2=True):
        out = []
        for name, pts, _c, _d in self.owner.model.segments():
            if name == "usb:master2" and not with_usb2:
                continue
            for a, b in zip(pts, pts[1:]):
                pa, pb = self._map(*a[:2]), self._map(*b[:2])
                out.append(QRectF(QPointF(min(pa.x(), pb.x()), min(pa.y(), pb.y())),
                                  QPointF(max(pa.x(), pb.x()), max(pa.y(), pb.y()))).adjusted(-3, -3, 3, 3))
        return out

    def _usb2_columns_px(self):
        """The USB riser and descent as full-height pixel columns (captions must stay out of them)."""
        m = self.owner.model
        if not m.two_masters:
            return []
        rx, lx = m.usb2_columns()
        row_y = self._map(0, ANALYZER_Y).y()
        pc_y = self._map(0, m.pc_center()[1]).y()
        return [QRectF(QPointF(self._map(rx, 0).x() - 4, -1e4), QPointF(self._map(rx, 0).x() + 4, row_y)),
                QRectF(QPointF(self._map(lx, 0).x() - 4, -1e4), QPointF(self._map(lx, 0).x() + 4, pc_y))]

    def _scene_rects(self):
        """Boards + every wire as drawn (used for the fit bounding box)."""
        return list(self._boards_px().values()) + self._wires_px(True)

    def _obstacles(self, with_usb2=True):
        return list(self._boards_px().values()) + self._wires_px(with_usb2) + self._usb2_columns_px()

    def _route_usb2(self, rects):
        """Fit the USB lane: 9 px above EVERY caption (the riser/descent columns are fixed geometry and were
        already kept clear of captions by the label placement)."""
        m = self.owner.model
        if not m.two_masters:
            return
        scale, ox, oy = self._xf
        m2 = self._boards_px()["master2"]
        rail_y = self._map(0, RAIL_Y["SDA"]).y()
        above = [r for r in rects.values() if r.bottom() < rail_y - 2]
        lane = min([r.top() for r in above] + [m2.top()]) - 9
        m.usb2_lane = (oy - lane) / scale

    def _layout(self, specs, sizes, w, h):
        """Route the USB line, then place every label against the REAL widget bounds with boards and wires
        as obstacles (this exact result is what paintEvent draws)."""
        m = self.owner.model
        m.usb2_lane = None
        rects0 = place_labels(specs, self.project, sizes, 10 ** 6, 10 ** 6, margin=-10 ** 6,
                              obstacles=self._obstacles(with_usb2=False))
        self._route_usb2(rects0)
        return place_labels(specs, self.project, sizes, w, h, margin=3, obstacles=self._obstacles())

    def _fit_key(self):
        o, m = self.owner, self.owner.model
        return (theme_serial(), self.width(), self.height(), o.compact, m.two_masters, tuple(m.order),
                tuple((a, d["name"], d["status"], d["conf"]) for a, d in sorted(m.slaves.items())))

    def _fit_once(self, level, pt, w, h):
        """One (text level, font size) attempt: largest scale at which everything fits.  Returns a dict
        with quality flags; leaves self._xf / model.usb2_lane at the attempt's result."""
        o = self.owner
        m = o.model
        o.label_level = level
        specs = m.label_specs(o.compact, level)
        pad = max(6, round(pt * 0.8))
        sizes = {}
        for sp in specs:
            f = label_font(o.compact, pt)
            f.setBold(sp[5])
            fm = QFontMetrics(f)
            sizes[sp[0]] = (fm.horizontalAdvance(sp[1]) + pad, fm.height() + 3)
        m.usb2_lane = None
        pts = np.array(m.bounds_points())
        x0, x1, y0, y1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
        scale = min(w / (x1 - x0), h / (y1 - y0))
        res = dict(level=level, pt=pt, specs=specs, sizes=sizes, clean=False, displaced=99, rects={})
        for _ in range(90):
            ox = (w - (x1 - x0) * scale) / 2 - x0 * scale
            oy = (h - (y1 - y0) * scale) / 2 + y1 * scale
            self._xf = (scale, ox, oy)
            rects0 = place_labels(specs, self.project, sizes, 10 ** 6, 10 ** 6, margin=-10 ** 6,
                                  obstacles=self._obstacles(with_usb2=False))
            self._route_usb2(rects0)
            box = [r for r in rects0.values()] + self._scene_rects()
            bx0, bx1 = min(r.left() for r in box), max(r.right() for r in box)
            by0, by1 = min(r.top() for r in box), max(r.bottom() for r in box)
            if bx1 - bx0 <= w - 8 and by1 - by0 <= h - 8:
                ox += (w - bx1 - bx0) / 2                     # centre boards + wires + labels
                oy += (h - by1 - by0) / 2
                self._xf = (scale, ox, oy)
                rects = self._layout(specs, sizes, w, h)
                rl = list(rects.values())
                obs = self._obstacles()
                overlap = (any(a.intersects(b) for i, a in enumerate(rl) for b in rl[i + 1:])
                           or any(r.intersects(o_) for r in rl for o_ in obs)
                           or any(r.left() < 0 or r.top() < 0 or r.right() > w or r.bottom() > h for r in rl))
                if not overlap:
                    res.update(clean=True, displaced=len(place_labels.displaced), rects=rects)
                    return res
                res.update(rects=rects)
            scale *= 0.97
        return res

    def _fit(self):
        """Uniform scale + offset so boards, wires and labels fit the REAL widget without overlapping.

        Every (text level, font size) attempt that fits cleanly is scored: readable font and large boards
        win, labels pushed away from their anchor lose.  The result is cached per widget size / device list;
        paintEvent draws exactly these rectangles.  While the window is being dragged only the previous
        attempt is re-fitted (fast); the full search runs once the size has settled."""
        o = self.owner
        m = o.model
        key = self._fit_key()
        cache = getattr(self, "_fit_cache", None)
        if cache is not None and cache["key"] == key:
            self._xf, self.sizes, self.label_rects, self.font_pt = cache["xf"], cache["sizes"], cache["rects"], cache["pt"]
            m.usb2_lane, o.label_level = cache["route"], cache["level"]
            return cache["specs"]
        w, h = max(1, self.width()), max(1, self.height())
        quick = cache is not None and getattr(self, "_settling", False)
        if quick:
            attempts = [(cache["level"], cache["pt"]), (2, 8)]
        elif o.compact:
            attempts = [(1, 8), (2, 8)]
        else:
            attempts = [(2, 10), (1, 10), (0, 10), (2, 9), (1, 9), (0, 9), (2, 8), (1, 8), (0, 8)]
        attempts = attempts + [(2, 7)] if not quick else attempts      # 8 pt is the floor; 7 pt only if nothing fits
        best = None
        for level, pt in attempts:
            if (level, pt) == (2, 7) and best is not None and best["clean"]:
                break
            res = self._fit_once(level, pt, w, h)
            res["state"] = (self._xf, m.usb2_lane)
            # readable font first, then board size, then labels still on their anchors
            res["score"] = (res["pt"] * 6 + res["state"][0][0] * 0.5 - res["displaced"] * 2
                            + (2 - res["level"]) * 4)       # fuller captions when they fit just as cleanly
            if res["clean"] and (best is None or not best["clean"] or res["score"] > best["score"]):
                best = res
            elif best is None or (not best["clean"] and not res["clean"]):
                best = res                                   # nothing clean yet: keep the latest attempt
            if quick and res["clean"]:
                break
        self._xf, m.usb2_lane = best["state"]
        o.label_level = best["level"]
        self.sizes, self.label_rects, self.font_pt = best["sizes"], best["rects"], best["pt"]
        if not self.label_rects:                             # degenerate size: still place something
            self.label_rects = self._layout(best["specs"], best["sizes"], w, h)
        entry = dict(key=key, xf=self._xf, sizes=self.sizes, rects=self.label_rects, pt=self.font_pt,
                     route=m.usb2_lane, level=best["level"], specs=best["specs"])
        if quick:
            self._quick_cache = entry                        # shown while dragging; full search follows
        else:
            self._fit_cache = entry
        return best["specs"]

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_fit_cache", None) is not None:
            self._settling = True
            if not hasattr(self, "_settle"):
                self._settle = QTimer(self)
                self._settle.setSingleShot(True)
                self._settle.timeout.connect(self._settled)
            self._settle.start(150)

    def _settled(self):
        self._settling = False
        self.update()

    def project(self, p):
        s, ox, oy = self._xf
        return QPointF(ox + p[0] * s, oy - p[1] * s)

    def _map(self, x, y):
        return self.project((x, y, 0))

    # ---- static layers: background + wires + boards, and the label boxes, drawn ONCE per size / device list
    def _layers(self, specs):
        """Pixmaps of everything that does not move.  Repainting wires, scaled board photos and ~25 captions with
        antialiasing cost ~27 ms per frame on a 125 % display (the 33 ms frame budget), so the dots ticked at 12 fps and
        the whole GUI thread starved; a frame now blits two pixmaps and draws only the glows, stage path, dots, badge."""
        o, m = self.owner, self.owner.model
        dpr = self.devicePixelRatioF()
        key = (theme_serial(), self._fit_key(), round(dpr, 2), o.label_level, self.font_pt, id(self.label_rects),
               tuple((k, t, c, bool(b)) for k, t, _a, _b, c, b, _c in specs),
               tuple((a, d["kind"]) for a, d in sorted(m.slaves.items())))
        cache = getattr(self, "_layer_cache", None)
        if cache is not None and cache["key"] == key:
            return cache
        w, h = max(1, self.width()), max(1, self.height())

        def pixmap(transparent):
            pm = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent if transparent else QColor(C["bg"]))
            return pm

        base = pixmap(False)
        p = QPainter(base)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for name, pts, col, dashed in m.segments():
            width = 3.2 if (name.startswith("rail") or name.startswith("usb")) else 2.4
            pen = QPen(QColor(col), width)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap if dashed else Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            if dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            for a, b in zip(pts, pts[1:]):
                p.drawLine(self._map(*a[:2]), self._map(*b[:2]))
        p.setPen(QPen(QColor(C["bg"]), 1.2))
        p.setBrush(QColor(C["text"]))
        for j in m.junctions():
            p.drawEllipse(self._map(*j[:2]), 3.5, 3.5)
        rects = {}
        for k, (cx, cy, sx, sy) in m.rects().items():
            r = QRectF(self._map(cx - sx / 2, cy + sy / 2), self._map(cx + sx / 2, cy - sy / 2)).normalized()
            if k == "pc":
                p.setBrush(QColor("#1B2740")); p.setPen(QPen(QColor(C["border"]), 1))
                p.drawRoundedRect(r, 4, 4)
                p.setBrush(QColor("#0E3A52"))
                p.drawRect(r.adjusted(r.width() * 0.09, r.height() * 0.12, -r.width() * 0.09, -r.height() * 0.3))
            else:
                kind = "pico" if k in ("master", "master2", "analyzer") else m.slaves[k]["kind"]
                image = hardware_image(kind, k in ("analyzer", "master2"))
                if not image.isNull():
                    p.drawImage(r, image)
                else:
                    p.setBrush(QColor(KIND_STYLE[kind][1])); p.setPen(QPen(QColor(C["border"]), 1))
                    p.drawRoundedRect(r, 6, 6)
            rects[k] = r
        p.end()
        labels = pixmap(True)                                 # captions sit ABOVE the dots, as before
        p = QPainter(labels)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for k, text, _a, _b, col, bold, _c in specs:
            f = label_font(o.compact, self.font_pt); f.setBold(bold)
            p.setFont(f)
            r = self.label_rects[k]
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(overlay_q(225)); p.drawRect(r)
            p.setPen(QColor(col))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        self._layer_cache = dict(key=key, base=base, labels=labels, rects=rects)
        return self._layer_cache

    def paintEvent(self, e):
        o = self.owner
        m = o.model
        specs = self._fit()
        layers = self._layers(specs)
        self._rects = layers["rects"]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(0, 0, layers["base"])
        for key, r in self._rects.items():                    # the only per-board work left: glow / selection / focus
            f = o.flash.get(key, 0)
            if o.selected == key or f > 0:
                glow = QColor(o.flash_color.get(key, C["cyan"]) if f > 0 else C["cyan"])
                glow.setAlphaF(max(0.8 if o.selected == key else 0.0, min(1.0, f)))
                p.setPen(QPen(glow, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(-3, -3, 3, 3), 4, 4)
            if o.focus == key:
                p.setPen(QPen(QColor(C["amber"]), 3)); p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(-6, -6, 6, 6), 5, 5)
        # stage overlay: highlighted transmitter->receiver path with arrowheads (the path only changes with the stage)
        stage = o._stage
        sk = (id(stage), id(o._stage_txn), getattr(o._stage_txn, "master_id", None), self._fit_key())
        if getattr(self, "_stage_path_key", None) != sk:
            self._stage_path_key, self._stage_path_val = sk, o._stage_paths(stage)
        path, col, nack = self._stage_path_val
        if path is not None:
            q = [self._map(*pt[:2]) for pt in path]
            c = QColor(col); c.setAlpha(110)
            p.setPen(QPen(c, 8)); p.drawPolyline(QPolygonF(q))
            p.setPen(QPen(QColor(col), 2)); p.setBrush(QColor(col))
            for a, b in zip(q, q[1:]):
                d = QPointF(b.x() - a.x(), b.y() - a.y())
                L = math.hypot(d.x(), d.y()) or 1
                ux, uy = d.x() / L, d.y() / L
                n_chev = max(1, int(L // 60))                   # a chevron every ~60 px: the direction reads at a glance
                for k in range(n_chev):
                    f = (k + 0.5) / n_chev
                    mid = QPointF(a.x() + d.x() * f, a.y() + d.y() * f)
                    p.drawPolygon(QPolygonF([QPointF(mid.x() + ux * 7, mid.y() + uy * 7),
                                             QPointF(mid.x() - ux * 5 - uy * 5, mid.y() - uy * 5 + ux * 5),
                                             QPointF(mid.x() - ux * 5 + uy * 5, mid.y() - uy * 5 - ux * 5)]))
        if (stage is not None and stage.ack is True and getattr(stage, "kind", "") == "ack"
                and stage.address in m.slaves):
            c = self._map(m.drop_x(stage.address, "SDA"), RAIL_Y["SDA"])
            p.setPen(QPen(QColor(ACK_COL), 3)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, 7, 7)
        if nack and stage is not None and stage.address in m.slaves:
            c = self._map(m.drop_x(stage.address, "SDA"), RAIL_Y["SDA"])
            p.setPen(QPen(QColor(NACK_COL), 3))
            p.drawLine(QPointF(c.x() - 6, c.y() - 6), QPointF(c.x() + 6, c.y() + 6))
            p.drawLine(QPointF(c.x() - 6, c.y() + 6), QPointF(c.x() + 6, c.y() - 6))
        for pt in o.particles:
            c = QColor(pt.color)
            p.setPen(Qt.PenStyle.NoPen)
            step = pt.total * TRAIL_STEP
            for k in range(TRAIL_POINTS, 0, -1):                 # comet tail, oldest (faintest, smallest) first
                s = pt.s - k * step
                if s < 0:
                    continue
                fade = 1.0 - k / (TRAIL_POINTS + 1)
                tx, ty, _ = pt.pos_at(s)
                tc = QColor(c); tc.setAlphaF(0.62 * fade)
                p.setBrush(tc); p.drawEllipse(self._map(tx, ty), 2.0 + 3.0 * fade, 2.0 + 3.0 * fade)
            x, y, _ = pt.pos()
            q = self._map(x, y)
            k = 1.35 if pt.mark else 1.0                           # ACK / NACK reply dots are larger
            halo = QColor(c); halo.setAlpha(80)
            p.setBrush(halo); p.drawEllipse(q, 11 * k, 11 * k)
            p.setBrush(c); p.drawEllipse(q, 5 * k, 5 * k)
            spark = QColor(c).lighter(190)
            p.setBrush(spark); p.drawEllipse(q, 2 * k, 2 * k)
            if pt.mark == "nack":                                  # a red cross ON the dot: NACK reads without colour too
                p.setPen(QPen(QColor(C["bg"]), 2))
                p.drawLine(QPointF(q.x() - 4, q.y() - 4), QPointF(q.x() + 4, q.y() + 4))
                p.drawLine(QPointF(q.x() - 4, q.y() + 4), QPointF(q.x() + 4, q.y() - 4))
                p.setPen(Qt.PenStyle.NoPen)
        rects = self.label_rects
        p.drawPixmap(0, 0, layers["labels"])                  # captions over the dots: exactly the rectangles _fit() validated
        self.stage_rect = None
        if stage is not None and o.stage_text:
            text = o.stage_text
            f = label_font(o.compact, self.font_pt); f.setBold(True); p.setFont(f)
            fm = QFontMetrics(f)
            w, h = fm.horizontalAdvance(text) + 10, fm.height() + 4
            r = o.place_badge(w, h, self.project, self.width(), self.height(), list(rects.values()))
            colour = QColor(o.stage_color or C["text"])
            p.setPen(QPen(colour, 1)); p.setBrush(overlay_q(235)); p.drawRect(r)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            self.stage_rect = r
        reason = getattr(o, "_fallback_reason", None)
        if reason:
            # Small corner badge, drawn OVER the finished diagram (costs no layout space,
            # so it can never squeeze the diagram itself). Full reason is the tooltip.
            text = "⚠ 3D→2D (GPU/driver)"
            f = QFont("Segoe UI", 7 if o.compact else 8); f.setBold(True); p.setFont(f)
            fm = QFontMetrics(f)
            w, h = fm.horizontalAdvance(text) + 8, fm.height() + 4
            r = QRectF(self.width() - w - 3, self.height() - h - 3, w, h)
            p.setPen(QPen(QColor(C["amber"]), 1)); p.setBrush(overlay_q(215)); p.drawRect(r)
            p.setPen(QColor(C["amber"]))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    def _at(self, pos):
        for k, r in self._rects.items():
            if r.contains(pos):
                return k
        return None

    def mouseMoveEvent(self, e):
        k = self._at(e.position())
        if k is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(e.globalPosition().toPoint(), self.owner.describe(k), self)

    def mouseReleaseEvent(self, e):
        self.clicked.emit(self._at(e.position()))
