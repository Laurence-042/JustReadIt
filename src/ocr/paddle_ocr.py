# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""PaddleOCR engine implementation.

Requires the ``paddleocr`` package (install via ``pip install ".[ocr-paddle]"``
or ``pip install paddleocr``).  The package is auto-installed on first use via
:func:`~src.translators._installer.ensure_package`.

PaddleOCR returns line-level detection results; each detected text region is
treated as both a *word box* and a *line box* so that downstream range
detectors receive a consistent input regardless of which OCR engine is active.

Language mapping
----------------
BCP-47 tags are mapped to PaddleOCR ``lang`` codes:

=============================  ============
BCP-47 prefix                  PaddleOCR lang
=============================  ============
``ja``                         ``japan``
``zh``, ``zh-CN``, ``zh-Hans`` ``ch``
``zh-TW``, ``zh-Hant``         ``chinese_cht``
``ko``                         ``korean``
``en``                         ``en``
*(anything else)*              ``en`` (fallback with warning)
=============================  ============
"""
from __future__ import annotations

import logging
import warnings

from PIL import Image

from .base import MissingOcrEngineError, OcrProvider
from .range_detectors import BoundingBox
from src.text_utils import normalize_text
from src.translators._installer import ensure_package

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BCP-47 → PaddleOCR language code
# ---------------------------------------------------------------------------

_LANG_MAP: dict[str, str] = {
    "ja":         "japan",
    "zh":         "ch",
    "zh-cn":      "ch",
    "zh-hans":    "ch",
    "zh-tw":      "chinese_cht",
    "zh-hant":    "chinese_cht",
    "ko":         "korean",
    "en":         "en",
}


def _to_paddle_lang(bcp47: str) -> str:
    """Convert a BCP-47 tag to a PaddleOCR ``lang`` code."""
    key = bcp47.lower()
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    # Try prefix match (e.g. "zh-sg" → "zh" → "ch")
    prefix = key.split("-")[0]
    if prefix in _LANG_MAP:
        return _LANG_MAP[prefix]
    warnings.warn(
        f"PaddleOcr: no language mapping for '{bcp47}', falling back to 'en'.",
        RuntimeWarning,
        stacklevel=3,
    )
    return "en"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _quad_to_bbox(quad: list, text: str) -> BoundingBox:
    """Convert a PaddleOCR quad ``[[x,y],…]`` (4 points) to a BoundingBox."""
    xs = [pt[0] for pt in quad]
    ys = [pt[1] for pt in quad]
    x = int(min(xs))
    y = int(min(ys))
    w = max(1, int(max(xs)) - x)
    h = max(1, int(max(ys)) - y)
    return BoundingBox(x=x, y=y, w=w, h=h, text=text)


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class PaddleOcr(OcrProvider):
    """OCR engine backed by PaddleOCR.

    Parameters
    ----------
    language_tag:
        BCP-47 tag of the preferred OCR language (default ``"ja"``).
        Mapped internally to a PaddleOCR ``lang`` code.

    Example
    -------
    ::

        from PIL import Image
        from src.ocr.paddle_ocr import PaddleOcr

        ocr = PaddleOcr("ja")
        word_boxes, line_boxes = ocr.recognise(Image.open("screenshot.png"))
        for box in line_boxes:
            print(box.text, box.x, box.y, box.w, box.h)
    """

    def __init__(self, language_tag: str = "ja", max_long_edge: int = 1920) -> None:
        super().__init__(max_long_edge=max_long_edge)
        ensure_package("paddleocr", "paddleocr")

        try:
            from paddleocr import PaddleOCR  # type: ignore[import]
        except ImportError as exc:
            raise MissingOcrEngineError(
                "paddleocr is not installed.  Run:\n"
                "  pip install paddleocr\n"
                "or install the project extras:\n"
                "  pip install \".[ocr-paddle]\""
            ) from exc

        self._language_tag = language_tag
        paddle_lang = _to_paddle_lang(language_tag)

        _log.info("Initialising PaddleOCR (lang=%s) …", paddle_lang)
        # show_log=False suppresses PaddleOCR's verbose internal logging.
        # use_angle_cls=True handles rotated text (common in manga bubbles).
        self._engine = PaddleOCR(
            use_angle_cls=True,
            lang=paddle_lang,
            use_gpu=False,
            show_log=False,
        )
        _log.info("PaddleOCR ready (lang=%s → paddle lang=%s)", language_tag, paddle_lang)

    @property
    def language_tag(self) -> str:
        """BCP-47 tag supplied at construction."""
        return self._language_tag

    def recognise(
        self, image: Image.Image
    ) -> tuple[list[BoundingBox], list[BoundingBox]]:
        """Run PaddleOCR on *image* and return bounding boxes.

        PaddleOCR detects text at line level.  Each detected region is
        returned as both a word box and a line box so that downstream
        range detectors receive the same interface as :class:`WindowsOcr`.

        Returns
        -------
        tuple[list[BoundingBox], list[BoundingBox]]
            ``(word_boxes, line_boxes)`` — both lists contain the same
            :class:`~src.ocr.range_detectors.BoundingBox` instances since
            PaddleOCR operates at line granularity.
        """
        # PaddleOCR accepts numpy arrays or file paths; PIL images need
        # to be converted.  We pass the raw pixel array.
        import numpy as np  # type: ignore[import]  # noqa: PLC0415

        ocr_img, scale = self._resize_for_ocr(image)
        arr = np.array(ocr_img.convert("RGB"))
        try:
            results = self._engine.ocr(arr, cls=True)
        except Exception as exc:
            _log.warning("PaddleOCR inference failed: %s", exc)
            return [], []

        boxes: list[BoundingBox] = []
        # results is a list-of-pages; we always pass a single image so
        # index 0 is the only page.  Guard against None (blank image).
        page = results[0] if results else None
        if not page:
            return [], []

        for item in page:
            # item = [quad_points, (text, confidence)]
            if not item or len(item) < 2:
                continue
            quad, (text, _conf) = item[0], item[1]
            text = normalize_text(str(text))
            if not text.strip():
                continue
            box = _quad_to_bbox(quad, text)
            if scale != 1.0:
                box = BoundingBox(
                    x=int(box.x / scale),
                    y=int(box.y / scale),
                    w=max(1, int(box.w / scale)),
                    h=max(1, int(box.h / scale)),
                    text=box.text,
                )
            boxes.append(box)

        return boxes, list(boxes)  # word_boxes == line_boxes for PaddleOCR
