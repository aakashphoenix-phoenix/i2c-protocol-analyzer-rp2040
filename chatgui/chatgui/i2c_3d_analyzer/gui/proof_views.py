"""Packet Proof window (one packet, every piece of evidence) and Cross-check window (capture vs references)."""
from __future__ import annotations

import base64
import datetime as _dt
import html

import pyqtgraph as pg
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter,
                             QTextBrowser, QVBoxLayout)

from analysis.cross_check import (compare_with_reference, decoder_agreement, known_content, known_transactions,
                                  parse_reference)
from analysis.packet_proof import build_proof
from config import COLORS as C
from storage.signal_export import write_edges_csv, write_vcd

OK, BAD, NA = "✔", "✘", "–"
LIGHT = dict(bg="#FFFFFF", text="#0F172A", text2="#475569", border="#CBD5E1", green="#047857", red="#B91C1C",
             amber="#92400E", head="#F1F5F9", cyan="#075985", blue="#1D4ED8")


def _mark(ok, pal):
    return (f"<b style='color:{pal['green']}'>{OK} PASS</b>" if ok is True else
            f"<b style='color:{pal['red']}'>{BAD} FAIL</b>" if ok is False else
            f"<b style='color:{pal['amber']}'>{NA} n/a</b>")


def _us(ns):
    return "—" if ns is None else f"{ns / 1000:.3f} µs"


