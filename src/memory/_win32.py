"""Low-level Win32 helpers for reading remote process memory.

All API calls use ``ctypes.WinDLL``; no pywin32 dependency (project convention).
Only read-access is requested — zero intrusion on the target process.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESS_VM_READ           = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT  = 0x00001000
MEM_FREE    = 0x00010000
MEM_RESERVE = 0x00002000

MEM_IMAGE   = 0x01000000
MEM_MAPPED  = 0x00040000
MEM_PRIVATE = 0x00020000

PAGE_NOACCESS          = 0x01
PAGE_READONLY          = 0x02
PAGE_READWRITE         = 0x04
PAGE_WRITECOPY         = 0x08
PAGE_EXECUTE           = 0x10
PAGE_EXECUTE_READ      = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD             = 0x100

# Bitmask of protection values that allow reading.
_READABLE_PROTECTIONS = (
    PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY
    | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
)


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """``MEMORY_BASIC_INFORMATION`` for 64-bit Windows.

    ctypes inserts the needed alignment padding automatically (4 bytes after
    ``AllocationProtect`` to align ``RegionSize`` to 8, and 4 bytes after
    ``Type`` to align the struct to 8).  Total size: 48 bytes on x64.
    """

    _fields_ = [
        ("BaseAddress",       ctypes.c_void_p),   # 8
        ("AllocationBase",    ctypes.c_void_p),   # 8
        ("AllocationProtect", wt.DWORD),           # 4 (+4 padding)
        ("RegionSize",        ctypes.c_size_t),    # 8
        ("State",             wt.DWORD),           # 4
        ("Protect",           wt.DWORD),           # 4
        ("Type",              wt.DWORD),           # 4 (+4 padding)
    ]


# ---------------------------------------------------------------------------
# WinAPI bindings
# ---------------------------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.OpenProcess.restype   = wt.HANDLE
_k32.OpenProcess.argtypes  = [wt.DWORD, wt.BOOL, wt.DWORD]

_k32.CloseHandle.restype   = wt.BOOL
_k32.CloseHandle.argtypes  = [wt.HANDLE]

_k32.VirtualQueryEx.restype  = ctypes.c_size_t
_k32.VirtualQueryEx.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]

_k32.ReadProcessMemory.restype  = wt.BOOL
_k32.ReadProcessMemory.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def open_process_readonly(pid: int) -> int:
    """Open *pid* with ``PROCESS_VM_READ | PROCESS_QUERY_INFORMATION``.

    Returns a raw HANDLE value.  Caller must call :func:`close_handle` when
    done.

    Raises
    ------
    OSError
        If ``OpenProcess`` fails (wrong PID, insufficient privileges, etc.).
    """
    access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    handle = _k32.OpenProcess(access, False, pid)
    if not handle:
        raise OSError(
            f"OpenProcess failed for PID {pid} "
            f"(error {ctypes.get_last_error()}). "
            "Make sure the game is running and JustReadIt has sufficient "
            "privileges (try running as administrator)."
        )
    return handle


def close_handle(handle: int) -> None:
    """Close a Win32 HANDLE."""
    _k32.CloseHandle(handle)


def is_readable(mbi: MEMORY_BASIC_INFORMATION) -> bool:
    """Return ``True`` if *mbi* describes a committed, readable region."""
    if mbi.State != MEM_COMMIT:
        return False
    if mbi.Protect & PAGE_GUARD:
        return False
    if mbi.Protect & PAGE_NOACCESS:
        return False
    return bool(mbi.Protect & _READABLE_PROTECTIONS)


def enumerate_regions(
    handle: int,
) -> list[tuple[int, int, int, int]]:
    """Return readable committed regions as ``(base, size, protect, type)``.

    Walks the x64 user-mode address space (0 → 0x7FFF_FFFF_FFFF).
    """
    regions: list[tuple[int, int, int, int]] = []
    mbi = MEMORY_BASIC_INFORMATION()
    mbi_size = ctypes.sizeof(mbi)
    addr = 0
    max_addr = 0x7FFFFFFFFFFF

    while addr < max_addr:
        ret = _k32.VirtualQueryEx(handle, addr, ctypes.byref(mbi), mbi_size)
        if ret == 0:
            break

        if is_readable(mbi):
            base = mbi.BaseAddress if mbi.BaseAddress is not None else 0
            rtype = mbi.Type if mbi.Type is not None else 0
            regions.append((base, mbi.RegionSize, mbi.Protect, rtype))

        next_addr = (mbi.BaseAddress if mbi.BaseAddress is not None else 0) + mbi.RegionSize
        if next_addr <= addr:
            break  # prevent infinite loop
        addr = next_addr

    return regions


def read_region(handle: int, base: int, size: int) -> bytes | None:
    """Read *size* bytes from *base* in the target process.

    Returns the bytes read, or ``None`` if ``ReadProcessMemory`` fails
    (e.g. the page was freed between ``VirtualQueryEx`` and the read).
    """
    buf = (ctypes.c_char * size)()
    bytes_read = ctypes.c_size_t(0)
    ok = _k32.ReadProcessMemory(handle, base, buf, size, ctypes.byref(bytes_read))
    if not ok or bytes_read.value == 0:
        return None
    return bytes(buf[: bytes_read.value])
