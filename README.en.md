# JustReadIt

> Windows-only hover-translation tool for Light.VN-based RPG games.

[中文版](README.md) | English

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-blue.svg)]()

JustReadIt is a real-time translation overlay for Windows. Move your mouse over in-game text, and a transparent overlay instantly shows you the translation — no copy-paste, no alt-tabbing.

---

## Features

- **DXGI Desktop Duplication capture** — works on DirectX games where `BitBlt`/`PrintWindow` produce black frames
- **Windows OCR** — built-in, no external model downloads required; upscales small fonts for accuracy
- **ReadProcessMemory memory scan** — zero-injection scan of game process heap memory; extracts clean original CJK text in ~10–50 ms including hot-region cache; cross-validates with Windows OCR via Levenshtein matching
- **Freeze mode** — hotkey freezes a screenshot as a topmost overlay so you can hover-translate at your own pace; right-click to dismiss and return focus to the game
- **Pluggable translation backends** — clean `Translator` ABC; free Google, Cloud Translation API, and OpenAI-compatible (RAG + function-calling context via `KnowledgeBase`) all implemented
- **Two-layer translation cache** — phash of the translation region (fast in-memory dedup) + persistent SQLite text cache keyed on source text, avoiding redundant translation calls across sessions
- **MCP Knowledge Base** — game-specific term dictionary and event log shared between the in-process translator and external MCP clients (Claude Desktop, Cursor, etc.)
- **PySide6 debug window** — capture preview, OCR bounding-box overlay, memory scan output panel

---

## Architecture

```
Mouse hover / Freeze hotkey
  -> DXGI Desktop Duplication capture (src/capture.py)
    -> Windows OCR small-area probe (src/ocr/windows_ocr.py)
      -> no text -> return to idle
      -> text found -> phash of translation region (src/cache.py)
        -> cache hit -> show overlay
        -> cache miss:
            -> full-screen Windows OCR -> range detection (src/ocr/range_detectors.py)
            -> text cache lookup (src/cache.py TranslationCache)
              -> cache hit -> show overlay
              -> cache miss:
                  -> pick_needle(ocr_text) -> MemoryScanner.scan(needle) (src/memory/)
                  -> Levenshtein cross-match (src/correction.py)
                    -> match success -> use clean memory-scan text
                    -> match failure -> fall back to OCR text
                  -> translate (src/translators/) -> cache -> show overlay (src/overlay.py)
```

### Component map

| Component | Path | Role |
|---|---|---|
| Target process | `src/target.py` | `GameTarget` frozen dataclass — PID, HWND, window/capture rects, dxcam output index |
| Screen capture | `src/capture.py` | DXGI Desktop Duplication via dxcam |
| OCR engine | `src/ocr/windows_ocr.py` | `WindowsOcr` — upscale-then-downscale for small fonts; PIL ↔ WinRT bitmap bridge |
| Range detection | `src/ocr/range_detectors.py` | `RangeDetector` ABC + `run_detectors()` chain runner |
| Memory scanner | `src/memory/scanner.py` | `MemoryScanner` — zero-injection `ReadProcessMemory` scan; hot-region cache; encoding auto-learning (UTF-16LE / UTF-8 / Shift-JIS); optional `mem_scan.dll` C accelerator |
| Memory Win32 | `src/memory/_win32.py` | `VirtualQueryEx` / `ReadProcessMemory` ctypes bindings — `PROCESS_VM_READ` only |
| Correction | `src/correction.py` | Levenshtein cross-match (rapidfuzz) between OCR and memory-scan results |
| Cache | `src/cache.py` | `PhashCache` — perceptual-hash in-memory dedup; `TranslationCache` — persistent SQLite cache keyed on `(source_text, source_lang, target_lang)` |
| Translation | `src/translators/` | `Translator` ABC; implemented: free Google, Cloud Translation API, OpenAI-compatible (RAG + function-calling via `KnowledgeBase`) |
| Knowledge base | `src/knowledge/` | Hybrid BM25 + vector (RRF) knowledge store; `record_term`, `record_event`, `search` — shared between in-process translator and MCP server |
| MCP server | `src/mcp_server.py` | stdio MCP server (`FastMCP`); exposes same 3 tools to Claude Desktop, Cursor, etc. |
| Config | `src/config.py` | `AppConfig` — typed `QSettings` wrapper, INI at `%APPDATA%\JustReadIt\config.ini` |
| Debug UI | `src/ui/` | PySide6 debug window + window picker |
| Overlay | `src/overlay.py` | Topmost transparent overlay, Freeze mode |

---

## Requirements

- **Windows 10 / 11** (Windows OCR and DXGI Desktop Duplication are Windows-only APIs)
- **Python 3.11+**
- The primary target is **Light.VN-based** titles; the memory scanner is general-purpose and works with any game process readable via `ReadProcessMemory`

---

## Installation

```powershell
# Clone the repository
git clone https://github.com/Laurence-042/JustReadIt.git
cd JustReadIt

# Install core dependencies
pip install -e .

# Install UI extras (PySide6) for the debug window
pip install -e ".[ui]"

# Install dev tools (pytest, type stubs, etc.)
pip install -e ".[dev]"
```

---

## Usage

```powershell
# Launch the PySide6 debug / test window
python main.py --debug

# Headless mode (not yet implemented)
python main.py
```

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
| `record_term` | Save a character name, location, item or vocabulary term with its translation |
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
- **Translation backends** (`src/translators/`): implement the `Translator` ABC.

All files begin with `from __future__ import annotations`. Types are pervasive (PEP 604 `X | None`, PEP 585 generics). See `.github/copilot-instructions.md` for full code-style guidelines.

---

## License

This Source Code Form is subject to the terms of the **Mozilla Public License, v. 2.0**.
If a copy of the MPL was not distributed with this file, you can obtain one at https://mozilla.org/MPL/2.0/.

See [`LICENSE`](LICENSE) for the full license text.