def proof_html(p, engine, pal=None, img_b64=None, standalone=False):
    pal = pal or dict(bg=C["card"], text=C["text"], text2=C["text2"], border=C["border"], green=C["green"],
                      red=C["red"], amber=C["amber"], head=C["card_hi"], cyan=C["cyan"], blue=C["blue"])
    t = p.txn
    origin = engine.capture_start_ns or t.t_start
    tbl = (f"style='border-collapse:collapse;margin:4px 0 12px 0' cellpadding=4 border=1 "
           f"bordercolor='{pal['border']}'")
    th = f"style='background:{pal['head']};text-align:left'"
    vcol = {"PROVEN": pal["green"], "PARTIAL": pal["amber"], "FAIL": pal["red"]}[p.verdict]
    h = []
    if standalone:
        h.append(f"<html><head><meta charset='utf-8'><title>Packet Proof #{t.id}</title><style>body{{font-family:"
                 f"Segoe UI,Arial,sans-serif;background:{pal['bg']};color:{pal['text']};margin:24px;max-width:1200px}}"
                 f"td,th{{font-size:13px}} code,.m{{font-family:Consolas,monospace}}</style></head><body>")
    data_hex = " | ".join(f"{'W' if f.rw == 'W' else 'R'} 0x{(f.addr or 0):02X}: " + " ".join(f"{b:02X}" for b in f.data)
                          for f in t.frames)
    h.append(f"<h2 style='margin:0'>Packet Proof — packet #{t.id} &nbsp;<span style='color:{vcol}'>{p.verdict}</span></h2>"
             f"<p style='color:{pal['text2']};margin:2px 0 10px 0'>{html.escape(p.source)} · captured "
             f"t = {(t.t_start - origin) / 1e9:.6f} s after capture start · {t.operation} · "
             f"<span class=m>{html.escape(data_hex)}</span> · status {t.status} · generated "
             f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S}</p>")
    if img_b64:
        h.append(f"<img src='data:image/png;base64,{img_b64}' style='max-width:100%;border:1px solid "
                 f"{pal['border']}'>")
    # 1 verdict
    h.append(f"<h3>1. Verdict</h3><table {tbl}><tr><th {th}>Check</th><th {th}>Result</th><th {th}>Evidence</th></tr>")
    for c in p.checks:
        h.append(f"<tr><td>{html.escape(c.name)}</td><td>{_mark(c.ok, pal)}</td><td>{html.escape(c.detail)}</td></tr>")
    h.append("</table>")
    # 2 master
    m = p.master
    h.append("<h3>2. Which master drove it</h3>")
    if m.get("report"):
        r = m["report"]
        b0 = "—" if r["b0"] < 0 else f"0x{r['b0']:02X}"
        h.append(f"<p><b>{html.escape(m['name'])}</b> — {html.escape(m['why'])}.</p><table {tbl}><tr>"
                 + "".join(f"<th {th}>{k}</th>" for k in ("report seq", "controller time", "address", "bytes written",
                                                         "bytes read", "first byte", "result", "attempts",
                                                         "report vs bus START"))
                 + f"</tr><tr><td>{r['seq']}</td><td>{r['t_us']:,} µs</td><td>0x{r['addr']:02X}</td><td>{r['wlen']}</td>"
                 f"<td>{r['rlen']}</td><td>{b0}</td>"
                 f"<td>{r['result']}</td><td>{r['attempts']} (arbitration lost {r['arb_lost']}×)</td>"
                 f"<td>{r['dt_ns'] / 1000:+.1f} µs</td></tr></table><p style='color:{pal['text2']}'>Clock alignment: "
                 f"{html.escape(r['clock'])}. A passive analyzer cannot see which master drives the wires; the packet is "
                 "named only because that master reported this exact transfer on its own USB port.</p>")
    else:
        h.append(f"<p><b>{html.escape(str(m.get('name')))}</b> — {html.escape(str(m.get('why', '')))}</p>")
    # 3 bits
    h.append(f"<h3>3. Bit by bit (SDA sampled on every SCL rising edge)</h3>")
    if p.bits:
        h.append(f"<table {tbl}><tr>" + "".join(f"<th {th}>{x}</th>" for x in (
            "#", "SCL ↑ at (from START)", "SDA sampled", "decoded bit", "role", "byte", "SDA driven by")) + "</tr>")
        for k, b in enumerate(p.bits):
            ok = b["sda"] == b["decoded"]
            colr = pal["green"] if b["kind"] == "ack" and b["decoded"] == 0 else (
                pal["red"] if b["kind"] == "ack" else pal["text"])
            first = b["role"] in ("A6", "D7", "X7")
            h.append(f"<tr{' style=border-top:2px solid ' + pal['text2'] if first else ''}><td>{k + 1}</td>"
                     f"<td class=m>{(b['t'] - t.t_start) / 1000:9.3f} µs</td><td class=m>{b['sda']}</td>"
                     f"<td class=m style='color:{pal['green'] if ok else pal['red']}'>{b['decoded']}</td>"
                     f"<td style='color:{colr}'><b>{b['role']}</b></td><td class=m>0x{b['byte']:02X}</td>"
                     f"<td>{b['driver']}</td></tr>")
        h.append("</table>")
    else:
        h.append(f"<p style='color:{pal['amber']}'>Raw edges UNAVAILABLE: {html.escape(p.raw_reason)}. The bytes "
                 "below are the analyzer's own decode.</p>")
    # 4 conditions
    if p.conditions:
        h.append(f"<h3>4. START / STOP conditions</h3><table {tbl}><tr><th {th}>Condition</th><th {th}>at</th>"
                 f"<th {th}>Check</th><th {th}>Evidence</th></tr>")
        for c in p.conditions:
            h.append(f"<tr><td>{c['kind']}</td><td class=m>{(c['t'] - t.t_start) / 1000:.3f} µs</td><td>"
                     f"{_mark(c['ok'], pal)}</td><td>{html.escape(c['detail'])}</td></tr>")
        h.append("</table>")
    # 5 timing
    if p.timing:
        tm = p.timing
        h.append(f"<h3>5. Timing measured on the edges vs NXP UM10204 {html.escape(p.spec_mode)}</h3>"
                 f"<p>f_SCL {tm.get('f_scl_hz', 0) / 1e3:.2f} kHz · SCL HIGH mean {_us(tm.get('t_high_mean'))} "
                 f"(min {_us(tm.get('t_high_min'))}) · SCL LOW mean {_us(tm.get('t_low_mean'))} (min "
                 f"{_us(tm.get('t_low_min'))}) · capture resolution ±{p.resolution_ns:g} ns"
                 + (f" · t_BUF from {tm.get('t_buf_source')}" if tm.get('t_buf_source') else "") + "</p>")
        h.append(f"<table {tbl}><tr><th {th}>Parameter</th><th {th}>Result</th><th {th}>Measured vs limit</th></tr>")
        for c in p.spec:
            h.append(f"<tr><td>{html.escape(c.name)}</td><td>{_mark(c.ok, pal)}</td><td>{html.escape(c.detail)}</td></tr>")
        h.append(f"</table><p style='color:{pal['text2']}'>Digital timing only: the analyzer samples logic levels, so "
                 "these are threshold-crossing intervals (±1 sample), not analog rise / fall times.</p>")
    # 6 ACK
    h.append(f"<h3>6. ACK / NACK</h3><table {tbl}><tr><th {th}>Byte</th><th {th}>9th clock</th><th {th}>Driven by</th>"
             f"<th {th}>Meaning</th></tr>")
    for a in p.acks:
        col = pal["green"] if a["ack"] else pal["red"]
        h.append(f"<tr><td class=m>{html.escape(a['byte'])}</td><td style='color:{col}'><b>"
                 f"{'ACK (SDA=0)' if a['ack'] else 'NACK (SDA=1)'}</b></td><td>{a['driver']}</td>"
                 f"<td>{html.escape(a['meaning'])}</td></tr>")
    h.append("</table>")
    # 7 meaning
    h.append("<h3>7. What the bytes mean</h3><pre style='font-family:Consolas,monospace;font-size:13px'>"
             + html.escape("\n".join(p.meaning)) + "</pre>")
    # 8 integrity + method
    h.append(f"<h3>8. Capture integrity</h3><table {tbl}>")
    for c in p.integrity:
        h.append(f"<tr><td>{html.escape(c.name)}</td><td>{_mark(c.ok, pal)}</td><td>{html.escape(c.detail)}</td></tr>")
    h.append("</table>")
    h.append(f"<h3>9. How this was proven</h3><ol style='color:{pal['text2']}'>"
             "<li>The analyzer Pico samples SDA and SCL and timestamps every change with its sample clock.</li>"
             "<li>The edges of this packet are taken from that record (nothing interpolated).</li>"
             "<li>A fresh decoder on the PC re-decodes them by the UM10204 rules (START = SDA↓ while SCL=1, bit = SDA "
             "on SCL↑, 9th bit = ACK/NACK, STOP = SDA↑ while SCL=1) and the result is compared with the packet as "
             "stored (on the RP2040 analyzer: decoded by the Pico firmware itself).</li>"
             "<li>Timing is measured between captured edges and compared with the UM10204 minimum values.</li>"
             "<li>The master is named only from that master's own transfer report.</li></ol>")
    if standalone:
        h.append("</body></html>")
    return "".join(h)


