# JustReadIt — Copilot Instructions

## Project Overview

Windows-only hover-translation tool for a Light.VN-based RPG game. Combines DXGI screen capture, Windows OCR, ReadProcessMemory text extraction, and pluggable translation backends to render context-aware translations as an overlay.

## Architecture

```
Mouse hover / Freeze hotkey
  → DXGI Desktop Duplication capture (src/capture.py)
    → Windows OCR small-area probe (src/ocr/windows_ocr.py)
      → no text → return to idle
      → text found → phash of translation region (src/cache.py)
        → cache hit → show overlay
        → cache miss:
            → full-screen Windows OCR → range detection (src/ocr/range_detectors.py)
            → pick_needle(ocr_text) → MemoryScanner.scan(needle) (src/memory/)
            → Levenshtein best_match(ocr_text, scan_results) (src/correction.py)
              → match success → use memory text (cleaner)
              → match failure → fall back to OCR text
            → translate (src/translators/) → cache → show overlay (src/overlay.py)
```

### Component Map

| Component | Path | Role |
|---|---|---|
| Target process | `src/target.py` | `GameTarget` frozen dataclass — PID, HWND, window/capture rects, dxcam output index. Immutable; `refresh()` returns a new instance |
| Screen capture | `src/capture.py` | DXGI Desktop Duplication via dxcam. Context-manager protocol. **Never** `BitBlt`/`PrintWindow` (black frames on DirectX) |
| OCR engine | `src/ocr/windows_ocr.py` | `WindowsOcr` — sole OCR engine (no manga-ocr). Upscale-then-downscale for small fonts; PIL ↔ WinRT bitmap bridge |
| Range detection | `src/ocr/range_detectors.py` | `RangeDetector` ABC + chain runner `run_detectors()`. Built-ins: `ParagraphDetector`, `TableRowDetector`, `SingleBoxDetector` |
| Memory scanner | `src/memory/scanner.py` | `MemoryScanner` — zero-intrusion `ReadProcessMemory` scanning. `ScanResult` frozen dataclass. `pick_needle()` extracts best CJK substring from OCR text. Hot-region caching + encoding auto-learning (UTF-16LE / UTF-8 / Shift-JIS). Optional `mem_scan.dll` C accelerator. |
| Memory Win32 | `src/memory/_win32.py` | `VirtualQueryEx` / `ReadProcessMemory` bindings. `PROCESS_VM_READ` only — read-only, zero intrusion. |
| Memory search | `src/memory/_search.py` | `find_all_positions()` byte search. Python fallback (`bytes.find`) + optional `mem_scan.dll` via ctypes. |
| Correction | `src/correction.py` | `best_match(ocr_text, candidates)` — Levenshtein cross-match (rapidfuzz) between OCR text and memory scan results. Returns best candidate or `None` (fall back to OCR). |
| Cache | `src/cache.py` | phash of translated-region screenshot as key — **stub** (`# TODO: implement PhashCache`) |
| Translation | `src/translators/` | `Translator` ABC in `base.py`. Planned: Cloud Translation API + OpenAI (with rolling summary agent) |
| Config | `src/config.py` | `AppConfig` — typed wrapper over `QSettings`, INI at `%APPDATA%\JustReadIt\config.ini` |
| Debug UI | `src/ui/` | PySide6 debug window + window picker. Launch: `main.py --debug` |
| Overlay | `src/overlay.py` | Topmost transparent window, handles Freeze mode — **stub** (`# TODO: implement TranslationOverlay`) |

### Workflows

**Hover mode**: mouse large-movement → settle → small-area OCR probe → if text, phash lookup → if miss, full OCR + memory scan + Levenshtein match + translate + cache → show overlay.

**Freeze mode**: hotkey → DXGI capture → display frozen screenshot as topmost overlay (holds focus) → user hovers overlay for translation → right-click dismisses → `AllowSetForegroundWindow(pid)` returns focus to game.

## Code Style

### Every file starts with

```python
from __future__ import annotations
```

### Naming

