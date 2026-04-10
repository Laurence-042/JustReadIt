# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ConfigModel and ConfigDelegate for ``QDataWidgetMapper`` integration.

``ConfigModel`` exposes a subset of :class:`~src.config.AppConfig` properties
as a **single-row** :class:`QAbstractTableModel`.  Pair with
:class:`QDataWidgetMapper` (``AutoSubmit``) and :class:`ConfigDelegate`
for zero-boilerplate two-way binding between widgets and the config
singleton.

The delegate handles :class:`QComboBox` items keyed by ``itemData``
(e.g. VK codes, BCP-47 tags) transparently.
"""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDataWidgetMapper,
    QStyledItemDelegate,
    QWidget,
)

from src.config import AppConfig


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ConfigModel(QAbstractTableModel):
    """Single-row table model exposing :class:`AppConfig` properties.

    Column constants (``INTERVAL_MS``, ``FREEZE_VK``, …) are used as the
    *section* argument to ``QDataWidgetMapper.addMapping()``.
    """

    # Column indices — add new mapped properties here.
    INTERVAL_MS = 0
    OCR_MAX_SIZE = 1
    OCR_LANGUAGE = 2
    FREEZE_VK = 3
    DUMP_VK = 4
    MEMORY_SCAN_ENABLED = 5
    _COL_COUNT = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        cfg = AppConfig()

        self._getters: dict[int, object] = {
            self.INTERVAL_MS: lambda: cfg.interval_ms,
            self.OCR_MAX_SIZE: lambda: cfg.ocr_max_size,
            self.OCR_LANGUAGE: lambda: cfg.ocr_language,
            self.FREEZE_VK: lambda: cfg.freeze_vk,
            self.DUMP_VK: lambda: cfg.dump_vk,
            self.MEMORY_SCAN_ENABLED: lambda: cfg.memory_scan_enabled,
        }
        self._setters: dict[int, object] = {
            self.INTERVAL_MS: lambda v: setattr(cfg, "interval_ms", int(v)),
            self.OCR_MAX_SIZE: lambda v: setattr(cfg, "ocr_max_size", int(v)),
            self.OCR_LANGUAGE: lambda v: setattr(cfg, "ocr_language", str(v)),
            self.FREEZE_VK: lambda v: setattr(cfg, "freeze_vk", int(v)),
            self.DUMP_VK: lambda v: setattr(cfg, "dump_vk", int(v)),
            self.MEMORY_SCAN_ENABLED: lambda v: setattr(
                cfg, "memory_scan_enabled", bool(v),
            ),
        }

        # Forward config change signals → dataChanged so mappers revert.
        cfg.interval_ms_changed.connect(
            lambda: self._notify(self.INTERVAL_MS),
        )
        cfg.ocr_max_size_changed.connect(
            lambda: self._notify(self.OCR_MAX_SIZE),
        )
        cfg.ocr_language_changed.connect(
            lambda: self._notify(self.OCR_LANGUAGE),
        )
        cfg.freeze_vk_changed.connect(
            lambda: self._notify(self.FREEZE_VK),
        )
        cfg.dump_vk_changed.connect(
            lambda: self._notify(self.DUMP_VK),
        )
        cfg.memory_scan_enabled_changed.connect(
            lambda: self._notify(self.MEMORY_SCAN_ENABLED),
        )

    # ── QAbstractTableModel interface ─────────────────────────────────

    def rowCount(  # noqa: N802
        self, parent: QModelIndex = QModelIndex(),
    ) -> int:
        return 0 if parent.isValid() else 1

    def columnCount(  # noqa: N802
        self, parent: QModelIndex = QModelIndex(),
    ) -> int:
        return 0 if parent.isValid() else self._COL_COUNT

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsEnabled

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        getter = self._getters.get(index.column())
        return getter() if getter else None

    def setData(  # noqa: N802
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        setter = self._setters.get(index.column())
        if setter is None:
            return False
        setter(value)
        # dataChanged is emitted via the config-signal → _notify path.
        return True

    # ── Convenience ───────────────────────────────────────────────────

    @classmethod
    def create_mapper(
        cls,
        parent: QWidget,
        *mappings: tuple[QWidget, int],
    ) -> QDataWidgetMapper:
        """Create a fully-wired :class:`QDataWidgetMapper`.

        Each ``(widget, column)`` pair is added as a mapping.  The mapper
        uses ``AutoSubmit`` with :class:`ConfigDelegate` so that:

        * Widget edits are written to :class:`~src.config.AppConfig`
          immediately.
        * External config changes (from another view) are propagated back
          to the mapped widgets via ``revert()`` automatically.
        * ``toFirst()`` restores initial values — no manual restore needed.
        """
        model = cls(parent)
        mapper = QDataWidgetMapper(parent)
        mapper.setModel(model)
        mapper.setItemDelegate(ConfigDelegate(parent))
        mapper.setSubmitPolicy(QDataWidgetMapper.SubmitPolicy.AutoSubmit)
        for widget, col in mappings:
            mapper.addMapping(widget, col)
        mapper.toFirst()
        model.dataChanged.connect(lambda *_: mapper.revert())
        return mapper

    # ── Private ───────────────────────────────────────────────────────

    def _notify(self, col: int) -> None:
        idx = self.index(0, col)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.EditRole])


# ---------------------------------------------------------------------------
# Delegate
# ---------------------------------------------------------------------------


class ConfigDelegate(QStyledItemDelegate):
    """Item delegate aware of :class:`QComboBox` ``itemData()`` keying.

    For :class:`QComboBox` editors the delegate:

    - **setEditorData**: finds the item whose ``itemData`` matches the model
      value and selects it (falling back to ``setCurrentText`` for editable
      combos).
    - **setModelData**: reads ``currentData()`` (or ``currentText()`` for
      editable combos when no data matches) and writes it to the model.

    All other widget types fall through to the base implementation which
    uses the widget's *user property* (``QSpinBox.value``,
    ``QCheckBox.checked``, etc.).
    """

    def setEditorData(  # noqa: N802
        self, editor: QWidget, index: QModelIndex,
    ) -> None:
        if isinstance(editor, QComboBox):
            value = index.data(Qt.ItemDataRole.EditRole)
            for i in range(editor.count()):
                if editor.itemData(i) == value:
                    editor.setCurrentIndex(i)
                    return
            if editor.isEditable() and value is not None:
                editor.setCurrentText(str(value))
        else:
            super().setEditorData(editor, index)

    def setModelData(  # noqa: N802
        self,
        editor: QWidget,
        model: QAbstractTableModel,
        index: QModelIndex,
    ) -> None:
        if isinstance(editor, QComboBox):
            data = editor.currentData()
            if data is not None:
                model.setData(index, data)
            elif editor.isEditable():
                text = editor.currentText().strip()
                if text:
                    model.setData(index, text)
        else:
            super().setModelData(editor, model, index)