class _WavePlot(pg.PlotWidget):
    """SDA / SCL of one packet with every sampled bit labelled (role + value) and START / STOP marked."""

    def __init__(self, p):
        super().__init__()
        self.setBackground(C["plot_deep"])
        self.setMinimumHeight(250)
        self.setMouseEnabled(x=True, y=False)
        self.getPlotItem().hideAxis("left")
        self.getPlotItem().setLabel("bottom", "time from START", units="s")
        t0 = p.txn.t_start
        if not p.samples:
            ti = pg.TextItem("Raw SDA/SCL edges unavailable for this packet", color=C["amber"])
            self.addItem(ti)
            return
        xs, sda, scl = [], [], []
        prev = None
        for t, s in p.samples:
            x = (t - t0) / 1e9
            if prev is not None:
                xs.append(x); sda.append(prev & 1); scl.append((prev >> 1) & 1)
            xs.append(x); sda.append(s & 1); scl.append((s >> 1) & 1)
            prev = s
        self.plot(xs, [2.2 + 0.8 * v for v in scl], pen=pg.mkPen(C["scl"], width=2))
        self.plot(xs, [0.6 + 0.8 * v for v in sda], pen=pg.mkPen(C["sda"], width=2))
        for y, name, col in ((2.6, "SCL", C["scl"]), (1.0, "SDA", C["sda"])):
            ti = pg.TextItem(name, color=col, anchor=(0, 0.5))
            ti.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            ti.setPos(xs[0], y)
            self.addItem(ti)
        f = QFont("Consolas", 8)
        for b in p.bits:
            x = (b["t"] - t0) / 1e9
            self.addItem(pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen(C["border"], width=1,
                                                                         style=Qt.PenStyle.DotLine)))
            col = C["ack"] if b["kind"] == "ack" and b["decoded"] == 0 else C["nack"] if b["kind"] == "ack" else C["text"]
            dense = len(p.bits) > 60                 # long packet: values only (roles are in the table; zoom with the wheel)
            ti = pg.TextItem(f"{b['sda']}" if dense else f"{b['role']}\n{b['sda']}", color=col, anchor=(0.5, 1))
            ti.setFont(f)
            ti.setPos(x, 0.5)
            self.addItem(ti)
        bytes_seen = {}
        for b in p.bits:
            bytes_seen.setdefault(b["byte_index"], []).append(b)
        for _bi, bs in bytes_seen.items():
            x = ((bs[0]["t"] + bs[min(7, len(bs) - 1)]["t"]) / 2 - t0) / 1e9
            kind = bs[0]["kind"]
            txt = (f"ADDR 0x{bs[0]['byte'] >> 1:02X} {'R' if bs[0]['byte'] & 1 else 'W'}" if kind == "addr"
                   else f"0x{bs[0]['byte']:02X}")
            ti = pg.TextItem(txt, color=C["blue"] if kind == "addr" else C["cyan"], anchor=(0.5, 1))
            ti.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            ti.setPos(x, 3.45)
            self.addItem(ti)
        for c in p.conditions:
            x = (c["t"] - t0) / 1e9
            self.addItem(pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen(C["amber"], width=2)))
            ti = pg.TextItem({"START": "S", "repeated START": "Sr", "STOP": "P"}[c["kind"]], color=C["amber"],
                             anchor=(0.5, 1))
            ti.setPos(x, 3.75)
            self.addItem(ti)
        self.setYRange(-0.6, 3.9, padding=0)
        self.setXRange(xs[0], xs[-1], padding=0.01)


