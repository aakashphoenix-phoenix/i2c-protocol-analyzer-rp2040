"""Packet Analyzer page — sortable/filterable decoded transaction table + detail panel."""
from __future__ import annotations

import html

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QLineEdit, QPushButton, QSplitter, QTableView, QTextEdit, QVBoxLayout,
                             QWidget)

from config import COLORS as C
from gui.theme import MONO_CSS
from gui.widgets import Card, FlowLayout, fit_table, fmt_time_ns, label
from models.packet_model import (C_ACK, C_ADDR, C_BYTES, C_DATA, C_DEV, C_DUR, C_EVT, C_ID, C_MASTER, C_RW, C_SEMANTIC,
                                 C_STAT, C_TS, PacketFilter, fmt_dur)
from analysis.playback import _nack_note
from analysis.multi_master import attribution_detail, attribution_text
import config
from storage.capture_storage import export_packets_csv

STATUSES = ["All", "Complete", "Incomplete", "Malformed", "Capture gap"]
# Quick-filter pills.  Master / device pills mirror the Master and Device dropdowns (one value at a time, exactly like
# the dropdown); the last one is a PacketFilter flag of its own.
PILL_ADDRS = {"rtc": 0x68, "bmp": 0x76}          # the OLED pill follows the configured OLED address (engine.sensors)


