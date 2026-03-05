"""Low-level Win32 helpers for DLL injection and Named Pipe I/O.

All API calls use ctypes.WinDLL; no pywin32 dependency (project convention).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESS_ALL_ACCESS     = 0x1F0FFF
MEM_COMMIT             = 0x00001000
MEM_RESERVE            = 0x00002000
MEM_RELEASE            = 0x00008000
PAGE_READWRITE         = 0x04

PIPE_ACCESS_DUPLEX     = 0x00000003
PIPE_TYPE_BYTE         = 0x00000000
PIPE_READMODE_BYTE     = 0x00000000
PIPE_WAIT              = 0x00000000
GENERIC_READ           = 0x80000000
GENERIC_WRITE          = 0x40000000
OPEN_EXISTING          = 3

_INVALID_HANDLE: int = ctypes.cast(ctypes.c_void_p(-1), ctypes.c_void_p).value  # type: ignore[arg-type]

# ---------------------------------------------------------------------------
# WinAPI declarations
# ---------------------------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.OpenProcess.restype          = wt.HANDLE
_k32.OpenProcess.argtypes         = [wt.DWORD, wt.BOOL, wt.DWORD]

_k32.VirtualAllocEx.restype       = ctypes.c_void_p
_k32.VirtualAllocEx.argtypes      = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                      wt.DWORD, wt.DWORD]

_k32.WriteProcessMemory.restype   = wt.BOOL
_k32.WriteProcessMemory.argtypes  = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

_k32.VirtualFreeEx.restype        = wt.BOOL
_k32.VirtualFreeEx.argtypes       = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]

_k32.GetModuleHandleA.restype     = wt.HMODULE
_k32.GetModuleHandleA.argtypes    = [ctypes.c_char_p]

_k32.GetProcAddress.restype       = ctypes.c_void_p
_k32.GetProcAddress.argtypes      = [wt.HMODULE, ctypes.c_char_p]

_k32.CreateRemoteThread.restype   = wt.HANDLE
_k32.CreateRemoteThread.argtypes  = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_void_p,
                                      wt.DWORD, ctypes.POINTER(wt.DWORD)]

_k32.WaitForSingleObject.restype  = wt.DWORD
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]

_k32.GetExitCodeThread.restype    = wt.BOOL
_k32.GetExitCodeThread.argtypes   = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]

_k32.CloseHandle.restype          = wt.BOOL
_k32.CloseHandle.argtypes         = [wt.HANDLE]

_k32.CreateNamedPipeW.restype     = wt.HANDLE
_k32.CreateNamedPipeW.argtypes    = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
                                      wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]

_k32.ConnectNamedPipe.restype     = wt.BOOL
_k32.ConnectNamedPipe.argtypes    = [wt.HANDLE, ctypes.c_void_p]

_k32.CreateFileW.restype          = wt.HANDLE
_k32.CreateFileW.argtypes         = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                      wt.DWORD, wt.DWORD, wt.HANDLE]

_k32.ReadFile.restype             = wt.BOOL
_k32.ReadFile.argtypes            = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                      ctypes.POINTER(wt.DWORD), ctypes.c_void_p]

_k32.WriteFile.restype            = wt.BOOL
_k32.WriteFile.argtypes           = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                      ctypes.POINTER(wt.DWORD), ctypes.c_void_p]

_k32.DisconnectNamedPipe.restype  = wt.BOOL
_k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InjectionError(RuntimeError):
    """Raised when DLL injection fails.  Message is actionable."""


class PipeError(OSError):
    """Raised on Named Pipe I/O failure."""


# ---------------------------------------------------------------------------
# DLL injection
# ---------------------------------------------------------------------------


def inject_dll(pid: int, dll_path: Path | str) -> None:
    """Inject *dll_path* into process *pid* via LoadLibraryA remote thread.

    Raises
    ------
    InjectionError
        On any Win32 failure with the error code and a suggestion.
    """
    dll_path = Path(dll_path).resolve()
    if not dll_path.exists():
        raise InjectionError(
            f"hook_engine.dll not found at {dll_path}.\n"
            "Build it first: run  src\\hook\\build.ps1  in an x64 MSVC prompt."
        )

    # DLL path as null-terminated ASCII bytes (ASCII because LoadLibraryA)
    dll_bytes = str(dll_path).encode("ascii") + b"\x00"

    h_proc = _k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_proc:
        raise InjectionError(
            f"OpenProcess({pid}) failed: error {ctypes.get_last_error()}. "
            "Run as Administrator."
        )
    try:
        remote_buf = _k32.VirtualAllocEx(
            h_proc, None, len(dll_bytes),
            MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE,
        )
        if not remote_buf:
            raise InjectionError(
                f"VirtualAllocEx failed: error {ctypes.get_last_error()}"
            )
        written = ctypes.c_size_t(0)
        _k32.WriteProcessMemory(
            h_proc, remote_buf,
            dll_bytes, len(dll_bytes),
            ctypes.byref(written),
        )
        k32_mod = _k32.GetModuleHandleA(b"kernel32.dll")
        load_lib = _k32.GetProcAddress(k32_mod, b"LoadLibraryA")
        if not load_lib:
            raise InjectionError("GetProcAddress(LoadLibraryA) failed")

        thr = _k32.CreateRemoteThread(
            h_proc, None, 0, load_lib, remote_buf, 0, None,
        )
        if not thr:
            raise InjectionError(
                f"CreateRemoteThread failed: error {ctypes.get_last_error()}"
            )
        _k32.WaitForSingleObject(thr, 10_000)
        exit_code = wt.DWORD(0)
        _k32.GetExitCodeThread(thr, ctypes.byref(exit_code))
        _k32.CloseHandle(thr)
        _k32.VirtualFreeEx(h_proc, remote_buf, 0, MEM_RELEASE)
        if exit_code.value == 0:
            raise InjectionError(
                f"LoadLibraryA returned NULL for {dll_path.name} — "
                "a dependency DLL is missing or the image is corrupt. "
                "Make sure MinHook.x64.dll is in the same directory as hook_engine.dll."
            )
    finally:
        _k32.CloseHandle(h_proc)


# ---------------------------------------------------------------------------
# Named Pipe helpers
# ---------------------------------------------------------------------------


def pipe_name(pid: int) -> str:
    """Return the pipe name used for a given game PID."""
    return rf"\\.\pipe\JRI-{pid}"


PIPE_UNLIMITED_INSTANCES = 255


def create_pipe_server(pid: int) -> int:
    """Create a duplex Named Pipe server for *pid* and return its HANDLE.

    Python is the server; the injected DLL connects as client.
    Call :func:`connect_pipe` (blocking) after injecting the DLL.

    ``nMaxInstances`` is ``PIPE_UNLIMITED_INSTANCES`` so that a new server
    instance can always be created even if a previous DLL client handle is
    still open (e.g. the DLL worker thread is mid-cleanup after the last
    session).  With ``nMaxInstances=1`` the old orphaned client handle keeps
    the pipe object alive, causing ``CreateNamedPipeW`` to fail with
    ERROR_PIPE_BUSY (231).
    """
    name = pipe_name(pid)
    h = _k32.CreateNamedPipeW(
        name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES,  # was 1 — see docstring
        65_536,     # out buffer
        65_536,     # in buffer
        5_000,      # timeout ms
        None,       # security
    )
    if h == _INVALID_HANDLE or h == 0:
        raise PipeError(
            f"CreateNamedPipeW({name!r}) failed: error {ctypes.get_last_error()}"
        )
    return h


def connect_pipe(h: int) -> None:
    """Block until the DLL connects to the pipe.

    Raises :exc:`PipeError` on failure.
    """
    ok = _k32.ConnectNamedPipe(h, None)
    err = ctypes.get_last_error()
    if not ok and err != 535:  # 535 = ERROR_PIPE_CONNECTED (already connected)
        raise PipeError(f"ConnectNamedPipe failed: error {err}")


def write_pipe(h: int, data: bytes) -> None:
    """Write all bytes of *data* to the pipe."""
    offset = 0
    while offset < len(data):
        written = wt.DWORD(0)
        ok = _k32.WriteFile(
            h,
            ctypes.cast(
                ctypes.c_char_p(data[offset:]),
                ctypes.c_void_p,
            ),
            len(data) - offset,
            ctypes.byref(written),
            None,
        )
        if not ok or written.value == 0:
            raise PipeError(f"WriteFile failed: error {ctypes.get_last_error()}")
        offset += written.value


def read_pipe_exact(h: int, n: int) -> bytes | None:
    """Read exactly *n* bytes from the pipe.

    Returns ``None`` if the pipe is closed / broken before all bytes arrive.
    """
    buf = bytearray(n)
    offset = 0
    while offset < n:
        chunk = (ctypes.c_char * (n - offset))()
        read = wt.DWORD(0)
        ok = _k32.ReadFile(h, chunk, n - offset, ctypes.byref(read), None)
        if not ok or read.value == 0:
            return None
        buf[offset: offset + read.value] = chunk[: read.value]
        offset += read.value
    return bytes(buf)


def close_pipe(h: int) -> None:
    """Disconnect and close a pipe handle."""
    _k32.DisconnectNamedPipe(h)
    _k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# Config / Result structs (Python ↔ C layout, must match hook_engine.c)
# ---------------------------------------------------------------------------

# struct Config (24 bytes, packed):
#   [1]  mode           0=search, 1=text-hook
#   [3]  _pad0
#   [4]  max_hooks
#   [8]  hook_address
#   [1]  arg_idx        0-3 register, 0xFF stack
#   [1]  deref          0=direct, 1=*(ptr+offset)
#   [2]  byte_offset
#   [2]  encoding       0=utf16le, 1=utf8
#   [2]  batch_size     MODE_SEARCH: hooks per batch (0 = all at once)
_CONFIG_FMT = "<B3xIQ2BHHH"
_CONFIG_SIZE = struct.calcsize(_CONFIG_FMT)  # must be 24

assert _CONFIG_SIZE == 24, f"Config struct size mismatch: {_CONFIG_SIZE}"

# struct ResultHdr (16 bytes):
#   [8]  hook_va
#   [4]  slot_i  (signed)
#   [2]  encoding
#   [2]  text_len
_RESULT_HDR_FMT  = "<QiHH"
_RESULT_HDR_SIZE = struct.calcsize(_RESULT_HDR_FMT)  # must be 16

assert _RESULT_HDR_SIZE == 16, f"ResultHdr struct size mismatch: {_RESULT_HDR_SIZE}"


def pack_search_config(max_hooks: int, batch_size: int = 0) -> bytes:
    """Pack a MODE_SEARCH Config.

    Parameters
    ----------
    max_hooks:
        Total hook capacity (pre-allocated for all batches).
    batch_size:
        Number of functions to hook in the first batch.  0 means hook all
        at once (original single-batch behaviour).
    """
    return struct.pack(_CONFIG_FMT,
                       0,          # mode = search
                       max_hooks,
                       0,          # hook_address unused
                       0, 0, 0, 0, # arg_idx, deref, byte_offset, encoding
                       batch_size,
                       )


def pack_hook_config(hook_address: int, arg_idx: int, deref: int,
                     byte_offset: int, encoding: int) -> bytes:
    """Pack a MODE_HOOK Config."""
    return struct.pack(_CONFIG_FMT,
                       1,           # mode = hook
                       0,           # max_hooks unused
                       hook_address,
                       arg_idx, deref, byte_offset, encoding,
                       0,           # batch_size unused in MODE_HOOK
                       )


def unpack_result_hdr(data: bytes) -> tuple[int, int, int, int]:
    """Unpack a ResultHdr into (hook_va, slot_i, encoding, text_len)."""
    return struct.unpack(_RESULT_HDR_FMT, data)


# CMD_DISABLE:   uint8_t(1) + uint32_t(count) + count × uint64_t(va)
# CMD_SCAN_NEXT: uint8_t(2) + uint32_t(batch_size)
_CMD_DISABLE:   int = 1
_CMD_SCAN_NEXT: int = 2


def pack_disable_command(vas: list[int]) -> bytes:
    """Pack a CMD_DISABLE command to send to the DLL mid-session.

    Instructs the DLL to immediately disable (and queue-remove) the hooks
    at each of the given virtual addresses.

    Parameters
    ----------
    vas:
        List of absolute virtual addresses to disable.
    """
    return struct.pack("<BI", _CMD_DISABLE, len(vas)) + struct.pack(f"<{len(vas)}Q", *vas)


def pack_scan_next_command(batch_size: int) -> bytes:
    """Pack a CMD_SCAN_NEXT command.

    Tells the DLL to hook the next *batch_size* functions from the current
    .pdata position and report a ``scan_done:N@pos`` control message.
    """
    return struct.pack("<BI", _CMD_SCAN_NEXT, batch_size)


# ---------------------------------------------------------------------------
# Module base lookup
# ---------------------------------------------------------------------------

_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.EnumProcessModules.restype  = wt.BOOL
_psapi.EnumProcessModules.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE),
                                       wt.DWORD, ctypes.POINTER(wt.DWORD)]
_psapi.GetModuleFileNameExW.restype  = wt.DWORD
_psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]


class _MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wt.DWORD),
        ("EntryPoint",  ctypes.c_void_p),
    ]


_psapi.GetModuleInformation.restype  = wt.BOOL
_psapi.GetModuleInformation.argtypes = [wt.HANDLE, wt.HMODULE,
                                         ctypes.POINTER(_MODULEINFO), wt.DWORD]

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ           = 0x0010


def get_module_base(pid: int, module_name: str) -> int | None:
    """Return the base address of *module_name* in process *pid*.

    *module_name* is matched case-insensitively against the filename part
    of the full module path (e.g. ``"ユニゾンコード.exe"``).

    Returns ``None`` if the module is not found or the process cannot be opened.
    """
    h = _k32.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid
    )
    if not h:
        return None
    try:
        needed = wt.DWORD(0)
        _psapi.EnumProcessModules(h, None, 0, ctypes.byref(needed))
        count = needed.value // ctypes.sizeof(wt.HMODULE)
        mods  = (wt.HMODULE * count)()
        if not _psapi.EnumProcessModules(h, mods, needed, ctypes.byref(needed)):
            return None

        buf = ctypes.create_unicode_buffer(260)
        mi  = _MODULEINFO()
        target = module_name.lower()

        for mod in mods:
            _psapi.GetModuleFileNameExW(h, mod, buf, 260)
            name = Path(buf.value).name.lower()
            if name == target:
                if _psapi.GetModuleInformation(
                    h, mod, ctypes.byref(mi), ctypes.sizeof(mi)
                ):
                    return mi.lpBaseOfDll
        return None
    finally:
        _k32.CloseHandle(h)

