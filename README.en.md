# JustReadIt

> Windows-only hover-translation tool for Light.VN-based RPG games.

[中文版](README.md) | English

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-blue.svg)]()

JustReadIt is a real-time translation overlay for Windows. Move your mouse over in-game text and a transparent overlay instantly shows you the translation — no copy-paste, no alt-tabbing.

---

## Features

- **DXGI Desktop Duplication capture** — works on DirectX games where `BitBlt`/`PrintWindow` produce black frames
- **Windows OCR** — built-in, no external model downloads required; upscales small fonts for accuracy
- **ReadProcessMemory memory scan** — zero-injection scan of the game process heap; extracts clean original CJK text in ~10–50 ms with hot-region caching; cross-validates with OCR via Levenshtein matching
- **Freeze mode** — hotkey (default F9) freezes a screenshot as a topmost overlay so you can hover-translate at your own pace; right-click or Escape to dismiss and return focus to the game
- **Pluggable translation backends** — clean `Translator` ABC; free Google, Cloud Translation API, and OpenAI-compatible (RAG + function-calling via `KnowledgeBase`) all implemented
- **Two-layer translation cache** — in-session in-memory dedup keyed by OCR text + persistent SQLite text cache keyed by source text, avoiding redundant translation calls across sessions
- **MCP Knowledge Base** — game-specific term dictionary and event log shared between the in-process translator and external MCP clients (Claude Desktop, Cursor, etc.)
- **System tray + compact main window** — minimises to the system tray while the pipeline keeps running; the compact window handles game-window selection, language switching, and translation preview
- **PySide6 debug window** — capture preview, OCR bounding-box overlay, memory-scan output panel, per-stage pipeline timings

---

## Architecture

```
Mouse hover / Freeze hotkey
  → DXGI Desktop Duplication capture (src/capture.py)
    → Windows OCR small-area probe (src/ocr/windows_ocr.py)
      → no text → return to idle
      → text found → PhashCache lookup by OCR text (src/cache.py)
        → hit → show overlay
        → miss:
            → full-screen OCR → range detection (src/ocr/range_detectors.py)
            → pick_needles(ocr_text) → MemoryScanner.scan(needle) (src/memory/)
            → Levenshtein cross-match (src/correction.py)
              → match success → use clean memory-scan text
              → match failure → fall back to OCR text
            → TranslationCache lookup (src/cache.py)
              → hit → show overlay
              → miss → translate (src/translators/) → cache → show overlay (src/overlay.py)
```

### Component map

| Component | Path | Role |
|---|---|---|
| App backend | `src/app_backend.py` | `AppBackend` — sole owner of all stateful resources; holds controller, knowledge base, translator, and overlays; forwards signals to views |
| Controller | `src/controller.py` | `HoverController` (QObject) — background worker thread; cursor poll → settle detection → OCR probe → full pipeline |
| Target process | `src/target.py` | `GameTarget` frozen dataclass — PID, HWND, window/capture rects, dxcam output index |
| Screen capture | `src/capture.py` | DXGI Desktop Duplication via dxcam |
| OCR engine | `src/ocr/windows_ocr.py` | `WindowsOcr` — upscale-then-downscale for small fonts; PIL ↔ WinRT bitmap bridge |
| Range detection | `src/ocr/range_detectors.py` | `RangeDetector` ABC + `run_detectors()` chain runner |
| Memory scanner | `src/memory/scanner.py` | `MemoryScanner` — zero-injection `ReadProcessMemory`; hot-region cache; encoding auto-learning (UTF-16LE / UTF-8 / Shift-JIS); optional `mem_scan.dll` C accelerator |
| Memory Win32 | `src/memory/_win32.py` | `VirtualQueryEx` / `ReadProcessMemory` ctypes bindings — `PROCESS_VM_READ` only |
| Correction | `src/correction.py` | Levenshtein cross-match (rapidfuzz) between OCR and memory-scan results |
| Cache | `src/cache.py` | `PhashCache` — in-session in-memory dedup (OCR text key); `TranslationCache` — persistent SQLite cache keyed on `(source_text, source_lang, target_lang)` |
| Translation | `src/translators/` | `Translator` ABC; implemented: free Google, Cloud Translation API, OpenAI-compatible |
| Knowledge base | `src/knowledge/` | Hybrid BM25 + vector (RRF) retrieval; `record_term`, `record_event`, `search_terms` — shared between in-process translator and MCP server |
| MCP server | `src/mcp_server.py` | stdio MCP server (`FastMCP`); exposes the same 3 tools to Claude Desktop, Cursor, etc. |
| Config | `src/config.py` | `AppConfig` singleton — reactive typed `QSettings` wrapper; INI at `%APPDATA%\JustReadIt\config.ini` |
| Main window | `src/ui/main_window.py` | `MainWindow` — compact user interface; game-window picker, language switcher, translation panel, system tray support |
| Debug window | `src/ui/debug_window.py` | `DebugWindow` — full pipeline debug view; capture preview, OCR overlay, memory-scan panel |
| Overlay | `src/overlay.py` | `TranslationOverlay` (hover mode) + `FreezeOverlay` (freeze mode) |

