"""Packet Analyzer page — sortable/filterable decoded transaction table + detail panel."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QLineEdit, QPushButton, QSplitter, QTableView, QTextEdit, QVBoxLayout,
                             QWidget)

from config import COLORS as C
from gui.widgets import Card, fmt_time_ns, label
from models.packet_model import PacketFilter, fmt_dur
from storage.capture_storage import export_packets_csv

STATUSES = ["All", "Complete", "Incomplete", "Malformed", "Capture gap"]


def packet_detail_html(engine, t, t_origin) -> str:
    reg = engine.registry
    name, conf = reg.name(t.address)
    h = [f"<h3 style='margin:0'>Packet #{t.id} {'<span style=color:' + C['demo'] + '>· DEMO DATA</span>' if t.demo else ''}</h3>",
         f"<p style='color:{C['text2']}'>t = {(t.t_start - t_origin) / 1e9:.7f} s · duration {fmt_dur(t.duration_ns)} · "
         f"status <b>{t.status}</b><br>Device: <b>{name}</b> ({conf.lower()})</p>"]
    for i, f in enumerate(t.frames):
        a = "—" if f.addr is None else f"0x{f.addr:02X}"
        byte0 = "" if f.addr is None else f" (address byte 0x{(f.addr << 1 | (f.rw == 'R')) & 0xFF:02X})" if not f.ten_bit else " (10-bit)"
        ack = {True: f"<span style='color:{C['green']}'>ACK</span>", False: f"<span style='color:{C['red']}'>NACK</span>",
               None: "?"}[f.addr_ack]
        h.append(f"<p><b>{f.start_kind}</b> · frame {i + 1}: address <b>{a}</b>{byte0} · "
                 f"<span style='color:{C['cyan'] if f.rw == 'R' else C['blue']}'>{'READ' if f.rw == 'R' else 'WRITE'}</span> · {ack}</p>")
        if f.data:
            rows = ["<table cellspacing=0 cellpadding=3 style='font-family:Consolas,monospace'>"
                    f"<tr style='color:{C['text2']}'><td>#</td><td>hex</td><td>bin</td><td>dec</td><td>chr</td><td>ack</td></tr>"]
            for j, (b, ak) in enumerate(zip(f.data, f.acks)):
                if j >= 64:
                    rows.append(f"<tr><td colspan=6 style='color:{C['text2']}'>… {len(f.data) - 64} more bytes</td></tr>")
                    break
                ch = chr(b) if 32 <= b < 127 else "·"
                note = "" if ak else (" (ends read — normal)" if t.status == "ok" and f.addr_ack is True and f.rw == "R" and j == len(f.data) - 1 and all(f.acks[:j]) else " ")
                rows.append(f"<tr><td style='color:{C['text2']}'>{j}</td><td><b>{b:02X}</b></td><td>{b:08b}</td>"
                            f"<td>{b}</td><td>{ch}</td><td style='color:{C['green'] if ak else C['red']}'>"
                            f"{'A' if ak else 'N'}{note}</td></tr>")
            rows.append("</table>")
            h.append("".join(rows))
    h.append(f"<p>Events: {' · '.join(f'{k} @ {fmt_time_ns(tt - t.t_start)}' for tt, k in t.events)}</p>")
    h.append(f"<p>Source: {t.source.upper()} · absolute START {t.t_start} ns · end {t.t_end} ns</p>")
    tm = t.timing
    for key, title in [('period_ns','Robust cycle mean period'),('high_ns','Mean SCL HIGH'),('low_ns','Mean SCL LOW'),('setup_min_ns','Observed minimum SDA setup'),('hold_min_ns','Observed minimum SDA hold'),('bus_free_ns','Previous STOP to START')]:
        h.append(f"<p>{title}: {fmt_time_ns(tm.get(key))}</p>")
    h.append('<p>Long LOW intervals (cause undetermined): ' + ', '.join(fmt_time_ns(v) for v in tm.get('long_low_ns', [])) + '</p>')
    h.append('<p>Byte first-rise to ACK-fall durations: ' + ', '.join(fmt_time_ns(b.t_end-b.t_first) for b in t.bytes if b.t_end is not None) + '</p>')
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
        f = Card()
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.addr = QComboBox()
        self.addr.addItem("All addresses / devices", None)
        self.rw = QComboBox()
        self.rw.addItems(["All", "READ", "WRITE"])
        self.ack = QComboBox()
        self.ack.addItems(["All", "All ACK", "Has NACK", "Address NACK"])
        self.status = QComboBox()
        self.status.addItems(STATUSES)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search: address, device, hex bytes, status…")
        self.search.setClearButtonEnabled(True)
        for w, lbl in ((self.addr, "Device"), (self.rw, "R/W"), (self.ack, "ACK"), (self.status, "Status")):
            r1.addWidget(label(lbl, "cardHint"))
            r1.addWidget(w)
        r1.addWidget(self.search, 1)
        f.add(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
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
        self.btn_goto = QPushButton("Go to time")
        for w in (self.only_bm, self.auto, self.btn_bm, self.btn_copy, self.btn_wave, self.btn_csv, self.btn_clear,
                  self.btn_unclear):
            r2.addWidget(w)
        r2.addStretch(1)
        r2.addWidget(self.goto)
        r2.addWidget(self.btn_goto)
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
        for c, w in enumerate([30, 60, 120, 70, 140, 60, 120, 300, 54, 150, 90, 100, 110]):
            self.table.setColumnWidth(c, w)
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
        lay.addWidget(split, 1)

        for w in (self.rw, self.ack, self.status):
            w.currentTextChanged.connect(self._apply)
        self.addr.currentIndexChanged.connect(self._apply)
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

    # ------------------------------------------------------------------
    def _apply(self):
        self.proxy.set(addr=self.addr.currentData(), rw=self.rw.currentText(), ack=self.ack.currentText(),
                       status=self.status.currentText(), text=self.search.text().strip(),
                       only_bookmarks=self.only_bm.isChecked())

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

    def refresh(self, engine):
        self.ctx.packet_model.sync()
        known = {self.addr.itemData(i) for i in range(self.addr.count())}
        for a in sorted(engine.registry.devices):
            if a not in known:
                self.addr.addItem(f"0x{a:02X} · {engine.registry.name(a)[0]}", a)
        self.count.setText(f"{self.proxy.rowCount():,} shown · {len(engine.packets):,} in memory"
                           + (f" · {engine.packets_dropped_from_memory:,} oldest trimmed (save/export retention is bounded)"
                              if engine.packets_dropped_from_memory else ""))
        if self.ctx.autoscroll and self.auto.isChecked():
            self.table.scrollToBottom()
