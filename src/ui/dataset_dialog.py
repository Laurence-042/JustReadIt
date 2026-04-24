# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""数据集标注对话框及相关 Qt 模型。

独立模块，可从调试窗口或其他入口打开。

Usage::

    from src.ui.dataset_dialog import DatasetDialog

    ds = PipelineDataset()
    dlg = DatasetDialog(ds, parent=self)
    dlg.exec()
"""
from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    Slot,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.dataset import LABEL_DISPLAY, LABELS, PipelineDataset, SampleRow

# ---------------------------------------------------------------------------
# Column indices
# ---------------------------------------------------------------------------

_COL_ID    = 0
_COL_TIME  = 1
_COL_OCR   = 2
_COL_MEM   = 3
_COL_CORR  = 4
_COL_LABEL = 5
_HEADERS   = ["ID", "时间", "OCR", "内存", "纠错", "标签"]


class _SampleModel(QAbstractTableModel):
    """Table model backed by a list[SampleRow].  Mutations go through the
    model so the view always stays in sync without full reloads."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[SampleRow] = []

    # ── QAbstractTableModel interface ─────────────────────────────────────

    def rowCount(self, parent=None) -> int:          # noqa: N802
        return len(self._rows)

    def columnCount(self, parent=None) -> int:       # noqa: N802
        return len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        s = self._rows[row]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_ID:    return s.id
            if col == _COL_TIME:  return s.captured_at[:19]
            if col == _COL_OCR:   return s.ocr_text[:80]
            if col == _COL_MEM:   return (" | ".join(s.memory_hits))[:80] if s.memory_hits else ""
            if col == _COL_CORR:  return s.corrected_text[:80]
            if col == _COL_LABEL: return LABEL_DISPLAY.get(s.label, s.label)
        if role == Qt.ItemDataRole.UserRole:
            return s  # the full SampleRow
        return None

    # ── Mutation helpers ──────────────────────────────────────────────────

    def reset_data(self, rows: list[SampleRow]) -> None:
        """Replace all rows (full reload from DB)."""
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def update_label(self, sample_id: int, new_label: str) -> int | None:
        """Update the label of sample_id in-place; return its row index or None."""
        for i, s in enumerate(self._rows):
            if s.id == sample_id:
                # frozen dataclass — replace via object.__setattr__ trick-free:
                import dataclasses  # noqa: PLC0415
                self._rows[i] = dataclasses.replace(s, label=new_label)
                idx = self.index(i, _COL_LABEL)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DisplayRole])
                return i
        return None

    def update_row(self, new_sample: SampleRow) -> int | None:
        """Replace the in-memory SampleRow for new_sample.id; emit dataChanged."""
        for i, s in enumerate(self._rows):
            if s.id == new_sample.id:
                self._rows[i] = new_sample
                left  = self.index(i, 0)
                right = self.index(i, len(_HEADERS) - 1)
                self.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole])
                return i
        return None

    def remove_ids(self, ids: set[int]) -> None:
        """Remove rows whose SampleRow.id is in *ids*."""
        # Walk in reverse so indices stay valid
        for i in range(len(self._rows) - 1, -1, -1):
            if self._rows[i].id in ids:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self._rows[i]
                self.endRemoveRows()

    def row_for_id(self, sample_id: int) -> int | None:
        for i, s in enumerate(self._rows):
            if s.id == sample_id:
                return i
        return None

    def sample_at(self, row: int) -> SampleRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


