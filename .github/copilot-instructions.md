# JustReadIt — Copilot Instructions

## Project Overview

Windows-only hover-translation tool for a Light.VN-based RPG game. Combines DXGI screen capture, Windows OCR, ReadProcessMemory text extraction, and pluggable translation backends to render context-aware translations as an overlay.

## Architecture

```
Mouse hover / Freeze hotkey
  → DXGI Desktop Duplication capture (src/capture.py)
    → Windows OCR small-area probe (src/ocr/windows_ocr.py)
      → no text → return to idle
      → text found → PhashCache lookup by OCR text (src/cache.py)
        → cache hit → show overlay
        → cache miss:
            → full-screen Windows OCR → range detection (src/ocr/range_detectors.py)
            → pick_needles(ocr_text) → MemoryScanner.scan(needle) (src/memory/)
            → Levenshtein best_match(ocr_text, scan_results) (src/correction.py)
              → match success → use memory text (cleaner)
              → match failure → fall back to OCR text
            → TranslationCache lookup (src/cache.py)
              → cache hit → show overlay
              → cache miss → translate (src/translators/) → cache → show overlay (src/overlay.py)
```

### Component Map

| Component | Path | Role |
|---|---|---|
| Controller | `src/controller.py` | `HoverController` (QObject) — central orchestrator. Poll loop on QThread: cursor tracking → settle detection → OCR probe → full pipeline → emit `translation_ready` / `freeze_triggered` signals |
| Target process | `src/target.py` | `GameTarget` frozen dataclass — PID, HWND, window/capture rects, dxcam output index. Immutable; `refresh()` returns a new instance |
| Screen capture | `src/capture.py` | DXGI Desktop Duplication via dxcam. Context-manager protocol. **Never** `BitBlt`/`PrintWindow` (black frames on DirectX) |
| OCR engine | `src/ocr/windows_ocr.py` | `WindowsOcr` — sole OCR engine (no manga-ocr). Upscale-then-downscale for small fonts; PIL ↔ WinRT bitmap bridge |
| Range detection | `src/ocr/range_detectors.py` | `RangeDetector` ABC + chain runner `run_detectors()`. Built-ins: `ParagraphDetector`, `TableRowDetector`, `SingleBoxDetector` |
| Memory scanner | `src/memory/scanner.py` | `MemoryScanner` — zero-intrusion `ReadProcessMemory` scanning. `ScanResult` frozen dataclass. `pick_needles()` extracts best CJK substrings from OCR text. Hot-region caching + encoding auto-learning (UTF-16LE / UTF-8 / Shift-JIS). Optional `mem_scan.dll` C accelerator |
| Memory Win32 | `src/memory/_win32.py` | `VirtualQueryEx` / `ReadProcessMemory` bindings. `PROCESS_VM_READ` only — read-only, zero intrusion |
| Memory search | `src/memory/_search.py` | `find_all_positions()` byte search. Python fallback (`bytes.find`) + optional `mem_scan.dll` via ctypes |
| Correction | `src/correction.py` | `best_match(ocr_text, candidates)` — Levenshtein cross-match (rapidfuzz) between OCR text and memory scan results. Returns best candidate or `None` (fall back to OCR) |
| Cache | `src/cache.py` | Two-level caching: `PhashCache` (in-memory, OCR-text-keyed; class name kept for backward compat) → `TranslationCache` (persistent SQLite `translations.db`, keyed by `(source_text, source_lang, target_lang)`) |
| Translation | `src/translators/` | `Translator` ABC in `base.py`. Three backends: `GoogleFreeTranslator`, `CloudTranslationTranslator`, `OpenAICompatTranslator`. Factory in `factory.py`. Error hierarchy: `TranslationError` → `AuthError`, `RateLimitError`, `NetworkError` |
| Translator installer | `src/translators/_installer.py` | `ensure_package()` — runtime auto-install of optional translator deps. Works in both venv and PyInstaller frozen mode |
| Knowledge base | `src/knowledge/` | `KnowledgeBase` — persistent SQLite with hybrid BM25 + vector retrieval (RRF). `OPENAI_TOOLS` + `execute_tool()` for LLM function-calling. Shared by `OpenAICompatTranslator` and MCP server |
| MCP server | `src/mcp_server.py` | FastMCP stdio server exposing `record_term`, `record_event`, `search_terms` tools. Entry: `python -m src.mcp_server [--db PATH]` |
| Paths | `src/paths.py` | `app_data_dir()`, `knowledge_db_path()`, `translations_db_path()` — all under `%APPDATA%\JustReadIt\`. No PySide6 dep, safe for headless imports |
| Config | `src/config.py` | `AppConfig` — typed wrapper over `QSettings`, INI at `%APPDATA%\JustReadIt\config.ini`. 14 settings covering OCR, pipeline, translator, overlay, and hover behaviour |
| Overlay | `src/overlay.py` | `TranslationOverlay` (QWidget) — semi-transparent popup for hover mode; full-window pixmap overlay for freeze mode. Signals: `hover_requested`, `freeze_dismissed` |
| Debug UI | `src/ui/` | PySide6 debug window + window picker. Launch: `main.py --debug` |

### Workflows

**Hover mode** (driven by `HoverController`): cursor poll (80 ms) → large movement (≥20 px) resets settle timer → after settle (500 ms) → crop ±70 px probe → `WindowsOcr.recognise()` → if text found → `PhashCache` lookup → if miss → full OCR + `run_detectors()` + `pick_needles()` + `MemoryScanner.scan()` + `best_match()` → `TranslationCache` lookup → if miss → translator → cache both levels → emit `translation_ready` → overlay popup.

**Freeze mode**: hotkey (default F9) edge-detect → DXGI capture → emit `freeze_triggered` → overlay shows frozen screenshot as topmost window → mouse move emits `hover_requested(x, y)` → controller runs probe at that point → right-click / Escape dismisses → `AllowSetForegroundWindow(pid)` returns focus to game.

## Code Style

### Every file starts with

```python
# This Source Code Form is subject to the terms of the Mozilla Public License…
```

followed by:

```python
from __future__ import annotations
```

### Naming

- Classes: `PascalCase` — `GameTarget`, `WindowsOcr`, `ParagraphDetector`
- Functions/methods: `snake_case` — `from_pid`, `grab_target`, `run_detectors`, `recognise` (British spelling)
- Private: single `_` prefix — `_pid_to_name`, `_ensure_dpi_aware`
- Constants: `_UPPER_SNAKE` — `_RETRY_INTERVAL_S`, `_BLACK_FRAME_THRESHOLD`
- Qt callbacks: `_on_<event>` — `_on_window_picked`, `_on_result`
- Qt overrides: use `# noqa: N802` to suppress pep8-naming on camelCase (`paintEvent`, `mouseMoveEvent`)

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

