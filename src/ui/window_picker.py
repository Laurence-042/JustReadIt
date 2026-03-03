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
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GA_ROOT = 2  # GetAncestor flag: return root (non-child) ancestor

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

_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_wchar * _MAX_PATH),
    ]


def _pid_to_exe(pid: int) -> str:
    """Return the lowercase exe basename for *pid*, or ``""``."""
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE:
        return ""
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return entry.szExeFile.lower()
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        return ""
    finally:
        _kernel32.CloseHandle(snap)


def _window_at(x: int, y: int) -> int:
    """Return the PID of the topmost non-shell visible window that contains (x, y).

    Strategy (in order):
    1. Read ``GetForegroundWindow()`` — after a real click the target window
       becomes foreground.  This is the most reliable signal for DirectX /
       hardware-composited game windows where ``WindowFromPoint`` and Z-order
       walks are intercepted by transparent UWP shell overlays.
    2. If the foreground window's process is a known shell process (e.g. the
       desktop or taskbar was clicked), fall back to a Z-order walk that skips
       shell processes and windows with WS_EX_TRANSPARENT / WS_EX_NOACTIVATE
       extended styles.

    Returns 0 if no suitable window is found.
    """
    # Extended-style flags that indicate "not a real interactive window".
    WS_EX_TRANSPARENT  = 0x00000020
    WS_EX_NOACTIVATE   = 0x08000000
    GWL_EXSTYLE        = -20

    def _is_shell_or_overlay(hwnd: int) -> bool:
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _pid_to_exe(pid.value)
        if exe in _SHELL_PROCESS_NAMES:
            return True
        ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex_style & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
            return True
        return False

    # ── Strategy 1: foreground window after click ──────────────────────
    fg = _user32.GetForegroundWindow()
    if fg and not _is_shell_or_overlay(fg):
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value:
            return pid.value

    # ── Strategy 2: Z-order walk, skip shell / transparent windows ─────
    hwnd = _user32.GetTopWindow(None)
    while hwnd:
        if _user32.IsWindowVisible(hwnd):
            raw = wt.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(raw))
            if raw.left <= x < raw.right and raw.top <= y < raw.bottom:
                if not _is_shell_or_overlay(hwnd):
                    pid = wt.DWORD(0)
                    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        return pid.value
        hwnd = _user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT = 2

    return 0


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
            pid = _window_at(pos.x(), pos.y())
            if pid:
                self.picked.emit(pid)
            else:
                self.cancelled.emit()
