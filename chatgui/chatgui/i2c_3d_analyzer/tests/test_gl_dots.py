"""The 3D (use_gl=True) renderer of the movement dots, exercised on the REAL pyqtgraph.opengl item classes and the REAL
Network3D code (no HAVE_GL=False patch).

PyOpenGL is not installed on every host (and the offscreen Qt platform has no GL context to paint with), so when `OpenGL`
cannot be imported a stub package is installed for the duration of this module and removed afterwards: pyqtgraph's item
classes (GLScatterPlotItem, GLLinePlotItem, GLImageItem, GLViewWidget ...) then load unchanged and keep exactly the state the
code hands them (positions, sizes, colours, depth value, add order) - which is what these tests assert.  What they cannot
prove is the rasterised pixels of a GPU frame; the draw-order / depth reasoning is checked against pyqtgraph's own rules
(items sorted by depthValue, stable; 'translucent' = GL_DEPTH_TEST on with the default GL_LESS)."""
import importlib.util
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

from analysis.playback import Playback
from tests.helpers import lines_for, seq_of, synth, write, write_read
from tests.test_hardware_pipeline import qt_app      # noqa: F401  (fixture)

FRAME = 1 / 30


class _FakeOpenGL(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


@pytest.fixture(scope="module")
def glnet(qt_app):
    """A private copy of gui.network_3d imported WITH pyqtgraph.opengl available (HAVE_GL True)."""
    before = set(sys.modules)
    if importlib.util.find_spec("OpenGL") is None:
        for name in ("OpenGL", "OpenGL.GL", "OpenGL.GLU", "OpenGL.GL.shaders", "OpenGL.arrays", "OpenGL.error",
                     "OpenGL.GL.ARB", "OpenGL.GL.ARB.shader_objects"):
            mod = _FakeOpenGL(name)
            mod.__path__ = []
            sys.modules[name] = mod
    path = importlib.util.find_spec("gui.network_3d").origin
    spec = importlib.util.spec_from_file_location("gui.network_3d_gl_copy", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert mod.HAVE_GL, "pyqtgraph.opengl could not be loaded"
    yield mod
    for name in set(sys.modules) - before:
        if name.startswith(("OpenGL", "pyqtgraph.opengl", "gui.network_3d_gl_copy")):
            del sys.modules[name]
    import pyqtgraph
    if hasattr(pyqtgraph, "opengl"):
        delattr(pyqtgraph, "opengl")


def engine_and_packets():
    from analysis.capture_engine import CaptureEngine
    e = CaptureEngine()
    e.set_source("demo")
    e.ingest_lines(lines_for(synth(seq_of(write(0x3C, [0x40] + list(range(16))),
                                          write_read(0x68, 0x00, [1, 2, 3, 4])))))
    for t, mid in zip(e.packets, (1, 2)):
        t.master_id, t.master_method = mid, "telemetry"
    return e


def make_gl_net(glnet, qt_app):
    n = glnet.Network3D(prefer_3d=True)
    n.resize(1100, 600)
    n.show()
    qt_app.processEvents()
    n.timer.stop()
    assert n.use_gl and n.is_3d and type(n.view).__name__ == "_GLView" and n.isVisible()
    return n


def gpu_is_blank(n):
    return all(np.all(np.asarray(getattr(n, name).size) == 0)
               for name in ("scatter_halo", "scatter_trail", "scatter", "scatter_spark"))


def start_dot(glnet, n, e, mid_packet=0, position=1.0):
    pb = Playback()
    pb.select(e.packets[mid_packet], pinned=True)
    pb.position = position
    n.show_stage(pb, e)
    n._tick(0.1)
    assert len(n.particles) == 1 and not gpu_is_blank(n)
    return pb


# ------------------------------------------------------------------ draw order and depth
def test_gl_dot_layers_are_drawn_last_whatever_is_added_afterwards(glnet, qt_app):
    n = make_gl_net(glnet, qt_app)
    e = engine_and_packets()
    layers = [n.scatter_halo, n.scatter_trail, n.scatter, n.scatter_spark]
    assert all(x.depthValue() == glnet.DOT_DEPTH_VALUE for x in layers)

    def drawn(view):                       # exactly pyqtgraph's rule (GLViewWidget.drawItemTree): stable sort by depthValue
        return sorted([i for i in view.items if i.parentItem() is None], key=lambda a: a.depthValue())
    assert drawn(n.view)[-4:] == layers and all(i.depthValue() < glnet.DOT_DEPTH_VALUE for i in drawn(n.view)[:-4])
    # the scatter layers were ADDED first (index 0..3): without the depth value they would be painted under every wire,
    # board photo and stage overlay added afterwards
    assert [n.view.items.index(x) for x in layers] == [0, 1, 2, 3]
    start_dot(glnet, n, e)                                      # a stage overlay (width-8 path, chevrons) is added later
    assert n._stage_items and n.view.items.index(n._stage_items[0]) > 3
    n._rebuild_dynamic()                                        # a theme switch / device discovery rebuilds wires and boards
    n.set_focus(0x68)
    assert drawn(n.view)[-4:] == layers
    n.close()


def test_gl_dot_layers_sit_strictly_above_each_other_and_above_every_wire_and_board(glnet, qt_app):
    n = make_gl_net(glnet, qt_app)
    e = engine_and_packets()
    start_dot(glnet, n, e)
    z = {name: float(np.max(np.asarray(getattr(n, name).pos)[:, 2]))
         for name in ("scatter_halo", "scatter_trail", "scatter", "scatter_spark")}
    # pyqtgraph 'translucent' = depth test ON (GL_LESS) and depth writes: equal depths would reject the core / centre that is
    # drawn after the halo, so each layer is strictly nearer the camera than the one drawn before it
    assert z["scatter_halo"] < z["scatter_trail"] < z["scatter"] < z["scatter_spark"]
    assert z["scatter_halo"] == pytest.approx(glnet.RAIL_Z + 0.1)
    assert min(z.values()) > max(glnet.RAIL_Z, 0.24, 0.3)       # wires / stage overlay (RAIL_Z), outlines, focus ring; photos 0.22
    # all four layers are fed the same dots, in the same order
    assert len(n.scatter_halo.pos) == len(n.scatter.pos) == len(n.scatter_spark.pos) == 1
    assert np.allclose(np.asarray(n.scatter_halo.pos)[:, :2], np.asarray(n.scatter.pos)[:, :2])
    n.close()


# ------------------------------------------------------------------ no stale GPU dots
@pytest.mark.parametrize("trigger", ["hide", "inactive", "no_stage", "drop"])
def test_gl_dropping_the_dots_blanks_the_gpu_buffers_at_once(glnet, qt_app, trigger):
    n = make_gl_net(glnet, qt_app)
    e = engine_and_packets()
    start_dot(glnet, n, e)
    if trigger == "hide":
        n.hide()                                                # a tab switch: _tick() no longer runs for this view
    elif trigger == "inactive":
        n.set_active(False)                                     # window minimised
    elif trigger == "no_stage":
        n.show_stage(Playback(), e)                             # a new capture: no stage any more
    else:
        n._drop_particles()
    assert n.particles == []
    assert gpu_is_blank(n), "frozen ghost dots stay in the GL scatter layers"
    n._tick(FRAME)                                              # (a later tick, if it runs at all, keeps it blank)
    assert gpu_is_blank(n)
    n.close()


def test_gl_ghost_free_across_tab_switches_and_resume(glnet, qt_app):
    n = make_gl_net(glnet, qt_app)
    e = engine_and_packets()
    pb = start_dot(glnet, n, e)
    n.hide()
    pb.position = 5.0
    n.show()
    qt_app.processEvents()
    n.timer.stop()
    assert n.particles == [] and gpu_is_blank(n)                # nothing resumed from before the switch
    n.show_stage(pb, e)                                         # the next refresh: the CURRENT stage, one dot from the start
    n._tick(0.0001)
    assert len(n.particles) == 1 and n.particles[0].s < 0.01 * n.particles[0].total and not gpu_is_blank(n)
    n.playback_paused = True                                    # Pause display: nothing ticks, so nothing new is spawned frozen
    pb.position = 7.0
    n.show_stage(pb, e)
    n.hide(); n.show(); qt_app.processEvents(); n.timer.stop()
    n.show_stage(pb, e)
    assert n.particles == [] and gpu_is_blank(n)
    n.playback_paused = False                                   # Resume: the stage on display gets its dot
    n.show_stage(pb, e)
    assert len(n.particles) == 1
    n.close()


# ------------------------------------------------------------------ theme switch
@pytest.mark.parametrize("renderer", ["gl", "2d"])
def test_theme_switch_dark_to_presentation_and_back_never_cuts_a_dot_in_flight(glnet, qt_app, renderer):
    from unittest.mock import patch

    import gui.network_3d as network
    from gui import theme
    app = qt_app
    theme.apply_theme(app, "dark")
    try:
        if renderer == "gl":
            n = make_gl_net(glnet, qt_app)
        else:
            with patch.object(network, "HAVE_GL", False):
                n = network.Network3D(prefer_3d=False)
            n.resize(1100, 600); n.show(); qt_app.processEvents(); n.timer.stop()
        e = engine_and_packets()
        pb = Playback(); pb.select(e.packets[1], pinned=True); pb.position = 4.0
        n.show_stage(pb, e)
        n._tick(0.1)
        dot, s0, key0 = n.particles[0], n.particles[0].s, n._stage_key
        items0 = len(n.view.items) if renderer == "gl" else None
        calls = []
        orig = n.retheme
        n.retheme = lambda: (calls.append(theme.current_theme()), orig())
        for name in ("presentation", "dark", "presentation"):
            theme.apply_theme(app, name)
            assert calls[-1] == name                                  # the diagram really was re-themed
            n.show_stage(pb, e)                                       # the next refresh after the switch
            assert n.particles == [dot] and n.particles_cut == 0      # the dot in flight is untouched ...
            assert n._stage_key == key0                               # ... and the stage is not restarted
            import config
            assert dot.color.lower() == config.MASTERS[2]["color"].lower()    # ... in the NEW theme's Master 2 write colour
            before = dot.s
            n._tick(FRAME)
            assert dot.s > before                                     # ... and keeps flying (no freeze, no teleport back)
            assert (dot.s - before) / dot.total < 0.2
            if renderer == "gl":
                assert len(n.view.items) == items0                    # the scene was rebuilt, not duplicated
                assert not gpu_is_blank(n)
        for _ in range(60):
            n._tick(FRAME)
        assert dot.done and n.particles == [] and n.particles_cut == 0 and n.particles_arrived == 1
        if renderer == "gl":
            assert gpu_is_blank(n)
        n.close()
    finally:
        theme.apply_theme(app, "dark")


def test_gl_scene_state_after_rebuild_is_complete_on_both_themes(glnet, qt_app):
    from gui import theme
    theme.apply_theme(qt_app, "dark")
    try:
        n = make_gl_net(glnet, qt_app)
        names = {type(i).__name__ for i in n.view.items}
        assert {"GLScatterPlotItem", "GLLinePlotItem"} <= names
        counts = {}
        for name in ("dark", "presentation", "dark"):
            theme.apply_theme(qt_app, name)
            counts[name] = len(n.view.items)
            assert set(n._boards) == {"master", "master2", "analyzer", "pc", 0x68, 0x76, 0x3C}
            assert [n.view.items.index(x) for x in (n.scatter_halo, n.scatter_trail, n.scatter, n.scatter_spark)] == [0, 1, 2, 3]
            assert tuple(n.view.opts["bgcolor"][:3]) == pytest.approx(
                tuple(int(theme.config.COLORS["bg"][i:i + 2], 16) / 255 for i in (1, 3, 5)), abs=0.01)
        assert counts["dark"] == counts["presentation"]
        n.close()
    finally:
        theme.apply_theme(qt_app, "dark")