Three built-in backends:

| Class | Backend | Extras group |
|---|---|---|
| `GoogleFreeTranslator` | Free unofficial Google Translate (`deep-translator`) | `translators-free` |
| `CloudTranslationTranslator` | Google Cloud Translation API v2 | `translators-cloud` |
| `OpenAICompatTranslator` | Any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, Azure) | `translators-openai` |

`build_translator(config, *, progress, knowledge_base)` in `src/translators/factory.py` reads `AppConfig.translator_backend` and constructs the right subclass. Provider registry (`ProviderInfo` + `PROVIDERS` list) keeps factory, UI dropdowns, and config in sync.

### OpenAI translator context strategy

`OpenAICompatTranslator` uses RAG from `KnowledgeBase` (long-term persistent) + recent translation pairs (short-term volatile buffer). An `add_to_history: bool` flag on `translate()` prevents OCR garbage from polluting the context. LLM-driven KB tool-calling loop runs up to 8 rounds per request.

### Two-level caching

1. `PhashCache` — in-memory, session-scoped, keyed by exact OCR region text (string dict). Class name kept for backward compat — perceptual hashing was removed. Stores `(translation, mem_text, corrected_text)` so debug panels can replay a cache hit without re-running the pipeline.
2. `TranslationCache` — persistent SQLite (`translations.db`), keyed by `(source_text, source_lang, target_lang)`. Same source text → skip translation API.

### Value objects — frozen dataclasses

- `GameTarget` (`src/target.py`) — `pid`, `hwnd`, `hmonitor`, `window_rect`, `capture_rect`, `dxcam_output_idx`, `process_name`. Constructed only via `from_pid(pid)` or `from_name(name)` classmethods. `refresh()` returns a new instance.
- `Rect` (`src/target.py`) — `left, top, right, bottom: int`; properties `width`, `height`, `area`; `as_tuple()`.
- `BoundingBox` (`src/ocr/range_detectors.py`) — `x, y, w, h: int`, `text: str = ""`; properties `right`, `bottom`, `center_x`, `center_y`; `contains(px, py)`, `distance_to_point(px, py)`.
- `ScanResult` (`src/memory/scanner.py`) — `text`, `encoding`, `address`, `region_base`.
- `KnowledgeEntry` (`src/knowledge/knowledge_base.py`) — `kind`, `original`, `translation`, `category`, `description`, `score`.
- `ProviderInfo` (`src/translators/base.py`) — translator provider metadata for factory/UI sync.

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

`pick_needles(ocr_text)` (plural) extracts up to 3 CJK substrings (centre, start, end of longest contiguous run). Controller tries each in order, stopping at first scan hit.

