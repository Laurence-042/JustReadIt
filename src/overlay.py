# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Topmost transparent overlay window for displaying translations.

Two display modes
-----------------
**Normal (hover) mode**
    A small semi-transparent popup drawn near the hovered text region,
    dismissed automatically after a configurable timeout.

**Freeze mode**
    The captured game-window screenshot is stretched over the original window
    area as a topmost frameless widget; the user hovers the frozen image to
    trigger translations exactly as in hover mode.  Right-clicking (or
    pressing Escape) dismisses the overlay and returns keyboard/mouse focus to
    the game process via ``AllowSetForegroundWindow`` + ``SetForegroundWindow``.

Focus handoff
-------------
Direct cross-process ``SetForegroundWindow`` is blocked by Windows unless the
calling process has been explicitly granted permission via
``AllowSetForegroundWindow(pid)``.  This module calls both in sequence, which
is the supported pattern.
"""
from __future__ import annotations

import ctypes
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QPoint, QRect, Qt, Signal, QTimer,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QPainter, QPixmap, QScreen,
)
from PySide6.QtWidgets import QApplication, QWidget

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _screen_dpr(phys_x: int, phys_y: int) -> tuple["QScreen | None", float]:
    """Return the QScreen and its devicePixelRatio for a Win32 physical-pixel coord.

    Win32 APIs (GetWindowRect, DwmGetWindowAttribute, DXGI) return coordinates
    in *physical* pixels, while Qt 6 ``QWidget`` geometry uses *logical*
    (device-independent) pixels.  This helper bridges the gap.

    ``QApplication.screenAt`` expects logical Qt coordinates; passing physical
    values is an approximation that works correctly when all monitors share the
    same DPI and gives a close result on mixed-DPI setups.
    """
    screen = QApplication.screenAt(QPoint(phys_x, phys_y))
    if screen is None:
        screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    return screen, dpr


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

def _allow_set_foreground(pid: int) -> None:
    """Grant *pid* permission to call ``SetForegroundWindow``."""
    _user32.AllowSetForegroundWindow(pid)


def _set_foreground(hwnd: int) -> bool:
    """Bring *hwnd* to the foreground.

    Must be called after :func:`_allow_set_foreground` when the caller does
    not already own the foreground lock.
    """
    return bool(_user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))


# ---------------------------------------------------------------------------
# TranslationOverlay
# ---------------------------------------------------------------------------

class TranslationOverlay(QWidget):
    """Topmost transparent overlay for translated text.

    Parameters
    ----------
    parent:
        Qt parent widget (normally ``None`` for a standalone top-level window).
    auto_hide_ms:
        Milliseconds before the normal-mode popup dismisses itself.
        Set to 0 to disable auto-hide.

    Signals
    -------
    hover_requested(x, y)
        Emitted in Freeze mode when the mouse moves over the screenshot.
        ``x`` and ``y`` are coordinates **relative to the game-window capture
        image** (i.e. monitor-local pixel coordinates starting at 0,0 for the
        top-left of the captured region).
    freeze_dismissed()
        Emitted when Freeze mode ends (right-click or Escape).
    """

    hover_requested = Signal(float, float)
    freeze_dismissed = Signal()

    # Default auto-hide timeout for normal hover popups (ms).
    _DEFAULT_AUTO_HIDE_MS: int = 5000

    def __init__(
        self,
        parent: QWidget | None = None,
        auto_hide_ms: int = _DEFAULT_AUTO_HIDE_MS,
    ) -> None:
        super().__init__(parent)

        flags = (
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._auto_hide_ms = auto_hide_ms
        self._translation_text: str = ""
        self._freeze_pixmap: QPixmap | None = None
        self._is_freeze_mode: bool = False
        self._freeze_window_origin: tuple[int, int] = (0, 0)
        self._target_pid: int | None = None
        self._target_hwnd: int | None = None

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

        self.setMouseTracking(False)

    # ------------------------------------------------------------------
    # Normal hover mode
    # ------------------------------------------------------------------

    def show_translation(
        self,
        text: str,
        near_rect: tuple[int, int, int, int] | None = None,
        screen_origin: tuple[int, int] = (0, 0),
    ) -> None:
        """Show *text* as a floating translation popup.

        Parameters
        ----------
        text:
            Translated text to display.
        near_rect:
            ``(x, y, w, h)`` bounding box of the source text in game-window-
            local pixel coordinates.  The popup is placed just below this box.
            When ``None`` the popup is placed 24 px below the current cursor.
        screen_origin:
            ``(left, top)`` of the game window's capture region in virtual-
            screen coordinates.  Used to convert game-local coords to screen
            coords.
        """
        self._hide_timer.stop()
        self._translation_text = text
        self._freeze_pixmap = None
        self._is_freeze_mode = False

        # ---- size the widget to fit the text ------------------------
        font = QFont("Segoe UI", 11)
        fm = QFontMetrics(font)
        max_w = 640
        lines = text.splitlines() if text else [""]
        text_w = min(max_w, max(fm.horizontalAdvance(l) for l in lines) + 32)
        text_h = fm.height() * len(lines) + 28
        self.resize(text_w, text_h)

        # ---- compute position ---------------------------------------
        ox_phys, oy_phys = screen_origin
        screen, dpr = _screen_dpr(ox_phys, oy_phys)
        # Convert physical Win32 coords to Qt logical pixels.
        if near_rect is not None:
            rx, ry, rw, rh = near_rect
            cx = round((ox_phys + rx + rw // 2) / dpr)
            cy = round((oy_phys + ry + rh + 8) / dpr)
        else:
            app = QApplication.instance()
            cursor_pos = app.cursorPos() if app is not None else QPoint(0, 0)  # type: ignore[union-attr]
            # cursorPos() is already in Qt logical pixels.
            cx = cursor_pos.x()
            cy = cursor_pos.y() + 24
            screen = QApplication.screenAt(cursor_pos) or screen

        sg = screen.geometry() if screen else None
        if sg is not None:
            x = min(max(cx - text_w // 2, sg.x()), sg.right() - text_w)
            y = min(max(cy, sg.y()), sg.bottom() - text_h)
        else:
            x = cx - text_w // 2
            y = cy

        self.move(x, y)
        self.update()
        self.show()

        if self._auto_hide_ms > 0:
            self._hide_timer.start(self._auto_hide_ms)

    # ------------------------------------------------------------------
    # Freeze mode
    # ------------------------------------------------------------------

    def enter_freeze_mode(
        self,
        screenshot: "PILImage",
        window_left: int,
        window_top: int,
        target_pid: int | None = None,
        target_hwnd: int | None = None,
    ) -> None:
        """Display *screenshot* as a topmost frozen-frame overlay.

        The widget is resized and positioned to exactly cover the source game
        window so the user cannot tell the difference from the live window.

        Parameters
        ----------
        screenshot:
            RGB ``PIL.Image`` of the entire captured game-window region.
        window_left, window_top:
            Top-left corner of the game window in virtual-screen coordinates.
            Passed directly to ``QWidget.move()``.
        target_pid:
            PID passed to :func:`_allow_set_foreground` on dismiss.
        target_hwnd:
            HWND passed to :func:`_set_foreground` on dismiss.
        """
        self._hide_timer.stop()
        self._is_freeze_mode = True
        self._target_pid = target_pid
        self._target_hwnd = target_hwnd
        self._translation_text = ""
        self._freeze_window_origin = (window_left, window_top)

        # Convert PIL → QPixmap
        img_rgb = screenshot.convert("RGB")
        raw = img_rgb.tobytes("raw", "RGB")
        qimg = QImage(
            raw,
            img_rgb.width,
            img_rgb.height,
            img_rgb.width * 3,
            QImage.Format.Format_RGB888,
        )
        pix = QPixmap.fromImage(qimg)

        # ── HiDPI: convert physical pixel sizes/coords to Qt logical pixels ──
        # Win32 returns physical pixels; Qt 6 widget geometry is in logical pixels.
        # Setting the pixmap's devicePixelRatio makes Qt render it without scaling.
        screen, dpr = _screen_dpr(window_left, window_top)
        pix.setDevicePixelRatio(dpr)
        self._freeze_pixmap = pix

        log_w = round(img_rgb.width  / dpr)
        log_h = round(img_rgb.height / dpr)
        log_left = round(window_left / dpr)
        log_top  = round(window_top  / dpr)

        self.resize(log_w, log_h)
        self.move(log_left, log_top)

        # In freeze mode we need to capture focus so the game cannot steal it.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setMouseTracking(True)
        self.update()
        self.show()
        self.activateWindow()
        self.raise_()

    def show_freeze_translation(self, text: str) -> None:
        """Update (or clear) the translation text drawn over the freeze overlay."""
        self._translation_text = text
        self.update()

    def exit_freeze_mode(self) -> None:
        """Dismiss the freeze overlay and return focus to the game process."""
        self._is_freeze_mode = False
        self._freeze_pixmap = None
        self._translation_text = ""
        # Restore passthrough mode for normal hover popups
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(False)
        self.hide()
        self._return_focus()
        self.freeze_dismissed.emit()

    def _return_focus(self) -> None:
        if self._target_pid is not None:
            try:
                _allow_set_foreground(self._target_pid)
            except Exception:
                pass
        if self._target_hwnd is not None:
            try:
                _set_foreground(self._target_hwnd)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._is_freeze_mode and self._freeze_pixmap is not None:
            painter.drawPixmap(0, 0, self._freeze_pixmap)
            if self._translation_text:
                self._draw_bubble(painter, self._translation_bubble_rect())
        elif self._translation_text:
            self._draw_bubble(painter, self.rect())

    def _translation_bubble_rect(self) -> QRect:
        """Return the rect for the translation bubble in freeze mode.

        The bubble is placed at the bottom quarter of the overlay, horizontally
        centred, with a comfortable margin.
        """
        w = min(self.width() - 40, 700)
        font = QFont("Segoe UI", 11)
        fm = QFontMetrics(font)
        lines = self._translation_text.splitlines() or [""]
        h = fm.height() * len(lines) + 28
        x = (self.width() - w) // 2
        y = self.height() - h - 40
        return QRect(x, y, w, h)

    def _draw_bubble(self, painter: QPainter, rect: QRect) -> None:
        """Draw a rounded semi-transparent background with the translation text."""
        bg = QColor(18, 18, 28, 215)
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)

        painter.setPen(QColor(235, 235, 250))
        font = QFont("Segoe UI", 11)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(14, 10, -14, -10),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            self._translation_text,
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._is_freeze_mode:
            self.hover_requested.emit(event.position().x(), event.position().y())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._is_freeze_mode and event.button() == Qt.MouseButton.RightButton:
            self.exit_freeze_mode()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            if self._is_freeze_mode:
                self.exit_freeze_mode()
            else:
                self.hide()
            return
        super().keyPressEvent(event)
