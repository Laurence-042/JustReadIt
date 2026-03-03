"""Screen capture via DXGI Desktop Duplication API.

Never use BitBlt / PrintWindow — DirectX / Light.VN windows return black frames
with those APIs. This module wraps ``dxcam`` which uses D3D11 DXGI Desktop
Duplication under the hood.

Public API
----------
Capturer
    Context-manager that owns a single ``dxcam.DXCamera`` instance and exposes
    thread-safe grab helpers.  Reuse one instance per session; creating a new
    camera every call is expensive.

capture_fullscreen() -> PIL.Image.Image
    One-shot convenience: grab the full primary monitor.

capture_region(left, top, right, bottom) -> PIL.Image.Image
    One-shot convenience: grab a sub-region (pixel coordinates, inclusive of
    left/top, exclusive of right/bottom — same convention as dxcam / Win32
    RECT).

Region coordinate system
------------------------
All coordinates are in *virtual screen* space (top-left origin, pixels).
For a single-monitor setup this is identical to physical pixels.
``right`` and ``bottom`` are **exclusive** (RECT style), matching Win32 and
winsdk conventions used elsewhere in this project.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import dxcam
import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from src.target import GameTarget


# How long to wait between retries when dxcam returns None (no new frame yet).
_RETRY_INTERVAL_S: float = 0.005
# Maximum total wait time before raising RuntimeError.
_GRAB_TIMEOUT_S: float = 2.0
# Pixel mean below this value is considered an "empty" warm-up frame from DXGI.
# DXGI Desktop Duplication may return an all-zero buffer on the very first
# AcquireNextFrame call after the duplicator is created; we treat that as
# "not ready yet" and keep retrying (same as a None frame).
_BLACK_FRAME_THRESHOLD: int = 2


class Capturer:
    """Wrapper around ``dxcam.DXCamera`` for DXGI Desktop Duplication capture.

    Parameters
    ----------
    device_idx:
        D3D adapter index (0 = primary GPU).
    output_idx:
        Monitor output index on that adapter (None = primary output).
    output_color:
        Pixel format forwarded to dxcam.  ``"RGB"`` matches PIL's default.

    Usage
    -----
    ::

        with Capturer() as cap:
            img = cap.grab()                         # full screen
            img = cap.grab(region=(0, 0, 400, 300))  # sub-region
    """

    def __init__(
        self,
        device_idx: int = 0,
        output_idx: int | None = None,
        output_color: str = "RGB",
    ) -> None:
        self._device_idx = device_idx
        self._output_idx = output_idx
        self._output_color = output_color
        self._camera: dxcam.DXCamera | None = None

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "Capturer":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Life-cycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Initialise the D3D device and acquire the DXGI output duplication."""
        if self._camera is not None:
            return
        kwargs: dict = dict(
            device_idx=self._device_idx,
            output_color=self._output_color,
        )
        if self._output_idx is not None:
            kwargs["output_idx"] = self._output_idx
        self._camera = dxcam.create(**kwargs)

    def close(self) -> None:
        """Release the DXGI duplication handle and D3D resources."""
        if self._camera is not None:
            # dxcam does not expose an explicit close/release method;
            # deleting the object triggers __del__ which calls release().
            del self._camera
            self._camera = None

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    @property
    def resolution(self) -> tuple[int, int]:
        """Full output resolution as ``(width, height)``."""
        if self._camera is None:
            raise RuntimeError("Capturer is not open. Use it as a context manager.")
        return (self._camera.width, self._camera.height)

    def grab(
        self,
        region: tuple[int, int, int, int] | None = None,
        timeout: float = _GRAB_TIMEOUT_S,
    ) -> Image.Image:
        """Capture a frame and return a ``PIL.Image.Image`` (RGB).

        Parameters
        ----------
        region:
            ``(left, top, right, bottom)`` in virtual-screen pixels.
            ``None`` captures the full output.
        timeout:
            Maximum seconds to wait for a non-None frame from the duplicator.

        Raises
        ------
        RuntimeError
            If the capturer is not open, or no frame arrives within *timeout*.
        """
        if self._camera is None:
            raise RuntimeError("Capturer is not open. Use it as a context manager.")

        deadline = time.monotonic() + timeout
        frame: np.ndarray | None = None

        while True:
            frame = self._camera.grab(region=region)
            if frame is not None and int(frame.mean()) > _BLACK_FRAME_THRESHOLD:
                # Got a real frame with actual content.
                break
            if time.monotonic() > deadline:
                if frame is None:
                    raise RuntimeError(
                        f"dxcam returned no frame within {timeout:.1f}s. "
                        "The DXGI output may not have been updated."
                    )
                # Frame arrived but is all-black (locked desktop, virtual
                # display, screensaver, …).  Return it — callers decide how
                # to handle a black capture.
                break
            time.sleep(_RETRY_INTERVAL_S)

        return Image.fromarray(frame, mode=self._output_color)

    def grab_target(self, target: "GameTarget", timeout: float = _GRAB_TIMEOUT_S) -> Image.Image:
        """Capture only the game window described by *target*.

        Uses ``target.capture_rect`` (monitor-local coordinates, already clipped
        to the host monitor) and validates that this :class:`Capturer` was opened
        for the correct output.

        Raises
        ------
        ValueError
            If the Capturer's ``output_idx`` does not match
            ``target.dxcam_output_idx``.  Recreate the Capturer with
            ``Capturer(output_idx=target.dxcam_output_idx)``.
        """
        if self._output_idx != target.dxcam_output_idx:
            raise ValueError(
                f"Capturer output_idx={self._output_idx!r} does not match "
                f"target.dxcam_output_idx={target.dxcam_output_idx}. "
                f"Recreate with Capturer(output_idx={target.dxcam_output_idx})."
            )
        return self.grab(region=target.capture_rect.as_tuple(), timeout=timeout)


