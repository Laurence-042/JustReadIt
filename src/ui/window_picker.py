"""Window picker — click-to-select any on-screen window.

Usage::

    picker = WindowPicker(parent)
    picker.picked.connect(lambda pid: ...)   # int: target PID
    picker.cancelled.connect(...)
    picker.start()                           # call after hiding the main window

The picker polls ``GetAsyncKeyState(VK_LBUTTON)`` every 50 ms.  It waits for
the button to be *released* at least once before accepting a click, so the
mouse-down that triggered the "Pick Window" button itself is ignored.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QCursor

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_GA_ROOT = 2  # GetAncestor flag: return root (non-child) ancestor


class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class WindowPicker(QObject):
    """Resolve a left mouse click to the PID of the clicked window.

    Signals
    -------
    picked(pid: int)
        Emitted with the PID of the process owning the clicked root window.
    cancelled()
        Emitted if picking was cancelled (e.g. right-click, or ``cancel()``).
    """

    picked = Signal(int)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)
        self._seen_release = False

    def start(self) -> None:
        """Begin polling.  Call after minimising/hiding the main window."""
        self._seen_release = False
        self._timer.start()

    def cancel(self) -> None:
        """Stop polling and emit ``cancelled``."""
        self._timer.stop()
        self.cancelled.emit()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        lbutton_down = bool(_user32.GetAsyncKeyState(0x01) & 0x8000)

        if not self._seen_release:
            # Ignore the button-down that came from clicking "Pick Window".
            if not lbutton_down:
                self._seen_release = True
            return

        rbutton_down = bool(_user32.GetAsyncKeyState(0x02) & 0x8000)
        if rbutton_down:
            self._timer.stop()
            self.cancelled.emit()
            return

        if lbutton_down:
            self._timer.stop()
            pos = QCursor.pos()
            hwnd = _user32.WindowFromPoint(_POINT(pos.x(), pos.y()))
            root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
            pid = wt.DWORD(0)
            _user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
            if pid.value:
                self.picked.emit(pid.value)
            else:
                self.cancelled.emit()
