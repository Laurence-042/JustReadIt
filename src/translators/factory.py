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

    Returns:
        A ready-to-use :class:`~src.translators.base.Translator`, or ``None``.
    """
    backend = config.translator_backend.lower().strip()

    if backend in ("none", ""):
        return None

    if backend == "cloud":
        from src.translators.cloud_translation import CloudTranslationTranslator

        api_key = config.cloud_api_key.strip() or None
        return CloudTranslationTranslator(api_key=api_key, progress=progress)

    if backend == "google_free":
        from src.translators.google_free import GoogleFreeTranslator

        return GoogleFreeTranslator(progress=progress)

    if backend == "openai":
        from src.translators.openai_translator import OpenAITranslator

        api_key = config.openai_api_key.strip()
        if not api_key:
            raise RuntimeError(
                "OpenAI API key is not configured.  "
                "Set it in the Translation Settings panel."
            )
        return OpenAITranslator(
            api_key=api_key,
            model=config.openai_model.strip() or "gpt-4o-mini",
            system_prompt=config.openai_system_prompt,
            context_window=config.openai_context_window,
            summary_trigger=config.openai_summary_trigger,
            base_url=config.openai_base_url.strip() or None,
            progress=progress,
        )

    valid = "'none', " + ", ".join(f"'{k}'" for k in PROVIDERS_BY_KEY)
    raise RuntimeError(
        f"Unknown translator backend: {backend!r}.  Valid values are: {valid}."
    )
