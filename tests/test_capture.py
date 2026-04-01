"""Tests for src/capture.py (DXGI Desktop Duplication).

All tests require a real display and D3D-capable GPU; they are skipped
automatically in headless CI environments where dxcam cannot initialise.

Run manually:
    pytest tests/test_capture.py -v
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Skip guard — skip the entire module if dxcam cannot open a device
# ---------------------------------------------------------------------------


def _dxcam_available() -> bool:
    try:
        import dxcam

        cam = dxcam.create()
        del cam
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _dxcam_available(),
    reason="dxcam cannot open a D3D device (headless / no GPU)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_all_black(img: Image.Image, threshold: int = 5) -> bool:
    """Return True if every pixel channel mean is below *threshold*."""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    return int(arr.mean()) < threshold


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCapturer:
    def test_context_manager_opens_and_closes(self) -> None:
        from src.capture import Capturer

        with Capturer() as cap:
            assert cap._camera is not None, "camera should be initialised inside context"
        assert cap._camera is None, "camera should be released on __exit__"

    def test_resolution_is_positive(self) -> None:
        from src.capture import Capturer

        with Capturer() as cap:
            w, h = cap.resolution
        assert w > 0
        assert h > 0

    def test_grab_returns_pil_image(self) -> None:
        from src.capture import Capturer

        with Capturer() as cap:
            img = cap.grab()
        assert isinstance(img, Image.Image)

    def test_grab_image_mode_is_rgb(self) -> None:
        from src.capture import Capturer

        with Capturer() as cap:
            img = cap.grab()
        assert img.mode == "RGB"

    def test_grab_fullscreen_matches_resolution(self) -> None:
        from src.capture import Capturer

        with Capturer() as cap:
            w, h = cap.resolution
            img = cap.grab()
        assert img.size == (w, h), (
            f"Expected ({w}, {h}), got {img.size}"
        )

    def test_grab_region_size(self) -> None:
        from src.capture import Capturer

        region = (0, 0, 320, 240)
        with Capturer() as cap:
            img = cap.grab(region=region)
        # dxcam region is (left, top, right, bottom) → width = right-left
        assert img.size == (320, 240), f"Expected (320, 240), got {img.size}"

    def test_grab_not_all_black(self) -> None:
        """Core check: DXGI capture should never return a pure-black frame.

        A black frame indicates BitBlt was mistakenly used (or the desktop is
        truly blank, which is unlikely during active development).
        """
        from src.capture import Capturer

        with Capturer() as cap:
            img = cap.grab()
        assert not _is_all_black(img), (
            "Captured frame is all-black — this suggests the capture path is "
            "NOT using DXGI Desktop Duplication (or the screen is blank)."
        )

    def test_closed_capturer_raises_on_grab(self) -> None:
        from src.capture import Capturer

        cap = Capturer()
        with pytest.raises(RuntimeError, match="not open"):
            cap.grab()


class TestOneShotHelpers:
    def test_capture_fullscreen_returns_image(self) -> None:
        from src.capture import capture_fullscreen

        img = capture_fullscreen()
        assert isinstance(img, Image.Image)
        assert img.mode == "RGB"
        w, h = img.size
        assert w > 0 and h > 0

    def test_capture_fullscreen_not_all_black(self) -> None:
        from src.capture import capture_fullscreen

        img = capture_fullscreen()
        assert not _is_all_black(img), "Full-screen capture returned a black frame."

    def test_capture_region_correct_size(self) -> None:
        from src.capture import capture_region

        img = capture_region(0, 0, 640, 480)
        assert img.size == (640, 480)

    def test_capture_region_not_all_black(self) -> None:
        from src.capture import capture_region

        img = capture_region(0, 0, 640, 480)
        assert not _is_all_black(img), "Region capture returned a black frame."


class TestBlackFrameRegression:
    """Regression suite for the 'no black frame on DirectX windows' requirement.

    To validate against a live Light.VN window, run with the game open:
        pytest tests/test_capture.py::TestBlackFrameRegression -v

    The test captures the primary monitor ten times in quick succession and
    asserts that at least one frame is non-black, ensuring the DXGI path is
    active even when a D3D-rendered window occupies the screen.
    """

    def test_repeated_grabs_include_non_black_frame(self) -> None:
        from src.capture import Capturer

        n_frames = 10
        non_black = 0

        with Capturer() as cap:
            for _ in range(n_frames):
                img = cap.grab()
                if not _is_all_black(img):
                    non_black += 1

        assert non_black > 0, (
            f"All {n_frames} consecutive frames were black — DXGI capture may "
            "be malfunctioning, or the screen was truly blank throughout."
        )
