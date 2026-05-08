# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OCR engine plugin interface.

All OCR engines must subclass :class:`OcrProvider` and implement:

* :meth:`~OcrProvider.recognise` — return word-level and line-level bounding
  boxes for the given PIL image.
* :attr:`~OcrProvider.language_tag` — the BCP-47 tag of the active language.

Built-in engines
----------------
* ``"windows"`` — :class:`~src.ocr.windows_ocr.WindowsOcr` (default; no
  extra dependencies beyond the Windows OCR capability package)
* ``"paddle"``  — :class:`~src.ocr.paddle_ocr.PaddleOcr` (requires
  ``paddleocr``; install via ``pip install ".[ocr-paddle]"``)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from PIL import Image

from src.ocr.range_detectors import BoundingBox


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class MissingOcrEngineError(RuntimeError):
    """Raised when the requested OCR engine or language capability is unavailable."""


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class OcrProvider(ABC):
    """Abstract base class for OCR engine implementations.

    Parameters
    ----------
    max_long_edge:
        Soft cap (pixels) on the longest image dimension fed to the engine.
        Frames larger than this are downsampled before recognition and
        coordinates are scaled back to the original space.  Default is
        ``1920`` (appropriate for 1080p captures; 4K frames are halved).

    Subclasses are responsible for:

    * Lazy-loading their dependencies (call
      :func:`~src.translators._installer.ensure_package` in ``__init__``
      for optional packages).
    * Converting engine-specific result types to
      :class:`~src.ocr.range_detectors.BoundingBox` values.
    * Scaling coordinates back to the original image's pixel space when the
      engine internally resizes the image (use :meth:`_resize_for_ocr`).
    """

    def __init__(self, *, max_long_edge: int = 1920) -> None:
        self._max_long_edge = max_long_edge

    # ------------------------------------------------------------------
    # Shared resize helper
    # ------------------------------------------------------------------

    def _resize_for_ocr(
        self,
        image: "Image.Image",
        *,
        upscale_factor: float = 1.0,
        hard_cap: int = 0,
    ) -> "tuple[Image.Image, float]":
        """Resize *image* for OCR and return ``(resized_image, scale)``.

        The effective scale is::

            scale = min(upscale_factor, max_long_edge/max_dim
                        [, hard_cap/max_dim if hard_cap > 0])

        so the image is never fed to the engine at more than
        ``max_long_edge`` pixels on the long edge (or ``hard_cap`` when the
        engine imposes a hard API limit, e.g. Windows OCR at 4096 px).
        When ``upscale_factor > 1`` and the image is small, it may be
        enlarged to improve recognition of small fonts.

        Parameters
        ----------
        image:
            Source image.
        upscale_factor:
            Desired scale for small images (default 1.0 = no upscale).
        hard_cap:
            Absolute pixel limit imposed by the engine API (0 = no limit).

        Returns
        -------
        tuple[Image.Image, float]
            ``(processed_image, scale)`` where *scale* is the factor applied
            to the image dimensions.  Divide OCR coordinates by *scale* to
            recover original-image coordinates.
        """
        from PIL import Image as _Image  # noqa: PLC0415

        max_dim = max(image.width, image.height)
        if max_dim == 0:
            return image, 1.0

        caps = [upscale_factor, self._max_long_edge / max_dim]
        if hard_cap > 0:
            caps.append(hard_cap / max_dim)
        scale = min(caps)

        if scale == 1.0:
            return image, 1.0

        new_w = max(1, int(image.width  * scale))
        new_h = max(1, int(image.height * scale))
        return image.resize((new_w, new_h), _Image.LANCZOS), scale

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def language_tag(self) -> str:
        """BCP-47 tag of the active recogniser language (e.g. ``"ja"``)."""

    @abstractmethod
    def recognise(
        self, image: "Image.Image"
    ) -> tuple[list[BoundingBox], list[BoundingBox]]:
        """Run OCR on *image*.

        Parameters
        ----------
        image:
            PIL Image to recognise (any mode; implementations convert as
            needed).  Coordinates in the returned boxes use the pixel
            coordinate space of this image (top-left origin).

        Returns
        -------
        tuple[list[BoundingBox], list[BoundingBox]]
            ``(word_boxes, line_boxes)`` in reading order.

            * *word_boxes* — one :class:`~src.ocr.range_detectors.BoundingBox`
              per recognised word or character cluster.
            * *line_boxes* — one entry per text line with the full line text.

            Both lists use the pixel coordinate space of *image*.
        """

    def recognise_text(self, image: "Image.Image") -> str:
        """Return the full recognised text string (no bounding boxes).

        Default implementation calls :meth:`recognise` and joins line texts
        with newlines.  Subclasses may override for efficiency.
        """
        _, line_boxes = self.recognise(image)
        return "\n".join(b.text for b in line_boxes if b.text)
