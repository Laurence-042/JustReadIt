"""PySide6 debug window for JustReadIt.

Provides a live view of the pipeline:
  - Capture preview with OCR bounding-box overlay
  - Windows OCR text output panel
  - Memory scan result panel
    - Levenshtein corrected text panel
  - Translation panel (placeholder until translators are implemented)

Launch via ``python main.py --debug``.
"""
from __future__ import annotations

import ctypes
import logging

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject,
    QSignalBlocker, QSize, QSortFilterProxyModel, QTimer,
    Signal, Slot, Qt,
)

from src.app_backend import AppBackend
from src.config import AppConfig
from src.dataset import LABEL_DISPLAY, LABELS, PipelineDataset, SampleRow
from src.knowledge import KnowledgeBase
from src.languages import display_name

_cfg = AppConfig()
_log = logging.getLogger(__name__)

from PySide6.QtGui import (
    QAction, QColor, QFont, QImage, QPainter, QPen, QPixmap, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog,
    QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QTableView, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QToolBar, QToolButton,
    QScrollArea,
    QVBoxLayout, QWidget,
)

from src.controller import OcrOutput, PipelineResult, RangeOutput, StepResult
from src.target import GameTarget
from src.ocr.windows_ocr import _ensure_apartment
from src.ocr.range_detectors import BoundingBox
from ._config_model import ConfigModel
from ._translator_settings import TranslatorSettingsWidget
from .window_picker import WindowPicker


# ---------------------------------------------------------------------------
# Language capability mapping  (BCP-47 tag → DISM capability name)
# ---------------------------------------------------------------------------

_LANG_CAPABILITIES: dict[str, str] = {
    "ja": "Language.OCR~~~ja-JP~0.0.1.0",
}

# Win32 ShellExecuteEx — used to launch an elevated PowerShell and get its
# process handle so we can poll for completion without blocking the UI thread.
class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize",         ctypes.c_ulong),
        ("fMask",          ctypes.c_ulong),
        ("hwnd",           ctypes.c_void_p),
        ("lpVerb",         ctypes.c_wchar_p),
        ("lpFile",         ctypes.c_wchar_p),
        ("lpParameters",   ctypes.c_wchar_p),
        ("lpDirectory",    ctypes.c_wchar_p),
        ("nShow",          ctypes.c_int),
        ("hInstApp",       ctypes.c_void_p),
        ("lpIDList",       ctypes.c_void_p),
        ("lpClass",        ctypes.c_wchar_p),
        ("hkeyClass",      ctypes.c_void_p),
        ("dwHotKey",       ctypes.c_ulong),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess",       ctypes.c_void_p),
    ]

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_TIMEOUT            = 0x00000102
_kernel32_ui = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------------------
# Knowledge Manager dialog
# ---------------------------------------------------------------------------