- Classes: `PascalCase` — `GameTarget`, `WindowsOcr`, `ParagraphDetector`
- Functions/methods: `snake_case` — `from_pid`, `grab_target`, `run_detectors`, `recognise` (British spelling)
- Private: single `_` prefix — `_pid_to_name`, `_ensure_dpi_aware`
- Constants: `_UPPER_SNAKE` — `_RETRY_INTERVAL_S`, `_BLACK_FRAME_THRESHOLD`
- Qt callbacks: `_on_<event>` — `_on_window_picked`, `_on_result`

### Type annotations

Pervasive. Use PEP 604 (`X | None`), PEP 585 generics (`list[int]`), `Sequence[T]` for read-only params. String-quoted forward refs for cross-module types.

### Imports

Standard library → third-party → local (PEP 8). Use `TYPE_CHECKING` guard for import-cycle-prone references. Relative imports within packages, absolute `src.*` from tests and `main.py`.

## Project Conventions

### Extensibility via rule chains (chain of responsibility)

Range detectors implement ABCs composed as ordered lists. A runner function walks the chain and returns the first non-`None` result. New rules are appended/inserted into the module-level default list.

```python
# src/ocr/range_detectors.py
class RangeDetector(ABC):
    def detect(self, boxes, cursor_x, cursor_y) -> list[BoundingBox] | None: ...

DEFAULT_DETECTORS: list[RangeDetector] = [ParagraphDetector(), TableRowDetector(), SingleBoxDetector()]

def run_detectors(detectors, boxes, x, y) -> list[BoundingBox]:
    # first non-None wins; SingleBoxDetector is the fallback
```

### Translation plugin interface

```python
# src/translators/base.py
class Translator(ABC):
    def translate(self, text: str, source_lang: str, target_lang: str) -> str: ...
```

Two planned built-ins: Cloud Translation API (short text) and OpenAI (dialogue/plot, with rolling summary agent + configurable system prompt).

### Value objects — frozen dataclasses

- `GameTarget` (`src/target.py`) — `pid`, `hwnd`, `hmonitor`, `window_rect`, `capture_rect`, `dxcam_output_idx`, `process_name`. Constructed only via `from_pid(pid)` or `from_name(name)` classmethods. `refresh()` returns a new instance.
- `Rect` (`src/target.py`) — `left, top, right, bottom: int`; properties `width`, `height`, `area`; `as_tuple()`.
- `BoundingBox` (`src/ocr/range_detectors.py`) — `x, y, w, h: int`, `text: str = ""`; properties `right`, `bottom`, `center_x`, `center_y`; `contains(px, py)`, `distance_to_point(px, py)`.
- `ScanResult` (`src/memory/scanner.py`) — `text`, `encoding`, `address`, `region_base`.

All are decorated `@dataclass(frozen=True)`. Lazy DPI-awareness (`_ensure_dpi_aware()`) to avoid conflicts with Qt.

### Win32 API: ctypes only (core modules)

Use `ctypes.WinDLL` with `use_last_error=True`. Win32 structs as `ctypes.Structure`. Callbacks via `WINFUNCTYPE`. `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS=9)` preferred over `GetWindowRect`. The **only** exception is `src/ui/window_picker.py` which uses pywin32 for brevity.

### Memory scanner: MemoryScanner

`MemoryScanner` (`src/memory/scanner.py`) provides zero-intrusion text extraction:

1. Opens target process with `PROCESS_VM_READ | PROCESS_QUERY_INFORMATION` (read-only).
2. `VirtualQueryEx` enumerates committed readable regions.
3. `ReadProcessMemory` reads each region; `find_all_positions()` searches for the needle.
4. Extracts null-terminated strings around each hit; deduplicates by text.
5. Hot-region caching: remembers regions where CJK text was found; scans them first next time.
6. Encoding auto-learning: tries UTF-16LE → UTF-8 → Shift-JIS; after first hit, prioritises the successful encoding.

`pick_needle(ocr_text)` extracts the most distinctive CJK substring (longest contiguous run, pick from middle for OCR accuracy).

