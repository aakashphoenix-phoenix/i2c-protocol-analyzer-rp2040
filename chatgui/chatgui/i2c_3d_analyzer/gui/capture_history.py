"""Capture History page — inspect, filter, manage, replay, and export captures."""
from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import os
import re

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QBrush, QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config
from config import COLORS as C
from gui.widgets import Card, FlowLayout, fit_table, label, mono_columns
from storage.capture_storage import (
    export_packets_csv,
    export_packets_json,
    export_session_excel,
    export_statistics_csv,
    load_excel_preview,
    preview_capture_file,
    preview_packets_from_engine,
)

PREVIEW_COLUMNS = [
    "Packet #",
    "Time (s)",
    "Addr",
    "Device",
    "Op",
    "Data (hex)",
    "ACK/NACK",
    "Master",
    "Status",
]
PREVIEW_MONO_COLS = (0, 1, 2, 5, 6)
PREVIEW_RIGHT_COLS = (0, 1)
SUPPORTED_EXTS = (".i2ccap", ".xlsx", ".csv")
FILTER_CATEGORIES = ("All", "Live", "Demo", "Exports")


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 10240:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024:.0f} KB"


def _read_i2ccap_meta(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            first = f.readline()
        data = json.loads(first).get("capture")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class CaptureHistoryPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._external_files: list[str] = []
        self._file_entries: list[dict] = []
        self._preview_state: dict = {"kind": "none"}
        self._current_preview_path: str | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        # ---------------------------------------------------------------- Session card
        act = Card("Session")
        r = FlowLayout(hspace=8, vspace=6)
        self.b_save = QPushButton("Save session…")
        self.b_save.setObjectName("primary")
        self.b_open = QPushButton("Open capture file…")
        self.b_preview_live = QPushButton("Preview current session")
        self.b_preview_live.setToolTip("Inspect the current in-memory capture without saving to disk")
        self.b_csv = QPushButton("Export packets CSV")
        self.b_json = QPushButton("Export packets JSON")
        self.b_excel = QPushButton("Export Excel (.xlsx)")
        self.b_excel.setToolTip("Export Summary, Packets, Devices, and Masters & Arbitration sheets to Excel")
        self.b_stats = QPushButton("Export performance statistics")
        self.b_report = QPushButton("Export summary report")
        self.b_report.setToolTip("HTML or Markdown summary of the current session (verified values only)")
        for b in (
            self.b_save,
            self.b_open,
            self.b_preview_live,
            self.b_csv,
            self.b_json,
            self.b_excel,
            self.b_stats,
            self.b_report,
        ):
            r.addWidget(b)
        act.add(r)

        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        self.session_lbl = label("In-memory session: idle", "cardHint", wrap=True)
        status_row.addWidget(self.session_lbl, 1)
        self.replay_progress = QProgressBar()
        self.replay_progress.setRange(0, 100)
        self.replay_progress.setValue(0)
        self.replay_progress.setFixedWidth(180)
        self.replay_progress.setFixedHeight(16)
        self.replay_progress.setStyleSheet(self._progress_css())
        self.replay_lbl = label("Replay idle", "cardHint", mono=True)
        status_row.addWidget(self.replay_progress)
        status_row.addWidget(self.replay_lbl)
        act.add(status_row)
        lay.addWidget(act)

        # ---------------------------------------------------------------- Main horizontal split
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(10)

        # ---- Left: Saved captures list & file management
        lc = Card("Saved captures", "")
        self.folder_lbl = label("", "cardHint", wrap=True)
        lc.add(self.folder_lbl)

        filter_bar = FlowLayout(hspace=6, vspace=6)
        self.filter_pills: dict[str, QPushButton] = {}
        for cat in FILTER_CATEGORIES:
            pill = QPushButton(cat)
            pill.setObjectName("pill")
            pill.setCheckable(True)
            pill.setChecked(cat == "All")
            pill.toggled.connect(lambda checked, c=cat: self._on_filter_pill(c, checked))
            filter_bar.addWidget(pill)
            self.filter_pills[cat] = pill
        self.file_filter = QComboBox()
        self.file_filter.addItems(list(FILTER_CATEGORIES))
        self.file_filter.setToolTip("Filter saved captures and exports by source/type")
        filter_bar.addWidget(self.file_filter)
        lc.add(filter_bar)

        self.file_search = QLineEdit()
        self.file_search.setPlaceholderText("Search files by name, type, source…")
        self.file_search.setClearButtonEnabled(True)
        lc.add(self.file_search)

        self.list = QListWidget()
        self.list.setStyleSheet(self._list_css())
        lc.add(self.list, 1)

        rr = FlowLayout(hspace=6, vspace=6)
        self.b_ref = QPushButton("Refresh list")
        self.b_folder = QPushButton("Open folder")
        self.b_open_ext = QPushButton("Open externally")
        self.b_rename = QPushButton("Rename")
        self.b_delete = QPushButton("Delete")
        self.b_delete.setObjectName("danger")
        for w in (self.b_ref, self.b_folder, self.b_open_ext, self.b_rename, self.b_delete):
            rr.addWidget(w)
        lc.add(rr)
        split.addWidget(lc)

        # ---- Right: Vertical split of Metadata/Playback and In-Tab Data Preview
        right_split = QSplitter(Qt.Orientation.Vertical)
        right_split.setHandleWidth(10)
        right_split.setChildrenCollapsible(False)

        rc = Card("Capture metadata & playback")
        meta_hdr = QHBoxLayout()
        meta_hdr.setSpacing(8)
        self.meta_hint = label("Formatted capture summary (decoded read-only)", "cardHint")
        meta_hdr.addWidget(self.meta_hint, 1)
        self.chk_raw_meta = QCheckBox("Raw JSON metadata")
        meta_hdr.addWidget(self.chk_raw_meta)
        rc.add(meta_hdr)

        self.meta = QTextEdit()
        self.meta.setReadOnly(True)
        self.meta.setMinimumHeight(120)
        rc.add(self.meta, 1)

        pr = FlowLayout(hspace=8, vspace=6)
        self.speed = QComboBox()
        self.speed.addItems(["Instant (decode whole file)", "1× real time", "5×", "20×"])
        self.b_play = QPushButton("▶ Load && play")
        self.b_play.setObjectName("primary")
        self.b_pause = QPushButton("Pause")
        self.b_pause.setCheckable(True)
        self.b_stop = QPushButton("Stop")
        self.stay_on_page = QCheckBox("Stay on Capture History")
        self.stay_on_page.setToolTip("Keep Capture History open instead of jumping to Live Dashboard on Load & play")
        for w in (label("Playback", "cardHint"), self.speed, self.b_play, self.b_pause, self.b_stop, self.stay_on_page):
            pr.addWidget(w)
        rc.add(pr)
        rc.add(
            label(
                "Playback is recorded data, not live traffic: the GUI shows a RECORDED CAPTURE banner, and the "
                "original hardware timestamps are re-decoded unchanged.",
                "cardHint",
                wrap=True,
            )
        )
        right_split.addWidget(rc)

        # ---- In-tab preview card
        pc = Card("Captured data preview")
        self.preview_source_lbl = label("Select a saved file or preview the current session", "cardHint", wrap=True)
        pc.add(self.preview_source_lbl)

        pbar = FlowLayout(hspace=8, vspace=6)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setToolTip("Select worksheet inside the Excel workbook")
        self.sheet_combo.hide()
        self.preview_addr = QComboBox()
        self.preview_addr.addItem("All addresses", None)
        self.preview_status = QComboBox()
        self.preview_status.addItems(["All statuses", "Complete", "Incomplete", "Malformed", "Capture gap", "Has NACK"])
        self.preview_search = QLineEdit()
        self.preview_search.setPlaceholderText("Filter preview: addr, hex, device, master, status…")
        self.preview_search.setClearButtonEnabled(True)
        self.preview_search.setMinimumWidth(220)
        for w in (self.sheet_combo, self.preview_addr, self.preview_status, self.preview_search):
            pbar.addWidget(w)
        pc.add(pbar)

        self.preview_table = QTableWidget(0, len(PREVIEW_COLUMNS))
        self.preview_table.setHorizontalHeaderLabels(PREVIEW_COLUMNS)
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setShowGrid(False)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.verticalHeader().setDefaultSectionSize(25)
        self.preview_table.setMinimumHeight(210)
        self._apply_packet_table_columns()
        pc.add(self.preview_table, 1)

        self.preview_count_lbl = label("", "cardHint")
        pc.add(self.preview_count_lbl)
        right_split.addWidget(pc)
        right_split.setSizes([290, 390])

        split.addWidget(right_split)
        split.setSizes([430, 950])
        lay.addWidget(split, 1)

        # ---------------------------------------------------------------- Signals
        self.b_save.clicked.connect(self._save_session)
        self.b_open.clicked.connect(self._open)
        self.b_preview_live.clicked.connect(self._preview_current_session)
        self.b_csv.clicked.connect(lambda: self._export("csv"))
        self.b_json.clicked.connect(lambda: self._export("json"))
        self.b_excel.clicked.connect(lambda: self._export("xlsx"))
        self.b_stats.clicked.connect(lambda: self._export("stats"))
        self.b_report.clicked.connect(ctx.export_report)

        self.file_filter.currentTextChanged.connect(self._on_filter_combo)
        self.file_search.textChanged.connect(lambda _t: self._populate_file_list())
        self.b_ref.clicked.connect(self.scan)
        self.b_folder.clicked.connect(self._open_folder)
        self.b_open_ext.clicked.connect(self._open_external)
        self.b_rename.clicked.connect(self._rename_selected)
        self.b_delete.clicked.connect(self._delete_selected)

        self.list.currentItemChanged.connect(self._show_meta)
        self.list.itemDoubleClicked.connect(lambda _it: self._on_item_activated())
        self.chk_raw_meta.toggled.connect(lambda _on: self._render_current_meta())
        self.b_play.clicked.connect(self._play)
        self.b_pause.toggled.connect(ctx.pause_replay)
        self.b_stop.clicked.connect(ctx.stop_replay)

        self.sheet_combo.currentIndexChanged.connect(lambda _idx: self._render_preview_table())
        self.preview_addr.currentIndexChanged.connect(lambda _idx: self._render_preview_table())
        self.preview_status.currentIndexChanged.connect(lambda _idx: self._render_preview_table())
        self.preview_search.textChanged.connect(lambda _t: self._render_preview_table())

        self.scan()
        self.refresh(getattr(ctx, "engine", None))

    # ---------------------------------------------------------------- Styles & theme
    def _list_css(self) -> str:
        return (
            f"QListWidget{{background:{C['table_bg']};border:1px solid {C['border']};border-radius:10px;}}"
            f"QListWidget::item{{padding:8px;border-bottom:1px solid {C['border']};}}"
            f"QListWidget::item:hover{{background:{C['hover']};}}"
            f"QListWidget::item:selected{{background:{C['table_sel']};color:{C['text']};}}"
        )

    def _progress_css(self) -> str:
        return (
            f"QProgressBar{{background:{C['input_bg']};border:1px solid {C['border']};border-radius:6px;"
            f"text-align:center;color:{C['text']};font-size:11px;}}"
            f"QProgressBar::chunk{{background:{C['cyan']};border-radius:5px;}}"
        )

    def retheme(self):
        """Re-apply live palette colors to inline stylesheets, HTML metadata, and table cell colors."""
        self.list.setStyleSheet(self._list_css())
        self.replay_progress.setStyleSheet(self._progress_css())
        self._render_current_meta()
        self._render_preview_table()

    # ---------------------------------------------------------------- File scanning & filtering
    def _on_filter_pill(self, cat: str, checked: bool):
        if not checked:
            if self.file_filter.currentText() == cat:
                pill = self.filter_pills[cat]
                pill.blockSignals(True)
                pill.setChecked(True)
                pill.blockSignals(False)
            return
        self._sync_filter_pills(cat)
        if self.file_filter.currentText() != cat:
            self.file_filter.setCurrentText(cat)
        else:
            self._populate_file_list()

    def _on_filter_combo(self, cat: str):
        self._sync_filter_pills(cat)
        self._populate_file_list()

    def _sync_filter_pills(self, active: str):
        for cat, pill in self.filter_pills.items():
            want = cat == active
            if pill.isChecked() != want:
                pill.blockSignals(True)
                pill.setChecked(want)
                pill.blockSignals(False)

    def _build_entry(self, path: str) -> dict | None:
        if not os.path.isfile(path):
            return None
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED_EXTS:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        meta = _read_i2ccap_meta(path) if ext == ".i2ccap" else {}
        if ext == ".i2ccap":
            is_demo = bool(meta.get("simulated") or meta.get("source") == "demo")
            category = "Demo" if is_demo else "Live"
            badge = "[DEMO · .i2ccap]" if is_demo else "[LIVE · .i2ccap]"
        elif ext == ".xlsx":
            category = "Exports"
            badge = "[EXCEL · .xlsx]"
        else:
            category = "Exports"
            badge = "[CSV · .csv]"
        return {
            "path": os.path.normpath(path),
            "name": os.path.basename(path),
            "ext": ext,
            "category": category,
            "badge": badge,
            "size": st.st_size,
            "size_str": _fmt_size(st.st_size),
            "mtime": st.st_mtime,
            "time_str": _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "meta": meta,
        }

    def _make_file_item(self, entry: dict) -> QListWidgetItem:
        it = QListWidgetItem(f"{entry['badge']}  {entry['name']}\n{entry['time_str']}  ·  {entry['size_str']}")
        it.setData(Qt.ItemDataRole.UserRole, entry["path"])
        it.setData(Qt.ItemDataRole.UserRole + 1, entry)
        it.setToolTip(entry["path"])
        return it

    def scan(self):
        folder = self.ctx.captures_dir()
        self.folder_lbl.setText(f"Folder: {folder}")
        paths_seen: set[str] = set()
        entries: list[dict] = []

        if os.path.isdir(folder):
            for fname in os.listdir(folder):
                p = os.path.normpath(os.path.join(folder, fname))
                if os.path.splitext(p)[1].lower() in SUPPORTED_EXTS and p not in paths_seen:
                    entry = self._build_entry(p)
                    if entry is not None:
                        entries.append(entry)
                        paths_seen.add(p)

        live_external = []
        for p in self._external_files:
            np = os.path.normpath(p)
            if np not in paths_seen:
                entry = self._build_entry(np)
                if entry is not None:
                    entries.append(entry)
                    paths_seen.add(np)
                    live_external.append(np)
            elif os.path.isfile(np):
                live_external.append(np)
        self._external_files = live_external

        entries.sort(key=lambda e: e["mtime"], reverse=True)
        self._file_entries = entries
        self._populate_file_list()

    def _populate_file_list(self):
        prev_path = self._current_preview_path
        if prev_path is None and self.list.currentItem() is not None:
            prev_path = self.list.currentItem().data(Qt.ItemDataRole.UserRole)

        cat = self.file_filter.currentText()
        q = self.file_search.text().strip().lower()

        self.list.blockSignals(True)
        self.list.clear()
        reselect_row = None
        for entry in self._file_entries:
            if cat != "All" and entry["category"] != cat:
                continue
            if q:
                hay = f"{entry['name']} {entry['badge']} {entry['category']} {entry['ext']} {entry['time_str']}".lower()
                if q not in hay:
                    continue
            it = self._make_file_item(entry)
            self.list.addItem(it)
            if prev_path and os.path.normpath(prev_path) == entry["path"]:
                reselect_row = self.list.count() - 1
        self.list.blockSignals(False)

        if reselect_row is not None:
            self.list.setCurrentRow(reselect_row)
            self._show_meta(self.list.currentItem())
        elif self.list.count() == 0 and self._preview_state.get("kind") != "live":
            self._preview_state = {"kind": "none"}
            self._current_preview_path = None
            self._render_current_meta()
            self._clear_preview_table("No matching files in list.")

    # ---------------------------------------------------------------- Session / file actions
    def _save_session(self):
        self.ctx.save_capture()
        self.scan()

    def _open(self):
        p, _ = QFileDialog.getOpenFileName(
            self,
            "Open capture",
            self.ctx.captures_dir(),
            "Supported files (*.i2ccap *.xlsx *.csv);;I²C capture (*.i2ccap);;Excel workbook (*.xlsx);;CSV (*.csv)",
        )
        if not p:
            return
        np = os.path.normpath(p)
        entry = self._build_entry(np)
        if entry is None:
            self.ctx.toast("Unsupported or unreadable file")
            return
        if np not in self._external_files:
            self._external_files.insert(0, np)
        if self.file_filter.currentText() not in ("All", entry["category"]):
            self.file_filter.setCurrentText("All")
        if self.file_search.text().strip():
            self.file_search.clear()
        self._current_preview_path = np
        self.scan()
        for row in range(self.list.count()):
            it = self.list.item(row)
            if os.path.normpath(it.data(Qt.ItemDataRole.UserRole)) == np:
                self.list.setCurrentRow(row)
                self._show_meta(it)
                break

    def _on_item_activated(self):
        it = self.list.currentItem()
        if it is None:
            return
        p = it.data(Qt.ItemDataRole.UserRole)
        if str(p).lower().endswith(".i2ccap"):
            self._play()
        else:
            self._open_external()

    def _open_folder(self):
        it = self.list.currentItem()
        target = self.ctx.captures_dir()
        if it is not None:
            p = it.data(Qt.ItemDataRole.UserRole)
            if p and os.path.exists(os.path.dirname(p)):
                target = os.path.dirname(p)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _open_external(self):
        it = self.list.currentItem()
        if it is None:
            self.ctx.toast("Select a file first")
            return
        p = it.data(Qt.ItemDataRole.UserRole)
        if not p or not os.path.exists(p):
            self.ctx.toast("Selected file no longer exists")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def _rename_selected(self):
        it = self.list.currentItem()
        if it is None:
            self.ctx.toast("Select a file first")
            return
        old_path = it.data(Qt.ItemDataRole.UserRole)
        if not old_path or not os.path.isfile(old_path):
            self.ctx.toast("Selected file no longer exists")
            return
        old_name = os.path.basename(old_path)
        old_ext = os.path.splitext(old_name)[1]
        new_name, ok = QInputDialog.getText(self, "Rename file", "New file name:", text=old_name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        if "/" in new_name or "\\" in new_name or os.path.basename(new_name) != new_name:
            QMessageBox.warning(self, "Invalid name", "Please enter a file name without folder separators.")
            return
        if not os.path.splitext(new_name)[1] and old_ext:
            new_name += old_ext
        new_path = os.path.normpath(os.path.join(os.path.dirname(old_path), new_name))
        if os.path.exists(new_path):
            QMessageBox.warning(self, "File exists", f"A file named '{new_name}' already exists.")
            return
        try:
            os.rename(old_path, new_path)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot rename", str(exc))
            return
        self._external_files = [new_path if os.path.normpath(x) == os.path.normpath(old_path) else x for x in self._external_files]
        self._current_preview_path = new_path
        self.scan()
        self.ctx.toast(f"Renamed to {new_name}")

    def _delete_selected(self):
        it = self.list.currentItem()
        if it is None:
            self.ctx.toast("Select a file first")
            return
        p = it.data(Qt.ItemDataRole.UserRole)
        if not p or not os.path.isfile(p):
            self.ctx.toast("Selected file no longer exists")
            return
        name = os.path.basename(p)
        ans = QMessageBox.question(
            self,
            "Delete file",
            f"Delete '{name}' from disk?\n\n{p}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(p)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot delete", str(exc))
            return
        np = os.path.normpath(p)
        self._external_files = [x for x in self._external_files if os.path.normpath(x) != np]
        if self._current_preview_path and os.path.normpath(self._current_preview_path) == np:
            self._current_preview_path = None
            self._preview_state = {"kind": "none"}
        self.scan()
        if self.list.count() == 0:
            self._render_current_meta()
            self._clear_preview_table("No saved files.")
        self.ctx.toast(f"Deleted {name}")

    # ---------------------------------------------------------------- Metadata & preview loading
    def _show_meta(self, it):
        if it is None:
            if self._preview_state.get("kind") != "live":
                self._preview_state = {"kind": "none"}
                self._current_preview_path = None
                self._render_current_meta()
                self._clear_preview_table("Select a file to preview.")
            return
        p = it.data(Qt.ItemDataRole.UserRole)
        entry = it.data(Qt.ItemDataRole.UserRole + 1) or self._build_entry(p) or {"path": p, "name": os.path.basename(p), "ext": os.path.splitext(p)[1].lower()}
        self._current_preview_path = p
        ext = entry.get("ext", os.path.splitext(p)[1].lower())

        if ext == ".i2ccap":
            self.b_play.setEnabled(True)
            try:
                preview = preview_capture_file(p)
            except Exception as exc:
                self._preview_state = {"kind": "error", "path": p, "error": str(exc)}
                self.meta.setPlainText(f"Cannot read: {exc}")
                self._clear_preview_table(f"Cannot preview {os.path.basename(p)}: {exc}")
                return
            self._preview_state = {"kind": "i2ccap", "path": p, "entry": entry, "data": preview}
            self._setup_packet_preview_controls(preview["packets"], f"File preview: {os.path.basename(p)}")
        elif ext == ".xlsx":
            self.b_play.setEnabled(False)
            try:
                wb_preview = load_excel_preview(p)
            except Exception as exc:
                self._preview_state = {"kind": "error", "path": p, "error": str(exc)}
                self.meta.setPlainText(f"Cannot read Excel workbook: {exc}")
                self._clear_preview_table(f"Cannot preview {os.path.basename(p)}: {exc}")
                return
            self._preview_state = {"kind": "xlsx", "path": p, "entry": entry, "data": wb_preview}
            self._setup_sheet_preview_controls(wb_preview, f"Excel preview: {os.path.basename(p)}")
        elif ext == ".csv":
            self.b_play.setEnabled(False)
            try:
                with open(p, "r", encoding="utf-8", newline="") as f:
                    reader = csv.reader(f)
                    all_rows = list(reader)
                headers = all_rows[0] if all_rows else []
                data_rows = all_rows[1:501]
                csv_preview = {"headers": headers, "rows": data_rows, "total_rows": max(0, len(all_rows) - 1)}
            except Exception as exc:
                self._preview_state = {"kind": "error", "path": p, "error": str(exc)}
                self.meta.setPlainText(f"Cannot read CSV: {exc}")
                self._clear_preview_table(f"Cannot preview {os.path.basename(p)}: {exc}")
                return
            self._preview_state = {"kind": "csv", "path": p, "entry": entry, "data": csv_preview}
            self._setup_flat_preview_controls(f"CSV preview: {os.path.basename(p)}")
        else:
            self.b_play.setEnabled(False)
            self._preview_state = {"kind": "none"}
            self._clear_preview_table("Unsupported file format.")

        self._render_current_meta()
        self._render_preview_table()

    def _preview_current_session(self):
        eng = self.ctx.engine
        if not eng.packets and not eng.raw_lines:
            self.ctx.toast("Current session has no captured packets yet")
            return
        packets = preview_packets_from_engine(eng)
        perf = eng.perf.compute()
        devices = [
            {
                "address": f"0x{a:02X}",
                "addr_int": a,
                "name": eng.registry.name(a)[0],
                "confidence": eng.registry.name(a)[1],
                "transactions": d.transactions,
                "reads": d.reads,
                "writes": d.writes,
                "bytes": d.total_bytes,
                "acks": d.acks,
                "nacks": d.nacks,
            }
            for a, d in sorted(eng.registry.devices.items())
        ]
        meta = {
            "format": "in-memory",
            "session_id": getattr(self.ctx, "session_id", "—"),
            "capture_started": eng.capture_wall_start.isoformat(timespec="seconds") if eng.capture_wall_start else None,
            "source": eng.source,
            "simulated": eng.source == "demo",
            "analyzer": {
                "firmware": eng.caps.firmware,
                "board": eng.caps.board,
                "protocol": eng.caps.proto,
                "sample_hz": eng.caps.sample_hz,
                "tick_ns": eng.caps.tick_ns,
                "caps": sorted(eng.caps.caps),
            } if eng.caps.known else {},
            "loss": {
                "lost_records": eng.lost_records,
                "fw_overflows": eng.fw_stats.get("overflows", 0),
                "fw_dropped": eng.fw_stats.get("dropped", 0),
                "transport_errors": eng.transport_errors,
                "raw_lines_truncated": len(eng.raw_lines) == eng.raw_lines.maxlen,
            },
            "summary": {
                "transactions": perf.total,
                "complete": perf.complete,
                "observed_s": perf.observed_ns / 1e9,
            },
        }
        self._current_preview_path = None
        self.list.blockSignals(True)
        self.list.setCurrentRow(-1)
        self.list.clearSelection()
        self.list.blockSignals(False)
        self._preview_state = {
            "kind": "live",
            "path": None,
            "data": {
                "meta": meta,
                "total_raw_lines": len(eng.raw_lines),
                "decoded_lines": len(eng.raw_lines),
                "packets": packets,
                "total_packets_decoded": len(eng.packets),
                "truncated": len(eng.packets) > len(packets),
                "devices": devices,
                "perf": {
                    "total": perf.total,
                    "complete": perf.complete,
                    "incomplete": perf.incomplete,
                    "malformed": perf.malformed,
                    "observed_s": perf.observed_ns / 1e9,
                    "freq_avg_hz": perf.freq_avg_hz,
                    "acks": perf.acks,
                    "nacks": perf.nacks,
                    "unexpected_nacks": perf.unexpected_nacks,
                },
            },
        }
        self._setup_packet_preview_controls(packets, f"Current in-memory session ({eng.source.upper()})")
        self._render_current_meta()
        self._render_preview_table()

    def _render_current_meta(self):
        kind = self._preview_state.get("kind")
        if kind == "none":
            self.meta.setHtml(f"<p style='color:{C['text2']}'>Select a saved capture or export on the left, or click "
                              f"<b>Preview current session</b>.</p>")
            return
        if kind == "error":
            self.meta.setPlainText(f"Cannot read: {self._preview_state.get('error', 'unknown error')}")
            return
        if kind in ("i2ccap", "live"):
            self.meta.setHtml(self._format_capture_meta_html(self._preview_state))
            return
        if kind == "xlsx":
            self.meta.setHtml(self._format_xlsx_meta_html(self._preview_state))
            return
        if kind == "csv":
            self.meta.setHtml(self._format_csv_meta_html(self._preview_state))

    def _format_capture_meta_html(self, state: dict) -> str:
        d = state["data"]
        meta = d.get("meta") or {}
        entry = state.get("entry") or {}
        is_demo = bool(meta.get("simulated") or meta.get("source") == "demo")
        src = str(meta.get("source") or ("demo" if is_demo else "unknown")).upper()
        if is_demo:
            badge_bg, badge_fg, badge_bd = C["demo_bg"], C["demo_text"], C["demo"]
            badge_txt = "DEMO · SIMULATED"
        elif src == "LIVE":
            badge_bg, badge_fg, badge_bd = C["sel"], C["green"], C["green"]
            badge_txt = "LIVE HARDWARE"
        else:
            badge_bg, badge_fg, badge_bd = C["replay_bg"], C["replay_text"], C["replay_bd"]
            badge_txt = f"{src}"

        an = meta.get("analyzer") or {}
        fw = an.get("firmware") or "UNAVAILABLE"
        board = an.get("board") or "—"
        proto = an.get("protocol")
        hz = an.get("sample_hz")
        rate_str = f"{hz / 1e6:g} MS/s" if hz else "UNAVAILABLE"
        proto_str = f" · proto v{proto}" if proto is not None else ""

        loss = meta.get("loss") or {}
        lost_rec = int(loss.get("lost_records") or 0)
        fw_ovf = int(loss.get("fw_overflows") or 0)
        fw_drop = int(loss.get("fw_dropped") or 0)
        tr_err = int(loss.get("transport_errors") or 0)
        trunc = bool(loss.get("raw_lines_truncated"))
        any_loss = any((lost_rec, fw_ovf, fw_drop, tr_err, trunc))
        loss_col = C["amber"] if any_loss else C["green"]
        loss_txt = (
            f"lost records: {lost_rec} · overflows: {fw_ovf} · dropped: {fw_drop} · "
            f"framing errors: {tr_err} · truncated: {'yes' if trunc else 'no'}"
        )

        perf = d.get("perf") or {}
        summ = meta.get("summary") or {}
        pkt_total = d.get("total_packets_decoded", len(d.get("packets") or []))
        summ_total = summ.get("transactions")
        pkt_line = f"{pkt_total:,} decoded"
        if summ_total is not None and summ_total != pkt_total:
            pkt_line += f" (saved summary: {summ_total:,})"
        pkt_line += f" · complete {perf.get('complete', 0):,} · incomplete {perf.get('incomplete', 0):,} · malformed {perf.get('malformed', 0):,}"

        devices = d.get("devices") or []
        dev_str = ", ".join(f"{dv['address']} {dv['name']} ({dv['transactions']})" for dv in devices) or "None observed"

        title = entry.get("name") or (f"In-memory session {meta.get('session_id', '—')}")
        file_line = (
            f"{entry.get('size_str', '')} · modified {entry.get('time_str', '')}"
            if entry
            else f"in-memory · {d.get('total_raw_lines', 0):,} raw record(s)"
        )
        cap_ts = meta.get("capture_started") or "Not recorded"
        saved_ts = meta.get("saved_at") or ("In-memory (unsaved)" if state["kind"] == "live" else "Not recorded")
        notes = meta.get("notes")

        rows = [
            ("Capture timestamp", cap_ts),
            ("Saved at", saved_ts),
            ("Analyzer", f"{fw} ({board}{proto_str}) · sample rate {rate_str}"),
            ("Loss / overflow", f"<span style='color:{loss_col}'>{html.escape(loss_txt)}</span>"),
            ("Packets", pkt_line),
            ("Devices ({})".format(len(devices)), dev_str),
        ]
        if perf.get("freq_avg_hz"):
            rows.append(("Measured SCL avg", f"{perf['freq_avg_hz'] / 1e3:.3f} kHz"))
        if notes:
            rows.append(("Notes", str(notes)))

        parts = [
            f"<div style='margin-bottom:6px;'>"
            f"<span style='background:{badge_bg};color:{badge_fg};border:1px solid {badge_bd};"
            f"border-radius:6px;padding:2px 8px;font-weight:700;'>{html.escape(badge_txt)}</span>"
            f"&nbsp;&nbsp;<b>{html.escape(str(title))}</b>"
            f"&nbsp;&nbsp;<span style='color:{C['text2']}'>({html.escape(file_line)})</span>"
            f"</div>",
            "<table cellspacing='0' cellpadding='2' width='100%'>",
        ]
        for k, v in rows:
            val_html = v if k == "Loss / overflow" else html.escape(str(v))
            parts.append(
                f"<tr><td style='color:{C['text2']};width:155px;vertical-align:top;'>{html.escape(k)}</td>"
                f"<td style='color:{C['text']};'>{val_html}</td></tr>"
            )
        parts.append("</table>")
        if self.chk_raw_meta.isChecked():
            parts.append(
                f"<p style='color:{C['text2']};margin:8px 0 4px 0;'><b>Raw JSON metadata</b></p>"
                f"<pre style='color:{C['text']};margin:0;'>{html.escape(json.dumps(meta, indent=2))}</pre>"
            )
        return "".join(parts)

    def _format_xlsx_meta_html(self, state: dict) -> str:
        entry = state.get("entry") or {}
        wb = state["data"]
        sheets = wb.get("sheets") or []
        parts = [
            f"<div style='margin-bottom:6px;'>"
            f"<span style='background:{C['sel']};color:{C['cyan']};border:1px solid {C['cyan']};"
            f"border-radius:6px;padding:2px 8px;font-weight:700;'>EXCEL WORKBOOK (.xlsx)</span>"
            f"&nbsp;&nbsp;<b>{html.escape(entry.get('name', ''))}</b>"
            f"&nbsp;&nbsp;<span style='color:{C['text2']}'>({entry.get('size_str', '')} · modified {entry.get('time_str', '')})</span>"
            f"</div>",
            f"<p style='color:{C['text2']};margin:4px 0;'>Sheets ({len(sheets)}): "
            + ", ".join(f"<b>{html.escape(s['name'])}</b> ({s['row_count']} rows)" for s in sheets)
            + "</p>",
        ]
        summary = next((s for s in sheets if s["name"] == "Summary"), None)
        if summary and summary["rows"]:
            parts.append("<table cellspacing='0' cellpadding='2' width='100%'>")
            for row in summary["rows"][:14]:
                sec = row[0] if len(row) > 0 else ""
                met = row[1] if len(row) > 1 else ""
                val = row[2] if len(row) > 2 else ""
                parts.append(
                    f"<tr><td style='color:{C['text2']};width:150px;'>{html.escape(sec)}</td>"
                    f"<td style='color:{C['text2']};width:210px;'>{html.escape(met)}</td>"
                    f"<td style='color:{C['text']};'>{html.escape(val)}</td></tr>"
                )
            parts.append("</table>")
        return "".join(parts)

    def _format_csv_meta_html(self, state: dict) -> str:
        entry = state.get("entry") or {}
        d = state["data"]
        headers = d.get("headers") or []
        return (
            f"<div style='margin-bottom:6px;'>"
            f"<span style='background:{C['sel']};color:{C['blue']};border:1px solid {C['blue']};"
            f"border-radius:6px;padding:2px 8px;font-weight:700;'>CSV EXPORT (.csv)</span>"
            f"&nbsp;&nbsp;<b>{html.escape(entry.get('name', ''))}</b>"
            f"&nbsp;&nbsp;<span style='color:{C['text2']}'>({entry.get('size_str', '')} · modified {entry.get('time_str', '')})</span>"
            f"</div>"
            f"<p style='color:{C['text2']}'>Rows: <b>{d.get('total_rows', 0):,}</b> · Columns: <b>{len(headers)}</b></p>"
            f"<p style='color:{C['text']}'>Headers: {html.escape(', '.join(headers[:18]))}"
            + (" …" if len(headers) > 18 else "")
            + "</p>"
        )

    # ---------------------------------------------------------------- Preview table rendering & filtering
    def _apply_packet_table_columns(self):
        self.preview_table.setColumnCount(len(PREVIEW_COLUMNS))
        self.preview_table.setHorizontalHeaderLabels(PREVIEW_COLUMNS)
        self.preview_table.horizontalHeader().setStretchLastSection(False)
        mono_columns(self.preview_table, [0, 1], right=True)
        mono_columns(self.preview_table, [2, 5, 6], right=False)
        fit_table(
            self.preview_table,
            {0: 76, 1: 96, 2: 68, 3: 130, 4: 100, 5: 220, 6: 130, 7: 120, 8: 110},
            stretch=5,
        )

    def _clear_preview_table(self, message: str):
        self.sheet_combo.hide()
        self.preview_addr.setEnabled(False)
        self.preview_status.setEnabled(False)
        self.preview_table.setRowCount(0)
        self.preview_source_lbl.setText(message)
        self.preview_count_lbl.setText("")

    def _setup_packet_preview_controls(self, packets: list[dict], source_text: str):
        self.sheet_combo.hide()
        self.preview_addr.setEnabled(True)
        self.preview_status.setEnabled(True)
        self.preview_source_lbl.setText(source_text)

        prev_addr = self.preview_addr.currentData()
        seen: dict[int, tuple[str, str]] = {}
        for row in packets:
            ai = row.get("addr_int")
            if ai is not None and ai not in seen:
                seen[ai] = (row["addr"], row["device"])

        self.preview_addr.blockSignals(True)
        self.preview_addr.clear()
        self.preview_addr.addItem("All addresses", None)
        for ai in sorted(seen):
            astr, dev = seen[ai]
            self.preview_addr.addItem(f"{astr} · {dev}", ai)
        idx = self.preview_addr.findData(prev_addr)
        self.preview_addr.setCurrentIndex(idx if idx >= 0 else 0)
        self.preview_addr.blockSignals(False)

    def _setup_sheet_preview_controls(self, wb_preview: dict, source_text: str):
        self.preview_source_lbl.setText(source_text)
        self.preview_addr.setEnabled(False)
        self.preview_status.setEnabled(False)
        sheets = wb_preview.get("sheets") or []
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        for s in sheets:
            self.sheet_combo.addItem(f"{s['name']} ({s['row_count']} rows)", s["name"])
        self.sheet_combo.setVisible(bool(sheets))
        self.sheet_combo.blockSignals(False)

    def _setup_flat_preview_controls(self, source_text: str):
        self.sheet_combo.hide()
        self.preview_addr.setEnabled(False)
        self.preview_status.setEnabled(False)
        self.preview_source_lbl.setText(source_text)

    def _render_preview_table(self):
        kind = self._preview_state.get("kind")
        if kind in ("i2ccap", "live"):
            self._render_packet_preview_table()
        elif kind == "xlsx":
            self._render_xlsx_preview_table()
        elif kind == "csv":
            self._render_csv_preview_table()

    def _render_packet_preview_table(self):
        d = self._preview_state.get("data") or {}
        packets = d.get("packets") or []
        addr_filter = self.preview_addr.currentData()
        status_filter = self.preview_status.currentText()
        q = self.preview_search.text().strip().lower()

        filtered = []
        for row in packets:
            if addr_filter is not None and row.get("addr_int") != addr_filter:
                continue
            if status_filter == "Complete" and row["raw_status"] != "ok":
                continue
            elif status_filter == "Incomplete" and row["raw_status"] != "incomplete":
                continue
            elif status_filter == "Malformed" and row["raw_status"] != "malformed":
                continue
            elif status_filter == "Capture gap" and row["raw_status"] != "gap":
                continue
            elif status_filter == "Has NACK" and row["nack_count"] == 0:
                continue
            if q:
                hay = (
                    f"{row['id']} {row['time_str']} {row['addr']} {row['device']} {row['op']} "
                    f"{row['full_data_hex']} {row['ack_nack']} {row['master']} {row['status']} "
                    f"{row['notes']} {row['semantic']}"
                ).lower()
                if q not in hay:
                    continue
            filtered.append(row)

        self._apply_packet_table_columns()
        self.preview_table.setUpdatesEnabled(False)
        try:
            self.preview_table.setRowCount(len(filtered))
            for r_i, row in enumerate(filtered):
                vals = [
                    str(row["id"]),
                    row["time_str"],
                    row["addr"],
                    row["device"],
                    row["op"],
                    row["data_hex"],
                    row["ack_nack"],
                    row["master"],
                    row["status"],
                ]
                tips = {
                    3: f"{row['device']} ({row['confidence']})",
                    5: row["full_data_hex"],
                    6: (
                        f"ACK {row['ack_counts']['acks']} · raw NACK {row['ack_counts']['raw_nacks']} · "
                        f"expected read NACK {row['ack_counts']['expected_read_nacks']} · "
                        f"unexpected NACK {row['ack_counts']['unexpected_nacks']}"
                    ),
                    7: row["master_detail"],
                    8: row["notes"] or row["status"],
                }
                for c_i, val in enumerate(vals):
                    item = QTableWidgetItem(val)
                    item.setToolTip(tips.get(c_i, val))
                    if c_i in PREVIEW_RIGHT_COLS:
                        item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                    fg = self._packet_cell_color(row, c_i)
                    if fg is not None:
                        item.setForeground(QBrush(QColor(fg)))
                    self.preview_table.setItem(r_i, c_i, item)
        finally:
            self.preview_table.setUpdatesEnabled(True)

        total_decoded = d.get("total_packets_decoded", len(packets))
        trunc_note = " (bounded preview)" if d.get("truncated") else ""
        self.preview_count_lbl.setText(
            f"Showing {len(filtered):,} of {len(packets):,} preview packet(s)"
            + (f" · {total_decoded:,} decoded" if total_decoded != len(packets) else "")
            + trunc_note
        )

    @staticmethod
    def _packet_cell_color(row: dict, col: int) -> str | None:
        if col == 4:
            return C["cyan"] if row["has_read"] else C["blue"]
        if col == 6:
            k = row["ack_counts"]
            if k["unexpected_nacks"]:
                return C["red"]
            if k["unverified_read_end_nacks"]:
                return C["amber"]
            return C["green"]
        if col == 7:
            mid = row.get("master_id")
            return config.MASTERS[mid]["color"] if mid in config.MASTERS else C["amber"]
        if col == 8:
            return {"ok": C["green"], "malformed": C["red"]}.get(row["raw_status"], C["gray"])
        if row["raw_status"] == "malformed":
            return C["red"]
        if row["raw_status"] in ("incomplete", "gap"):
            return C["gray"]
        return None

    def _render_tabular_sheet(self, headers: list[str], rows: list[list[str]], total_rows: int, label_prefix: str):
        q = self.preview_search.text().strip().lower()
        filtered = [r for r in rows if not q or q in " ".join(str(x) for x in r).lower()]
        cols = max([len(headers)] + [len(r) for r in filtered], default=0)
        hdrs = headers + [f"Col {i + 1}" for i in range(len(headers), cols)]

        self.preview_table.setUpdatesEnabled(False)
        try:
            self.preview_table.setColumnCount(cols)
            self.preview_table.setHorizontalHeaderLabels(hdrs)
            for c_i in range(cols):
                self.preview_table.setItemDelegateForColumn(c_i, None)
            self.preview_table.setRowCount(len(filtered))
            for r_i, r_vals in enumerate(filtered):
                for c_i in range(cols):
                    txt = str(r_vals[c_i]) if c_i < len(r_vals) else ""
                    item = QTableWidgetItem(txt)
                    item.setToolTip(txt)
                    self.preview_table.setItem(r_i, c_i, item)
            hh = self.preview_table.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            hh.setStretchLastSection(cols > 0)
            for c_i in range(cols):
                need = hh.fontMetrics().horizontalAdvance(str(hdrs[c_i] or "")) + 36
                self.preview_table.setColumnWidth(c_i, max(100, min(280, need)))
        finally:
            self.preview_table.setUpdatesEnabled(True)

        self.preview_count_lbl.setText(
            f"{label_prefix}: showing {len(filtered):,} of {len(rows):,} loaded row(s) (total {total_rows:,})"
        )

    def _render_xlsx_preview_table(self):
        sheets = (self._preview_state.get("data") or {}).get("sheets") or []
        if not sheets:
            self._clear_preview_table("Workbook has no sheets.")
            return
        idx = max(0, min(self.sheet_combo.currentIndex(), len(sheets) - 1))
        sheet = sheets[idx]
        self._render_tabular_sheet(
            sheet.get("headers") or [],
            sheet.get("rows") or [],
            sheet.get("row_count", 0),
            f"Sheet '{sheet['name']}'",
        )

    def _render_csv_preview_table(self):
        d = self._preview_state.get("data") or {}
        self._render_tabular_sheet(
            d.get("headers") or [],
            d.get("rows") or [],
            d.get("total_rows", 0),
            "CSV",
        )

    # ---------------------------------------------------------------- Playback & export
    def _history_tab_index(self) -> int | None:
        pages = getattr(self.ctx, "pages", None)
        if isinstance(pages, (list, tuple)):
            for i, p in enumerate(pages):
                if p is self:
                    return i
        stack = getattr(self.ctx, "stack", None)
        if stack is not None:
            idx = stack.indexOf(self)
            if idx >= 0:
                return idx
            parent = self.parentWidget()
            while parent is not None:
                idx = stack.indexOf(parent)
                if idx >= 0:
                    return idx
                parent = parent.parentWidget()
        return None

    def _play(self):
        it = self.list.currentItem()
        if it is None:
            self.ctx.toast("Select a capture first")
            return
        path = it.data(Qt.ItemDataRole.UserRole)
        if not str(path).lower().endswith(".i2ccap"):
            self.ctx.toast("Select an .i2ccap capture file for playback")
            return
        speed = [0, 1, 5, 20][self.speed.currentIndex()]
        self.b_pause.setChecked(False)
        self.ctx.open_capture(path, speed)
        if self.stay_on_page.isChecked():
            idx = self._history_tab_index()
            if idx is not None:
                nav = getattr(self.ctx, "nav", None)
                if nav is not None and nav.currentRow() != idx:
                    nav.setCurrentRow(idx)
                stack = getattr(self.ctx, "stack", None)
                if stack is not None and stack.currentIndex() != idx:
                    stack.setCurrentIndex(idx)
        self.refresh(getattr(self.ctx, "engine", None))

    def _export(self, kind):
        eng = self.ctx.engine
        if not eng.packets and kind != "stats":
            self.ctx.toast("Nothing to export yet")
            return
        sid = re.sub(r"[^\w.-]+", "_", str(getattr(self.ctx, "session_id", "") or "")).strip("_")
        demo_tag = "_DEMO" if eng.source == "demo" else ""
        xlsx_default = f"capture_{sid}{demo_tag}.xlsx" if sid else f"session_export{demo_tag}.xlsx"
        ext = {
            "csv": "CSV (*.csv)",
            "json": "JSON (*.json)",
            "stats": "CSV (*.csv)",
            "xlsx": "Excel Workbook (*.xlsx)",
        }[kind]
        default = {
            "csv": "packets.csv",
            "json": "packets.json",
            "stats": "statistics.csv",
            "xlsx": xlsx_default,
        }[kind]
        p, _ = QFileDialog.getSaveFileName(self, "Export", os.path.join(self.ctx.captures_dir(), default), ext)
        if not p:
            return
        if kind == "xlsx" and not p.lower().endswith(".xlsx"):
            p += ".xlsx"
        try:
            if kind == "csv":
                n = export_packets_csv(p, eng)
            elif kind == "json":
                n = export_packets_json(p, eng)
            elif kind == "xlsx":
                n = export_session_excel(p, eng, session_id=getattr(self.ctx, "session_id", ""))
            else:
                export_statistics_csv(p, eng)
                n = None
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        if kind == "xlsx":
            self.ctx.toast(f"Exported Excel workbook ({n:,} packets): {os.path.basename(p)}")
        elif n is not None:
            self.ctx.toast(f"Exported {n} packets")
        else:
            self.ctx.toast("Statistics exported")
        self.scan()

    def refresh(self, engine):
        if engine is None:
            return
        sid = getattr(self.ctx, "session_id", "—")
        src = str(getattr(engine, "source", "none") or "none").upper()
        if engine.source == "demo":
            src = "DEMO · SIMULATED"
        pkts = len(getattr(engine, "packets", ()))
        raw = len(getattr(engine, "raw_lines", ()))
        devs = len(getattr(getattr(engine, "registry", None), "devices", {}))
        self.session_lbl.setText(
            f"In-memory session {sid} · source: {src} · {pkts:,} packet(s) · {raw:,} raw record(s) · {devs:,} device(s)"
        )

        replay_idx = int(getattr(self.ctx, "replay_idx", 0) or 0)
        replay_lines = getattr(self.ctx, "replay_lines", None) or []
        total_lines = len(replay_lines)
        replay_timer = getattr(self.ctx, "replay_timer", None)
        timer_active = bool(replay_timer is not None and replay_timer.isActive())
        replay_paused = bool(getattr(self.ctx, "replay_paused", False))
        replay_speed = getattr(self.ctx, "replay_speed", 0)

        if timer_active and total_lines > 0:
            pct = max(0, min(100, int(round(100 * replay_idx / total_lines))))
            state = "Paused" if replay_paused else f"Playing {replay_speed}×"
            self.replay_progress.setValue(pct)
            self.replay_lbl.setText(f"Replay {state}: {replay_idx:,} / {total_lines:,} records ({pct}%)")
        elif engine.source == "replay" and total_lines > 0 and replay_idx >= total_lines:
            self.replay_progress.setValue(100)
            self.replay_lbl.setText(f"Replay complete: {total_lines:,} / {total_lines:,} records (100%)")
        elif engine.source == "replay":
            self.replay_progress.setValue(100 if pkts else 0)
            self.replay_lbl.setText(f"Recorded capture loaded: {pkts:,} packet(s)")
        else:
            self.replay_progress.setValue(0)
            self.replay_lbl.setText("Replay idle")