class PacketProofDialog(QDialog):
    def __init__(self, engine, txn, parent=None, on_newest=None):
        super().__init__(parent)
        self.engine, self.txn = engine, txn
        self.proof = build_proof(engine, txn)
        p = self.proof
        self.setWindowTitle(f"Packet Proof — packet #{txn.id}")
        self.resize(1320, 920)
        lay = QVBoxLayout(self)
        vcol = {"PROVEN": C["green"], "PARTIAL": C["amber"], "FAIL": C["red"]}[p.verdict]
        passed = sum(1 for c in p.checks if c.ok is True)
        self.head = QLabel(f"<span style='font-size:15pt'><b>Packet #{txn.id}</b> · {html.escape(txn.operation)} · "
                           f"<b style='color:{vcol}'>{p.verdict}</b></span>  <span style='color:{C['text2']}'>"
                           f"{passed}/{len(p.checks)} checks passed · {html.escape(p.source)}</span>")
        lay.addWidget(self.head)
        split = QSplitter(Qt.Orientation.Vertical)
        self.plot = _WavePlot(p)
        split.addWidget(self.plot)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        self.text.setHtml(proof_html(p, engine))
        split.addWidget(self.text)
        split.setSizes([300, 620])
        lay.addWidget(split, 1)
        row = QHBoxLayout()
        self.btn_save = QPushButton("Save proof report (HTML)…")
        self.btn_vcd = QPushButton("Export edges VCD…")
        self.btn_csv = QPushButton("Export edges CSV…")
        self.btn_copy = QPushButton("Copy summary")
        close = QPushButton("Close")
        for b in (self.btn_vcd, self.btn_csv):
            b.setEnabled(bool(p.samples))
            b.setToolTip("The captured SDA/SCL edges of this packet; open the VCD in PulseView with the I2C decoder for "
                         "an independent reference decode" if p.samples else "raw edges unavailable")
        for b in (self.btn_save, self.btn_vcd, self.btn_csv, self.btn_copy):
            row.addWidget(b)
        self.btn_newest = None
        if not p.raw_available and on_newest is not None:
            self.btn_newest = QPushButton("Prove the newest packet of this device instead")
            self.btn_newest.setObjectName("primary")
            self.btn_newest.clicked.connect(lambda: on_newest(txn))
            row.addWidget(self.btn_newest)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.btn_save.clicked.connect(self.save_report)
        self.btn_vcd.clicked.connect(lambda: self._export("vcd"))
        self.btn_csv.clicked.connect(lambda: self._export("csv"))
        self.btn_copy.clicked.connect(self._copy)
        close.clicked.connect(self.close)

    def summary(self):
        p = self.proof
        lines = [f"Packet #{self.txn.id}: {p.verdict}"] + [
            f"  [{'PASS' if c.ok else 'FAIL' if c.ok is False else 'n/a'}] {c.name}: {c.detail}" for c in p.checks]
        return "\n".join(lines)

    def _copy(self):
        QGuiApplication.clipboard().setText(self.summary())

    def report_html(self):
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        self.plot.grab().save(buf, "PNG")
        return proof_html(self.proof, self.engine, pal=LIGHT, img_b64=base64.b64encode(bytes(ba)).decode(),
                          standalone=True)

    def save_report(self, path=None):
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save packet proof", f"packet_proof_{self.txn.id}.html",
                                                  "HTML (*.html)")
        if not path:
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.report_html())
        return path

    def _export(self, kind, path=None):
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Export captured edges", f"packet_{self.txn.id}.{kind}",
                                                  "VCD (*.vcd)" if kind == "vcd" else "CSV (*.csv)")
        if not path:
            return None
        try:
            if kind == "vcd":
                write_vcd(path, self.proof.samples, self.engine.tick_ns, f"packet #{self.txn.id}", self.proof.source)
            else:
                write_edges_csv(path, self.proof.samples)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return None
        return path


