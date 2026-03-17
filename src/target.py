"""GameTarget — shared process/window descriptor for all JustReadIt modules.

Resolves a PID or process name to:
  - ``pid``              (int)  — target process ID; used by Frida attach and
                                  AllowSetForegroundWindow.
  - ``hwnd``             (int)  — handle to the process's main window; used for
                                  SetForegroundWindow.
  - ``window_rect``      (Rect) — bounding box of the main window in **virtual-
                                  screen** coordinates (physical pixels). Useful
                                  for positioning overlay windows.
  - ``capture_rect``     (Rect) — window rect clipped to its host monitor and
                                  converted to **monitor-local** coordinates
                                  (origin = monitor top-left). This is what dxcam
                                  expects as a ``region`` argument.
  - ``dxcam_output_idx`` (int)  — dxcam output index for the monitor the window
                                  is on. Pass to ``dxcam.create(output_idx=…)``.
  - ``process_name``     (str)  — resolved executable basename (e.g. "Game.exe").

Coordinate systems
------------------
Windows uses two overlapping coordinate systems:
* **Virtual screen**: origin at the top-left of the primary monitor, extends
  into negative territory for monitors to the left or above.  ``GetWindowRect``
  returns virtual-screen coordinates.
* **Monitor-local**: origin at the top-left of each individual monitor output.
  dxcam / DXGI require region coordinates in monitor-local space.

``GameTarget`` handles the conversion automatically:
  ``capture_rect = window_rect ∩ monitor_rect  −  monitor_origin``

All Win32 API calls are made through ``ctypes`` only — no pywin32 required.

DPI awareness
-------------
This module calls ``SetProcessDpiAwareness(PROCESS_SYSTEM_DPI_AWARE)`` once at
import time so that ``GetWindowRect`` returns physical pixel coordinates,
consistent with what dxcam / DXGI Desktop Duplication reports.  If the call
fails (e.g., DPI awareness was already set by the host process) the error is
silently ignored.

Main-window selection
---------------------
Among all top-level visible windows belonging to the target PID, the one with
the largest client area is selected.  This reliably identifies the game window
on single-game setups; if the process has no visible window at all,
``GameTarget.from_pid`` raises ``WindowNotFoundError``.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DPI awareness helpers
# ---------------------------------------------------------------------------

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 — matches what Qt 6 sets.
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_dpi_aware = False


def _ensure_dpi_aware() -> None:
    """Set DPI awareness to PER_MONITOR_AWARE_V2 if not already configured.

    Called lazily from :meth:`GameTarget.from_pid` / :meth:`from_name` rather
    than at module import time.  This prevents a conflict with Qt 6 which sets
    the same level via ``SetProcessDpiAwarenessContext`` on ``QApplication``
    construction — setting it earlier with the older API causes Qt to receive
    ERROR_ACCESS_DENIED and print a console warning.  Deferring ensures Qt can
    set it first when present.
    """
    global _dpi_aware
    if _dpi_aware:
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except (OSError, AttributeError) as exc:
        # Already set (e.g. by Qt), or Windows < 10 1703.  Fall back to the
        # older shcore API — safe to call when V2 is already active (no-op).
        _log.debug("SetProcessDpiAwarenessContext V2 unavailable (%s), trying shcore", exc)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except OSError as exc2:
            _log.debug("SetProcessDpiAwareness also failed: %s", exc2)
    _dpi_aware = True

# ---------------------------------------------------------------------------
# Win32 API bindings
# ---------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# DWMWA_EXTENDED_FRAME_BOUNDS (attribute 9) returns the visible window rect
# without the invisible DWM drop-shadow / resize-border that GetWindowRect
# includes.  Available on Vista+; always prefer it over GetWindowRect.
_DWMWA_EXTENDED_FRAME_BOUNDS: int = 9

# BOOL WINAPI EnumWindows(WNDENUMPROC, LPARAM)
_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

# PROCESSENTRY32W for CreateToolhelp32Snapshot
_MAX_PATH = 260

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

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# MONITORINFO for GetMonitorInfoW
_MONITORINFOF_PRIMARY = 0x00000001


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork",    wt.RECT),
        ("dwFlags",   wt.DWORD),
    ]


_MONITOR_DEFAULTTONEAREST = 2
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProcessNotFoundError(RuntimeError):
    """Raised when no process matches the given name or PID."""


class WindowNotFoundError(RuntimeError):
    """Raised when the target process has no visible top-level window."""


class AmbiguousProcessNameError(RuntimeError):
    """Raised when multiple processes share the given name."""

    def __init__(self, name: str, pids: list[int]) -> None:
        self.pids = pids
        super().__init__(
            f"Multiple processes named {name!r}: PIDs {pids}. "
            "Pass the specific PID via --pid."
        )


# ---------------------------------------------------------------------------
# Rect helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in virtual-screen coordinates (physical pixels).

    Follows the Win32 RECT convention: ``left``/``top`` are inclusive,
    ``right``/``bottom`` are exclusive.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def as_tuple(self) -> tuple[int, int, int, int]:
        """Return ``(left, top, right, bottom)`` — compatible with dxcam region."""
        return (self.left, self.top, self.right, self.bottom)


# ---------------------------------------------------------------------------
# Internal Win32 helpers
# ---------------------------------------------------------------------------


def _pid_to_name(pid: int) -> str:
    """Return the executable basename for *pid*, or ``""`` if unavailable."""
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE:
        return ""
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return ""
        while True:
            if entry.th32ProcessID == pid:
                return entry.szExeFile
            if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
        return ""
    finally:
        _kernel32.CloseHandle(snap)


def _name_to_pids(name: str) -> list[int]:
    """Return all PIDs whose executable basename matches *name* (case-insensitive).

    Accepts both ``"game"`` and ``"game.exe"`` as input.
    """
    needle = name.lower()
    if not needle.endswith(".exe"):
        needle += ".exe"

    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE:
        raise ProcessNotFoundError(
            f"CreateToolhelp32Snapshot failed (error {ctypes.get_last_error()})"
        )
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == needle:
                pids.append(entry.th32ProcessID)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return pids


def _window_title(hwnd: int) -> str:
    """Return the window title for *hwnd*, or ``""`` if none."""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_rect_visible(hwnd: int) -> Rect:
    """Return the *visible* bounding rect of *hwnd*.

    Prefers ``DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`` which
    excludes the invisible DWM drop-shadow / transparent resize border that
    ``GetWindowRect`` includes.  Falls back to ``GetWindowRect`` when DWM
    composition is off (Windows 7 basic theme, remote desktop, etc.).
    """
    raw = wt.RECT()
    if _dwmapi.DwmGetWindowAttribute(
        hwnd,
        _DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(raw),
        ctypes.sizeof(raw),
    ) != 0:  # S_OK == 0; non-zero → DWM composition off
        _user32.GetWindowRect(hwnd, ctypes.byref(raw))
    return Rect(raw.left, raw.top, raw.right, raw.bottom)


def _main_window_for_pid(pid: int) -> tuple[int, Rect]:
    """Return ``(hwnd, Rect)`` for the best visible top-level window of *pid*.

    Selection rules (in priority order):
    1. Windows **with a non-empty title** — the real game window always has a
       title; DirectX render-surface windows and message-only windows do not.
       Among titled windows, pick the one with the largest area.
    2. If no titled window is found, fall back to the largest visible window
       regardless of title.

    Raises :exc:`WindowNotFoundError` if no qualifying window is found.
    """
    # Collect all candidate (hwnd, rect, has_title) tuples.
    candidates: list[tuple[int, Rect, bool]] = []

    def _callback(hwnd: int, _: int) -> bool:
        # Must be visible
        if not _user32.IsWindowVisible(hwnd):
            return True

        # Must be a root window (no parent in the parent chain)
        if _user32.GetAncestor(hwnd, 2) != hwnd:  # GA_ROOT = 2
            return True

        # Must belong to our target PID
        proc_id = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value != pid:
            return True

        rect = _window_rect_visible(hwnd)
        if rect.area <= 0:
            return True

        candidates.append((hwnd, rect, bool(_window_title(hwnd))))
        return True

    _user32.EnumWindows(_WNDENUMPROC(_callback), 0)

    if not candidates:
        raise WindowNotFoundError(
            f"No visible top-level window found for PID {pid}. "
            "Make sure the game window is open and not minimised."
        )

    # Prefer titled windows (game main window) over untitled ones (DX surfaces).
    titled = [(hwnd, rect) for hwnd, rect, has_title in candidates if has_title]
    pool = titled if titled else [(hwnd, rect) for hwnd, rect, _ in candidates]

    best_hwnd, best_rect = max(pool, key=lambda t: t[1].area)
    return best_hwnd, best_rect


def _monitor_for_hwnd(hwnd: int) -> tuple[int, Rect, bool]:
    """Return ``(hmonitor, monitor_rect, is_primary)`` for the monitor the window is on.

    Uses ``MONITOR_DEFAULTTONEAREST`` so windows near a monitor edge still
    resolve correctly.
    """
    hmonitor = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info))
    r = info.rcMonitor
    rect = Rect(r.left, r.top, r.right, r.bottom)
    is_primary = bool(info.dwFlags & _MONITORINFOF_PRIMARY)
    return int(hmonitor), rect, is_primary


def _enumerate_monitor_rects() -> list[Rect]:
    """Return all monitor rects ordered primary-first, then left-to-right."""
    rects: list[Rect] = []

    def _cb(
        hmonitor: int, hdc: int, rect_ptr: ctypes.POINTER(wt.RECT), _: int  # type: ignore[valid-type]
    ) -> bool:
        r = rect_ptr.contents
        rects.append(Rect(r.left, r.top, r.right, r.bottom))
        return True

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    # Primary monitor is at virtual-screen origin (0, 0) — sort so it comes first.
    rects.sort(key=lambda r: (r.left != 0 or r.top != 0, r.left, r.top))
    return rects


def _compute_capture_rect(window_rect: Rect, monitor_rect: Rect) -> Rect:
    """Clip *window_rect* to *monitor_rect* and convert to monitor-local coords.

    dxcam requires ``region`` in monitor-local space (origin = monitor top-left).
    """
    left   = max(window_rect.left,   monitor_rect.left)
    top    = max(window_rect.top,    monitor_rect.top)
    right  = min(window_rect.right,  monitor_rect.right)
    bottom = min(window_rect.bottom, monitor_rect.bottom)
    ox, oy = monitor_rect.left, monitor_rect.top
    return Rect(left - ox, top - oy, right - ox, bottom - oy)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameTarget:
    """Resolved process + window descriptor shared by all JustReadIt modules.

    Do not instantiate directly; use :meth:`from_pid` or :meth:`from_name`.

    Attributes
    ----------
    pid:
        Target process ID.
    hwnd:
        Main window handle (largest visible top-level window of the process).
    window_rect:
        Bounding box of the main window in **virtual-screen** coordinates
        (physical pixels, can be negative on multi-monitor setups).
        Use this for positioning overlay windows.
    capture_rect:
        ``window_rect`` clipped to its host monitor and converted to
        **monitor-local** coordinates.  Pass
        ``target.capture_rect.as_tuple()`` to
        :meth:`~src.capture.Capturer.grab`.
    dxcam_output_idx:
        dxcam output index for the monitor the window is on.  Pass to
        ``Capturer(output_idx=target.dxcam_output_idx)``.
    process_name:
        Resolved executable basename (e.g. ``"LightVN.exe"``).
    """

    pid: int
    hwnd: int
    hmonitor: int
    window_rect: Rect
    capture_rect: Rect
    dxcam_output_idx: int  # informational only — may not match dxcam (device_idx, output_idx)
    process_name: str

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_pid(cls, pid: int) -> "GameTarget":
        """Build a :class:`GameTarget` from a known *pid*.

        Raises
        ------
        WindowNotFoundError
            If the process has no visible top-level window.
        """
        _ensure_dpi_aware()
        hwnd, window_rect = _main_window_for_pid(pid)
        hmonitor, monitor_rect, _ = _monitor_for_hwnd(hwnd)
        capture_rect = _compute_capture_rect(window_rect, monitor_rect)
        monitor_rects = _enumerate_monitor_rects()
        try:
            dxcam_output_idx = monitor_rects.index(monitor_rect)
        except ValueError:
            _log.debug("Monitor rect not found in dxcam outputs, falling back to output 0")
            dxcam_output_idx = 0  # fallback: primary
        name = _pid_to_name(pid) or f"<PID {pid}>"
        return cls(
            pid=pid,
            hwnd=hwnd,
            hmonitor=hmonitor,
            window_rect=window_rect,
            capture_rect=capture_rect,
            dxcam_output_idx=dxcam_output_idx,
            process_name=name,
        )

    @classmethod
    def from_name(cls, name: str) -> "GameTarget":
        """Build a :class:`GameTarget` from a process name (e.g. ``"Game.exe"``).

        Raises
        ------
        ProcessNotFoundError
            If no running process matches *name*.
        AmbiguousProcessNameError
            If multiple processes share *name*.  Pass the specific PID instead.
        WindowNotFoundError
            If the matched process has no visible top-level window.
        """
        _ensure_dpi_aware()
        pids = _name_to_pids(name)
        if not pids:
            raise ProcessNotFoundError(
                f"No running process named {name!r}. "
                "Check the name in Task Manager and make sure the game is running."
            )
        if len(pids) > 1:
            raise AmbiguousProcessNameError(name, pids)
        return cls.from_pid(pids[0])

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def refresh(self) -> "GameTarget":
        """Re-query the window rect (call if the game window was moved/resized)."""
        hwnd, window_rect = _main_window_for_pid(self.pid)
        hmonitor, monitor_rect, _ = _monitor_for_hwnd(hwnd)
        capture_rect = _compute_capture_rect(window_rect, monitor_rect)
        monitor_rects = _enumerate_monitor_rects()
        try:
            dxcam_output_idx = monitor_rects.index(monitor_rect)
        except ValueError:
            _log.debug("Monitor rect not found in dxcam outputs on refresh, falling back to output 0")
            dxcam_output_idx = 0
        return GameTarget(
            pid=self.pid,
            hwnd=hwnd,
            hmonitor=hmonitor,
            window_rect=window_rect,
            capture_rect=capture_rect,
            dxcam_output_idx=dxcam_output_idx,
            process_name=self.process_name,
        )
