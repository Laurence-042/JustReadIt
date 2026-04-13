# JustReadIt

> 专为 Light.VN 引擎 RPG 游戏设计的 Windows 悬停翻译工具。

中文 | [English](README.en.md)

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-blue.svg)]()

JustReadIt 是一个 Windows 实时翻译悬浮窗工具。将鼠标移到游戏文字上，透明悬浮窗即时显示翻译结果——无需复制粘贴，无需切换窗口。

---

## 功能特性

- **DXGI Desktop Duplication 截图** — 专为 DirectX 游戏设计，彻底避免 `BitBlt`/`PrintWindow` 截黑屏的问题
- **Windows OCR** — 使用系统内置 OCR，无需下载外部模型；对小字体自动超分辨率提升识别精度
- **ReadProcessMemory 内存扫描** — 零注入读取游戏进程堆内存，含热区缓存约 10–50 ms 提取原始 CJK 文本，再与 OCR 结果做 Levenshtein 交叉验证
- **冻结模式** — 快捷键（默认 F9）将当前画面冻结为置顶悬浮截图，可随意悬停翻译；右键 / Escape 关闭并将焦点归还游戏
- **可扩展翻译后端** — 干净的 `Translator` 抽象基类；已实现：免费 Google、Cloud Translation API、OpenAI 兼容接口（RAG + 函数调用，基于 `KnowledgeBase`）
- **双层翻译缓存** — 会话内内存去重（以 OCR 文本为键）+ SQLite 持久化文本缓存（以源文本为键），跨会话避免重复翻译请求
- **MCP 知识库** — 游戏专属术语词典与剧情事件日志，进程内翻译器与外部 MCP 客户端（Claude Desktop、Cursor 等）共享同一数据库
- **系统托盘 + 紧凑主窗口** — 最小化到系统托盘后流水线持续运行；简洁主窗口支持游戏窗口选择、语言切换与翻译预览
- **PySide6 调试窗口** — 截图预览、OCR 边框叠层、内存扫描结果面板、流水线各阶段耗时

---

## 架构

```
鼠标移动 / 冻结快捷键
  → DXGI Desktop Duplication 截图 (src/capture.py)
    → Windows OCR 小区域探测 (src/ocr/windows_ocr.py)
      → 无文字 → 返回空闲
      → 有文字 → PhashCache 以 OCR 文本为键查询 (src/cache.py)
        → 命中 → 显示悬浮窗
        → 未命中:
            → 全屏 OCR → 范围检测 (src/ocr/range_detectors.py)
            → pick_needles(ocr_text) → MemoryScanner.scan(needle) (src/memory/)
            → Levenshtein 交叉匹配 (src/correction.py)
              → 匹配成功 → 使用内存扫描得到的干净文本
              → 匹配失败 → 回退到 OCR 文本
            → TranslationCache 查询 (src/cache.py)
              → 命中 → 显示悬浮窗
              → 未命中 → 翻译 (src/translators/) → 写入缓存 → 显示悬浮窗 (src/overlay.py)
```

### 模块一览

| 模块 | 路径 | 作用 |
|---|---|---|
| 应用后端 | `src/app_backend.py` | `AppBackend` — 所有有状态资源的唯一所有者；持有控制器、知识库、翻译器和悬浮窗；向视图转发信号 |
| 控制器 | `src/controller.py` | `HoverController`（QObject）— 后台工作线程；光标轮询 → 稳定检测 → OCR 探测 → 完整流水线 |
| 目标进程 | `src/target.py` | `GameTarget` 冻结数据类 — PID、HWND、窗口/捕获矩形、dxcam 输出索引 |
| 屏幕截图 | `src/capture.py` | 基于 dxcam 的 DXGI Desktop Duplication |
| OCR 引擎 | `src/ocr/windows_ocr.py` | `WindowsOcr` — 小字体超分后缩放；PIL ↔ WinRT 位图桥接 |
| 范围检测 | `src/ocr/range_detectors.py` | `RangeDetector` 抽象基类 + `run_detectors()` 责任链 |
| 内存扫描 | `src/memory/scanner.py` | `MemoryScanner` — 零注入 `ReadProcessMemory`；热区缓存；编码自学习（UTF-16LE / UTF-8 / Shift-JIS）；可选 `mem_scan.dll` C 加速器 |
| 内存 Win32 | `src/memory/_win32.py` | `VirtualQueryEx` / `ReadProcessMemory` ctypes 绑定 — 仅 `PROCESS_VM_READ` |
| 结果校正 | `src/correction.py` | OCR 与内存扫描结果之间的 Levenshtein 交叉匹配（rapidfuzz） |
| 缓存 | `src/cache.py` | `PhashCache` — 会话内内存去重（OCR 文本为键）；`TranslationCache` — `(源文本, 源语言, 目标语言)` 为键的 SQLite 持久化缓存 |
| 翻译后端 | `src/translators/` | `Translator` 抽象基类；已实现：免费 Google、Cloud Translation API、OpenAI 兼容 |
| 知识库 | `src/knowledge/` | BM25 + 向量混合（RRF）检索；`record_term`、`record_event`、`search_terms` — 进程内翻译器与 MCP 服务器共享 |
| MCP 服务器 | `src/mcp_server.py` | stdio MCP 服务器（`FastMCP`）；向 Claude Desktop、Cursor 等暴露同样的 3 个工具 |
| 配置 | `src/config.py` | `AppConfig` 单例 — 类型化响应式 `QSettings` 封装，INI 位于 `%APPDATA%\JustReadIt\config.ini` |
| 主窗口 | `src/ui/main_window.py` | `MainWindow` — 紧凑用户界面；游戏窗口选择、语言切换、翻译预览、系统托盘支持 |
| 调试窗口 | `src/ui/debug_window.py` | `DebugWindow` — 完整流水线调试视图；截图预览、OCR 叠层、内存扫描面板 |
| 悬浮窗 | `src/overlay.py` | `TranslationOverlay`（悬停模式）+ `FreezeOverlay`（冻结模式） |

