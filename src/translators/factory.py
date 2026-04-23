# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Factory for constructing :class:`~src.translators.base.Translator` instances
from :class:`~src.config.AppConfig` settings.

Usage::

    from src.config import AppConfig
    from src.translators.factory import build_translator

    cfg = AppConfig()
    translator = build_translator(cfg)   # None when backend == "none"
    if translator:
        result = translator.translate("こんにちは")
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from src.translators._panel_base import build_from_config as _dispatch

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.translators.base import Translator


def build_translator(
    config: "AppConfig",
    *,
    progress: Callable[[str], None] | None = None,
    knowledge_base: "object | None" = None,
) -> "Translator | None":
    """Instantiate the configured translation backend.

    Delegates to the headless ``build_from_config`` function registered in
    :data:`~src.translators._panel_base.BUILDER_REGISTRY` for the active
    backend key.  Returns ``None`` when the backend is ``"none"``.

    Auto-installation of the required third-party package is performed inside
    each translator's ``__init__`` via
    :func:`~src.translators._installer.ensure_package`.  If the package
    cannot be installed a :py:exc:`RuntimeError` is raised.

    Args:
        config: Application configuration to read translator settings from.
        progress: Optional callback receiving status strings during
            dependency installation (e.g. to update a UI label).
        knowledge_base: Optional :class:`~src.knowledge.KnowledgeBase` to
            pass to the OpenAI-compatible backend for RAG and tool calling.

    Returns:
        A ready-to-use :class:`~src.translators.base.Translator`, or ``None``.
    """
    backend = config.translator.backend.lower().strip()
    return _dispatch(backend, config, progress=progress, knowledge_base=knowledge_base)