`ScanResult` (frozen dataclass): `text`, `encoding`, `address`, `region_base`.

Optional C accelerator `mem_scan.dll` (`src/memory/mem_scan.c`) uses `memchr` + `memcmp` for ~5–15 GB/s throughput. Built via `src/memory/build.ps1` or VS Code task "Build mem_scan.dll".

`src/memory/__init__.py` exports `MemoryScanner`, `ScanResult`, `pick_needle`.

### Resource management

`Capturer` implements `__enter__`/`__exit__`. DXGI warm-up frames (near-zero pixel mean) are retried up to a deadline. Handle cleanup via `try/finally`.

### Error handling

Custom exceptions inherit `RuntimeError`, defined near the code that raises them. Messages must be **actionable and user-facing** (e.g. include install commands, suggest `--pid`). Use `warnings.warn(RuntimeWarning)` for recoverable fallbacks.

Key exceptions: `ProcessNotFoundError`, `WindowNotFoundError`, `AmbiguousProcessNameError` (in `target.py`), `MissingOcrLanguageError` (in `windows_ocr.py`).

### Async / Threading

- No project-wide async. Windows OCR WinRT async calls are bridged via `asyncio.run()` inside synchronous methods.
- Threading via Qt `QThread` + `moveToThread()` + signal/slot (no shared mutable state).
- COM apartment: `_winrt.init_apartment(STA)` (idempotent).

### Configuration

`AppConfig` wraps `QSettings` (INI, `%APPDATA%\JustReadIt\config.ini`) — each setting is a `@property` with getter/setter, coercion, and default. Fresh `QSettings` handle per access via `_make_qsettings()`; `.sync()` after every write.

Current settings: `ocr_language: str = "ja"`, `interval_ms: int = 1500`.

### Stubs

Unimplemented modules (`cache.py`, `overlay.py`) contain **only** a module docstring + one `# TODO` comment. Do not add placeholder classes or `pass`-only methods.

## Build & Test

```powershell
# Create venv and install
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,ui]"

# Build optional memory scanner C accelerator
powershell -File src/memory/build.ps1
# Or run the "Build mem_scan.dll" VS Code build task

# Install Windows OCR Japanese language pack (admin, ~6 MB, no reboot)
powershell -ExecutionPolicy Bypass -File scripts\install_ja_ocr.ps1

# Run tests
pytest tests/

# Launch debug UI
python main.py --debug
```

`mem_scan.dll` at `src/memory/mem_scan.dll` is optional — the scanner falls back to Python `bytes.find()` if the DLL is absent.

### Testing conventions

- Framework: pytest (no unittest). Tests grouped into classes by feature; `setup_method(self)` for per-test setup.
- Hardware-dependent tests are **not yet written** — they are skipped by omission. When added, use `pytestmark = pytest.mark.skipif(...)` at module level.
- No mocking — tests run against real hardware or are skipped. Pure unit tests use synthetic data.
- Factory functions build synthetic layouts: `_make_paragraph_boxes(n_lines, n_words, ...)` in `test_ocr.py`.
- `pytest.approx()` for float geometry; `pytest.raises(ExcType, match=r"...")` for error assertions.
- White-box testing of internal helpers (e.g. `_group_into_lines`) is acceptable.

## Key Constraints

- Screen capture **must** use DXGI Desktop Duplication API — never `BitBlt`/`PrintWindow`.
- Text extraction: `ReadProcessMemory` scanning (`src/memory/`). Zero intrusion, read-only.
- License: **MPL-2.0** (file-level weak copyleft).
- Windows-only; Python ≥ 3.11.
- OCR: Windows OCR only — no GPU dependency, no manga-ocr.
- Focus return: `AllowSetForegroundWindow(pid)` — direct cross-process `SetForegroundWindow` is blocked by Windows.
- phash cache key: perceptual hash of the *translated region screenshot*, not the full screen.

## Reference

Full design rationale and TODO list: [`doc/story.md`](../doc/story.md)

# Language
优先使用中文回复，英文也可以接受