`ScanResult` (frozen dataclass): `text`, `encoding`, `address`, `region_base`.

Optional C accelerator `mem_scan.dll` (`src/memory/mem_scan.c`) uses `memchr` + `memcmp` for ~5–15 GB/s throughput. Built via `src/memory/build.ps1` or VS Code task "Build mem_scan.dll".

`src/memory/__init__.py` exports `MemoryScanner`, `ScanResult`, `pick_needles`.

### Runtime auto-install of optional deps

Translator backends call `ensure_package(pip_name, import_name)` from `src/translators/_installer.py` in their `__init__`. Works in both dev (subprocess pip) and PyInstaller frozen executables (bundled pip API + installs to `%APPDATA%\JustReadIt\lib`).

### Resource management

`Capturer` implements `__enter__`/`__exit__`. `KnowledgeBase` and `TranslationCache` also support context-manager protocol. DXGI warm-up frames (near-zero pixel mean) are retried up to a deadline. Handle cleanup via `try/finally`.

### Error handling

Custom exceptions inherit `RuntimeError`, defined near the code that raises them. Messages must be **actionable and user-facing** (e.g. include install commands, suggest `--pid`). Use `warnings.warn(RuntimeWarning)` for recoverable fallbacks.

Key exceptions: `ProcessNotFoundError`, `WindowNotFoundError`, `AmbiguousProcessNameError` (in `target.py`), `MissingOcrLanguageError` (in `windows_ocr.py`), `TranslationError` → `AuthError`, `RateLimitError`, `NetworkError` (in `translators/base.py`).

### Async / Threading

- No project-wide async. Windows OCR WinRT async calls are bridged via `asyncio.run()` inside synchronous methods.
- Threading via Qt `QThread` + `moveToThread()` + signal/slot (no shared mutable state). `HoverController.setup()` / `teardown()` run on the worker thread.
- COM apartment: `_winrt.init_apartment(STA)` (idempotent).

### Configuration

`AppConfig` wraps `QSettings` (INI, `%APPDATA%\JustReadIt\config.ini`) — each setting is a `@property` with getter/setter, coercion, and default. Fresh `QSettings` handle per access via `_make_qsettings()`; `.sync()` after every write.

Key settings: `ocr_language`, `ocr_max_size` (1920 — caps image fed to OCR; halves 4K frames), `interval_ms` (1500), `settle_ms`, `memory_scan_enabled` (True), `translator_backend`, `translator_target_lang`, `cloud_api_key`, `openai_api_key`, `openai_model` (`gpt-4o-mini`), `openai_base_url`, `openai_system_prompt`, `openai_context_window` (10), `openai_summary_trigger` (20), `openai_tools_enabled` (True — disable for models that struggle with function-calling), `openai_disable_thinking` (True — prepends empty `<think></think>` prefill to suppress reasoning on local thinking models; **never enable on standard OpenAI endpoint**), `dump_vk` (`0x77` / F8), `freeze_vk` (`0x78` / F9).

### Persistent data paths

All data files live under `%APPDATA%\JustReadIt\` (`src/paths.py`):
- `config.ini` — app settings
- `knowledge.db` — game knowledge base (terms, events, FTS5)
- `translations.db` — translation cache

`src/paths.py` has no PySide6 dependency — safe to import from MCP server and headless scripts.

## Build & Test

```powershell
# Create venv and install (all extras)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,ui,translators-free,translators-cloud,translators-openai,knowledge]"

# Build optional memory scanner C accelerator
powershell -File src/memory/build.ps1
# Or run the "Build mem_scan.dll" VS Code build task

# Install Windows OCR Japanese language pack (admin, ~6 MB, no reboot)
powershell -ExecutionPolicy Bypass -File scripts\install_ja_ocr.ps1

# Run tests
pytest tests/

# Launch debug UI
python main.py --debug

# Launch hover mode
python main.py --pid <PID>   # or --name <process_name>

# Launch MCP server (for Claude Desktop / Cursor / Copilot)
python -m src.mcp_server [--db <path>]
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
- License: **MPL-2.0** (file-level weak copyleft). Every `.py` file starts with the MPL boilerplate comment.
- Windows-only; Python ≥ 3.11.
- OCR: Windows OCR only — no GPU dependency, no manga-ocr.
- Focus return: `AllowSetForegroundWindow(pid)` — direct cross-process `SetForegroundWindow` is blocked by Windows.
- `PhashCache` key: exact OCR region text string (no longer perceptual hash — class name preserved for backward compat).

## Reference

Full design rationale and TODO list: [`doc/story.md`](../doc/story.md)

# Language
优先使用中文回复，英文也可以接受
