# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Topmost transparent overlay windows for displaying translations.

Three classes
-------------
``Overlay``
    Abstract base — a frameless, topmost, translucent :class:`QWidget`.

``TranslationOverlay``
    A small semi-transparent popup drawn near the hovered text region,
    dismissed automatically after a configurable timeout.

``FreezeOverlay``
    A full-window screenshot overlay that covers the game window.
    Right-click or Escape dismisses the overlay and returns keyboard/mouse
    focus to the game process via ``AllowSetForegroundWindow`` +
    ``SetForegroundWindow``.

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
# Overlay — base class
# ---------------------------------------------------------------------------

class Overlay(QWidget):
    """Frameless topmost translucent overlay — common base for all overlays.

    Subclasses override :meth:`paintEvent` to draw their content.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        flags = (
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    # Shared drawing helper ------------------------------------------------

    @staticmethod
    def _draw_bubble(painter: QPainter, rect: QRect, text: str) -> None:
        """Draw a rounded semi-transparent bubble containing *text*."""
        bg = QColor(18, 18, 28, 215)
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)

        painter.setPen(QColor(235, 235, 250))
        font = QFont("Segoe UI", 11)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(14, 10, -14, -10),
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            text,
        )


# ---------------------------------------------------------------------------
# TranslationOverlay — hover-mode translation popup
# ---------------------------------------------------------------------------

class TranslationOverlay(Overlay):
    """Semi-transparent popup that displays translated text near the source.

    Parameters
    ----------
    parent:
        Qt parent widget (normally ``None``).
    auto_hide_ms:
        Timeout before the popup hides itself.  ``0`` disables auto-hide.
    """

    _DEFAULT_AUTO_HIDE_MS: int = 5000

    def __init__(
        self,
        parent: QWidget | None = None,
        auto_hide_ms: int = _DEFAULT_AUTO_HIDE_MS,
    ) -> None:
        super().__init__(parent)
        self._auto_hide_ms = auto_hide_ms
        self._text: str = ""
        self._is_loading: bool = False

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # ------------------------------------------------------------------
    # Public API
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
            ``(left, top)`` of the game window in virtual-screen coordinates.
        """
        self._hide_timer.stop()
        self._is_loading = False
        self._text = text

        # ---- size the widget to fit the text ------------------------
        font = QFont("Segoe UI", 11)
        fm = QFontMetrics(font)
        max_w = 640
        lines = text.splitlines() if text else [""]
        text_w = min(max_w, max(fm.horizontalAdvance(ln) for ln in lines) + 32)
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

    def show_progress(
        self,
        step: str,
        near_rect: tuple[int, int, int, int] | None = None,
        screen_origin: tuple[int, int] = (0, 0),
    ) -> None:
        """Show a loading-state indicator during pipeline processing.

        Unlike :meth:`show_translation` this does **not** start the auto-hide
        timer — the popup stays until replaced by the real translation.

        Parameters
        ----------
        step:
            Short description of the current processing stage, e.g.
            ``"正在翻译\u2026"``.
        near_rect:
            Same as :meth:`show_translation`.  ``None`` falls back to cursor
            position.
        screen_origin:
            Same as :meth:`show_translation`.
        """
        self._hide_timer.stop()
        self._is_loading = True
        self._text = step

        # ---- size the widget (same logic as show_translation) ------
        font = QFont("Segoe UI", 10)
        fm = QFontMetrics(font)
        text_w = min(320, fm.horizontalAdvance(step) + 48)
        text_h = fm.height() + 24
        self.resize(text_w, text_h)

        # ---- compute position --------------------------------------
        ox_phys, oy_phys = screen_origin
        screen, dpr = _screen_dpr(ox_phys, oy_phys)
        if near_rect is not None:
            rx, ry, rw, rh = near_rect
            cx = round((ox_phys + rx + rw // 2) / dpr)
            cy = round((oy_phys + ry + rh + 8) / dpr)
        else:
            app = QApplication.instance()
            cursor_pos = app.cursorPos() if app is not None else QPoint(0, 0)  # type: ignore[union-attr]
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
        # Fallback auto-hide: if the pipeline finishes without emitting a
        # translation (e.g. OCR found no text), hide the loading bubble after
        # 2 seconds so it does not linger on screen.
        self._hide_timer.start(2000)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._is_loading:
            self._draw_loading_bubble(painter, self.rect(), self._text)
        else:
            self._draw_bubble(painter, self.rect(), self._text)

    @staticmethod
    def _draw_loading_bubble(painter: QPainter, rect: QRect, text: str) -> None:
        """Draw a muted progress-indicator bubble."""
        bg = QColor(20, 28, 48, 185)
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)

        painter.setPen(QColor(140, 155, 200))
        font = QFont("Segoe UI", 10)
        font.setItalic(True)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(14, 8, -14, -8),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# FreezeOverlay — frozen screenshot with dismiss + focus handoff
# ---------------------------------------------------------------------------

class FreezeOverlay(Overlay):
    """Full-window screenshot overlay for freeze mode.

    Covers the game window with a frozen image.  Right-click or Escape
    dismisses the overlay and returns focus to the game process.

    Signals
    -------
    hover_requested(x, y)
        Emitted on mouse-move while the overlay is active.  Coordinates are
        in **image pixels** (physical), suitable for passing directly to
        :meth:`HoverController.on_freeze_hover`.
    dismissed()
        Emitted when the overlay is dismissed (right-click or Escape).
    """

    hover_requested = Signal(float, float)
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._text: str = ""
        self._target_pid: int | None = None
        self._target_hwnd: int | None = None
        self._active: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """``True`` while the freeze overlay is visible."""
        return self._active

    def freeze(
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
        target_pid:
            PID used for :func:`AllowSetForegroundWindow` on dismiss.
        target_hwnd:
            HWND used for :func:`SetForegroundWindow` on dismiss.
        """
        self._active = True
        self._target_pid = target_pid
        self._target_hwnd = target_hwnd
        self._text = ""

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

        # HiDPI: convert physical pixel sizes/coords to Qt logical pixels.
        screen, dpr = _screen_dpr(window_left, window_top)
        pix.setDevicePixelRatio(dpr)
        self._pixmap = pix

        log_w = round(img_rgb.width / dpr)
        log_h = round(img_rgb.height / dpr)
        log_left = round(window_left / dpr)
        log_top = round(window_top / dpr)

        self.resize(log_w, log_h)
        self.move(log_left, log_top)

        # Capture focus so the game cannot steal it.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setMouseTracking(True)
        self.update()
        self.show()
        self.activateWindow()
        self.raise_()

    def show_translation(self, text: str) -> None:
        """Update (or clear) the translation text drawn over the freeze image."""
        self._text = text
        self.update()

    def dismiss(self) -> None:
        """Hide the overlay and return focus to the game process."""
        self._active = False
        self._pixmap = None
        self._text = ""
        # Restore passthrough mode for base-class state.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(False)
        self.hide()
        self._return_focus()
        self.dismissed.emit()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

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

    def _bubble_rect(self) -> QRect:
        """Centred bottom-quarter rect for the translation bubble."""
        w = min(self.width() - 40, 700)
        font = QFont("Segoe UI", 11)
        fm = QFontMetrics(font)
        lines = self._text.splitlines() or [""]
        h = fm.height() * len(lines) + 28
        x = (self.width() - w) // 2
        y = self.height() - h - 40
        return QRect(x, y, w, h)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)
        if self._text:
            self._draw_bubble(painter, self._bubble_rect(), self._text)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._active:
            dpr = self.devicePixelRatio()
            self.hover_requested.emit(
                event.position().x() * dpr,
                event.position().y() * dpr,
            )
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.dismiss()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)
