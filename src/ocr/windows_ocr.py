"""Windows OCR integration – converts a PIL Image to a list of BoundingBox.

The Windows OCR API (Windows.Media.Ocr) requires pixel data wrapped in a
SoftwareBitmap.  Public API is synchronous; the underlying WinRT async call
is driven by ``asyncio.run()``.

Japanese requires the ``Language.OCR~~~ja-JP~0.0.1.0`` Windows Capability.
Run the provided installation script (administrator required)::

    powershell -ExecutionPolicy Bypass -File scripts\\install_ja_ocr.ps1

Or install manually in an elevated PowerShell::

    Add-WindowsCapability -Online -Name Language.OCR~~~ja-JP~0.0.1.0

The capability is ~6 MB and does NOT change system language or UI.
"""
from __future__ import annotations

import asyncio
import logging

from PIL import Image

import winrt._winrt as _winrt
import winrt.windows.globalization as glob
import winrt.windows.graphics.imaging as gi
import winrt.windows.media.ocr as wocr
import winrt.windows.storage.streams as wss

from .range_detectors import BoundingBox

_log = logging.getLogger(__name__)


def _ensure_apartment() -> None:
    """Initialise COM STA for the current thread (idempotent)."""
    try:
        _winrt.init_apartment(_winrt.STA)
    except Exception:
        pass  # already initialised on this thread


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pil_to_software_bitmap(image: Image.Image) -> gi.SoftwareBitmap:
    """Convert a PIL Image (any mode) to a BGRA8 SoftwareBitmap."""
    rgba = image.convert("RGBA")
    # PIL stores RGBA; Windows OCR expects BGRA8 – swap R and B channels.
    r, g, b, a = rgba.split()
    bgra = Image.merge("RGBA", (b, g, r, a))
    raw: bytes = bgra.tobytes()

    bmp = gi.SoftwareBitmap(
        gi.BitmapPixelFormat.BGRA8,
        rgba.width,
        rgba.height,
        gi.BitmapAlphaMode.PREMULTIPLIED,
    )
    buf = wss.Buffer(len(raw))
    buf.length = len(raw)
    with memoryview(buf) as mv:
        mv[:] = raw
    bmp.copy_from_buffer(buf)
    return bmp


# Capability name used for Japanese OCR on Windows 10/11.
_JA_OCR_CAPABILITY = "Language.OCR~~~ja-JP~0.0.1.0"

_INSTALL_HINT = (
    "Run the provided script (elevated PowerShell):\n"
    "  powershell -ExecutionPolicy Bypass -File scripts\\install_ja_ocr.ps1\n"
    "Or manually:\n"
    f"  Add-WindowsCapability -Online -Name {_JA_OCR_CAPABILITY}"
)


class MissingOcrLanguageError(RuntimeError):
    """Raised when the requested Windows OCR language capability is not installed."""


def _create_engine(language_tag: str = "ja") -> wocr.OcrEngine:
    """Return an OcrEngine for *language_tag*.

    Raises
    ------
    MissingOcrLanguageError
        When the requested language capability is not installed on this system.
    """
    _ensure_apartment()
    lang = glob.Language(language_tag)
    if wocr.OcrEngine.is_language_supported(lang):
        engine = wocr.OcrEngine.try_create_from_language(lang)
        if engine is not None:
            return engine

    raise MissingOcrLanguageError(
        f"Windows OCR language '{language_tag}' is not installed on this system.\n"
        + _INSTALL_HINT
    )


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class WindowsOcr:
    """Thin synchronous wrapper around the Windows OCR engine.

    Parameters
    ----------
    language_tag:
        BCP-47 tag of the preferred OCR language (default ``"ja"``).
        If not installed on this system, falls back to the user-profile
        language with a ``RuntimeWarning``.

    Example
    -------
    ::

        from PIL import Image
        from src.ocr.windows_ocr import WindowsOcr

        ocr = WindowsOcr()
        boxes = ocr.recognise(Image.open("screenshot.png"))
        for box in boxes:
            print(box.text, box.x, box.y, box.w, box.h)
    """

    def __init__(self, language_tag: str = "ja", upscale_factor: float = 2.0) -> None:
        try:
            self._engine: wocr.OcrEngine = _create_engine(language_tag)
        except MissingOcrLanguageError:
            raise
        # Windows OCR maximum image dimension is 4096 px.  Clamp the factor so
        # we don't exceed it even on large captures.
        self._upscale_factor = upscale_factor
        _log.info(
            "Windows OCR engine ready (language: %s, upscale: %.1f×)",
            self._engine.recognizer_language.language_tag,
            self._upscale_factor,
        )

    @property
    def language_tag(self) -> str:
        """BCP-47 tag of the active recogniser language."""
        return self._engine.recognizer_language.language_tag

    def recognise(self, image: Image.Image) -> list[BoundingBox]:
        """Run OCR on *image* and return word-level bounding boxes.

        The image is optionally upscaled by ``upscale_factor`` (set at
        construction) before recognition and coordinates are scaled back so
        callers always receive boxes in the original image's pixel space.
        Upscaling improves accuracy for small text (game dialog fonts are
        typically 24-32 px at 1080p, below the OCR optimum of 40 px).

        Coordinates are in the pixel space of *image* (origin = top-left corner
        of the image, i.e. the captured region).

        Parameters
        ----------
        image:
            The PIL Image to recognise.  Typically a full-window capture from
            ``src.capture``.  The image is converted to BGRA8 internally.

        Returns
        -------
        list[BoundingBox]
            One entry per recognised word, in reading order.
        """
        # Compute effective scale — clamp so neither dimension exceeds 4096 px.
        max_dim = max(image.width, image.height)
        scale = min(self._upscale_factor, 4096 / max_dim) if max_dim > 0 else 1.0

        if scale != 1.0:
            new_w = max(1, int(image.width  * scale))
            new_h = max(1, int(image.height * scale))
            ocr_img = image.resize((new_w, new_h), Image.LANCZOS)
        else:
            ocr_img = image

        bmp = _pil_to_software_bitmap(ocr_img)
        result: wocr.OcrResult = asyncio.run(self._engine.recognize_async(bmp))
        boxes: list[BoundingBox] = []
        for line in result.lines:
            for word in line.words:
                r = word.bounding_rect  # windows_foundation.Rect (floats)
                boxes.append(
                    BoundingBox(
                        x=int(r.x       / scale),
                        y=int(r.y       / scale),
                        w=int(r.width   / scale),
                        h=int(r.height  / scale),
                        text=word.text,
                    )
                )
        return boxes

    def recognise_text(self, image: Image.Image) -> str:
        """Return the full recognised text string (no bounding boxes)."""
        max_dim = max(image.width, image.height)
        scale = min(self._upscale_factor, 4096 / max_dim) if max_dim > 0 else 1.0
        if scale != 1.0:
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.LANCZOS,
            )
        bmp = _pil_to_software_bitmap(image)
        result: wocr.OcrResult = asyncio.run(self._engine.recognize_async(bmp))
        return result.text
