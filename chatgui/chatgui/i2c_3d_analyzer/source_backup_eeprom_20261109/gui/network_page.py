"""3D I²C Network page — large interactive topology with node inspector."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QSplitter, QVBoxLayout, QWidget

from config import COLORS as C
from gui.network_3d import Network3D
from gui.widgets import Card, fmt_time_ns, label


class NetworkPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.selected = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(10)
        main = Card("3D I²C network", "built from observed traffic · drag rotate · scroll zoom · click a node", shadow=False)
        self.net = Network3D(prefer_3d=ctx.settings.value("use_3d", True, bool))
        self.net.node_clicked.connect(self._select)
        main.add(self.net, 1)
        ctl = QHBoxLayout()
        self.rot = QCheckBox("Auto-rotate camera")
        self.rot.setChecked(ctx.settings.value("auto_rotate", False, bool))
        self.rot.toggled.connect(self.net.set_auto_rotate)
        self.net.set_auto_rotate(self.rot.isChecked())
        ctl.addWidget(self.rot)
        reset = QPushButton("Reset view")
        reset.clicked.connect(self._reset)
        ctl.addWidget(reset)
        ctl.addStretch(1)
        self.renderer_lbl = label("", "cardHint")
        self._update_renderer_label()
        self.net.renderer_changed.connect(self._update_renderer_label)
        ctl.addWidget(self.renderer_lbl)
        main.add(ctl)
        split.addWidget(main)

        side = Card("Node inspector")
        self.info = label("Click a node in the scene.", wrap=True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        side.add(self.info, 1)
        self.btn_filter = QPushButton("Show this device's packets")
        self.btn_filter.setObjectName("primary")
        self.btn_filter.setEnabled(False)
        self.btn_filter.clicked.connect(lambda: ctx.focus_device(self.selected, switch=True))
        side.add(self.btn_filter)
        note = label("Names: <b>Confirmed</b> = ID register matched on the bus · <b>Configured</b> = what this bench is "
                     "wired as · <b>Likely</b> = common part at that address · <b>Unknown</b> = not in database. "
                     "An address alone cannot prove which chip is present.", "cardHint", wrap=True)
        side.add(note)
        side.setMinimumWidth(300)
        split.addWidget(side)
        split.setSizes([1100, 340])
        lay.addWidget(split, 1)

    def _update_renderer_label(self):
        if self.net.is_3d:
            mode = "OpenGL 3D"
        elif self.net._gl_fallback_done:
            mode = "2D (fell back from 3D at runtime)"
        else:
            mode = "2D fallback (OpenGL unavailable or disabled)"
        self.renderer_lbl.setText(f"Renderer: {mode}")

    def _reset(self):
        # Fitted default view for THIS (large) diagram; the dashboard box resets independently.
        self.rot.setChecked(False)
        self.net.reset_view()

    def _select(self, key):
        self.selected = key
        self.net.set_selected(key)
        self.btn_filter.setEnabled(isinstance(key, int))
        self.refresh(self.ctx.engine)

    def refresh(self, engine):
        reg = engine.registry
        now = engine.hw_now or 0
        self.net.set_devices(reg.node_list(now))
        k = self.selected
        if isinstance(k, int) and k in reg.devices:
            d = reg.devices[k]
            name, conf = reg.name(k)
            t0 = engine.packets[0].t_start if engine.packets else 0
            rows = [("Address", f"0x{k:02X} ({'10-bit' if d.ten_bit else '7-bit'})"), ("Name", f"{name}"),
                    ("Confidence", conf + (f" — {d.confirm_detail}" if d.confirm_detail else "")),
                    ("Status", reg.status(k, now)), ("Transactions", f"{d.transactions:,}"),
                    ("Reads / writes", f"{d.reads:,} / {d.writes:,}"),
                    ("Bytes read / written", f"{d.bytes_read:,} / {d.bytes_written:,}"),
                    ("ACK / NACK", f"{d.acks:,} / {d.nacks:,}"), ("Address NACKs", str(d.addr_nacks)),
                    ("Rate (5 s)", f"{reg.rate(k, now):.2f} txn/s"),
                    ("First seen", fmt_time_ns(d.first_seen - t0) if d.first_seen is not None else "—"),
                    ("Last seen", fmt_time_ns(d.last_seen - t0) if d.last_seen is not None else "—")]
            self.info.setText("<table cellspacing=6>" + "".join(
                f"<tr><td style='color:{C['text2']}'>{a}</td><td><b>{b}</b></td></tr>" for a, b in rows) + "</table>")
        elif k in ("master", "analyzer"):
            self.info.setText(self.net.describe(k))
        elif not reg.devices:
            self.info.setText("No traffic decoded yet. Slave nodes appear here as soon as their address is seen "
                              "on the bus — including devices that were not configured.")
