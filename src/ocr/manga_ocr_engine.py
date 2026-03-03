"""manga-ocr engine wrapper with GPU detection and graceful CPU degradation.

Design rule (copilot-instructions.md):
    CPU manga-ocr latency is unacceptable for an interactive overlay.
    If no CUDA-capable GPU is detected at import time, ``GPU_AVAILABLE`` is
    set to ``False`` and callers should skip manga-ocr entirely, using the
    Windows OCR result directly.

Usage pattern
-------------
::

    from src.ocr.manga_ocr_engine import MangaOcrEngine, GPU_AVAILABLE

    if GPU_AVAILABLE:
        engine = MangaOcrEngine()
        text = engine.recognize(cropped_image)
    else:
        text = windows_ocr_result_text  # fall back
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manga_ocr import MangaOcr as _MangaOcrType
    from PIL import Image

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GPU probe – runs once at import time
# ---------------------------------------------------------------------------

def _probe_cuda() -> bool:
    """Return True if a CUDA-capable GPU is available."""
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            _log.info(
                "CUDA available – GPU: %s (CUDA %s)",
                torch.cuda.get_device_name(0),
                torch.version.cuda,
            )
            return True
        _log.warning("torch is installed but CUDA is not available; manga-ocr disabled.")
        return False
    except ImportError:
        _log.warning("torch not installed; manga-ocr disabled.")
        return False


GPU_AVAILABLE: bool = _probe_cuda()
"""``True`` when a CUDA-capable GPU was detected at import time.

Callers should check this flag before constructing :class:`MangaOcrEngine`::

    from src.ocr.manga_ocr_engine import GPU_AVAILABLE, MangaOcrEngine

    if GPU_AVAILABLE:
        engine = MangaOcrEngine()
"""


# ---------------------------------------------------------------------------
# Engine class
# ---------------------------------------------------------------------------

class MangaOcrEngine:
    """High-precision Japanese OCR powered by the manga-ocr model on GPU.

    The model is loaded lazily on first construction.  Loading takes a few
    seconds (model download on first run, then cached by HuggingFace).

    Raises
    ------
    RuntimeError
        If no CUDA-capable GPU is available.  Guard with ``GPU_AVAILABLE``
        before constructing.

    Example
    -------
    ::

        from PIL import Image
        from src.ocr.manga_ocr_engine import MangaOcrEngine, GPU_AVAILABLE

        if GPU_AVAILABLE:
            engine = MangaOcrEngine()
            text = engine.recognize(Image.open("text_region.png"))
    """

    def __init__(self) -> None:
        if not GPU_AVAILABLE:
            raise RuntimeError(
                "MangaOcrEngine requires a CUDA-capable GPU. "
                "GPU_AVAILABLE is False on this system. "
                "Use the Windows OCR result directly as fallback."
            )

        _log.info("Loading manga-ocr model onto GPU…")
        # Import deferred: avoids several-second model-load cost at module import.
        from manga_ocr import MangaOcr  # noqa: PLC0415

        self._model: _MangaOcrType = MangaOcr()
        _log.info("manga-ocr model ready.")

    def recognize(self, image: "Image.Image") -> str:
        """Return the recognised Japanese text for the given image crop.

        Parameters
        ----------
        image:
            A PIL Image of the text region to recognise.  Typically produced
            by cropping the full-window capture to the bounding-box range
            returned by the range detectors.

        Returns
        -------
        str
            The recognised Japanese text string.
        """
        return self._model(image)