class _KnowledgeManagerDialog(QDialog):
    """Modal dialog for browsing and deleting knowledge-base entries."""

    def __init__(self, knowledge_base: "KnowledgeBase", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kb = knowledge_base
        self.setWindowTitle("📚 知识库管理")
        self.setMinimumSize(800, 500)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        # ── Terms tab ──
        self._terms_table = QTableWidget()
        self._terms_table.setColumnCount(4)
        self._terms_table.setHorizontalHeaderLabels(["分类", "原文", "译文", "描述"])
        self._terms_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._terms_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._terms_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._terms_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._terms_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._terms_table.setSortingEnabled(True)
        terms_widget = QWidget()
        terms_lay = QVBoxLayout(terms_widget)
        terms_lay.setContentsMargins(4, 4, 4, 4)
        terms_btn_row = QHBoxLayout()
        self._btn_del_term = QPushButton("🗑 删除所选")
        self._btn_del_term.clicked.connect(self._on_delete_term)
        terms_btn_row.addWidget(self._btn_del_term)
        terms_btn_row.addStretch()
        self._lbl_terms_count = QLabel()
        terms_btn_row.addWidget(self._lbl_terms_count)
        terms_lay.addLayout(terms_btn_row)
        terms_lay.addWidget(self._terms_table)
        self._tabs.addTab(terms_widget, "术语")

        # ── Events tab ──
        self._events_table = QTableWidget()
        self._events_table.setColumnCount(2)
        self._events_table.setHorizontalHeaderLabels(["ID", "摘要"])
        self._events_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._events_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._events_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._events_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._events_table.setSortingEnabled(True)
        events_widget = QWidget()
        events_lay = QVBoxLayout(events_widget)
        events_lay.setContentsMargins(4, 4, 4, 4)
        events_btn_row = QHBoxLayout()
        self._btn_del_event = QPushButton("🗑 删除所选")
        self._btn_del_event.clicked.connect(self._on_delete_event)
        events_btn_row.addWidget(self._btn_del_event)
        events_btn_row.addStretch()
        self._lbl_events_count = QLabel()
        events_btn_row.addWidget(self._lbl_events_count)
        events_lay.addLayout(events_btn_row)
        events_lay.addWidget(self._events_table)
        self._tabs.addTab(events_widget, "事件")

        # ── Bottom buttons ──
        btn_box = QHBoxLayout()
        self._btn_refresh = QPushButton("🔄 刷新")
        self._btn_refresh.clicked.connect(self._load_data)
        btn_box.addWidget(self._btn_refresh)
        btn_box.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_box.addWidget(close_btn)
        layout.addLayout(btn_box)

        self._load_data()

    def _load_data(self) -> None:
        """Reload both tables from the knowledge base."""
        self._load_terms()
        self._load_events()

    def _load_terms(self) -> None:
        self._terms_table.setSortingEnabled(False)
        self._terms_table.setRowCount(0)
        terms = self._kb.get_all_terms()
        self._terms_table.setRowCount(len(terms))
        for row, entry in enumerate(terms):
            self._terms_table.setItem(row, 0, QTableWidgetItem(entry.category))
            item_orig = QTableWidgetItem(entry.original)
            self._terms_table.setItem(row, 1, item_orig)
            self._terms_table.setItem(row, 2, QTableWidgetItem(entry.translation))
            self._terms_table.setItem(row, 3, QTableWidgetItem(entry.description))
        self._terms_table.setSortingEnabled(True)
        self._lbl_terms_count.setText(f"{len(terms)} 条术语")

    def _load_events(self) -> None:
        self._events_table.setSortingEnabled(False)
        self._events_table.setRowCount(0)
        rows = self._kb.get_all_events_rows()
        self._events_table.setRowCount(len(rows))
        for row, (eid, summary) in enumerate(rows):
            id_item = QTableWidgetItem()
            id_item.setData(Qt.ItemDataRole.DisplayRole, eid)
            self._events_table.setItem(row, 0, id_item)
            self._events_table.setItem(row, 1, QTableWidgetItem(summary))
        self._events_table.setSortingEnabled(True)
        self._lbl_events_count.setText(f"{len(rows)} 条事件")

    def _on_delete_term(self) -> None:
        selected = self._terms_table.selectedItems()
        if not selected:
            return
        rows = sorted({item.row() for item in selected}, reverse=True)
        originals = [self._terms_table.item(r, 1).text() for r in rows]
        if QMessageBox.question(
            self,
            "删除术语",
            f"确定删除 {len(originals)} 条术语？\n" + "\n".join(f"  • {o}" for o in originals[:10]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        for orig in originals:
            self._kb.delete_term(orig)
        self._load_terms()

    def _on_delete_event(self) -> None:
        selected = self._events_table.selectedItems()
        if not selected:
            return
        row_indices = sorted({item.row() for item in selected}, reverse=True)
        event_ids = [
            self._events_table.item(r, 0).data(Qt.ItemDataRole.DisplayRole)
            for r in row_indices
        ]
        if QMessageBox.question(
            self,
            "删除事件",
            f"确定删除 {len(event_ids)} 条事件？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        for eid in event_ids:
            self._kb.delete_event(int(eid))
        self._load_events()


# ---------------------------------------------------------------------------
# Dataset browser / annotation dialog
# ---------------------------------------------------------------------------

# Column indices
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


class _DatasetDialog(QDialog):
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



# ---------------------------------------------------------------------------
# Capture preview with bbox overlay
# ---------------------------------------------------------------------------

_BBOX_COLORS = [
    QColor(255,  80,  80),
    QColor( 80, 200,  80),
    QColor( 80, 130, 255),
    QColor(255, 200,  50),
    QColor(200,  80, 255),
    QColor( 80, 220, 220),
]


class _PreviewLabel(QLabel):
    """QLabel subclass that scales the captured frame and draws bbox overlays."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(QSize(400, 300))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setText("暂无画面。\n请选择游戏窗口。")
        self._raw: QPixmap | None = None
        self._boxes: list[BoundingBox] = []
        self._crop_rect: tuple[int, int, int, int] | None = None
        self._orig_w = 1
        self._orig_h = 1

        # Overlay visibility flags (toggled by checkboxes in the UI).
        self.show_image: bool = True
        self.show_boxes: bool = True
        self.show_labels: bool = True
        self.show_region: bool = True
        self.show_lines: bool = True
        self._line_boxes: list[BoundingBox] = []

    def update_frame(
        self,
        img_bytes: bytes,
        boxes: list[BoundingBox],
        line_boxes: list[BoundingBox],
        crop_rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        qimg = QImage.fromData(img_bytes)
        self._raw = QPixmap.fromImage(qimg)
        self._orig_w = qimg.width()
        self._orig_h = qimg.height()
        self._boxes = boxes
        self._line_boxes = line_boxes
        self._crop_rect = crop_rect
        self._render()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._raw is not None:
            self._render()

    def _render(self) -> None:
        if self._raw is None:
            return
        lw, lh = self.width(), self.height()

        if self.show_image:
            scaled = self._raw.scaled(
                lw, lh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            # Compute the same size as the scaled image would have, but
            # fill with a dark background instead of the game frame.
            tmp = self._raw.scaled(
                lw, lh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            scaled = QPixmap(tmp.width(), tmp.height())
            scaled.fill(QColor(40, 40, 40))

        sx = scaled.width()  / self._orig_w
        sy = scaled.height() / self._orig_h
        ox = (lw - scaled.width())  // 2
        oy = (lh - scaled.height()) // 2

        if self._boxes and (self.show_boxes or self.show_labels):
            painter = QPainter(scaled)
            font = QFont("Consolas", 7)
            painter.setFont(font)
            for i, box in enumerate(self._boxes):
                color = _BBOX_COLORS[i % len(_BBOX_COLORS)]
                rx = int(box.x * sx)
                ry = int(box.y * sy)
                rw = max(1, int(box.w * sx))
                rh = max(1, int(box.h * sy))
                if self.show_boxes:
                    painter.setPen(QPen(color, 1))
                    painter.drawRect(rx, ry, rw, rh)
                if self.show_labels:
                    label_w = min(rw, 150)
                    painter.fillRect(rx, max(0, ry - 11), label_w, 11, QColor(0, 0, 0, 160))
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(rx + 1, max(9, ry - 1), box.text[:24])
            painter.end()

        if self._line_boxes and self.show_lines:
            plines = QPainter(scaled)
            pen_line = QPen(QColor(80, 255, 200), 1, Qt.PenStyle.DotLine)
            plines.setPen(pen_line)
            for lb in self._line_boxes:
                plines.drawRect(
                    int(lb.x * sx), int(lb.y * sy),
                    max(1, int(lb.w * sx)), max(1, int(lb.h * sy)),
                )
            plines.end()

        if self._crop_rect is not None and self.show_region:
            cl, ct, cr, cb = self._crop_rect
            painter2 = QPainter(scaled)
            pen = QPen(QColor(255, 255, 100), 2, Qt.PenStyle.DashLine)
            painter2.setPen(pen)
            painter2.drawRect(
                int(cl * sx), int(ct * sy),
                max(1, int((cr - cl) * sx)),
                max(1, int((cb - ct) * sy)),
            )
            painter2.end()

        canvas = QPixmap(lw, lh)
        canvas.fill(QColor(28, 28, 28))
        p2 = QPainter(canvas)
        p2.drawPixmap(ox, oy, scaled)
        p2.end()
        self.setPixmap(canvas)


# ---------------------------------------------------------------------------
# Helper: labelled text panel
# ---------------------------------------------------------------------------

def _make_panel(title: str) -> tuple[QGroupBox, QTextEdit]:
    grp = QGroupBox(title)
    te = QTextEdit()
    te.setReadOnly(True)
    te.setFont(QFont("Consolas", 9))
    lay = QVBoxLayout(grp)
    lay.setContentsMargins(3, 3, 3, 3)
    lay.addWidget(te)
    return grp, te


# ---------------------------------------------------------------------------
# Step panel with per-step latency label + proportion bar
# ---------------------------------------------------------------------------

class _StepPanel(QWidget):
    """Pipeline step panel showing title, rolling-average latency, and a
    proportion bar that grows with the step's share of total pipeline time.

    Layout (top to bottom inside a styled frame):
      header row  ·  <title>  ────────  avg: X ms  ·  now: Y ms
      proportion  ·  ████████░░░░░░░░░░░░░░░  (fraction of total elapsed)
      text area   ·  read-only QTextEdit
    """

    _EMA_ALPHA: float = 0.2   # exponential moving-average smoothing factor

    def __init__(
        self,
        title: str,
        color: tuple[int, int, int],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._base_title = title
        self._color = color
        self._avg_ms: float = 0.0
        self._n: int = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)

        # Styled frame for the visual border
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(4, 5, 4, 4)
        frame_lay.setSpacing(3)

        # Header row: bold title on the left, latency info on the right
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._lbl_title = QLabel(f"<b>{title}</b>")
        self._lbl_latency = QLabel("avg: —    now: —")
        self._lbl_latency.setStyleSheet("color: #999; font-size: 8pt;")
        self._lbl_latency.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self._lbl_title)
        header.addStretch()
        header.addWidget(self._lbl_latency)
        frame_lay.addLayout(header)

        # Proportion bar
        r, g, b = color
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setFixedHeight(5)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: #2e2e2e; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: rgb({r},{g},{b}); border-radius: 2px; }}"
        )
        frame_lay.addWidget(self._bar)

        # Read-only text area
        self.te = QTextEdit()
        self.te.setReadOnly(True)
        self.te.setFont(QFont("Consolas", 9))
        self.te.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.te.setStyleSheet(
            "QScrollBar:vertical { background: #252525; width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: #4a4a4a; min-height: 20px;"
            " border-radius: 3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        )
        frame_lay.addWidget(self.te, 1)

        outer.addWidget(frame, 1)

        # Reference kept for add_settings_row()
        self._frame_lay = frame_lay

    # ------------------------------------------------------------------

    def add_settings_row(self, widget: QWidget) -> None:
        """Insert a compact settings row between the proportion bar and text.

        The widget is inserted at index 2 (after header + bar, before te).
        """
        self._frame_lay.insertWidget(2, widget)

    def set_subtitle(self, subtitle: str) -> None:
        """Update the optional subtitle appended to the title label."""
        if subtitle:
            self._lbl_title.setText(f"<b>{self._base_title}</b>  [{subtitle}]")
        else:
            self._lbl_title.setText(f"<b>{self._base_title}</b>")

    def update_timing(self, now_ms: float, total_ms: float) -> None:
        """Update EMA average, latency label text, and proportion bar."""
        self._n += 1
        if self._n == 1:
            self._avg_ms = now_ms
        else:
            self._avg_ms = (
                self._avg_ms * (1.0 - self._EMA_ALPHA)
                + now_ms * self._EMA_ALPHA
            )
        self._lbl_latency.setText(
            f"avg {self._avg_ms:.0f} ms  ·  now {now_ms:.0f} ms"
        )
        ratio = now_ms / total_ms if total_ms > 0 else 0.0
        self._bar.setValue(int(min(ratio, 1.0) * 1000))


# ---------------------------------------------------------------------------
# Main debug window
# ---------------------------------------------------------------------------

class DebugWindow(QMainWindow):
    """Full-pipeline debug window.

    Toolbar
    -------
    Pick Window
        Minimises this window and waits for the user to click the game process.
    OCR lang
        Combo of installed Windows OCR languages.
    Interval
        Refresh interval in ms.

    Panels
    ------
    Left:  Capture preview (scaled, with bbox overlay).
    Right: Windows OCR · Detected Region · Memory Scan · Translation.

    When opened from :class:`MainWindow`, caller passes *knowledge_base*,
    *translator*, and *target* so that both windows share a single backend
    instance and knowledge base.  Set *standalone=False* to prevent this
    window from calling ``QApplication.quit()`` on close.
    """

    #: Emitted just before the window closes, regardless of *standalone*.
    closed = Signal()

    def __init__(self, backend: AppBackend, *, standalone: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("JustReadIt")
        self.resize(1400, 820)

        primary = QApplication.primaryScreen()
        if primary is not None:
            self.move(primary.availableGeometry().center() - self.rect().center())

        self._standalone = standalone
        self._backend = backend
        self._picker: WindowPicker | None = None

        self._install_proc_handle: int | None = None
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(500)
        self._install_timer.timeout.connect(self._poll_install)

        # -- state for debug dump --
        self._last_result: PipelineResult | None = None

        self._build_ui()

        # Connect to backend signals — views own no backend resources.
        self._backend.translation_ready.connect(self._on_translation)
        self._backend.pipeline_debug.connect(self._on_result)
        self._backend.pipeline_progress.connect(self._on_pipeline_progress)
        self._backend.freeze_triggered.connect(self._on_freeze_triggered)
        self._backend.dump_triggered.connect(self._on_dump_triggered)
        self._backend.error.connect(self._on_error)
        self._backend.ready.connect(self._on_worker_ready)
        self._backend.freeze_overlay.dismissed.connect(self._on_freeze_dismissed)
        self._backend.paused_changed.connect(self._on_paused_changed)
        self._backend.recording_changed.connect(self._on_recording_changed)

        # If the backend is already running when this window is created,
        # the ready signal was emitted before our connection — catch up now.
        if self._backend.is_running:
            self._on_worker_ready()

        # Populate target label if backend already has a target.
        if backend.target is not None:
            t = backend.target
            self._lbl_target.setText(
                f"{t.process_name}  (PID {t.pid})"
                f"  [{t.window_rect.width}\u00d7{t.window_rect.height}]"
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        self._btn_pick = QPushButton("⊕ 选择窗口")
        self._btn_pick.setToolTip("最小化本窗口，点击游戏窗口以选中目标")
        self._btn_pick.clicked.connect(self._start_picking)
        tb.addWidget(self._btn_pick)
        tb.addSeparator()

        tb.addWidget(QLabel("目标: "))
        self._lbl_target = QLabel("—")
        self._lbl_target.setMinimumWidth(260)
        tb.addWidget(self._lbl_target)
        tb.addSeparator()

        tb.addWidget(QLabel(" 悬停延迟: "))
        self._spn_interval = QSpinBox()
        self._spn_interval.setRange(200, 15000)
        self._spn_interval.setValue(1500)
        self._spn_interval.setSuffix(" ms")
        self._spn_interval.setToolTip(
            "鼠标悬停多久（毫秒）后触发翻译流水线。\n"
            "鼠标大幅移动（≥20 px）会重置计时器，\n"
            "即鼠标不停移动时不会重复触发。"
        )
        tb.addWidget(self._spn_interval)
        tb.addSeparator()

        tb.addWidget(QLabel(" 冻结键: "))
        self._cmb_freeze_key = QComboBox()
        _VK_FKEYS = [
            ("F1", 0x70), ("F2", 0x71), ("F3", 0x72), ("F4", 0x73),
            ("F5", 0x74), ("F6", 0x75), ("F7", 0x76), ("F8", 0x77),
            ("F9", 0x78), ("F10", 0x79), ("F11", 0x7A), ("F12", 0x7B),
        ]
        for label, vk in _VK_FKEYS:
            self._cmb_freeze_key.addItem(label, userData=vk)
        self._cmb_freeze_key.setToolTip("切换冻结模式的快捷键")
        tb.addWidget(self._cmb_freeze_key)
        tb.addSeparator()

        tb.addWidget(QLabel(" 快照键: "))
        self._cmb_dump_key = QComboBox()
        for label, vk in _VK_FKEYS:
            self._cmb_dump_key.addItem(label, userData=vk)
        self._cmb_dump_key.setToolTip("按下后将 OCR / 内存 / 校正文本快照复制到剪贴板")
        tb.addWidget(self._cmb_dump_key)
        tb.addSeparator()

        self._btn_pause = QPushButton("\u23f8 暂停")
        self._btn_pause.setToolTip("暂停 / 恢复翻译流水线")
        self._btn_pause.setFixedWidth(78)
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause_clicked)
        tb.addWidget(self._btn_pause)

        self._btn_record = QPushButton("\u23fa 记录")
        self._btn_record.setToolTip("开始 / 停止将流水线样本录入数据集（用于算法迭代）")
        self._btn_record.setFixedWidth(78)
        self._btn_record.setCheckable(True)
        self._btn_record.clicked.connect(self._on_record_clicked)
        tb.addWidget(self._btn_record)

        # ── Right-aligned tools menu ──────────────────────────────────
        _spacer = QWidget()
        _spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(_spacer)

        self._act_clear_cache = QAction("🗑 清除缓存", self)
        self._act_clear_cache.setToolTip(
            "清空内存缓存和持久化翻译缓存。\n"
            "更新应用后使用，强制以改进的逻辑重新翻译。"
        )
        self._act_clear_cache.triggered.connect(self._on_clear_cache)

        act_knowledge = QAction("📚 知识库管理", self)
        act_knowledge.setToolTip("浏览或删除知识库中的术语和事件。")
        act_knowledge.triggered.connect(self._on_open_knowledge_manager)

        act_dataset = QAction("📊 数据集标注", self)
        act_dataset.setToolTip("浏览、标注和删除流水线数据集样本。")
        act_dataset.triggered.connect(self._on_open_dataset)

        _tools_menu = QMenu(self)
        _tools_menu.addAction(self._act_clear_cache)
        _tools_menu.addSeparator()
        _tools_menu.addAction(act_knowledge)
        _tools_menu.addAction(act_dataset)

        self._btn_tools = QToolButton()
        self._btn_tools.setText("工具")
        self._btn_tools.setToolTip("缓存 / 知识库管理")
        self._btn_tools.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._btn_tools.setMenu(_tools_menu)
        tb.addWidget(self._btn_tools)

        # ── Install progress bar (hidden until capability install) ───
        self._install_bar = QWidget()
        _ibl = QHBoxLayout(self._install_bar)
        _ibl.setContentsMargins(6, 3, 6, 3)
        self._install_lbl = QLabel("正在安装…")
        self._install_prog = QProgressBar()
        self._install_prog.setRange(0, 0)   # indeterminate
        self._install_prog.setFixedHeight(16)
        _ibl.addWidget(self._install_lbl)
        _ibl.addWidget(self._install_prog, 1)
        self._install_bar.setVisible(False)

        # ── Central splitter ───────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        central = QWidget(self)
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(self._install_bar)
        central_lay.addWidget(splitter)
        self.setCentralWidget(central)

        # -- Left column: game preview + overlay toggles --
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(2)

        # Overlay-visibility checkboxes.
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(4, 2, 4, 0)
        toggle_row.setSpacing(10)

        self._chk_image  = QCheckBox("画面")
        self._chk_lines  = QCheckBox("OCR行")
        self._chk_boxes  = QCheckBox("OCR框")
        self._chk_labels = QCheckBox("OCR结果")
        self._chk_region = QCheckBox("聚合范围")

        for chk in (self._chk_image, self._chk_lines, self._chk_boxes,
                    self._chk_labels, self._chk_region):
            chk.setChecked(True)
            toggle_row.addWidget(chk)

        toggle_row.addWidget(_vsep := QFrame())
        _vsep.setFrameShape(QFrame.Shape.VLine)
        _vsep.setFrameShadow(QFrame.Shadow.Sunken)
        _vsep.setFixedWidth(2)

        self._chk_mem_scan = QCheckBox("内存扫描")
        self._chk_mem_scan.setToolTip(
            "启用 ReadProcessMemory 内存扫描（提升文本精度，但会增加内存占用）\n"
            "内存较大的游戏建议关闭以使用纯 OCR 模式"
        )
        toggle_row.addWidget(self._chk_mem_scan)
        toggle_row.addStretch()

        left_lay.addLayout(toggle_row)

        self._preview = _PreviewLabel(self)
        left_lay.addWidget(self._preview, 1)  # stretch=1 so preview fills space

        # Wire checkboxes → preview flags; re-render on toggle.
        self._chk_image.toggled.connect(self._on_toggle_image)
        self._chk_lines.toggled.connect(self._on_toggle_lines)
        self._chk_boxes.toggled.connect(self._on_toggle_boxes)
        self._chk_labels.toggled.connect(self._on_toggle_labels)
        self._chk_region.toggled.connect(self._on_toggle_region)

        splitter.addWidget(left)

        # -- Right column: text panels (in a scroll area for small screens) --
        right = QSplitter(Qt.Orientation.Vertical)
        right.setMinimumHeight(860)  # 220+120+100+140+180+100 = 860; scroll below this
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)

        self._panel_wocr   = _StepPanel("Windows OCR",  (80,  160, 255))
        self._panel_region = _StepPanel("检测区域",      (80,  210, 120))
        self._panel_mem    = _StepPanel("内存扫描",      (255, 160,  50))
        self._panel_corr   = _StepPanel("校正文本",      (180, 100, 255))
        self._panel_tl     = _StepPanel("翻译",          ( 80, 220, 200))

        # ── OCR settings row (embedded inside the Windows OCR panel) ──
        ocr_settings = QWidget()
        ocr_sl = QHBoxLayout(ocr_settings)
        ocr_sl.setContentsMargins(0, 2, 0, 2)
        ocr_sl.setSpacing(6)
        ocr_sl.addWidget(QLabel("语言:"))
        self._cmb_lang = QComboBox()
        self._cmb_lang.setToolTip("Windows OCR 识别语言")
        self._populate_languages()
        ocr_sl.addWidget(self._cmb_lang)
        ocr_sl.addSpacing(12)
        ocr_sl.addWidget(QLabel("最大尺寸:"))
        self._spn_ocr_max = QSpinBox()
        self._spn_ocr_max.setRange(480, 7680)
        self._spn_ocr_max.setSingleStep(240)
        self._spn_ocr_max.setSuffix(" px")
        self._spn_ocr_max.setToolTip(
            "送入 Windows OCR 的图像最大长边（像素）。\n"
            "1920 对 1080p 无影响，4K 帧减半。\n"
            "下次启动流水线时生效。"
        )
        ocr_sl.addWidget(self._spn_ocr_max)
        ocr_sl.addStretch()
        self._panel_wocr.add_settings_row(ocr_settings)

        # Restore OCR language (not managed by the config-model mapper
        # because the combo has custom language-pack install logic).
        saved_lang = _cfg.ocr.language
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == saved_lang:
                self._cmb_lang.setCurrentIndex(i)
                break

        # Convenience aliases so the rest of the code keeps working unchanged.
        self._te_wocr   = self._panel_wocr.te
        self._te_region = self._panel_region.te
        self._te_mem    = self._panel_mem.te
        self._te_corr   = self._panel_corr.te
        self._te_tl     = self._panel_tl.te

        self._te_region.setPlaceholderText(
            "区域文本将在范围检测后显示。"
        )
        self._te_mem.setPlaceholderText(
            "内存扫描结果显示在此。\n"
            "通过 ReadProcessMemory 扫描游戏堆内存中的 OCR 文本子串。"
        )
        self._te_corr.setPlaceholderText(
            "校正文本（OCR ↔ 内存最佳匹配）显示在此。\n"
            "无高置信匹配时回退到 OCR 区域文本。"
        )
        self._te_tl.setPlaceholderText(
            "请在下方配置翻译后端并点击「应用」以启用。"
        )

        right.addWidget(self._panel_wocr)
        right.addWidget(self._panel_region)
        right.addWidget(self._panel_mem)
        right.addWidget(self._panel_corr)
        right.addWidget(self._build_translator_settings_panel())
        right.addWidget(self._panel_tl)
        right.setSizes([80, 120, 100, 140, 280, 140])

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 980])

        # ── Status bar ─────────────────────────────────────────────────
        self.setStatusBar(QStatusBar(self))
        # Right-aligned notification label for transient action feedback.
        self._notify_label = QLabel("")
        self._notify_label.setStyleSheet("padding-right: 8px; color: #ccc;")
        self.statusBar().addPermanentWidget(self._notify_label)
        self._notify_timer = QTimer(self)
        self._notify_timer.setSingleShot(True)
        self._notify_timer.timeout.connect(lambda: self._notify_label.setText(""))

        # Connect widget change handlers AFTER populating to avoid spurious
        # signals (e.g. the install prompt for missing OCR language packs).
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)

        # ── QDataWidgetMapper ────────────────────────────────────────
        # Two-way binding: widget edits → config, config changes → widget.
        # Replaces manual restore loops, widget→config handlers, and
        # blockSignals sync slots for all mapped widgets.
        self._mapper = ConfigModel.create_mapper(
            self,
            (self._spn_interval, ConfigModel.INTERVAL_MS),
            (self._spn_ocr_max, ConfigModel.OCR_MAX_SIZE),
            (self._cmb_freeze_key, ConfigModel.FREEZE_VK),
            (self._cmb_dump_key, ConfigModel.DUMP_VK),
            (self._chk_mem_scan, ConfigModel.MEMORY_SCAN_ENABLED),
        )

        # OCR language combo is NOT mapped (custom install logic in
        # _on_lang_changed); sync from config manually.
        _cfg.ocr.language_changed.connect(self._sync_lang_combo)

    # ------------------------------------------------------------------
    # Overlay toggle handlers
    # ------------------------------------------------------------------

    def _on_toggle_image(self, checked: bool) -> None:
        self._preview.show_image = checked
        self._preview._render()

    def _on_toggle_lines(self, checked: bool) -> None:
        self._preview.show_lines = checked
        self._preview._render()

    def _on_toggle_boxes(self, checked: bool) -> None:
        self._preview.show_boxes = checked
        self._preview._render()

    def _on_toggle_labels(self, checked: bool) -> None:
        self._preview.show_labels = checked
        self._preview._render()

    def _on_toggle_region(self, checked: bool) -> None:
        self._preview.show_region = checked
        self._preview._render()

    # ------------------------------------------------------------------
    # Language helpers
    # ------------------------------------------------------------------

    def _populate_languages(self) -> None:
        """Fill lang combo with available Windows OCR languages."""
        try:
            import winrt.windows.media.ocr as wocr
            _ensure_apartment()

            installed_tags: set[str] = set()
            for lang in wocr.OcrEngine.available_recognizer_languages:
                tag = lang.language_tag
                installed_tags.add(tag)
                self._cmb_lang.addItem(display_name(tag), userData=tag)

            for tag, capability in _LANG_CAPABILITIES.items():
                # WinRT tags are region-specific (e.g. "ja-JP"), while
                # _LANG_CAPABILITIES keys are bare subtags ("ja").  Accept
                # any installed tag that equals or starts with "<tag>-".
                if any(t == tag or t.startswith(tag + "-") for t in installed_tags):
                    continue
                self._cmb_lang.addItem(
                    f"{tag}  ⬇ 点击安装 (~6 MB)",
                    userData=tag,
                )

            for i in range(self._cmb_lang.count()):
                if self._cmb_lang.itemData(i) == "en-US":
                    self._cmb_lang.setCurrentIndex(i)
                    break
        except Exception as exc:
            self._cmb_lang.addItem(f"(error: {exc})", userData="en-US")

    @property
    def _selected_language(self) -> str:
        return self._cmb_lang.currentData() or "en-US"

    @Slot(int)
    def _on_lang_changed(self, index: int) -> None:
        tag = self._cmb_lang.itemData(index)
        if not tag:
            return

        if tag in _LANG_CAPABILITIES:
            try:
                import winrt.windows.media.ocr as wocr
                import winrt.windows.globalization as glob
                _ensure_apartment()
                if not wocr.OcrEngine.is_language_supported(glob.Language(tag)):
                    self._start_install(tag)
                    return
            except Exception as exc:
                _log.warning("WinRT OCR language check failed for %r: %s", tag, exc)

        _cfg.ocr.language = tag  # signal → backend restart if running
        if self._backend.is_running:
            self.statusBar().showMessage(
                f"正在以 lang={tag} 重启流水线…"
            )

    # ------------------------------------------------------------------
    # Language pack installation
    # ------------------------------------------------------------------

    def _start_install(self, lang_tag: str) -> None:
        capability = _LANG_CAPABILITIES[lang_tag]
        reply = QMessageBox.question(
            self,
            "安装 Windows OCR 语言包",
            f"OCR 语言 '{lang_tag}' 的语言包尚未安装。\n\n"
            f"Capability:  {capability}\n\n"
            "是否立即安装？（约 6 MB，仅 OCR 数据 — 不会更改系统语言）\n"
            "安装时会出现管理员（UAC）提权提示。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        args = (
            f"-NoProfile -ExecutionPolicy Bypass "
            f"-Command \"Add-WindowsCapability -Online -Name '{capability}'\""
        )
        sei = _SHELLEXECUTEINFOW()
        sei.cbSize       = ctypes.sizeof(sei)
        sei.fMask        = _SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb       = "runas"
        sei.lpFile       = "powershell.exe"
        sei.lpParameters = args
        sei.nShow        = 1  # SW_SHOWNORMAL
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        if not ok or not sei.hProcess:
            self.statusBar().showMessage(
                "无法启动安装程序 — UAC 被拒绝或未找到 PowerShell。",
                8000,
            )
            return

        self._install_proc_handle = sei.hProcess
        self._install_lbl.setText(
            f"正在安装 {capability}…  "
            "（可能需要一分钟 — 请勿关闭此窗口）"
        )
        self._install_bar.setVisible(True)
        self._install_timer.start()
        self.statusBar().showMessage(f"正在安装 {capability}…")

    @Slot()
    def _poll_install(self) -> None:
        if self._install_proc_handle is None:
            self._install_timer.stop()
            return
        result = _kernel32_ui.WaitForSingleObject(
            ctypes.c_void_p(self._install_proc_handle), 0
        )
        if result != _WAIT_TIMEOUT:
            self._finish_install()

    def _finish_install(self) -> None:
        self._install_timer.stop()
        if self._install_proc_handle is not None:
            _kernel32_ui.CloseHandle(
                ctypes.c_void_p(self._install_proc_handle)
            )
            self._install_proc_handle = None
        self._install_bar.setVisible(False)

        current_tag = self._cmb_lang.currentData()
        self._cmb_lang.currentIndexChanged.disconnect(self._on_lang_changed)
        self._cmb_lang.clear()
        self._populate_languages()
        self._cmb_lang.currentIndexChanged.connect(self._on_lang_changed)
        for i in range(self._cmb_lang.count()):
            if self._cmb_lang.itemData(i) == current_tag:
                self._cmb_lang.setCurrentIndex(i)
                break
        self.statusBar().showMessage(
            "语言包安装完成 — 已自动生效。", 8000
        )

    # ------------------------------------------------------------------
    # Window picking
    # ------------------------------------------------------------------

    def _start_picking(self) -> None:
        self.statusBar().showMessage(
            "请点击游戏窗口以选中目标…  （右键取消）"
        )
        self._btn_pick.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.CrossCursor)
        self.showMinimized()
        self._picker = WindowPicker(self)
        self._picker.picked.connect(self._on_window_picked)
        self._picker.cancelled.connect(self._on_pick_cancelled)
        QTimer.singleShot(400, self._picker.start)

    @Slot(int)
    def _on_window_picked(self, pid: int) -> None:
        QApplication.restoreOverrideCursor()
        self.showNormal()
        self._btn_pick.setEnabled(True)
        try:
            target = GameTarget.from_pid(pid)
        except Exception as exc:
            self.statusBar().showMessage(f"GameTarget error: {exc}", 8000)
            return
        self._set_target(target)

    @Slot()
    def _on_pick_cancelled(self) -> None:
        QApplication.restoreOverrideCursor()
        self.showNormal()
        self._btn_pick.setEnabled(True)
        self.statusBar().showMessage("已取消选择。", 3000)

    def _set_target(self, target: GameTarget) -> None:
        w = target.window_rect.width
        h = target.window_rect.height
        self._lbl_target.setText(
            f"{target.process_name}  (PID {target.pid})  [{w}×{h}]"
        )
        self.statusBar().showMessage(
            f"目标: {target.process_name}  PID={target.pid}"
            f"  output_idx={target.dxcam_output_idx}",
            5000,
        )
        self._backend.set_target(target)

    # ------------------------------------------------------------------
    # Pipeline lifecycle (delegated to AppBackend)
    # ------------------------------------------------------------------

    @Slot()
    def _on_clear_cache(self) -> None:
        self._backend.clear_caches()
        self.statusBar().showMessage("翻译缓存已清除。", 3000)

    @Slot()
    def _on_worker_ready(self) -> None:
        self._btn_pause.setEnabled(True)
        lang = self._selected_language
        interval = self._spn_interval.value()
        self.statusBar().showMessage(
            f"运行中 — 语言={lang}  延迟={interval} ms"
        )

    # ------------------------------------------------------------------
    # Result / error handlers
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_result(self, result: PipelineResult) -> None:
        """Update debug panels with intermediate pipeline data."""
        self._last_result = result
        ocr = result.ocr.value
        rng = result.range_det.value
        self._preview.update_frame(
            result.img_bytes, ocr.boxes, ocr.line_boxes, rng.crop_rect
        )
        header = f"[ {len(ocr.boxes)} boxes  \u2014  {result.elapsed_ms:.0f} ms ]\n\n"
        self._te_wocr.setPlainText(header + ocr.text)
        self._panel_region.set_subtitle(rng.detector_name)
        self._te_region.setPlainText(rng.region_text)
        self._te_mem.setPlainText(result.scan.value)
        self._te_corr.setPlainText(result.corr.value)
        if result.translate.value:
            self._te_tl.setPlainText(result.translate.value)
        total = max(result.elapsed_ms, 1.0)
        self._panel_wocr.update_timing(result.ocr.ms, total)
        self._panel_region.update_timing(result.range_det.ms, total)
        self._panel_mem.update_timing(result.scan.ms, total)
        self._panel_corr.update_timing(result.corr.ms, total)
        self._panel_tl.update_timing(result.translate.ms, total)

    @Slot(str, object, object)
    def _on_pipeline_progress(
        self,
        step: str,
        near_rect: object,
        screen_origin: object,
    ) -> None:
        """Show a loading indicator in the status bar."""
        self.statusBar().showMessage(f"⏳ {step}")

    @Slot(str, object, object)
    def _on_translation(
        self,
        text: str,
        near_rect: object,
        screen_origin: object,
    ) -> None:
        """Update translation panel (overlay is handled by AppBackend)."""
        if self._te_tl is not None:
            self._te_tl.setPlainText(text)

    @Slot(object, int, int, int, int)
    def _on_freeze_triggered(
        self,
        screenshot: object,
        window_left: int,
        window_top: int,
        pid: int,
        hwnd: int,
    ) -> None:
        """Update status bar when freeze mode starts (overlay handled by AppBackend)."""
        freeze_key = self._cmb_freeze_key.currentText()
        self.statusBar().showMessage(f"❄ Freeze — 右键/Esc 退出  ({freeze_key} 再次切换)")

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._notify(f"⚠  {message}", 10000)
        self._te_wocr.append(f"\n[worker error] {message}")

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------

    @Slot()
    def _on_pause_clicked(self) -> None:
        self._backend.set_paused(not self._backend.is_paused)

    @Slot(bool)
    def _on_paused_changed(self, paused: bool) -> None:
        if paused:
            self._btn_pause.setText("\u25b6 恢复")
            self.statusBar().showMessage("已暂停")
        else:
            self._btn_pause.setText("\u23f8 暂停")
            lang = self._selected_language
            interval = self._spn_interval.value()
            self.statusBar().showMessage(
                f"运行中 — 语言={lang}  延迟={interval} ms"
            )

    @Slot()
    def _on_record_clicked(self) -> None:
        self._backend.set_recording(self._btn_record.isChecked())

    @Slot(bool)
    def _on_recording_changed(self, recording: bool) -> None:
        with QSignalBlocker(self._btn_record):
            self._btn_record.setChecked(recording)
        self._btn_record.setText("⏹ 停止" if recording else "⏺ 记录")
        self._btn_record.setStyleSheet(
            "QPushButton { color: #e55; font-weight: bold; }" if recording else ""
        )
        self.statusBar().showMessage(
            "🔴 数据集记录中…" if recording else "⏹ 数据集记录已停止", 4000
        )

    @Slot()
    def _on_open_dataset(self) -> None:
        """Open the dataset annotation dialog, initialising the DB if needed."""
        from src.paths import dataset_db_path   # noqa: PLC0415
        from src.dataset import PipelineDataset  # noqa: PLC0415
        # Prefer the already-open backend dataset so recorded-but-not-committed
        # rows are immediately visible.  Fall back to a fresh read-only connection.
        ds = self._backend.dataset or PipelineDataset.open(dataset_db_path())
        dlg = _DatasetDialog(ds, parent=self)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    # ------------------------------------------------------------------
    # Clean shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closed.emit()
        super().closeEvent(event)
        if self._standalone:
            QApplication.instance().quit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Freeze mode
    # ------------------------------------------------------------------

    @Slot()
    def _on_dump_triggered(self) -> None:
        """Show OCR / Memory / Corrected snapshot in a dialog and copy to clipboard."""
        r = self._last_result
        if r is None:
            QMessageBox.information(self, "调试快照", "暂无流水线结果可导出。")
            return
        ocr_text  = r.range_det.value.region_text.strip()
        mem_text  = r.scan.value.strip()
        corr_text = r.corr.value.strip()
        tl_text   = r.translate.value.strip()
        lines = [
            "=== JustReadIt Debug Snapshot ===",
            f"[OCR]\n{ocr_text}",
            f"[Memory]\n{mem_text}" if mem_text else "[Memory]\n无结果",
            f"[Corrected]\n{corr_text}" if corr_text else "[Corrected]\n无结果",
        ]
        if tl_text:
            lines.append(f"[Translation]\n{tl_text}")
        text = "\n\n".join(lines)
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("📋 调试快照已复制到剪贴板", 4000)

    @Slot()
    def _on_freeze_dismissed(self) -> None:
        lang = self._selected_language
        interval = self._spn_interval.value()
        self.statusBar().showMessage(
            f"运行中 — 语言={lang}  延迟={interval} ms"
        )

    # ------------------------------------------------------------------
    # Interval change
    # ------------------------------------------------------------------

    def _notify(self, msg: str, ms: int = 4000) -> None:
        """Show a transient right-aligned message in the status bar."""
        self._notify_label.setText(msg)
        self._notify_timer.start(ms)

    # ------------------------------------------------------------------
    # Knowledge Manager
    # ------------------------------------------------------------------

    @Slot()
    def _on_open_knowledge_manager(self) -> None:
        """Open the Knowledge Manager dialog (non-modal)."""
        dlg = _KnowledgeManagerDialog(self._backend.knowledge_base, parent=self)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    # ------------------------------------------------------------------
    # Reactive config → widget sync (unmapped widgets only)
    # ------------------------------------------------------------------
    # Widgets bound via QDataWidgetMapper auto-sync; only the OCR language
    # combo (which has custom install logic) needs a manual sync slot.

    @Slot(str)
    def _sync_lang_combo(self, tag: str) -> None:
        """Update OCR language combo from config (not managed by mapper)."""
        with QSignalBlocker(self._cmb_lang):
            for i in range(self._cmb_lang.count()):
                if self._cmb_lang.itemData(i) == tag:
                    self._cmb_lang.setCurrentIndex(i)
                    break

    # ------------------------------------------------------------------
    # Translator settings panel
    # ------------------------------------------------------------------

    def _build_translator_settings_panel(self) -> QWidget:
        """Translator configuration panel backed by the shared widget."""
        grp = QGroupBox("翻译设置")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(0)
        self._tl_settings = TranslatorSettingsWidget(
            self._backend,
            auto_build=True,
            parent=grp,
        )
        lay.addWidget(self._tl_settings)
        return grp