class CrossCheckDialog(QDialog):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.reference_result = None
        self.setWindowTitle("Cross-check: capture vs known transactions and a reference analyzer")
        self.resize(1100, 760)
        lay = QVBoxLayout(self)
        self.text = QTextBrowser()
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.btn_ref = QPushButton("Import reference analyzer decode…")
        self.btn_ref.setToolTip("sigrok-cli / PulseView I2C annotation text, or a Saleae Logic 1 / 2 I2C analyzer CSV")
        self.btn_refresh = QPushButton("Refresh")
        close = QPushButton("Close")
        row.addWidget(self.btn_ref); row.addWidget(self.btn_refresh); row.addStretch(1); row.addWidget(close)
        lay.addLayout(row)
        self.btn_ref.clicked.connect(lambda: self.load_reference())
        self.btn_refresh.clicked.connect(self.render)
        close.clicked.connect(self.close)
        self.render()

    def load_reference(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Reference analyzer I2C decode", "",
                                                  "Decode exports (*.txt *.csv *.log);;All files (*)")
        if not path:
            return None
        try:
            fmt, ref = parse_reference(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Cannot read reference", str(e))
            return None
        self.reference_result = (path, fmt, compare_with_reference(self.engine, ref))
        self.render()
        return self.reference_result

    def render(self):
        e = self.engine
        pal = dict(green=C["green"], red=C["red"], amber=C["amber"], head=C["card_hi"], border=C["border"])
        tbl = f"style='border-collapse:collapse' cellpadding=5 border=1 bordercolor='{C['border']}'"
        th = f"style='background:{C['card_hi']};text-align:left'"
        h = ["<h2>Cross-check of this capture</h2>"]
        rows, unknown = known_transactions(e)
        h.append(f"<h3>1. Known transactions: each master's own transfer log vs the bus</h3><table {tbl}><tr>"
                 + "".join(f"<th {th}>{x}</th>" for x in ("Master", "clock aligned", "reported by the master",
                                                         "found on the bus", "not found", "still arriving",
                                                         "before capture", "match"))
                 + "</tr>")
        for r in rows:
            pct = "—" if r["match_pct"] is None else f"{r['match_pct']:.2f} %"
            ok = r["match_pct"] is not None and r["missing"] == 0
            h.append(f"<tr><td>{html.escape(r['master'])}</td><td>{'yes' if r['clock_locked'] else 'no'}</td>"
                     f"<td>{r['reported']:,}</td><td>{r['matched']:,}</td><td>{r['missing']:,}</td><td>{r['settling']:,}"
                     f"</td><td>{r['before_capture']:,}</td><td>{_mark(ok if r['match_pct'] is not None else None, pal)} "
                     f"{pct}</td></tr>")
        h.append(f"</table><p>Bus packets no master reported (shown as master UNKNOWN): <b>{unknown:,}</b> of "
                 f"{len(e.packets):,}.</p>")
        d = decoder_agreement(e)
        h.append("<h3>2. Two independent decoders</h3>")
        if d["applicable"]:
            tot = d["agree"] + d["disagree"]
            h.append(f"<p>Pico firmware decode vs PC decode of the raw edge stream: <b>{d['agree']:,}</b> packets "
                     f"identical, <b>{d['disagree']}</b> different ({_mark(d['disagree'] == 0 and tot > 0, pal)}).</p>")
        else:
            h.append("<p>This source has a single decoder (the PC decodes raw edges); open a packet in Packet Proof "
                     "to re-decode its edges independently.</p>")
        k = known_content(e)
        h.append(f"<h3>3. Known device content</h3><table {tbl}><tr><th {th}>Check</th><th {th}>Result</th></tr>"
                 f"<tr><td>0x76 chip-ID register 0xD0 reads 0x60 (BME280) or 0x58 (BMP280) — fitted chip: "
                 f"<b>{html.escape(k['chip'])}</b> {html.escape(str(k['chip_ids']))}</td>"
                 f"<td>{k['chip_id_ok']}/{k['chip_id_reads']} "
                 f"{_mark(k['chip_id_ok'] == k['chip_id_reads'] if k['chip_id_reads'] else None, pal)}</td></tr>"
                 f"<tr><td>DS3231 time registers 0x00-0x06 are valid BCD</td><td>{k['rtc_bcd_ok']}/{k['rtc_reads']} "
                 f"{_mark(k['rtc_bcd_ok'] == k['rtc_reads'] if k['rtc_reads'] else None, pal)}</td></tr></table>")
        h.append("<h3>4. Reference logic analyzer</h3>")
        if self.reference_result:
            path, fmt, r = self.reference_result
            h.append(f"<p>{html.escape(fmt)}: <code>{html.escape(path)}</code><br>{r['reference']} reference "
                     f"transactions, <b>{r['matched']}</b> identical in this capture (aligned from packet "
                     f"#{r['start_packet']}) — {_mark(r['matched'] == r['reference'] and r['reference'] > 0, pal)} "
                     f"{r.get('match_pct', 0):.1f} %</p>")
            if r["mismatches"]:
                h.append(f"<table {tbl}><tr><th {th}>difference</th><th {th}>reference</th><th {th}>this capture</th>"
                         "</tr>" + "".join(f"<tr><td>{m['op']}</td><td><code>{html.escape(m['reference'])}</code></td>"
                                           f"<td><code>{html.escape(m['ours'])}</code></td></tr>"
                                           for m in r["mismatches"]) + "</table>")
        else:
            h.append("<p>No reference imported. Capture the same bus with a reference logic analyzer (or export this "
                     "capture's edges as VCD from the Digital Waveform / Packet Proof and decode them in PulseView with "
                     "its I2C decoder), export the I2C decode (sigrok-cli <code>-P i2c -A i2c</code> text, PulseView "
                     "annotations, or a Saleae Logic CSV) and import it here.</p>")
        self.text.setHtml("".join(h))
