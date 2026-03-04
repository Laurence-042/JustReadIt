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

import psutil
import win32api
import win32con
import win32gui

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QInputDialog

# Shell / system process names to skip when picking a window.
# These processes own transparent overlay windows that sit on top of everything
# and are commonly returned by WindowFromPoint even when clicking a game.
_SHELL_PROCESS_NAMES = frozenset({
    "explorer.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "dwm.exe",
    "textinputhost.exe",
    "applicationframehost.exe",
})


def _is_shell(pid: int) -> bool:
    """Return True if *pid* belongs to a known shell/system process."""
    try:
        return psutil.Process(pid).name().lower() in _SHELL_PROCESS_NAMES
    except psutil.NoSuchProcess:
        return True


def _enumerate_windows() -> list[tuple[str, int]]:
    """Return ``(title, pid)`` for every visible top-level window with a
    non-empty title, skipping known shell/system processes."""
    results: list[tuple[str, int]] = []

    def _cb(hwnd: int, _: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        _, pid = win32gui.GetWindowThreadProcessId(hwnd)
        if _is_shell(pid):
            return True
        results.append((title, pid))
        return True

    win32gui.EnumWindows(_cb, None)
    return results


def _foreground_pid() -> int:
    """Return the PID of the current foreground window if it is not a shell
    or overlay process, otherwise return 0."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return 0
    _, pid = win32gui.GetWindowThreadProcessId(hwnd)
    if not pid or _is_shell(pid):
        return 0
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & (win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE):
        return 0
    return pid


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
        lbutton_down = bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)

        if not self._seen_release:
            # Ignore the button-down that came from clicking "Pick Window".
            if not lbutton_down:
                self._seen_release = True
            return

        if win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000:
            self._timer.stop()
            self.cancelled.emit()
            return

        if lbutton_down:
            self._timer.stop()
            pid = _foreground_pid()
            if pid:
                self.picked.emit(pid)
                return
            # Foreground detection failed (e.g. exclusive-fullscreen game that
            # never takes Win32 foreground focus).  Let the user pick manually.
            self._pick_from_list()

    def _pick_from_list(self) -> None:
        """Show a dialog listing all visible non-shell windows; emit
        ``picked`` or ``cancelled`` based on the user's choice."""
        windows = _enumerate_windows()
        if not windows:
            self.cancelled.emit()
            return

        # Build display labels; append PID when titles are duplicated.
        title_counts: dict[str, int] = {}
        for title, _ in windows:
            title_counts[title] = title_counts.get(title, 0) + 1
        labels = [
            f"{title}  (PID {pid})" if title_counts[title] > 1 else title
            for title, pid in windows
        ]

        chosen, ok = QInputDialog.getItem(
            None,
            "选择目标窗口",
            "未能自动识别目标窗口，请手动选择：",
            labels,
            0,
            False,
        )
        if ok:
            idx = labels.index(chosen)
            self.picked.emit(windows[idx][1])
        else:
            self.cancelled.emit()