def packet_detail_html(engine, t, t_origin) -> str:
    reg = engine.registry
    name, conf = reg.name(t.address)
    h = [f"<h3 style='margin:0'>Packet #{t.id} {'<span style=color:' + C['demo'] + '>· DEMO DATA</span>' if t.demo else ''}</h3>",
         f"<p style='color:{C['text2']}'>t = {(t.t_start - t_origin) / 1e9:.7f} s · duration {fmt_dur(t.duration_ns)} · "
         f"status <b>{t.status}</b><br>Device: <b>{name}</b> ({conf.lower()})<br>"
         f"Master: <b>{attribution_detail(t, engine.masters)}</b></p>"]
    from gui.frame_html import alert_html, frame_table_html
    alert, _kind = alert_html(engine, t)
    if alert:
        h.append(f"<p style='margin:4px 0 6px 0'>{alert}</p>")
    h.append(frame_table_html(engine, t))
    if t.semantic:
        # only what the sensor decoder verified from this packet's bytes; nothing is shown (or invented) when empty
        h.append(f"<p style='font-size:15px;margin:2px 0 8px 0'><span style='color:{C['text2']}'>DECODED</span>&nbsp; "
                 f"<b style='color:{C['cyan']}'>{html.escape(t.semantic)}</b>"
                 + (f" <span style='color:{C['demo']}'>· DEMO · SIMULATED</span>" if t.demo else "") + "</p>")
    for i, f in enumerate(t.frames):
        a = "—" if f.addr is None else f"0x{f.addr:02X}"
        byte0 = "" if f.addr is None else f" (address byte 0x{(f.addr << 1 | (f.rw == 'R')) & 0xFF:02X})" if not f.ten_bit else " (10-bit)"
        ack = {True: f"<span style='color:{C['green']}'>ACK</span>", False: f"<span style='color:{C['red']}'>NACK</span>",
               None: "?"}[f.addr_ack]
        h.append(f"<p><b>{f.start_kind}</b> · frame {i + 1}: address <b>{a}</b>{byte0} · "
                 f"<span style='color:{C['cyan'] if f.rw == 'R' else C['blue']}'>{'READ' if f.rw == 'R' else 'WRITE'}</span> · {ack}</p>")
        if f.data:
            rows = [f"<table cellspacing=0 cellpadding=3 style=\"{MONO_CSS}\">"
                    f"<tr style='color:{C['text2']}'><td>#</td><td>hex</td><td>bin</td><td>dec</td><td>chr</td><td>ack</td></tr>"]
            for j, (b, ak) in enumerate(zip(f.data, f.acks)):
                if j >= 64:
                    rows.append(f"<tr><td colspan=6 style='color:{C['text2']}'>… {len(f.data) - 64} more bytes</td></tr>")
                    break
                ch = chr(b) if 32 <= b < 127 else "·"
                rows.append(f"<tr><td style='color:{C['text2']}'>{j}</td><td><b>{b:02X}</b></td><td>{b:08b}</td>"
                            f"<td>{b}</td><td>{ch}</td><td style='color:{C['green'] if ak else C['red']}'>"
                            f"{'A' if ak else 'N'}</td></tr>")
                if not ak:      # the (long) NACK explanation gets a row of its own: the 6-column table stays narrow
                    rows.append(f"<tr><td></td><td colspan=5 style='color:{C['red']}'>↳ "
                                f"{html.escape(_nack_note(t, i, 'data', j))}</td></tr>")
            rows.append("</table>")
            h.append("".join(rows))
    h.append(f"<p>Events: {' · '.join(f'{k} @ {fmt_time_ns(tt - t.t_start)}' for tt, k in t.events)}</p>")
    h.append(f"<p>Source: {t.source.upper()} · absolute START {t.t_start} ns · end {t.t_end} ns</p>")
    tm = t.timing
    from analysis.playback import format_capture_ns
    hz = tm.get('frequency_hz')
    h.append(f"<p><b>Measured SCL</b>: " + (f"{hz / 1e3:.3f} kHz · {tm.get('frequency_scope', '')} · source "
             f"{tm.get('frequency_source', '')}" if hz else "UNKNOWN — no captured SCL cycles") +
             (f" · individual cycles {tm['cycle_min_hz'] / 1e3:.2f}–{tm['cycle_max_hz'] / 1e3:.2f} kHz"
              if tm.get('cycle_min_hz') else "") + "</p>")
    h.append(f"<p>START {format_capture_ns(tm.get('start_ns'))} · STOP "
             f"{format_capture_ns(tm.get('stop_ns')) if tm.get('stop_ns') else 'UNKNOWN (not captured)'} · "
             f"transaction {fmt_time_ns(t.duration_ns) if t.t_end is not None else 'UNKNOWN'}</p>")
    for key, title in [('period_ns','Robust cycle mean period'),('high_ns','Mean SCL HIGH'),('low_ns','Mean SCL LOW'),('setup_min_ns','Observed minimum SDA setup'),('hold_min_ns','Observed minimum SDA hold'),('bus_free_ns','Previous STOP to START')]:
        h.append(f"<p>{title}: {fmt_time_ns(tm.get(key))}</p>")
    res = tm.get('sample_resolution_ns') or tm.get('timing_resolution_ns')
    if res:
        h.append(f"<p style='color:{C['text2']}'>Digital limit: every edge is known to ±1 sample ({fmt_time_ns(res)}). "
                 "Individual cycles are quantised (the robust packet mean averages that out); setup / hold values are "
                 "digital threshold intervals, not analog t<sub>SU;DAT</sub> / t<sub>HD;DAT</sub> or rise-time "
                 "measurements, and are never judged pass / fail.</p>")
    h.append('<p>Long LOW intervals (cause undetermined): ' + ', '.join(fmt_time_ns(v) for v in tm.get('long_low_ns', [])) + '</p>')
    h.append('<p>Byte durations (first bit rise → ACK-clock fall): ' + ', '.join(
        (f"0x{b.value:02X} {fmt_time_ns(d)}" if d is not None else f"0x{b.value:02X} UNKNOWN")
        for b, d in zip(t.bytes, tm.get('byte_durations_ns') or [None] * len(t.bytes))) + '</p>')
    if t.notes:
        h.append(f"<p style='color:{C['amber']}'>Notes: {'; '.join(t.notes)}</p>")
    return "".join(h)


class PacketAnalyzerPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        # live phase of the packet the 3D network is playing (same playback cursor, same banner text); its table row
        # fills in stage by stage and is highlighted (PacketModel.set_live)
        from gui.phase_panel import PhasePanel
        pc = Card("Live bus phase — in lockstep with the 3D I²C network")
        self.phase = PhasePanel()
        pc.add(self.phase)
        lay.addWidget(pc)
        f = Card()
        # ---- quick-filter pills (checkable; synced both ways with the dropdowns below and with PacketFilter)
        r0 = FlowLayout(hspace=8, vspace=6)
        r0.addWidget(label("Quick filters", "cardHint"))
        self.pills = {}
        for mid, m in config.MASTERS.items():
            self._add_pill(r0, ("master", mid), m["label"], f"Only packets attributed to {m['label']} ({m['board']}). "
                           "Same as the Master dropdown.")
        for key, text in (("rtc", "DS3231 (0x68)"), ("bmp", "BMP280 (0x76)"), ("oled", "SSD1306 (0x3C)")):
            self._add_pill(r0, ("addr", key), text, "Only packets addressing this device. Same as the Device dropdown.")
        self._add_pill(r0, ("errors", None), "Errors / NACKs Only",
                       "Incomplete / malformed packets and NACKs that are not the normal end-of-read NACK "
                       "(address NACK, written-byte NACK, early read NACK, end-of-read NACK without a captured STOP).")
        f.add(r0)
        r1 = FlowLayout(hspace=8, vspace=6)               # wraps onto a second row instead of widening the page
        self.addr = QComboBox()
        self.addr.addItem("All addresses / devices", None)
        self.rw = QComboBox()
        self.rw.addItems(["All", "READ", "WRITE"])
        self.ack = QComboBox()
        self.ack.addItems(["All", "All ACK", "Has NACK", "Address NACK"])
        self.status = QComboBox()
        self.status.addItems(STATUSES)
        self.master = QComboBox()
        self.master.addItem("All masters", "All")
        for mid, m in config.MASTERS.items():
            self.master.addItem(f"{m['label']} ({m['board']})", mid)
        self.master.addItem("Unknown (not validated)", "unattributed")
        self.master.setToolTip("Which controller drove the packet: matched from that master's own USB telemetry, "
                               "or configured ownership (OLED = Master 1). A passive tap alone cannot tell.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search: address, device, hex bytes, status…")
        self.search.setClearButtonEnabled(True)
        for w, lbl in ((self.addr, "Device"), (self.master, "Master"), (self.rw, "R/W"), (self.ack, "ACK"),
                       (self.status, "Status")):
            r1.addWidget(label(lbl, "cardHint"))
            r1.addWidget(w)
        self.search.setFixedWidth(300)
        r1.addWidget(self.search)
        f.add(r1)
        r2 = FlowLayout(hspace=8, vspace=6)
        self.only_bm = QCheckBox("Bookmarked only")
        self.auto = QCheckBox("Auto-scroll")
        self.auto.setChecked(True)
        self.btn_bm = QPushButton("★ Bookmark")
        self.btn_copy = QPushButton("Copy packet")
        self.btn_wave = QPushButton("Open in waveform")
        self.btn_csv = QPushButton("Export visible CSV")
        self.btn_clear = QPushButton("Clear visible")
        self.btn_unclear = QPushButton("Show all")
        self.goto = QDoubleSpinBox()
        self.goto.setDecimals(6)
        self.goto.setMaximum(1e9)
        self.goto.setSuffix(" s")
        self.goto.setMinimumWidth(140)
        self.btn_goto = QPushButton("Go to time")
        self.btn_proof = QPushButton("🔬 Packet Proof")
        self.btn_proof.setObjectName("primary")
        self.btn_proof.setToolTip("Every piece of evidence for the selected packet (the newest one when none is selected): "
                                  "raw SDA/SCL edges, bit-by-bit decode, START/STOP, timing vs UM10204, ACK/NACK, which "
                                  "master sent it, what the bytes mean - savable as an HTML report")
        self.btn_check = QPushButton("✔ Cross-check")
        self.btn_check.setToolTip("Compare the capture with the masters' own transfer logs (known transactions), the "
                                  "two independent decoders, known device content and an imported reference "
                                  "logic-analyzer decode")
        self.btn_vis_err = QPushButton("⚠ Visualize error waveform")
        self.btn_vis_err.setToolTip("Open the selected packet (if it is an error packet), else the newest error packet, in "
                                    "the Digital Waveform: paused on it, fitted, every field labelled above SCL / SDA and "
                                    "the error cause in a banner")
        self.btn_vis_err.clicked.connect(self._visualize_error)
        for w in (self.btn_proof, self.btn_check, self.btn_vis_err, self.only_bm, self.auto, self.btn_bm, self.btn_copy,
                  self.btn_wave, self.btn_csv, self.btn_clear, self.btn_unclear, self.goto, self.btn_goto):
            r2.addWidget(w)
        f.add(r2)
        lay.addWidget(f)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(10)
        self.proxy = PacketFilter()
        self.proxy.setSourceModel(ctx.packet_model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(False)
        # numeric / hex columns are MONO_FONT (PacketModel FontRole), so they are wider than proportional text; each
        # column is also never narrower than its header label (fit_table), so no header word is cut
        # widths hold the LONGEST normal cell (12 hex bytes of a frame, 24 ACK letters, 'Unknown device @0x51' ...) so no cell
        # is elided with '...'
        fit_table(self.table, {0: 32, C_ID: 58, C_TS: 108, C_ADDR: 72, C_DEV: 130, 5: 76, C_RW: 108, C_DATA: 300,
                               C_SEMANTIC: 270, C_MASTER: 136, C_BYTES: 72, C_ACK: 380, C_EVT: 130, C_DUR: 112, C_STAT: 140,
                               13: 90, 14: 160, 15: 160, 16: 130, 17: 250, 18: 100, 19: 150, 20: 370})
        # Default column order, most useful first (the table scrolls sideways): # - time - address - device - DECODED -
        # raw hex bytes (its W:/R: prefix says the direction) - R/W - master - ACK/NACK - mode ...  The inline decode sits
        # directly left of the raw bytes so it is on screen without scrolling; "Master" (a logical column appended last,
        # stable indices) is moved behind them.  Long cells (12 hex bytes, long device names) carry their full text as tooltip.
        for target, logical in enumerate([0, C_ID, C_TS, C_ADDR, C_DEV, C_SEMANTIC, C_DATA, C_RW, C_MASTER, C_ACK, 5]):
            hh.moveSection(hh.visualIndex(logical), target)
        tc = Card()
        tc.add(self.table, 1)
        self.count = label("", "cardHint")
        tc.add(self.count)
        split.addWidget(tc)
        dc = Card("Decoded packet detail")
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setHtml(f"<p style='color:{C['text2']}'>Select a packet.</p>")
        dc.add(self.detail, 1)
        split.addWidget(dc)
        split.setSizes([1000, 420])
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        dc.setMinimumWidth(260)
        tc.setMinimumWidth(420)
        lay.addWidget(split, 1)

        for w in (self.rw, self.ack, self.status):
            w.currentTextChanged.connect(self._apply)
        self.addr.currentIndexChanged.connect(self._apply)
        self.master.currentIndexChanged.connect(self._apply)
        self.search.textChanged.connect(self._apply)
        self.only_bm.toggled.connect(self._apply)
        self.auto.toggled.connect(lambda v: setattr(ctx, "autoscroll", v))
        self.table.selectionModel().selectionChanged.connect(self._sel)
        self.table.doubleClicked.connect(lambda i: self.btn_bm.click() if i.column() == 0 else None)
        self.btn_bm.clicked.connect(self._bookmark)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_wave.clicked.connect(self._wave)
        self.btn_csv.clicked.connect(self._csv)
        self.btn_clear.clicked.connect(self._clear_visible)
        self.btn_unclear.clicked.connect(lambda: self.proxy.set(min_id=0))
        self.btn_goto.clicked.connect(self._goto)
        self.btn_proof.clicked.connect(lambda: self.open_proof())
        self.btn_check.clicked.connect(lambda: self.open_cross_check())
        self._sync_pills()

    # ------------------------------------------------------------------ quick-filter pills
    def _add_pill(self, layout, key, text, tip):
        b = QPushButton(text)
        b.setObjectName("pill")
        b.setCheckable(True)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.toggled.connect(lambda on, k=key: self._on_pill(k, on))
        layout.addWidget(b)
        self.pills[key] = b
        return b

    def pill_addr(self, name):
        """Device address behind an address pill (the OLED one follows the configured OLED address)."""
        return self.ctx.engine.sensors.oled_addr if name == "oled" else PILL_ADDRS[name]

    def _on_pill(self, key, on):
        kind, ident = key
        if kind == "master":
            if on:
                self.master.setCurrentIndex(max(0, self.master.findData(ident)))
            elif self.master.currentData() == ident:
                self.master.setCurrentIndex(0)
        elif kind == "addr":
            a = self.pill_addr(ident)
            if on:
                self.set_address_filter(a)
            elif self.addr.currentData() == a:
                self.set_address_filter(None)
        else:
            self._apply()
        self._sync_pills()

    def _sync_pills(self):
        """The dropdowns are the single source of truth for master / device: the pills mirror them (so two device
        pills can never both be on, and a filter set from another page - a clicked 3D node - lights its pill)."""
        for (kind, ident), b in self.pills.items():
            if kind == "master":
                want = self.master.currentData() == ident
            elif kind == "addr":
                want = self.addr.currentData() == self.pill_addr(ident)
            else:
                continue
            if b.isChecked() != want:
                b.blockSignals(True)
                b.setChecked(want)
                b.blockSignals(False)
        oled = self.pills[("addr", "oled")]
        text = f"SSD1306 (0x{self.pill_addr('oled'):02X})"
        if oled.text() != text:
            oled.setText(text)

    # ------------------------------------------------------------------
    def retheme(self):
        """Theme switch: the detail HTML embeds colours, and the table repaints with the new foreground colours."""
        self.phase.retheme()
        r = self._row()
        if r is not None:
            self.show_detail(self.ctx.packet_model.txn(r))
        elif self.detail.toPlainText() == "Select a packet.":
            self.detail.setHtml(f"<p style='color:{C['text2']}'>Select a packet.</p>")
        self.table.viewport().update()

    def _apply(self):
        self.proxy.set(addr=self.addr.currentData(), rw=self.rw.currentText(), ack=self.ack.currentText(),
                       status=self.status.currentText(), text=self.search.text().strip(),
                       only_bookmarks=self.only_bm.isChecked(), master=self.master.currentData(),
                       errors_only=self.pills[("errors", None)].isChecked())
        self._sync_pills()

    def set_address_filter(self, addr):
        i = self.addr.findData(addr)
        if i < 0 and addr is not None:
            self.addr.addItem(f"0x{addr:02X}", addr)
            i = self.addr.count() - 1
        self.addr.setCurrentIndex(max(0, i))

    def _row(self):
        idx = self.table.selectionModel().selectedRows()
        return self.proxy.mapToSource(idx[0]).row() if idx else None

    def _sel(self):
        r = self._row()
        if r is None:
            return
        m = self.ctx.packet_model
        self.detail.setHtml(packet_detail_html(self.ctx.engine, m.txn(r), m.t_origin or 0))
        self.ctx.select_transaction(m.txn(r))

    def show_detail(self, txn):
        """Shared selection: details for the packet chosen in any view (same record)."""
        m = self.ctx.packet_model
        self.detail.setHtml(packet_detail_html(self.ctx.engine, txn, m.t_origin or 0))

    def select_packet(self, txn):
        """Select and scroll to txn's row (a jump from another page, e.g. the diagnostics log).  Auto-scroll is paused so
        the next refresh does not scroll it away.  Returns False when the current filters hide the packet (its details
        are still shown)."""
        m = self.ctx.packet_model
        m.sync()
        r = next((i for i in range(len(m.rows) - 1, -1, -1) if m.rows[i] is txn), None)
        pi = self.proxy.mapFromSource(m.index(r, 0)) if r is not None else None
        if pi is None or not pi.isValid():
            self.show_detail(txn)
            return False
        self.auto.setChecked(False)
        self.table.selectRow(pi.row())
        self.table.scrollTo(pi)
        return True

    def selected_transaction(self):
        r = self._row()
        return self.ctx.packet_model.txn(r) if r is not None else None

    def open_proof(self, txn=None):
        """Packet Proof of the selected packet (or the newest one)."""
        from gui.proof_views import PacketProofDialog
        eng = self.ctx.engine
        txn = txn or self.selected_transaction() or (eng.packets[-1] if eng.packets else None)
        if txn is None:
            self.ctx.toast("No packet captured yet")
            return None
        dlg = PacketProofDialog(eng, txn, self, on_newest=self._prove_newest_like)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()
        self._proof_dialog = dlg
        return dlg

    def _prove_newest_like(self, txn):
        """The newest complete packet to the same device whose raw edges are still in memory."""
        eng = self.ctx.engine
        end = eng.edges[-1][0] if eng.edges else None
        for t in reversed(eng.packets[-4000:]):
            if t.address == txn.address and t.status == "ok" and end is not None and (t.t_end or 0) + 70_000 <= end:
                if getattr(self, "_proof_dialog", None) is not None:
                    self._proof_dialog.close()
                self.select_packet(t)
                return self.open_proof(t)
        self.ctx.toast("No recent packet of this device with raw edges in memory")
        return None

    def open_cross_check(self):
        from gui.proof_views import CrossCheckDialog
        dlg = CrossCheckDialog(self.ctx.engine, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()
        self._check_dialog = dlg
        return dlg

    def _bookmark(self):
        r = self._row()
        if r is not None:
            self.ctx.packet_model.toggle_bookmark(r)

    def _copy(self):
        r = self._row()
        if r is None:
            return
        m = self.ctx.packet_model
        t = m.txn(r)
        name = self.ctx.engine.registry.name(t.address)[0]
        txt = (f"#{t.id}\t{m.ts(t.t_start):.6f}s\t0x{(t.address or 0):02X}\t{name}\t{t.operation}\t"
               f"{t.data_hex()}\t{t.ack_seq()}\t{t.event_seq()}\t{fmt_dur(t.duration_ns)}\t{t.status}")
        QGuiApplication.clipboard().setText(txt)
        self.ctx.toast("Packet copied to clipboard")

    def _wave(self):
        r = self._row()
        if r is not None:
            self.ctx.show_waveform(r)

    def _visualize_error(self):
        from analysis.frame_view import is_error
        t = self.selected_transaction()
        if t is not None and is_error(self.ctx.engine, t):
            self.ctx.visualize_packet(t)
        else:
            self.ctx.visualize_last_error()

    def _csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export visible packets", "packets.csv", "CSV (*.csv)")
        if not path:
            return
        m = self.ctx.packet_model
        rows = [m.txn(self.proxy.mapToSource(self.proxy.index(i, 0)).row()) for i in range(self.proxy.rowCount())]
        n = export_packets_csv(path, self.ctx.engine, rows)
        self.ctx.toast(f"Exported {n} packets")

    def _clear_visible(self):
        pk = self.ctx.engine.packets
        self.proxy.set(min_id=pk[-1].id if pk else 0)

    def _goto(self):
        m = self.ctx.packet_model
        target = self.goto.value()
        best, bd = None, None
        for i in range(self.proxy.rowCount()):
            r = self.proxy.mapToSource(self.proxy.index(i, 0)).row()
            d = abs(m.ts(m.txn(r).t_start) - target)
            if bd is None or d < bd:
                best, bd = i, d
        if best is not None:
            self.auto.setChecked(False)
            self.table.selectRow(best)
            self.table.scrollTo(self.proxy.index(best, 0))

    def show_live(self, playback, run_state="running", held=False):
        """Called by the main window on every playback step (also while this page is hidden: it is cheap)."""
        self.phase.show_playback(playback, run_state, held)

    def refresh(self, engine):
        self.ctx.packet_model.sync()
        self._sync_pills()                       # the SSD1306 pill follows Settings → OLED address
        known = {self.addr.itemData(i) for i in range(self.addr.count())}
        for a in sorted(engine.registry.devices):
            if a not in known:
                self.addr.addItem(f"0x{a:02X} · {engine.registry.name(a)[0]}", a)
        self.count.setText(f"{self.proxy.rowCount():,} shown · {len(engine.packets):,} in memory"
                           + (f" · {engine.packets_dropped_from_memory:,} oldest trimmed (save/export retention is bounded)"
                              if engine.packets_dropped_from_memory else ""))
        if self.ctx.autoscroll and self.auto.isChecked():
            self.table.scrollToBottom()