---

## 环境要求

- **Windows 10 / 11**（Windows OCR 与 DXGI Desktop Duplication 均为 Windows 专属 API）
- **Python 3.11+**
- 主要针对 **Light.VN** 引擎游戏；内存扫描器为通用实现，可用于任何支持 `ReadProcessMemory` 的游戏进程

---

## 安装

```powershell
# 克隆仓库
git clone https://github.com/Laurence-042/JustReadIt.git
cd JustReadIt

# 创建虚拟环境并安装所有扩展
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[ui,dev,translators-free,translators-cloud,translators-openai,knowledge]"

# 安装 Windows OCR 日语语言包（约 6 MB，无需重启）
powershell -ExecutionPolicy Bypass -File scripts\install_ja_ocr.ps1

# 构建可选 C 加速器（mem_scan.dll）
powershell -File src\memory\build.ps1
```

---

## 使用方法

```powershell
# 启动用户窗口（默认）
python main.py

# 启动完整调试窗口
python main.py --debug
```

**基本流程：**

1. 点击 **"选择游戏窗口"** 附加到正在运行的游戏。
2. 翻译流水线自动启动；翻译结果显示在悬浮窗和主窗口中。
3. 按冻结快捷键（默认 **F9**）进入截图检视模式；右键或 Escape 关闭。
4. 最小化主窗口 → 缩小到系统托盘，流水线保持运行。
5. 点击 **"调试视图"** 按钮可打开完整的调试面板。

---

## 配置说明

所有配置存储于 `%APPDATA%\JustReadIt\config.ini`，可通过主窗口或调试窗口的设置面板修改。主要配置项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ocr/language` | `ja` | OCR 识别语言（BCP-47） |
| `ocr/max_size` | `1920` | OCR 图像长边上限（px）；4K 帧自动降采样 |
| `pipeline/interval_ms` | `1500` | 翻译冷却间隔（ms） |
| `pipeline/memory_scan_enabled` | `true` | 是否启用内存扫描以获取干净文本 |
| `translator/backend` | — | 翻译后端：`google_free` / `cloud` / `openai` |
| `translator/target_lang` | — | 目标语言（BCP-47） |
| `hotkey/freeze_vk` | `0x78`（F9） | 冻结模式快捷键虚拟键码 |
| `hotkey/dump_vk` | `0x77`（F8） | 调试转储快捷键虚拟键码 |

---

## MCP 知识库

游戏知识库（`%APPDATA%\JustReadIt\knowledge.db`）通过 stdio [Model Context Protocol](https://modelcontextprotocol.io/) 服务器对外暴露，外部 MCP 客户端（Claude Desktop、Cursor、VS Code 等）可直接查询或写入知识库。

**安装 MCP 依赖**

```powershell
pip install -e ".[knowledge]"
```

**手动启动服务器**（用于测试）

```powershell
python -m src.mcp_server
# 指定自定义数据库路径
python -m src.mcp_server --db C:\path\to\my_game.db
```

**配置 Claude Desktop**（`%APPDATA%\Claude\claude_desktop_config.json`）

```json
{
  "mcpServers": {
    "justreadit": {
      "command": "python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "C:/Users/<你的用户名>/Documents/workspace/python/JustReadIt"
    }
  }
}
```

**可用工具**

| 工具 | 说明 |
|---|---|
| `record_term` | 保存角色名、地点、道具或词汇术语及其译文 |
| `record_event` | 追加 2–4 句剧情事件摘要 |
| `search_terms` | 对所有已存储术语和事件执行 BM25 + 向量混合检索 |

此处写入的知识对进程内 OpenAI 兼容翻译器立即生效（反之亦然），因为两者共享同一个 SQLite 文件。

---

## 开发

```powershell
# 运行测试套件
pytest
```

代码库遵循**责任链**模式实现可扩展规则：

- **范围检测器**（`src/ocr/range_detectors.py`）：实现 `RangeDetector` 并追加到 `DEFAULT_DETECTORS`。
- **翻译后端**（`src/translators/`）：实现 `Translator` 抽象基类并在 `src/translators/factory.py` 中注册。

所有文件均以 MPL-2.0 版权声明和 `from __future__ import annotations` 开头。类型标注全面使用 PEP 604（`X | None`）和 PEP 585 泛型。完整代码风格指南见 `.github/copilot-instructions.md`。

---

## 许可证

本源代码受 **Mozilla Public License, v. 2.0** 约束。
如未随本文件收到 MPL 副本，可从 https://mozilla.org/MPL/2.0/ 获取。

完整许可证文本见 [`LICENSE`](LICENSE).
