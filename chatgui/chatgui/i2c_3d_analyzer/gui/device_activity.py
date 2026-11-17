"""Device Activity page — 3D topology + per-address statistics; clicking a node filters packets."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QHeaderView, QPushButton, QSplitter, QTableView, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

import pyqtgraph as pg
from analysis.device_activity import ranking_text
from analysis.playback import format_capture_ns
from config import COLORS as C
from analysis.device_registry import DeviceStats
from gui.network_3d import Network3D
from gui.theme import on_change
from gui.widgets import Card, fit_table, label, mono_columns, styled_plot
from models.packet_model import (C_ACK, C_ADDR, C_DATA, C_DUR, C_ID, C_MASTER, C_RW, C_SEMANTIC, C_TS, COLUMNS,
                                  PacketFilter)

# numeric columns (MONO_FONT, right-aligned) / identifier + timestamp columns (MONO_FONT, left) of every table here
TBL_NUM, TBL_MONO = (4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16), (0, 17)
ACT_NUM, ACT_MONO = tuple(range(3, 11)), (0, 11, 12)
MM_NUM, MM_MONO = tuple(range(4, 15)), (2,)
MX_NUM, MX_MONO = tuple(range(3, 10)), (1,)
ARB_NUM, ARB_MONO = (3, 4, 5), (0, 2)

HEAD = ["Address", "Device", "Identity confidence", "Activity", "Reads", "Writes", "Bytes", "ACK", "NACK", "Last seen (s)", "Rate (txn/s)"]
HEAD += ["Health", "Unexpected NACK", "NACK % (eligible bits)", "Successful txns", "Failed txns",
         "Incomplete/unverified", "Last confirmed success", "Latest failure", "Freshness/confidence"]
ACT_HEAD = ["Address", "Device", "Status", "READ frames", "WRITE frames", "READ data bytes", "WRITE data bytes",
            "READ frames/s", "WRITE frames/s", "Bus transactions", "Address-NACK attempts", "Latest READ (capture t)",
            "Latest WRITE (capture t)", "Why"]
ACT_COL = {"ACTIVE": C["green"], "NO OBSERVED ACTIVITY": C["text2"], "PARTIAL": C["amber"], "UNKNOWN": C["amber"]}
MM_HEAD = ["Master", "Board", "COM port", "Bus-access health", "Transfers OK", "Address NACK", "Data NACK",
           "Arbitration lost", "Retries", "Gave up", "Bus-busy waits", "Bus-busy timeouts", "Timeouts",
           "Recoveries", "Bus packets attributed", "Telemetry \u2194 capture", "Why"]
MM_KEYS = {4: "txn_ok", 5: "addr_nack", 6: "data_nack", 7: "arb_lost", 8: "retries", 9: "gave_up",
           10: "busy_waits", 11: "busy_timeouts", 12: "timeouts", 13: "recoveries"}
MM_BAD = {5, 6, 9, 11, 12, 13}          # red when non-zero: target NACKs or lost bus access
MX_HEAD = ["Master", "Target", "Device", "OK (controller)", "Address NACK", "Data NACK", "Arbitration lost",
           "Gave up", "Timeouts", "Bus packets (analyzer)"]
ARB_HEAD = ["Capture t", "Loser", "Target", "Attempt", "Lost at byte", "Back-off", "Outcome", "Winner (bus packet)"]
MM_COL = {"HEALTHY": C["green"], "CONTENDED": C["cyan"], "DEGRADED": C["red"], "UNKNOWN": C["amber"],
          "NOT CONNECTED": C["text2"]}


def _table(head):
    t = QTableWidget(0, len(head))
    t.setHorizontalHeaderLabels(head)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    # every column (the last one too) is as wide as its header / content: a header word is never cut; a narrow
    # table scrolls sideways instead
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.horizontalHeader().setHighlightSections(False)
    return t


def _fill(table, r, vals, tips=None):
    for c, v in enumerate(vals):
        it = table.item(r, c)
        if it is None:
            it = QTableWidgetItem()
            table.setItem(r, c, it)
        it.setText(str(v))
        it.setToolTip((tips or {}).get(c, str(v)))
        it.setData(Qt.ItemDataRole.ForegroundRole, None)      # theme colour unless re-coloured below
    return [table.item(r, c) for c in range(len(vals))]
STATUS_COL = {"Active": C["green"], "Idle": C["text2"], "Not responding": C["red"], "No device (NACK)": C["red"]}


def _palette():
    """Re-read the state colour maps from the live palette after a theme switch."""
    ACT_COL.update({"ACTIVE": C["green"], "NO OBSERVED ACTIVITY": C["text2"], "PARTIAL": C["amber"], "UNKNOWN": C["amber"]})
    MM_COL.update({"HEALTHY": C["green"], "CONTENDED": C["cyan"], "DEGRADED": C["red"], "UNKNOWN": C["amber"],
                   "NOT CONNECTED": C["text2"]})
    STATUS_COL.update({"Active": C["green"], "Idle": C["text2"], "Not responding": C["red"], "No device (NACK)": C["red"]})


on_change(_palette)


class DeviceActivityPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.selected = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        vs = QSplitter(Qt.Orientation.Vertical)
        vs.setHandleWidth(10)
        vs.setChildrenCollapsible(False)        # never squeeze a box below its readable minimum
        # The communication diagram lives on the 3D I2C Network page.  A hidden, non-OpenGL
        # instance is kept only because other code still calls p_dev.net (focus/playback sync).
        self.net = Network3D(prefer_3d=False, compact=True, parent=self)
        self.net.hide()
        ct = Card("Detected addresses", "unknown addresses are listed, never ignored")
        self.tbl = QTableWidget(0, len(HEAD))
        self.tbl.setHorizontalHeaderLabels(HEAD)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setShowGrid(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(False)
        self.tbl.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.tbl.horizontalHeader().setHighlightSections(False)
        mono_columns(self.tbl, TBL_NUM, right=True)
        mono_columns(self.tbl, TBL_MONO)
        self.tbl.cellClicked.connect(lambda r, c: ctx.focus_device(self.tbl.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        ct.add(self.tbl, 1)
        ct.setMinimumHeight(222)
        vs.addWidget(ct)
        # ---- Controllers: two masters share the bus (controller telemetry + analyzer attribution)
        cm = Card("Controllers (multi-master)",
                  "per-master counts come from each master's own USB telemetry \u00b7 arbitration losses are "
                  "retried by the loser and are NEVER counted as NACKs \u00b7 bus packets = analyzer packets "
                  "attributed to that master")
        self.mm_banner = label("", "cardHint", wrap=True)
        cm.add(self.mm_banner)
        self.mm = _table(MM_HEAD)
        self.mm.setMinimumHeight(118)          # header + both masters without scrolling
        mono_columns(self.mm, MM_NUM, right=True)
        mono_columns(self.mm, MM_MONO)
        cm.add(self.mm)
        mrow = QSplitter(Qt.Orientation.Horizontal)
        self.mx = _table(MX_HEAD)
        mono_columns(self.mx, MX_NUM, right=True)
        mono_columns(self.mx, MX_MONO)
        self.mx.cellClicked.connect(lambda r, c: ctx.focus_device(self.mx.item(r, 1).data(Qt.ItemDataRole.UserRole)))
        mrow.addWidget(self.mx)
        self.arb = _table(ARB_HEAD)
        mono_columns(self.arb, ARB_NUM, right=True)
        mono_columns(self.arb, ARB_MONO)
        self.arb.cellClicked.connect(self._arb_clicked)
        arb_box = QWidget()
        ab = QVBoxLayout(arb_box)
        ab.setContentsMargins(0, 0, 0, 0)
        self.btn_vis_arb = QPushButton("⚡ Visualize Arbitration")
        self.btn_vis_arb.setToolTip("Open the winning bus packet of the selected arbitration row (the newest one when none "
                                    "is selected) in the Digital Waveform, the collision marked in amber above the traces")
        self.btn_vis_arb.clicked.connect(self._visualize_arb)
        ab.addWidget(self.btn_vis_arb)
        ab.addWidget(self.arb, 1)
        mrow.addWidget(arb_box)
        mrow.setChildrenCollapsible(False)
        mrow.setSizes([760, 660])
        self.mx.setMinimumHeight(150)
        cm.add(mrow, 1)
        vs.addWidget(cm)
        # ---- Device Communication Activity (frames from decoded records only)
        ca = Card("Device Communication Activity",
                  "READ/WRITE frames = acknowledged address phases in complete captured transactions · "
                  "rolling 10 s of capture time · click a row to filter packets, waveform and highlight in 3D")
        self.act_banner = label("", "cardHint", wrap=True)
        ca.add(self.act_banner)
        self.rank_lbl = label("", wrap=True)
        self.rank_lbl.setTextFormat(Qt.TextFormat.RichText)
        ca.add(self.rank_lbl)
        row = QSplitter(Qt.Orientation.Horizontal)
        self.act = QTableWidget(0, len(ACT_HEAD))
        self.act.setHorizontalHeaderLabels(ACT_HEAD)
        self.act.verticalHeader().setVisible(False)
        self.act.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.act.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.act.setAlternatingRowColors(True)
        self.act.setShowGrid(False)
        self.act.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.act.horizontalHeader().setStretchLastSection(False)
        self.act.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.act.horizontalHeader().setHighlightSections(False)
        mono_columns(self.act, ACT_NUM, right=True)
        mono_columns(self.act, ACT_MONO)
        self.act.cellClicked.connect(lambda r, c: ctx.focus_device(self.act.item(r, 0).data(Qt.ItemDataRole.UserRole)))
        row.addWidget(self.act)
        self.act_plot = styled_plot("Frames per device (rolling 10 s)", "frames", "device address", 215)
        self.act_plot.setMinimumWidth(460)
        self.act_plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        import numpy as _np
        # persistent bar items updated with setOpts (removing/recreating them every refresh made the chart flash)
        self.act_bars = [pg.BarGraphItem(x=_np.zeros(0), height=_np.zeros(0), width=0.34, brush=C["blue"], pen=None),
                         pg.BarGraphItem(x=_np.zeros(0), height=_np.zeros(0), width=0.34, brush=C["cyan"], pen=None)]
        for b in self.act_bars:
            self.act_plot.getPlotItem().addItem(b)
        self._act_sig = None
        row.addWidget(self.act_plot)
        row.setChildrenCollapsible(False)
        row.setSizes([860, 560])
        self.act.setMinimumHeight(200)
        ca.add(row, 1)
        vs.addWidget(ca)
        cp = Card("Packets for selected device")
        self.sel_lbl = label("All devices", "cardHint")
        cp.header.addWidget(self.sel_lbl)
        self.proxy = PacketFilter()
        self.proxy.setSourceModel(ctx.packet_model)
        self.pt = QTableView()
        self.pt.setModel(self.proxy)
        self.pt.setAlternatingRowColors(True)
        self.pt.verticalHeader().setVisible(False)
        self.pt.verticalHeader().setDefaultSectionSize(25)
        self.pt.setShowGrid(False)
        self.pt.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        keep = {C_ID, C_TS, C_ADDR, C_MASTER, C_RW, C_DATA, C_SEMANTIC, C_ACK, C_DUR}
        for c in range(len(COLUMNS)):
            self.pt.setColumnHidden(c, c not in keep)
        ph = self.pt.horizontalHeader()
        ph.moveSection(ph.visualIndex(C_MASTER), ph.visualIndex(C_ADDR) + 1)
        ph.moveSection(ph.visualIndex(C_SEMANTIC), ph.visualIndex(C_DATA))          # decoded text directly left of the raw bytes
        fit_table(self.pt, {C_ID: 62, C_TS: 118, C_ADDR: 80, C_MASTER: 150, C_RW: 112, C_DATA: 420, C_SEMANTIC: 310,
                            C_ACK: 380, C_DUR: 112})
        ph.setStretchLastSection(False)
        self.pt.doubleClicked.connect(lambda i: ctx.show_waveform(self.proxy.mapToSource(i).row()))
        cp.add(self.pt, 1)
        vs.addWidget(cp)
        vs.setSizes([222, 360, 520, 260])
        lay.addWidget(vs, 1)

    def select(self, key):
        self.selected = key if isinstance(key, int) else None
        self.net.set_focus(self.selected)
        self.proxy.set(addr=self.selected)
        name = self.ctx.engine.registry.name(self.selected)[0] if self.selected is not None else ""
        self.sel_lbl.setText(f"0x{self.selected:02X} · {name}" if self.selected is not None else "All devices")

    def refresh_activity(self, engine):
        a = engine.device_activity()
        sim = "SIMULATED — NOT VERIFIED HARDWARE · " if a["simulated"] else ""
        win = (f"window: observed {a['window_s']:.6g} s of capture time ({a['window_note']})" if a.get("window_note")
               else f"window: rolling {a['window_s']:g} s of capture time")
        self.act_banner.setText(f"{sim}{win} · "
                                f"{'reliable' if a['reliable'] else 'UNRELIABLE: ' + a['reason']} · units: {a['units']} · "
                                "ties: all tied devices listed, ascending address · 'least' includes zero only for "
                                "devices verified connected in a reliable window")
        self.rank_lbl.setText("<br>".join((f"<span style='color:{C['demo']}'>SIMULATED</span> " if a["simulated"] else "") + t
                                          for t in (ranking_text(a, "reads", "<b>READ</b>"),
                                                    ranking_text(a, "writes", "<b>WRITE</b>"))))
        rows = list(a["rows"].values())
        self.act.setRowCount(len(rows))
        for i, r in enumerate(rows):
            lr, lw = r["latest_read"], r["latest_write"]
            vals = [f"0x{r['addr']:02X}", r["name"], r["status"] + (" · SIMULATED" if a["simulated"] else ""),
                    str(r["reads"]), str(r["writes"]), str(r["read_bytes"]), str(r["write_bytes"]),
                    f"{r['read_rate']:.2f}", f"{r['write_rate']:.2f}", str(r["transactions"]), str(r["nack_attempts"]),
                    f"{format_capture_ns(lr[0])} #{lr[1]}" if lr else "none observed",
                    f"{format_capture_ns(lw[0])} #{lw[1]}" if lw else "none observed", r["why"]]
            if r["status"] == "UNKNOWN":
                for k in range(3, 11):
                    vals[k] = "UNKNOWN"
            elif r["status"] == "PARTIAL":
                for k in range(3, 10):
                    vals[k] = "≥ " + vals[k]
            for c, v in enumerate(vals):
                it = self.act.item(i, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.act.setItem(i, c, it)
                it.setText(v)
                it.setToolTip(v)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, r["addr"])
                if c == 2:
                    it.setForeground(QBrush(QColor(ACT_COL.get(r["status"], C["text"]))))
        pi = self.act_plot.getPlotItem()
        known = [r for r in rows if r["status"] in ("ACTIVE", "NO OBSERVED ACTIVITY")]
        sig = tuple((r["addr"], r["writes"], r["reads"]) for r in known)
        if sig != self._act_sig:
            self._act_sig = sig
            import numpy as np
            xs = np.arange(len(known), dtype=float)
            self.act_bars[0].setOpts(x=xs - 0.18, height=np.array([r["writes"] for r in known], float), width=0.34)
            self.act_bars[1].setOpts(x=xs + 0.18, height=np.array([r["reads"] for r in known], float), width=0.34)
            pi.getAxis("bottom").setTicks([[(i, f"0x{r['addr']:02X}") for i, r in enumerate(known)]] if known else [[]])
        span = (f"observed {a['window_s']:.4g} s" if a.get("window_note") else f"rolling {a['window_s']:g} s")
        title = (("SIMULATED · " if a["simulated"] else "") +
                 f"Frames per device · {span}<br>blue WRITE · cyan READ · UNKNOWN not plotted")
        if title != getattr(self, "_act_title", None):
            self._act_title = title
            self.act_plot.setTitle(title)
        return a

    def _arb_clicked(self, r, c):
        item = self.arb.item(r, 7)
        tid = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        txn = next((t for t in reversed(self.ctx.engine.packets) if t.id == tid), None) if tid is not None else None
        if txn is not None:
            self.ctx.select_transaction(txn)

    def _visualize_arb(self):
        rows = self.arb.selectionModel().selectedRows() if self.arb.selectionModel() else []
        r = rows[0].row() if rows else (self.arb.currentRow() if self.arb.currentRow() >= 0 else None)
        if r is not None and self.arb.item(r, 7) is not None:
            pid = self.arb.item(r, 7).data(Qt.ItemDataRole.UserRole)
            txn = next((t for t in reversed(self.ctx.engine.packets) if t.id == pid), None) if pid is not None else None
            if txn is not None:
                self.ctx.visualize_packet(txn)
                return
        self.ctx.visualize_last_arbitration()

    def refresh_masters(self, engine):
        tr = engine.masters
        s = tr.summary()
        sim = ("" if engine.source != "demo" else
               "SIMULATED demo bench: two SIMULATED controllers, attributed from their simulated telemetry "
               "(NOT VERIFIED HARDWARE) \u00b7 " if s.get("simulated") else "SIMULATED demo: single simulated master \u00b7 ")
        meth = s["methods"]
        self.mm_banner.setText(
            f"{sim}{s['packets']:,} bus packets \u00b7 attributed by controller telemetry {meth.get('telemetry', 0):,}"
            f" \u00b7 by configured ownership {meth.get('ownership', 0):,} \u00b7 demo {meth.get('demo', 0):,} "
            f"\u00b7 unattributed {max(0, s['packets'] - s['attributed']):,} \u00b7 segments cut inside a byte "
            f"(abort/recovery/collision) {engine.mid_byte_cuts} \u00b7 tBUF violations {engine.tbuf_violations} "
            f"\u00b7 controller counters = change since that master's first stats report in this capture")
        rows = tr.rows()
        self.mm.setRowCount(len(rows))
        for i, r in enumerate(rows):
            st = r["stats"]
            port = r["port"] + ("" if r["connected"] else " (not open)")
            vals = [f"{r['label']} \u00b7 {r['pico']}", r["board"], port, r["state"]]
            vals += [f"{st[MM_KEYS[k]]:,}" if r["have_stats"] else "\u2014" for k in range(4, 14)]
            vals += [f"{r['bus_packets']:,}", r["clock"], r["detail"]]
            items = _fill(self.mm, i, vals, {3: r["detail"]})
            items[3].setForeground(QBrush(QColor(MM_COL.get(r["state"], C["text"]))))
            items[0].setForeground(QBrush(QColor(tr.cfg[r["master_id"]].get("color", C["text"]))))
            if r["have_stats"]:
                for k in (7, 8):
                    if st[MM_KEYS[k]]:
                        items[k].setForeground(QBrush(QColor(C["cyan"])))       # contention, not a fault
                for k in MM_BAD:
                    if st[MM_KEYS[k]]:
                        items[k].setForeground(QBrush(QColor(C["red"])))
        reg = engine.registry
        addrs = sorted(set(reg.configured) | {a for a in reg.devices if reg.devices[a].last_ack_seen is not None})
        mrows = tr.matrix(addrs)
        self.mx.setRowCount(len(mrows))
        for i, r in enumerate(mrows):
            vals = [r["master"], f"0x{r['addr']:02X}", reg.name(r["addr"])[0], r["ok"], r["addr_nack"],
                    r["data_nack"], r["arb_lost"], r["gave_up"], r["timeouts"], r["bus_packets"]]
            items = _fill(self.mx, i, vals)
            items[1].setData(Qt.ItemDataRole.UserRole, r["addr"])
            if r["master_id"] in tr.cfg:
                items[0].setForeground(QBrush(QColor(tr.cfg[r["master_id"]].get("color", C["text"]))))
            for k in (4, 5):
                if vals[k]:
                    items[k].setForeground(QBrush(QColor(C["red"])))
            if r["arb_lost"]:
                items[6].setForeground(QBrush(QColor(C["cyan"])))
            if r["gave_up"]:
                items[7].setForeground(QBrush(QColor(C["amber"])))
        arbs = tr.arb_rows(200)
        self.arb.setRowCount(len(arbs))
        for i, a in enumerate(arbs):
            vals = [format_capture_ns(a["t_bus_ns"]) if a["t_bus_ns"] is not None else "not aligned yet",
                    a["master"], f"0x{a['addr']:02X}", a["attempt"], a["byte"], f"{a['backoff_us']} \u00b5s",
                    "retried" if a["retry"] else "GAVE UP (transfer skipped)", a["winner"]]
            items = _fill(self.arb, i, vals)
            items[7].setData(Qt.ItemDataRole.UserRole, a["winner_id"])
            items[6].setForeground(QBrush(QColor(C["cyan"] if a["retry"] else C["amber"])))

    def refresh(self, engine):
        self.refresh_activity(engine)
        self.refresh_masters(engine)
        if getattr(self.ctx, "autoscroll", False):
            self.pt.scrollToBottom()
        reg = engine.registry
        now = engine.hw_now or 0
        t0 = engine.packets[0].t_start if engine.packets else 0
        addrs = sorted(set(reg.devices) | set(reg.configured))
        self.net.set_devices(reg.node_list(now))
        self.ctx.packet_model.sync()
        self.tbl.setRowCount(len(addrs))
        for r, a in enumerate(addrs):
            d = reg.devices.get(a, DeviceStats(a))
            name, conf = reg.name(a)
            st = reg.status(a, now)
            vals = [f"0x{a:02X}", name, conf + (f" ({d.confirm_detail})" if d.confirm_detail else ""), st,
                    f"{d.reads:,}", f"{d.writes:,}", f"{d.total_bytes:,}", f"{d.acks:,}", f"{d.nacks:,}",
                    f"{(d.last_seen - t0) / 1e9:.3f}" if d.last_seen is not None else "—", f"{reg.rate(a, now):.2f}"]
            # HEALTHY / DEGRADED only on trustworthy live hardware; demo / presentation / replay / disconnected: UNKNOWN
            trusted, reason = engine.measurement_trust()
            h = engine.communication.device(a, trusted, reason)
            last = (f"HW {h['last_success_hw']/1e9:.6f} s; {h['last_success_age_s']:.1f} s ago"
                    if h['last_success_hw'] is not None else "Never confirmed")
            rate = f"{h['unexpected_nack_pct']:.3f}%" if h['unexpected_nack_pct'] is not None else "Unmeasurable"
            age = f"{h['observation_age_s']:.1f} s" if h['observation_age_s'] is not None else "No observations"
            vals += [h['status'], str(h['unexpected_nacks']), rate, str(h['successful']), str(h['failed']),
                     str(h['incomplete']), last, h['last_failure'] or "None observed", age+"; "+h['confidence']]
            for c, v in enumerate(vals):
                it = self.tbl.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.tbl.setItem(r, c, it)
                it.setText(v)
                it.setToolTip(h['detail'] if c == 11 else str(v))
                if c in (8, 12):
                    it.setForeground(QBrush(QColor(C["red"])))
                if c == 7:
                    it.setForeground(QBrush(QColor(C["green"])))
                if c == 11:
                    it.setForeground(QBrush(QColor(C['red'] if h['status'] == 'DEGRADED' else
                                                   C['green'] if h['status'] == 'HEALTHY' else C['amber'])))
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, a)
                if c == 3:
                    it.setForeground(QBrush(QColor(STATUS_COL.get(st, C["text"]))))
                if c == 2:
                    it.setForeground(QBrush(QColor({"Confirmed": C["green"], "Configured": C["cyan"],
                                                    "Likely": C["amber"]}.get(conf, C["red"]))))