---

## Requirements

- **Windows 10 / 11** (Windows OCR and DXGI Desktop Duplication are Windows-only APIs)
- **Python 3.11+**
- Primary target is **Light.VN-based** titles; the memory scanner is general-purpose and works with any game process readable via `ReadProcessMemory`

---

## Installation

```powershell
# Clone the repository
git clone https://github.com/Laurence-042/JustReadIt.git
cd JustReadIt

# Create a virtual environment and install all extras
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[ui,dev,translators-free,translators-cloud,translators-openai,knowledge]"

# Install the Windows OCR Japanese language pack (~6 MB, no reboot needed)
powershell -ExecutionPolicy Bypass -File scripts\install_ja_ocr.ps1

# Build the optional C accelerator (mem_scan.dll)
powershell -File src\memory\build.ps1
```

---

## Usage

```powershell
# Launch the compact user window (default)
python main.py

# Launch the full pipeline debug window
python main.py --debug
```

**Basic workflow:**

1. Click **"Pick Game Window"** to attach to the running game.
2. The translation pipeline starts automatically; results appear in the floating overlay and the main window.
3. Press the Freeze hotkey (default **F9**) to enter screenshot-inspection mode; right-click or Escape to dismiss.
4. Minimise the main window → it collapses to the system tray while the pipeline keeps running.
5. Click **"Debug view"** inside the main window to open the full debug panel.

---

## Configuration

All settings are stored in `%APPDATA%\JustReadIt\config.ini` and can be changed from the Settings panel in either window. Key settings:

| Key | Default | Description |
|---|---|---|
| `ocr/language` | `ja` | OCR recognition language (BCP-47) |
| `ocr/max_size` | `1920` | Maximum long-edge (px) fed to OCR; 4K frames are downsampled |
| `pipeline/interval_ms` | `1500` | Translation cooldown (ms) |
| `pipeline/memory_scan_enabled` | `true` | Enable memory scan for clean source text |
| `translator/backend` | — | Translation backend: `google_free` / `cloud` / `openai` |
| `translator/target_lang` | — | Target language (BCP-47) |
| `hotkey/freeze_vk` | `0x78` (F9) | Freeze-mode hotkey virtual key code |
| `hotkey/dump_vk` | `0x77` (F8) | Debug-dump hotkey virtual key code |

---

## MCP Knowledge Base

The game knowledge base (`%APPDATA%\JustReadIt\knowledge.db`) is exposed as a stdio [Model Context Protocol](https://modelcontextprotocol.io/) server so external MCP clients — Claude Desktop, Cursor, VS Code, etc. — can browse or populate the knowledge base directly.

**Install the MCP dependency**

```powershell
pip install -e ".[knowledge]"
```

**Start the server manually** (for testing)

```powershell
python -m src.mcp_server
# Override database path
python -m src.mcp_server --db C:\path\to\my_game.db
```

**Configure Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "justreadit": {
      "command": "python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "C:/Users/<you>/Documents/workspace/python/JustReadIt"
    }
  }
}
```

**Available tools**

| Tool | Description |
|---|---|
| `record_term` | Save a character name, location, item, or vocabulary term with its translation |
| `record_event` | Append a 2–4 sentence story-event summary |
| `search_terms` | Hybrid BM25 + vector search across all stored terms and events |

Knowledge written here is immediately visible to the in-process OpenAI-compatible translator (and vice-versa), because both share the same SQLite file.

---

## Development

```powershell
# Run the test suite
pytest
```

The codebase follows a **chain-of-responsibility** pattern for extensible rules:

- **Range detectors** (`src/ocr/range_detectors.py`): implement `RangeDetector` and append to `DEFAULT_DETECTORS`.
- **Translation backends** (`src/translators/`): implement the `Translator` ABC and register in `src/translators/factory.py`.

All files begin with the MPL-2.0 copyright notice and `from __future__ import annotations`. Types are pervasive (PEP 604 `X | None`, PEP 585 generics). See `.github/copilot-instructions.md` for full code-style guidelines.

---

## License

This Source Code Form is subject to the terms of the **Mozilla Public License, v. 2.0**.
If a copy of the MPL was not distributed with this file, you can obtain one at https://mozilla.org/MPL/2.0/.

See [`LICENSE`](LICENSE) for the full license text.