class _LabelFilterProxy(QSortFilterProxyModel):
    """Proxy that filters rows by label value."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._label_filter: str = ""

    def set_label_filter(self, label: str) -> None:
        self._label_filter = label
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        if not self._label_filter:
            return True
        src = self.sourceModel()
        s: SampleRow | None = src.data(src.index(source_row, 0), Qt.ItemDataRole.UserRole)
        return s is not None and s.label == self._label_filter


class DatasetDialog(QDialog):
    """Modal dialog for browsing and annotating pipeline dataset samples."""

    def __init__(self, dataset: PipelineDataset, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ds = dataset
        self._current_id: int | None = None
        self.setWindowTitle("📊 流水线数据集")
        self.setMinimumSize(1100, 620)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        root = QVBoxLayout(self)

        # ── Filter row ────────────────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("筛选标签:"))
        self._cmb_filter = QComboBox()
        self._cmb_filter.addItem("全部", userData="")
        for lbl in LABELS:
            self._cmb_filter.addItem(LABEL_DISPLAY[lbl], userData=lbl)
        self._cmb_filter.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._cmb_filter)
        filter_row.addStretch()
        self._lbl_count = QLabel()
        filter_row.addWidget(self._lbl_count)
        btn_refresh = QPushButton("🔄 刷新")
        btn_refresh.clicked.connect(self._full_reload)
        filter_row.addWidget(btn_refresh)
        root.addLayout(filter_row)

        # ── Model / proxy ─────────────────────────────────────────────────
        self._model = _SampleModel(self)
        self._proxy = _LabelFilterProxy(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.ItemDataRole.DisplayRole)

        # Keep count label in sync whenever the proxy recounts
        self._model.rowsInserted.connect(self._update_count)
        self._model.rowsRemoved.connect(self._update_count)
        self._model.modelReset.connect(self._update_count)
        self._proxy.rowsInserted.connect(self._update_count)
        self._proxy.rowsRemoved.connect(self._update_count)
        self._proxy.modelReset.connect(self._update_count)

        # ── Main splitter ─────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        # Table view
        self._view = QTableView()
        self._view.setModel(self._proxy)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(_COL_ID, Qt.SortOrder.DescendingOrder)
        hdr = self._view.horizontalHeader()
        hdr.setSectionResizeMode(_COL_ID,    QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_COL_TIME,  QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_COL_OCR,   QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(_COL_MEM,   QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(_COL_CORR,  QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(_COL_LABEL, QHeaderView.ResizeMode.ResizeToContents)
        self._view.verticalHeader().setVisible(False)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        self._view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._view)

        # ── Annotation panel ──────────────────────────────────────────────
        ann_widget = QWidget()
        ann_lay = QVBoxLayout(ann_widget)
        ann_lay.setContentsMargins(8, 4, 4, 4)

        ann_lay.addWidget(QLabel("<b>OCR 文本</b>"))
        self._te_ocr = QTextEdit()
        self._te_ocr.setReadOnly(True)
        self._te_ocr.setFixedHeight(80)
        ann_lay.addWidget(self._te_ocr)

        ann_lay.addWidget(QLabel("<b>内存候选</b>"))
        self._te_hits = QTextEdit()
        self._te_hits.setReadOnly(True)
        self._te_hits.setFixedHeight(80)
        ann_lay.addWidget(self._te_hits)

        ann_lay.addWidget(QLabel("<b>纠错结果</b>"))
        self._te_corr = QTextEdit()
        self._te_corr.setReadOnly(True)
        self._te_corr.setFixedHeight(60)
        ann_lay.addWidget(self._te_corr)

        ann_lay.addWidget(QLabel("<b>翻译</b>"))
        self._te_tl = QTextEdit()
        self._te_tl.setReadOnly(True)
        self._te_tl.setFixedHeight(60)
        ann_lay.addWidget(self._te_tl)

        ann_lay.addSpacing(8)
        ann_lay.addWidget(QLabel("<b>标注</b>"))

        lbl_row = QHBoxLayout()
        lbl_row.addWidget(QLabel("标签:"))
        self._cmb_label = QComboBox()
        for lbl in LABELS:
            self._cmb_label.addItem(LABEL_DISPLAY[lbl], userData=lbl)
        lbl_row.addWidget(self._cmb_label, 1)
        ann_lay.addLayout(lbl_row)

        ann_lay.addWidget(QLabel("正确纠错（仅当标签为 ✗ 纠错错误时填写）:"))
        self._le_expected = QTextEdit()
        self._le_expected.setPlaceholderText("正确的纠错结果")
        self._le_expected.setFixedHeight(60)
        ann_lay.addWidget(self._le_expected)

        ann_lay.addWidget(QLabel("备注:"))
        self._te_notes = QTextEdit()
        self._te_notes.setPlaceholderText("可选：描述具体问题")
        self._te_notes.setFixedHeight(60)
        ann_lay.addWidget(self._te_notes)

        save_row = QHBoxLayout()
        self._btn_save_ann = QPushButton("💾 保存标注  [Enter]")
        self._btn_save_ann.setEnabled(False)
        self._btn_save_ann.clicked.connect(self._on_save_annotation)
        save_row.addWidget(self._btn_save_ann)
        self._btn_save_next = QPushButton("💾→ 保存并跳下一条  [Shift+Enter]")
        self._btn_save_next.setEnabled(False)
        self._btn_save_next.clicked.connect(self._on_save_and_next)
        save_row.addWidget(self._btn_save_next)
        self._btn_del_sample = QPushButton("🗑 删除样本  [Del]")
        self._btn_del_sample.setEnabled(False)
        self._btn_del_sample.clicked.connect(self._on_delete_sample)
        save_row.addWidget(self._btn_del_sample)
        save_row.addStretch()
        ann_lay.addLayout(save_row)
        ann_lay.addStretch()

        splitter.addWidget(ann_widget)
        splitter.setSizes([650, 450])

        # ── Bottom ────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_export = QPushButton("📥 导出 CSV")
        btn_export.setToolTip("将当前筛选的样本导出为 CSV 文件")
        btn_export.clicked.connect(self._on_export_csv)
        btn_row.addWidget(btn_export)
        btn_export_test = QPushButton("🧪 导出为测试集")
        btn_export_test.setToolTip(
            "将已标注的样本（label=ok / bad_correction）导出为\n"
            "correction_samples.csv 格式，可直接用于 pytest 回归测试。"
        )
        btn_export_test.clicked.connect(self._on_export_correction_samples)
        btn_row.addWidget(btn_export_test)
        btn_recalc = QPushButton("♻️ 重新计算纠错")
        btn_recalc.setToolTip(
            "对数据集中所有样本重新执行 best_match，\n"
            "并根据结果自动更新标注。"
        )
        btn_recalc.clicked.connect(self._on_recalculate_corrections)
        btn_row.addWidget(btn_recalc)
        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        # ── Keyboard shortcuts ────────────────────────────────────────────
        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(self._on_save_annotation)
        QShortcut(
            QKeySequence(Qt.KeyboardModifier.ShiftModifier | Qt.Key.Key_Return), self
        ).activated.connect(self._on_save_and_next)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self).activated.connect(self._on_delete_sample)
        for _i, _lbl in enumerate(LABELS, start=1):
            def _make_label_slot(l: str):
                def _slot() -> None:
                    for ii in range(self._cmb_label.count()):
                        if self._cmb_label.itemData(ii) == l:
                            self._cmb_label.setCurrentIndex(ii)
                            break
                return _slot
            QShortcut(QKeySequence(f"{_i}"), self).activated.connect(_make_label_slot(_lbl))

        self._full_reload()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _full_reload(self) -> None:
        """Fetch all samples from DB and reset model (infrequent)."""
        label_filter = self._cmb_filter.currentData() or ""
        samples = self._ds.list_samples(limit=10_000, label_filter="")
        self._model.reset_data(samples)
        # Apply visual filter via proxy (no extra DB query)
        self._proxy.set_label_filter(label_filter)
        self._clear_annotation_panel()

    def _update_count(self) -> None:
        self._lbl_count.setText(f"{self._proxy.rowCount()} 条样本")

    def _selected_proxy_rows(self) -> list[int]:
        """Unique proxy row indices currently selected."""
        return sorted({idx.row() for idx in self._view.selectionModel().selectedIndexes()})

    def _proxy_row_to_sample(self, proxy_row: int) -> SampleRow | None:
        src_idx = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
        return self._model.sample_at(src_idx.row())

    def _clear_annotation_panel(self) -> None:
        self._current_id = None
        self._te_ocr.clear()
        self._te_hits.clear()
        self._te_corr.clear()
        self._te_tl.clear()
        self._le_expected.clear()
        self._te_notes.clear()
        self._btn_save_ann.setEnabled(False)
        self._btn_save_next.setEnabled(False)
        self._btn_del_sample.setEnabled(False)

    def _load_annotation_panel(self, sample: SampleRow) -> None:
        self._current_id = sample.id
        self._te_ocr.setPlainText(sample.ocr_text)
        self._te_hits.setPlainText("\n".join(sample.memory_hits))
        self._te_corr.setPlainText(sample.corrected_text)
        self._te_tl.setPlainText(sample.translated_text)
        for i in range(self._cmb_label.count()):
            if self._cmb_label.itemData(i) == sample.label:
                self._cmb_label.setCurrentIndex(i)
                break
        self._le_expected.setPlainText(sample.expected_correction)
        self._te_notes.setPlainText(sample.notes)
        self._btn_save_ann.setEnabled(True)
        self._btn_save_next.setEnabled(True)
        self._btn_del_sample.setEnabled(True)

    def _find_next_unlabeled_proxy_row(self, from_proxy_row: int) -> int:
        n = self._proxy.rowCount()
        if n == 0:
            return -1
        for offset in range(1, n + 1):
            row = (from_proxy_row + offset) % n
            s = self._proxy_row_to_sample(row)
            if s is not None and s.label == "unlabeled":
                return row
        return -1

    def _select_proxy_row(self, proxy_row: int) -> None:
        idx = self._proxy.index(proxy_row, 0)
        self._view.selectionModel().select(
            idx,
            self._view.selectionModel().SelectionFlag.ClearAndSelect
            | self._view.selectionModel().SelectionFlag.Rows,
        )
        self._view.scrollTo(idx)
        self._view.setCurrentIndex(idx)

    # ── Slots ─────────────────────────────────────────────────────────────

    @Slot()
    def _on_filter_changed(self) -> None:
        label_filter = self._cmb_filter.currentData() or ""
        self._proxy.set_label_filter(label_filter)
        self._clear_annotation_panel()

    @Slot()
    def _on_selection_changed(self) -> None:
        rows = self._selected_proxy_rows()
        if len(rows) == 1:
            s = self._proxy_row_to_sample(rows[0])
            if s is not None:
                self._load_annotation_panel(s)
            else:
                self._clear_annotation_panel()
        elif len(rows) > 1:
            self._clear_annotation_panel()
            self._btn_del_sample.setEnabled(True)
        else:
            self._clear_annotation_panel()

    @Slot()
    def _on_save_annotation(self) -> None:
        if self._current_id is None:
            return
        new_label = self._cmb_label.currentData()
        self._ds.annotate(
            self._current_id,
            label=new_label,
            expected_correction=self._le_expected.toPlainText().strip(),
            notes=self._te_notes.toPlainText().strip(),
        )
        # Update model in-place — one dataChanged signal, no reload
        self._model.update_label(self._current_id, new_label)

    @Slot()
    def _on_save_and_next(self) -> None:
        if self._current_id is None:
            return
        new_label = self._cmb_label.currentData()
        self._ds.annotate(
            self._current_id,
            label=new_label,
            expected_correction=self._le_expected.toPlainText().strip(),
            notes=self._te_notes.toPlainText().strip(),
        )
        cur_proxy_rows = self._selected_proxy_rows()
        cur_proxy_row = cur_proxy_rows[0] if cur_proxy_rows else 0
        self._model.update_label(self._current_id, new_label)
        # Proxy may have hidden this row if filter no longer matches — clamp
        effective = min(cur_proxy_row, self._proxy.rowCount() - 1)
        next_row = self._find_next_unlabeled_proxy_row(effective)
        if next_row >= 0:
            self._select_proxy_row(next_row)

    @Slot()
    def _on_context_menu(self) -> None:
        from PySide6.QtGui import QCursor  # noqa: PLC0415
        rows = self._selected_proxy_rows()
        if not rows:
            return
        menu = QMenu(self)
        act_del = menu.addAction(f"🗑 删除选中 {len(rows)} 条样本")
        act_del.triggered.connect(self._on_delete_sample)
        menu.exec(QCursor.pos())

    @Slot()
    def _on_delete_sample(self) -> None:
        rows = self._selected_proxy_rows()
        ids: set[int] = set()
        for r in rows:
            s = self._proxy_row_to_sample(r)
            if s is not None:
                ids.add(s.id)
        if not ids and self._current_id is not None:
            ids = {self._current_id}
        if not ids:
            return
        label = f"确定删除选中的 {len(ids)} 条样本？" if len(ids) > 1 else f"确定删除样本 #{next(iter(ids))}？"
        if QMessageBox.question(
            self, "删除样本", label,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        for sid in ids:
            self._ds.delete(sid)
        self._model.remove_ids(ids)
        self._clear_annotation_panel()

    @Slot()
    def _on_recalculate_corrections(self) -> None:
        """Recompute best_match for every sample and apply smart label updates.

        Rules
        -----
        * **未标注** (unlabeled) → 直接更新 corrected_text。
        * **标注没问题**（ok / 其他非 bad_correction）且结果发变
          → label = ``bad_correction``，expected_correction = 旧 corrected_text。
        * **bad_correction** 且新结果 == expected_correction
          → label = ``ok``，清除 expected_correction。
        * **bad_correction** 且新结果 ≠ expected_correction
          → 仅更新 corrected_text，保留 expected。
        """
        import dataclasses  # noqa: PLC0415
        from src.correction import best_match  # noqa: PLC0415

        all_samples = self._ds.list_samples(limit=100_000)
        if not all_samples:
            QMessageBox.information(self, "重新计算纠错", "没有样本。")
            return

        reply = QMessageBox.question(
            self, "重新计算纠错",
            f"将对全部 {len(all_samples)} 条样本重新执行 best_match，"
            f"并自动更新标注。\n小心：此操作不可彤销。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        stats = {"updated": 0, "newly_bad": 0, "recovered": 0, "unchanged": 0}

        for s in all_samples:
            new_corr = best_match(s.ocr_text, s.memory_hits, s.needle) or s.ocr_text

            if s.label == "unlabeled":
                if new_corr == s.corrected_text:
                    stats["unchanged"] += 1
                    continue
                self._ds.update_corrected_text(s.id, corrected_text=new_corr)
                updated = dataclasses.replace(s, corrected_text=new_corr)
                self._model.update_row(updated)
                stats["updated"] += 1

            elif s.label == "bad_correction":
                if new_corr == s.expected_correction:
                    # Fixed: promote to ok
                    self._ds.update_corrected_text(
                        s.id, corrected_text=new_corr,
                        label="ok", expected_correction="",
                    )
                    updated = dataclasses.replace(
                        s, corrected_text=new_corr, label="ok", expected_correction=""
                    )
                    self._model.update_row(updated)
                    self._model.update_label(s.id, "ok")
                    stats["recovered"] += 1
                else:
                    if new_corr == s.corrected_text:
                        stats["unchanged"] += 1
                        continue
                    # Still wrong — only update corrected_text
                    self._ds.update_corrected_text(s.id, corrected_text=new_corr)
                    updated = dataclasses.replace(s, corrected_text=new_corr)
                    self._model.update_row(updated)
                    stats["updated"] += 1

            else:  # ok / bad_range / bad_memory / other
                if new_corr == s.corrected_text:
                    stats["unchanged"] += 1
                    continue
                # Previously looked fine, now result changed — flag it
                self._ds.update_corrected_text(
                    s.id, corrected_text=new_corr,
                    label="bad_correction",
                    expected_correction=s.corrected_text,
                )
                updated = dataclasses.replace(
                    s, corrected_text=new_corr,
                    label="bad_correction",
                    expected_correction=s.corrected_text,
                )
                self._model.update_row(updated)
                stats["newly_bad"] += 1

        # Reload panels in case the current selected row changed
        self._clear_annotation_panel()

        QMessageBox.information(
            self, "重新计算完成",
            f"共处理 {len(all_samples)} 条样本：\n"
            f"  · 直接更新：{stats['updated']}\n"
            f"  · 新增「纠错错误」：{stats['newly_bad']}\n"
            f"  · 自动恢复为「正确」：{stats['recovered']}\n"
            f"  · 无变化：{stats['unchanged']}",
        )

    @Slot()
    def _on_export_csv(self) -> None:
        label_filter = self._cmb_filter.currentData() or ""
        samples = self._ds.list_samples(limit=10_000, label_filter=label_filter)
        if not samples:
            QMessageBox.information(self, "导出 CSV", "没有可导出的样本。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出数据集为 CSV", "pipeline_dataset.csv", "CSV 文件 (*.csv)",
        )
        if not path:
            return
        import csv  # noqa: PLC0415
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "captured_at", "ocr_text", "memory_hits",
                "needle", "corrected_text", "translated_text",
                "label", "expected_correction", "notes", "annotated_at",
            ])
            for s in samples:
                writer.writerow([
                    s.id, s.captured_at, s.ocr_text,
                    " | ".join(s.memory_hits),
                    s.needle, s.corrected_text, s.translated_text,
                    s.label, s.expected_correction, s.notes,
                    s.annotated_at or "",
                ])
        QMessageBox.information(self, "导出完成", f"已导出 {len(samples)} 条样本到：\n{path}")

    @Slot()
    def _on_export_correction_samples(self) -> None:
        """Export annotated rows as ``correction_samples.csv`` for pytest.

        Only fully-annotated rows (``ok`` / ``bad_correction``) are included.
        ``label`` and ``expected_correction`` are NOT written to the test CSV;
        the annotated correct result is placed directly into ``expected``:

        * ``ok``             → ``expected = corrected_text``
                               (``match_mode=contains_all`` if non-empty, else ``none``)
        * ``bad_correction`` → ``expected = expected_correction``
                               (``match_mode=contains_all`` if non-empty, else skipped)
        * all other labels   → excluded
        """
        import csv   # noqa: PLC0415
        import json  # noqa: PLC0415

        all_samples = self._ds.list_samples(limit=10_000)

        exportable = []
        skipped_incomplete = 0
        for s in all_samples:
            if s.label == "ok":
                exportable.append(s)
            elif s.label == "bad_correction":
                if s.expected_correction:
                    exportable.append(s)
                else:
                    skipped_incomplete += 1

        if not exportable:
            msg = "没有可导出的样本（需要 label=ok 或已填写正确纠错的 bad_correction）。"
            if skipped_incomplete:
                msg += f"\n\n已跳过 {skipped_incomplete} 条 bad_correction（未填写正确纠错）。"
            QMessageBox.information(self, "导出为测试集", msg)
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出为测试集 CSV",
            "correction_samples_export.csv", "CSV 文件 (*.csv)",
        )
        if not path:
            return

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "ocr_text", "memory_hits", "needle",
                "match_mode", "expected", "must_not_contain", "notes",
            ])
            for s in exportable:
                if s.label == "ok":
                    expected = s.corrected_text
                else:  # bad_correction — expected_correction is the ground truth
                    expected = s.expected_correction
                match_mode = "contains_all" if expected else "none"
                writer.writerow([
                    f"sample_{s.id}",
                    s.ocr_text,
                    json.dumps(s.memory_hits, ensure_ascii=False),
                    s.needle,
                    match_mode,
                    expected,
                    "",   # must_not_contain — fill manually if needed
                    s.notes,
                ])

        skip_msg = (
            f"\n\n已跳过 {skipped_incomplete} 条 bad_correction（未填写正确纠错）。"
            if skipped_incomplete else ""
        )
        QMessageBox.information(
            self, "导出完成",
            f"已导出 {len(exportable)} 条样本到：\n{path}{skip_msg}\n\n"
            "提示：可用以下命令跑回归：\n"
            f"pytest tests/test_correction_dataset.py "
            f"--correction-samples={path}",
        )
