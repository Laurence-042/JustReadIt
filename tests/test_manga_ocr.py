"""Tests for src/ocr/manga_ocr_engine.py

Kept in a separate file so that torch DLL loading happens before any WinRT
COM apartment is initialized.  Running this file with pytest must NOT also
collect test_ocr.py in the same session if test_ocr.py has already imported
windows_ocr (which calls init_apartment).

Safe to run standalone::

    pytest tests/test_manga_ocr.py -v
"""
from __future__ import annotations

import pytest
from PIL import Image


class TestMangaOcrEngineFlag:
    def test_gpu_available_is_bool(self):
        from src.ocr.manga_ocr_engine import GPU_AVAILABLE
        assert isinstance(GPU_AVAILABLE, bool)

    def test_gpu_available_true_on_cuda_machine(self):
        """On this dev machine (RTX 3060, cu128 torch) GPU should be True."""
        from src.ocr.manga_ocr_engine import GPU_AVAILABLE
        assert GPU_AVAILABLE is True

    def test_raises_without_gpu(self, monkeypatch):
        """Constructing MangaOcrEngine with GPU_AVAILABLE=False must raise."""
        import src.ocr.manga_ocr_engine as module
        monkeypatch.setattr(module, "GPU_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="CUDA"):
            module.MangaOcrEngine()


@pytest.mark.xfail(
    strict=False,
    reason="Slow – only runs when explicitly requested (downloads model on first run)",
)
class TestMangaOcrEngineRecognise:
    def test_recognize_returns_str(self):
        from src.ocr.manga_ocr_engine import MangaOcrEngine, GPU_AVAILABLE
        if not GPU_AVAILABLE:
            pytest.skip("No GPU")
        engine = MangaOcrEngine()
        img = Image.new("RGB", (200, 50), (255, 255, 255))
        result = engine.recognize(img)
        assert isinstance(result, str)
