# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Translator panel plug-in protocol and registry.

Each translator backend may ship a companion **panel module** that provides a
:class:`PySide6.QtWidgets.QWidget` subclass following the interface defined
below.  The panel is owned by :class:`~src.ui._translator_settings.TranslatorSettingsWidget`
and is dynamically loaded at runtime so that PySide6 is never imported in
headless / non-UI contexts.

Panel contract
--------------
Every panel class *must* implement the following five methods:

``load_from_config(cfg: AppConfig) -> None``
    Populate all fields from the persistent config.

``save_to_config(cfg: AppConfig) -> None``
    Persist all field values back to config.

``build_translator(cfg, *, progress, knowledge_base) -> Translator | None``
    Construct and return a :class:`~src.translators.base.Translator` instance
    using the **current UI field values** (API key, model, …).  *cfg* may be
    used for read-only access to shared settings (e.g. source language).
    Should **not** call ``save_to_config`` internally.

``connect_dirty(slot: Callable[[], None]) -> None``
    Connect every field's change signal to *slot* so the parent widget can
    track unsaved edits.

Registries
----------
``PANEL_REGISTRY`` maps each provider key to the dotted module path that
contains the ``Panel`` class.  Add new entries here when shipping a new
backend; the UI picks them up automatically.

``BUILDER_REGISTRY`` maps each provider key to the dotted module path that
exports a ``build_from_config(cfg, *, progress, knowledge_base)`` function.
This is used by the headless factory (no PySide6 required) to construct a
translator directly from :class:`~src.config.AppConfig`.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from PySide6.QtWidgets import QWidget
    from src.config import AppConfig
    from src.translators.base import Translator


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

#: Maps each provider key to the dotted module that exports a ``Panel`` class.
#: Add an entry here when adding a new translator backend.
PANEL_REGISTRY: dict[str, str] = {
    "google_free": "src.translators.google_free_panel",
    "cloud": "src.translators.cloud_panel",
    "openai": "src.translators.openai_panel",
}

#: Maps each provider key to the dotted module that exports a
#: ``build_from_config(cfg, *, progress, knowledge_base)`` function.
#: This registry is PySide6-free and safe to import in headless contexts.
BUILDER_REGISTRY: dict[str, str] = {
    "google_free": "src.translators.google_free",
    "cloud": "src.translators.google_cloud_translation",
    "openai": "src.translators.openai_translator",
}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def get_panel_class(key: str) -> "type[QWidget] | None":
    """Lazily import and return the panel class for *key*.

    Returns ``None`` when no panel is registered for the given provider key
    (e.g. ``"none"``).
    """
    module_path = PANEL_REGISTRY.get(key)
    if not module_path:
        return None
    mod = importlib.import_module(module_path)
    return mod.Panel  # type: ignore[attr-defined]


def build_from_config(
    key: str,
    cfg: "AppConfig",
    *,
    progress: "Callable[[str], None] | None" = None,
    knowledge_base: object = None,
) -> "Translator | None":
    """Dispatch to the registered headless builder for *key*.

    Returns ``None`` when *key* is ``"none"`` or not registered.
    Raises :py:exc:`RuntimeError` when the key is unknown.
    """
    if key in ("none", ""):
        return None
    module_path = BUILDER_REGISTRY.get(key)
    if not module_path:
        from src.translators.base import PROVIDERS_BY_KEY  # noqa: PLC0415
        valid = "'none', " + ", ".join(f"'{k}'" for k in PROVIDERS_BY_KEY)
        raise RuntimeError(
            f"Unknown translator backend: {key!r}.  Valid values are: {valid}."
        )
    mod = importlib.import_module(module_path)
    return mod.build_from_config(cfg, progress=progress, knowledge_base=knowledge_base)  # type: ignore[attr-defined]
