# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""知识库管理对话框。

独立模块，可从调试窗口或其他 UI 入口以非模态方式打开。

Usage::

    from src.ui.knowledge_manager import KnowledgeManagerDialog

    dlg = KnowledgeManagerDialog(knowledge_base, parent=self)
    dlg.setWindowModality(Qt.WindowModality.NonModal)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from src.knowledge import KnowledgeBase


class KnowledgeManagerDialog(QDialog):
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
