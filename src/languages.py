# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Thin language helpers backed by the :mod:`langcodes` library.

Provides:

* **TARGET_PRESETS** — BCP-47 codes shown in every target-language combo.
* **display_name()** — ``"ja-JP"`` → ``"ja-JP — 日本語"``.

No PySide6 dependency — safe to import from headless modules, translators,
and tests.
"""
from __future__ import annotations

import langcodes

# ---------------------------------------------------------------------------
# Target-language presets
# ---------------------------------------------------------------------------
# Ordered list of BCP-47 codes shown in every target-language combo box.
# The code is stored in ``AppConfig.translator_target_lang`` and passed
# directly to translators.  Display labels come from ``display_name()``.

TARGET_PRESETS: list[str] = [
    "zh-CN",
    "zh-TW",
    "en",
    "ko",
    "fr",
    "de",
    "ja",
    "es",
    "pt",
    "ru",
    "ar",
    "it",
]


def display_name(tag: str) -> str:
    """Return ``"tag — NativeName"`` for any BCP-47 tag.

    Uses :func:`langcodes.Language.display_name` with the tag's own base
    language so the label appears in its native script (e.g. ``"日本語"``).

    Falls back to the bare *tag* if ``langcodes`` cannot resolve it.

    >>> display_name("ja")
    'ja — 日本語'
    >>> display_name("zh-CN")
    'zh-CN — 中文（中国）'
    """
    if not langcodes.tag_is_valid(tag):
        return tag
    try:
        lang = langcodes.Language.get(tag)
        native = lang.display_name(lang.language)
        if native and native != tag:
            return f"{tag} — {native}"
    except Exception:  # noqa: BLE001
        pass
    return tag