# ---------------------------------------------------------------------------
# Module-level one-shot helpers
# ---------------------------------------------------------------------------


def capture_fullscreen(
    device_idx: int = 0,
    output_idx: int | None = None,
) -> Image.Image:
    """Capture the full primary monitor and return a PIL Image (RGB).

    Opens and closes an internal :class:`Capturer` on every call.
    For repeated captures, create a :class:`Capturer` once and reuse it.
    """
    with Capturer(device_idx=device_idx, output_idx=output_idx) as cap:
        return cap.grab()


def capture_region(
    left: int,
    top: int,
    right: int,
    bottom: int,
    device_idx: int = 0,
    output_idx: int | None = None,
) -> Image.Image:
    """Capture a rectangular sub-region and return a PIL Image (RGB).

    Coordinates follow the Win32 RECT convention:
    ``left`` / ``top`` are inclusive; ``right`` / ``bottom`` are exclusive.

    Opens and closes an internal :class:`Capturer` on every call.
    For repeated captures, create a :class:`Capturer` once and reuse it.
    """
    with Capturer(device_idx=device_idx, output_idx=output_idx) as cap:
        return cap.grab(region=(left, top, right, bottom))


def capture_window(
    target: "GameTarget",
    device_idx: int = 0,
) -> Image.Image:
    """Capture the game window described by *target* and return a PIL Image (RGB).

    This is the **primary capture entry point** in normal operation — it
    constrains the DXGI grab to the game window rect, so OCR and phash never
    see content from other windows or the taskbar.

    The correct dxcam output (monitor) is selected automatically from
    ``target.dxcam_output_idx``.

    Opens and closes an internal :class:`Capturer` on every call.
    For repeated captures, create a :class:`Capturer` once with
    ``Capturer(output_idx=target.dxcam_output_idx)`` and call
    :meth:`Capturer.grab_target` instead.
    """
    with Capturer(device_idx=device_idx, output_idx=target.dxcam_output_idx) as cap:
        return cap.grab_target(target)
