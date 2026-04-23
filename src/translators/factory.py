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

from src.translators.base import PROVIDERS_BY_KEY

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

    Reads all settings from *config*.  Returns ``None`` when the backend is
    set to ``"none"`` or the API key is missing for backends that require one.

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

    if backend in ("none", ""):
        return None

    if backend == "cloud":
        from src.translators.cloud_translation import CloudTranslationTranslator

        api_key = config.translator.cloud.api_key.strip() or None
        return CloudTranslationTranslator(api_key=api_key, progress=progress)

    if backend == "google_free":
        from src.translators.google_free import GoogleFreeTranslator

        return GoogleFreeTranslator(progress=progress)

    if backend == "openai":
        from src.translators.openai_translator import OpenAICompatTranslator

        return OpenAICompatTranslator(
            api_key=config.translator.openai.api_key.strip(),
            model=config.translator.openai.model.strip() or "gpt-4o-mini",
            system_prompt=config.translator.openai.system_prompt,
            context_window=config.translator.openai.context_window,
            base_url=config.translator.openai.base_url.strip() or None,
            knowledge_base=knowledge_base,  # type: ignore[arg-type]
            tools_enabled=config.translator.openai.tools_enabled,
            disable_thinking=config.translator.openai.disable_thinking,
            progress=progress,
        )

    valid = "'none', " + ", ".join(f"'{k}'" for k in PROVIDERS_BY_KEY)
    raise RuntimeError(
        f"Unknown translator backend: {backend!r}.  Valid values are: {valid}."
    )
