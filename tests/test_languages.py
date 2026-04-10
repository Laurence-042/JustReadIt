# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tests for :mod:`src.languages` and translator-level normalisation."""
from __future__ import annotations

import pytest

from src.languages import TARGET_PRESETS, display_name
from src.translators.google_free import _to_deep_translator


# ---------------------------------------------------------------------------
# display_name  (backed by langcodes)
# ---------------------------------------------------------------------------

class TestDisplayName:
    """``display_name()`` should return ``'tag — NativeName'``."""

    @pytest.mark.parametrize(
        ("tag", "expected_fragment"),
        [
            ("ja",    "日本語"),
            ("ja-JP", "日本語"),
            ("zh-Hans-CN", "中文"),
            ("zh-Hant-TW", "中文"),
            ("en",    "English"),
            ("ko",    "한국어"),
            ("fr",    "français"),
            ("de",    "Deutsch"),
        ],
    )
    def test_known_languages(self, tag: str, expected_fragment: str) -> None:
        result = display_name(tag)
        assert result.startswith(f"{tag} — ")
        assert expected_fragment in result

    def test_unknown_tag_returns_bare(self) -> None:
        assert display_name("xx-YY") == "xx-YY"


# ---------------------------------------------------------------------------
# TARGET_PRESETS integrity
# ---------------------------------------------------------------------------

class TestTargetPresets:
    def test_no_duplicates(self) -> None:
        assert len(TARGET_PRESETS) == len(set(TARGET_PRESETS))

    def test_zh_cn_is_first(self) -> None:
        """zh-Hans-CN is the most common target; should be first preset."""
        assert TARGET_PRESETS[0] == "zh-Hans-CN"

    def test_all_strings(self) -> None:
        for code in TARGET_PRESETS:
            assert isinstance(code, str)


# ---------------------------------------------------------------------------
# deep-translator BCP-47 → Google legacy downgrades
# ---------------------------------------------------------------------------

class TestDeepTranslator:
    """``_to_deep_translator()`` only does BCP-47 → legacy downgrades."""

    @pytest.mark.parametrize(
        ("bcp47", "expected"),
        [
            # All Chinese variants resolve via langcodes script inference
            ("zh-CN",      "zh-CN"),
            ("zh-TW",      "zh-TW"),
            ("zh-Hans-CN", "zh-CN"),   # Windows OCR form
            ("zh-Hant-TW", "zh-TW"),   # Windows OCR form
            ("zh-Hans",    "zh-CN"),
            ("zh-Hant",    "zh-TW"),
        ],
    )
    def test_chinese_variants(self, bcp47: str, expected: str) -> None:
        assert _to_deep_translator(bcp47) == expected

    @pytest.mark.parametrize(
        ("bcp47", "expected"),
        [
            ("ja-JP", "ja"),
            ("en-US", "en"),
            ("en-GB", "en"),
            ("ko-KR", "ko"),
            ("fr-FR", "fr"),
            ("de-DE", "de"),
        ],
    )
    def test_strip_region(self, bcp47: str, expected: str) -> None:
        assert _to_deep_translator(bcp47) == expected

    @pytest.mark.parametrize("code", ["ja", "en", "ko", "fr", "de", "es", "it"])
    def test_bare_passthrough(self, code: str) -> None:
        assert _to_deep_translator(code) == code

    def test_hebrew_legacy(self) -> None:
        assert _to_deep_translator("he") == "iw"

    def test_norwegian(self) -> None:
        assert _to_deep_translator("nb") == "no"
        assert _to_deep_translator("nn") == "no"

    def test_filipino(self) -> None:
        assert _to_deep_translator("fil") == "tl"
