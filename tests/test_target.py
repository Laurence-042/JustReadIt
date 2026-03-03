"""Tests for src/target.py (GameTarget).

Tests that require a real display / running processes are collected under
TestGameTargetLive and use a dynamically discovered process that owns a
visible window.  They are skipped in headless environments.

Run manually:
    pytest tests/test_target.py -v
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

import pytest

from src.target import (
    AmbiguousProcessNameError,
    GameTarget,
    ProcessNotFoundError,
    Rect,
    WindowNotFoundError,
    _compute_capture_rect,
)


# ---------------------------------------------------------------------------
# Helpers — discover a suitable live test target
# ---------------------------------------------------------------------------

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
_user32 = ctypes.WinDLL("user32")


def _find_any_windowed_pid() -> int | None:
    """Return the PID of any process that owns a large visible top-level window."""
    best: tuple[int, int] | None = None  # (area, pid)

    def _cb(hwnd: int, _: int) -> bool:
        nonlocal best
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetAncestor(hwnd, 2) != hwnd:
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        if area > 10_000 and (best is None or area > best[0]):
            best = (area, pid.value)
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return best[1] if best else None


_LIVE_PID: int | None = _find_any_windowed_pid()
_live = pytest.mark.skipif(
    _LIVE_PID is None,
    reason="No process with a visible window found (headless environment).",
)


# ---------------------------------------------------------------------------
# Rect unit tests (no Win32 needed)
# ---------------------------------------------------------------------------


class TestRect:
    def test_width_height(self) -> None:
        r = Rect(10, 20, 110, 220)
        assert r.width == 100
        assert r.height == 200

    def test_area(self) -> None:
        r = Rect(0, 0, 100, 50)
        assert r.area == 5000

    def test_area_zero_for_degenerate(self) -> None:
        assert Rect(0, 0, 0, 0).area == 0
        assert Rect(10, 10, 5, 20).area == 0  # negative width

    def test_as_tuple(self) -> None:
        r = Rect(1, 2, 3, 4)
        assert r.as_tuple() == (1, 2, 3, 4)

    def test_frozen(self) -> None:
        r = Rect(0, 0, 10, 10)
        with pytest.raises((AttributeError, TypeError)):
            r.left = 5  # type: ignore[misc]


class TestComputeCaptureRect:
    def test_single_monitor_no_offset(self) -> None:
        monitor = Rect(0, 0, 1920, 1080)
        window  = Rect(100, 50, 800, 600)
        result  = _compute_capture_rect(window, monitor)
        assert result == Rect(100, 50, 800, 600)

    def test_window_on_secondary_monitor_left(self) -> None:
        """Monitor to the left of primary has negative virtual-screen x."""
        monitor = Rect(-1920, 0, 0, 1080)
        window  = Rect(-1800, 100, -200, 900)
        result  = _compute_capture_rect(window, monitor)
        # Shift by monitor origin: +1920 to x
        assert result == Rect(120, 100, 1720, 900)

    def test_window_larger_than_monitor_clips(self) -> None:
        """A window spanning two monitors is clipped to the host monitor."""
        monitor = Rect(0, 0, 1920, 1080)
        window  = Rect(-100, -50, 2000, 1200)  # overflows all edges
        result  = _compute_capture_rect(window, monitor)
        assert result == Rect(0, 0, 1920, 1080)

    def test_result_non_negative_origin(self) -> None:
        monitor = Rect(1920, 0, 3840, 1080)
        window  = Rect(2000, 100, 3500, 900)
        result  = _compute_capture_rect(window, monitor)
        assert result.left >= 0 and result.top >= 0


# ---------------------------------------------------------------------------
# Error-path tests (no visible window required)
# ---------------------------------------------------------------------------


class TestGameTargetErrors:
    def test_from_pid_invalid_raises_window_not_found(self) -> None:
        """A PID that does not exist should raise WindowNotFoundError (no windows)."""
        # PID 0 is the System Idle Process and has no windows.
        with pytest.raises(WindowNotFoundError):
            GameTarget.from_pid(0)

    def test_from_name_unknown_raises_process_not_found(self) -> None:
        with pytest.raises(ProcessNotFoundError):
            GameTarget.from_name("__nonexistent_game_xyz__.exe")

    def test_from_name_accepts_without_exe_suffix(self) -> None:
        """from_name should not raise because of a missing .exe suffix —
        it either raises ProcessNotFoundError (name genuinely absent) or
        returns a valid target.  It must NOT raise AttributeError / TypeError."""
        try:
            GameTarget.from_name("__nonexistent_game_xyz__")
        except ProcessNotFoundError:
            pass  # expected
        except AmbiguousProcessNameError:
            pass  # also fine


# ---------------------------------------------------------------------------
# Live tests (require a windowed process)
# ---------------------------------------------------------------------------


class TestGameTargetLive:
    @_live
    def test_from_pid_returns_target(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert isinstance(target, GameTarget)

    @_live
    def test_pid_matches(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert target.pid == _LIVE_PID

    @_live
    def test_hwnd_nonzero(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert target.hwnd != 0

    @_live
    def test_window_rect_positive_area(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert target.window_rect.area > 0

    @_live
    def test_window_rect_type(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert isinstance(target.window_rect, Rect)

    @_live
    def test_process_name_nonempty(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert target.process_name

    @_live
    def test_from_name_roundtrip(self) -> None:
        """from_pid and from_name should agree on pid and hwnd."""
        target_by_pid = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        name = target_by_pid.process_name
        try:
            target_by_name = GameTarget.from_name(name)
        except AmbiguousProcessNameError:
            pytest.skip(f"Multiple processes named {name!r} — can't round-trip by name")
        assert target_by_name.pid == target_by_pid.pid

    @_live
    def test_refresh_returns_new_target(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        refreshed = target.refresh()
        assert isinstance(refreshed, GameTarget)
        assert refreshed.pid == target.pid
        # rect should be equal or close (window may have micro-moved)
        assert abs(refreshed.window_rect.area - target.window_rect.area) < 100_000

    @_live
    def test_capture_rect_positive_area(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert target.capture_rect.area > 0

    @_live
    def test_capture_rect_non_negative_origin(self) -> None:
        """capture_rect must be in monitor-local space (no negative coords)."""
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        r = target.capture_rect
        assert r.left >= 0 and r.top >= 0

    @_live
    def test_dxcam_output_idx_nonnegative(self) -> None:
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        assert target.dxcam_output_idx >= 0

    @_live
    def test_as_tuple_compatible_with_capturer(self) -> None:
        """capture_rect.as_tuple() must produce a valid dxcam region."""
        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        region = target.capture_rect.as_tuple()
        assert len(region) == 4
        left, top, right, bottom = region
        assert right > left
        assert bottom > top
        assert left >= 0 and top >= 0


# ---------------------------------------------------------------------------
# capture_window integration (requires dxcam + windowed process)
# ---------------------------------------------------------------------------

def _dxcam_available() -> bool:
    try:
        import dxcam
        cam = dxcam.create()
        del cam
        return True
    except Exception:
        return False


_live_with_dxcam = pytest.mark.skipif(
    _LIVE_PID is None or not _dxcam_available(),
    reason="Requires a windowed process and a D3D-capable GPU.",
)


class TestCaptureWindow:
    @_live_with_dxcam
    def test_capture_window_returns_image(self) -> None:
        from PIL import Image
        from src.capture import capture_window

        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        img = capture_window(target)
        assert isinstance(img, Image.Image)
        assert img.mode == "RGB"

    @_live_with_dxcam
    def test_capture_window_size_matches_rect(self) -> None:
        from src.capture import capture_window

        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        img = capture_window(target)
        expected_w = target.capture_rect.width
        expected_h = target.capture_rect.height
        assert img.size == (expected_w, expected_h), (
            f"Expected {expected_w}x{expected_h}, got {img.size[0]}x{img.size[1]}"
        )

    @_live_with_dxcam
    def test_grab_target_equivalent_to_capture_window(self) -> None:
        """Capturer.grab_target and capture_window must return same-sized images."""
        from src.capture import Capturer, capture_window

        target = GameTarget.from_pid(_LIVE_PID)  # type: ignore[arg-type]
        img_helper = capture_window(target)
        with Capturer(output_idx=target.dxcam_output_idx) as cap:
            img_method = cap.grab_target(target)
        assert img_helper.size == img_method.size
