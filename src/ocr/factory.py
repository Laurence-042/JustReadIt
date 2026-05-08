# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Factory for constructing :class:`~src.ocr.base.OcrProvider` instances
from :class:`~src.config.AppConfig` settings.

Usage::

    from src.config import AppConfig
    from src.ocr.factory import build_ocr

    cfg = AppConfig()
    ocr = build_ocr(cfg)          # uses cfg.ocr.engine, .language, .max_size
    word_boxes, line_boxes = ocr.recognise(image)

Or construct with explicit parameters (e.g. for thread-local re-creation)::

    from src.ocr.factory import build_ocr_engine

    ocr = build_ocr_engine("paddle", language_tag="ja", max_long_edge=1920)
"""
from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.ocr.base import OcrProvider


def build_ocr_engine(
    engine: str,
    language_tag: str = "ja",
    max_long_edge: int = 1920,
) -> "OcrProvider":
    """Construct an OCR engine by name.

    Parameters
    ----------
    engine:
        Engine key — ``"windows"`` or ``"paddle"``.  Unknown values fall back
        to ``"windows"`` with a :py:exc:`RuntimeWarning`.
    language_tag:
        BCP-47 language tag passed to the engine (e.g. ``"ja"``).
    max_long_edge:
        Maximum long edge in pixels passed to both engines for downsampling
        large frames before recognition.

    Returns
    -------
    OcrProvider
        A ready-to-use OCR engine instance.
    """
    key = engine.lower().strip()

    if key == "paddle":
        from src.ocr.paddle_ocr import PaddleOcr
        return PaddleOcr(language_tag, max_long_edge=max_long_edge)

    if key != "windows":
        warnings.warn(
            f"Unknown OCR engine '{engine}', falling back to 'windows'.",
            RuntimeWarning,
            stacklevel=2,
        )

    from src.ocr.windows_ocr import WindowsOcr
    return WindowsOcr(language_tag, max_ocr_long_edge=max_long_edge)


def build_ocr(config: "AppConfig") -> "OcrProvider":
    """Construct the configured OCR engine from *config*.

    Reads :attr:`~src.config._OcrConfig.engine`,
    :attr:`~src.config._OcrConfig.language`, and
    :attr:`~src.config._OcrConfig.max_size` from *config*.

    Returns
    -------
    OcrProvider
        A ready-to-use OCR engine instance.
    """
    return build_ocr_engine(
        engine=config.ocr.engine,
        language_tag=config.ocr.language,
        max_long_edge=config.ocr.max_size,
    )
