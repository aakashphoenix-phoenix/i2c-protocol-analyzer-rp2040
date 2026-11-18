"""Focused tests for theme callback order, wire contrast in Dark/Presentation, and white-wash removal."""
from types import SimpleNamespace

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

import config
from gui import network_3d, theme


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_theme_hex_uniqueness_and_wire_contrast():
    expected_dups = {
        "dark": {("cyan", "m1_read", "sda"), ("blue", "m1"), ("amber", "scl"), ("header_bg", "status_bg")},
        "presentation": {("cyan", "m1_read", "sda"), ("blue", "m1"), ("amber", "scl"), ("header_bg", "status_bg")},
    }
    for name, expected in expected_dups.items():
        preset = config.THEMES[name]
        seen: dict[str, list[str]] = {}
        for k, v in preset.items():
            if isinstance(v, str) and v.startswith("#"):
                seen.setdefault(v.lower(), []).append(k)
        dups = {tuple(sorted(keys)) for keys in seen.values() if len(keys) > 1}
        assert dups == expected, f"{name} unexpected hex aliases: {dups}"

        bg = preset["bg"]
        for tok in ("sda", "scl", "gnd", "usb", "unattr", "m1", "m1_read", "m2", "m2_read", "text", "text2"):
            assert _contrast(preset[tok], bg) >= 4.5, f"{name}.{tok} ({preset[tok]}) too faint on {bg}"

    pres = config.THEMES["presentation"]
    assert _contrast(pres["m2"], pres["m2_read"]) >= 1.3


def test_line_col_updates_before_network3d_retheme_and_updates_legend_and_2d():
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "dark", force=True)
    w = network_3d.Network3D(prefer_3d=False)
    observed: list[tuple[str, dict, str]] = []
    orig_retheme = w.retheme

    def spy_retheme():
        observed.append((theme.current_theme(), dict(network_3d.LINE_COL), network_3d.UNATTR_COL))
        orig_retheme()

    w.retheme = spy_retheme
    try:
        theme.apply_theme(app, "presentation")
        assert observed, "Network3D.retheme() was not called during apply_theme()"
        cur, snap_line_col, snap_unattr = observed[-1]
        pres = config.THEMES["presentation"]
        assert cur == "presentation"
        assert snap_line_col == {"SDA": pres["sda"], "SCL": pres["scl"], "GND": pres["gnd"], "USB": pres["usb"]}
        assert snap_unattr == pres["unattr"]

        seg_cols = {name: col for name, _pts, col, _dashed in w.model.segments()}
        assert seg_cols["rail:SDA"] == pres["sda"]
        assert seg_cols["rail:SCL"] == pres["scl"]
        assert seg_cols["rail:GND"] == pres["gnd"]
        assert seg_cols["usb:analyzer"] == pres["usb"]

        legend_html = {txt: lb.text() for txt, lb in w._legend_items}
        assert pres["sda"] in legend_html["SDA"]
        assert pres["scl"] in legend_html["SCL"]
        assert pres["gnd"] in legend_html["GND"]
        assert pres["usb"] in legend_html["USB"]
        assert pres["m2_read"] in legend_html["M2 T→C"]

        w.resize(640, 360)
        layers = w.view._layers(w.view._fit())
        assert layers["base"] is not None

        theme.apply_theme(app, "dark")
        cur2, snap2, snap2_unattr = observed[-1]
        dark = config.THEMES["dark"]
        assert cur2 == "dark"
        assert snap2 == {"SDA": dark["sda"], "SCL": dark["scl"], "GND": dark["gnd"], "USB": dark["usb"]}
        assert snap2_unattr == dark["unattr"]
    finally:
        w.deleteLater()
        theme.apply_theme(app, "dark", force=True)


def test_no_white_wash_tap_overlay_or_hardcoded_white_junctions(monkeypatch):
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "presentation", force=True)
    w = network_3d.Network3D(prefer_3d=False)
    orig_view = w.view
    try:
        w.model.update([(0x76, "BMP280", "OK", "Confirmed")])
        w.resize(640, 360)
        # Verify 2D Topology2D renders junction markers in theme text color (dark ink), not #FFFFFF
        layers = w.view._layers(w.view._fit())
        j0 = w.model.junctions()[0]
        pt = w.view._map(*j0[:2])
        img = layers["base"].toImage()
        dpr = layers["base"].devicePixelRatioF()
        px_col = QColor(img.pixel(round(pt.x() * dpr), round(pt.y() * dpr)))
        assert px_col.name().lower() == config.THEMES["presentation"]["text"].lower()

        # Verify 3D _rebuild_dynamic() and _set_stage() via lightweight gl stub (no GPU context needed)
        scatter_calls = []

        class _StubItem:
            def __init__(self, **kw):
                self.kw = kw

            def translate(self, *_a):
                pass

            def scale(self, *_a):
                pass

        stub_gl = SimpleNamespace(
            GLLinePlotItem=lambda **kw: _StubItem(**kw),
            GLScatterPlotItem=lambda **kw: scatter_calls.append(kw) or _StubItem(**kw),
            GLMeshItem=lambda **kw: _StubItem(**kw),
            GLImageItem=lambda *a, **kw: _StubItem(**kw),
        )
        monkeypatch.setattr(network_3d, "gl", stub_gl, raising=False)
        monkeypatch.setattr(network_3d, "box_mesh", lambda *_a: None)
        monkeypatch.setattr(network_3d, "hardware_texture", lambda *_a: None)

        class _DummyView:
            def __init__(self):
                self.items = []
                self.bg = None

            def addItem(self, it):
                self.items.append(it)

            def removeItem(self, it):
                if it in self.items:
                    self.items.remove(it)

            def setBackgroundColor(self, c):
                self.bg = c

        w.use_gl = True
        w.view = _DummyView()
        w._dynamic = []
        w._labels = {}
        w._stage_label = None
        w._label_widgets = lambda *_a, **_kw: []

        recorded_lines = []
        orig_line = w._line

        def spy_line(pts, color, width=2.0, alpha=1.0, dynamic=True, mode="line_strip", store=None):
            recorded_lines.append((color, width, alpha, mode))
            return orig_line(pts, color, width, alpha, dynamic=dynamic, mode=mode, store=store)

        w._line = spy_line
        w._rebuild_dynamic()

        # No 0.14 faint halo lines, primary wires use alpha=1.0 and thicker widths >= 2.2
        assert recorded_lines
        assert all(alpha == 1.0 for _c, _w, alpha, _m in recorded_lines)
        assert all(width >= 2.2 for _c, width, _a, _m in recorded_lines)
        assert scatter_calls
        expected_j_col = network_3d.rgba(config.THEMES["presentation"]["text"], 0.95)
        assert scatter_calls[-1]["color"] == expected_j_col

        # Verify tap overlay in _set_stage uses theme color rather than "#FFFFFF"
        recorded_lines.clear()
        stage = SimpleNamespace(address=0x76, kind="addr", text="ADDR 0x76", direction="W", frame_rw="W", ack=True, note="")
        txn = SimpleNamespace(address=0x76, master_id=1)
        w._set_stage(stage, txn)
        assert recorded_lines
        assert all(str(col).upper() != "#FFFFFF" for col, _w, _a, _m in recorded_lines)
        assert any(col == config.COLORS["gray"] for col, _w, _a, _m in recorded_lines)    # dashed neutral analyzer taps
    finally:
        w.use_gl = False
        w.view = orig_view
        w.deleteLater()
        theme.apply_theme(app, "dark", force=True)
