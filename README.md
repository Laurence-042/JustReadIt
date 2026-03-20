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
- **冻结模式** — 快捷键将当前画面冻结为置顶悬浮截图，可随意悬停翻译；右键关闭并将焦点归还游戏
- **可扩展翻译后端** — 干净的 `Translator` 抽象基类；已实现：免费 Google、Cloud Translation API、OpenAI 兼容接口（RAG + 函数调用，基于 `KnowledgeBase`）
- **双层翻译缓存** — 感知哈希（phash）内存去重 + SQLite 持久化文本缓存（以源文本为键），跨会话避免重复翻译请求
- **MCP 知识库** — 游戏专属术语词典与剧情事件日志，进程内翻译器与外部 MCP 客户端（Claude Desktop、Cursor 等）共享同一数据库
- **PySide6 调试窗口** — 截图预览、OCR 边框叠层、内存扫描结果面板

---

## 架构

```
鼠标移动 / 冻结快捷键
  -> DXGI Desktop Duplication 截图 (src/capture.py)
    -> Windows OCR 小区域探测 (src/ocr/windows_ocr.py)
      -> 无文字 -> 返回空闲
      -> 有文字 -> 对翻译区域计算 phash (src/cache.py)
        -> 缓存命中 -> 显示悬浮窗
        -> 缓存未命中:
            -> 全屏 OCR -> 范围检测 (src/ocr/range_detectors.py)
            -> 文本缓存查询 (src/cache.py TranslationCache)
              -> 缓存命中 -> 显示悬浮窗
              -> 缓存未命中:
                  -> pick_needle(ocr_text) -> MemoryScanner.scan(needle) (src/memory/)
                  -> Levenshtein 交叉匹配 (src/correction.py)
                    -> 匹配成功 -> 使用内存扫描得到的干净文本
                    -> 匹配失败 -> 回退到 OCR 文本
                  -> 翻译 (src/translators/) -> 写入缓存 -> 显示悬浮窗 (src/overlay.py)
```

### 模块一览

| 模块 | 路径 | 作用 |
|---|---|---|
| 目标进程 | `src/target.py` | `GameTarget` 冻结数据类 — PID、HWND、窗口/捕获矩形、dxcam 输出索引 |
| 屏幕截图 | `src/capture.py` | 基于 dxcam 的 DXGI Desktop Duplication |
| OCR 引擎 | `src/ocr/windows_ocr.py` | `WindowsOcr` — 小字体超分后缩放；PIL ↔ WinRT 位图桥接 |
| 范围检测 | `src/ocr/range_detectors.py` | `RangeDetector` 抽象基类 + `run_detectors()` 责任链 |
| 内存扫描 | `src/memory/scanner.py` | `MemoryScanner` — 零注入 `ReadProcessMemory`；热区缓存；编码自学习（UTF-16LE / UTF-8 / Shift-JIS）；可选 `mem_scan.dll` C 加速器 |
| 内存 Win32 | `src/memory/_win32.py` | `VirtualQueryEx` / `ReadProcessMemory` ctypes 绑定 — 仅 `PROCESS_VM_READ` |
| 结果校正 | `src/correction.py` | OCR 与内存扫描结果之间的 Levenshtein 交叉匹配（rapidfuzz） |
| 缓存 | `src/cache.py` | `PhashCache` — 感知哈希内存去重；`TranslationCache` — `(源文本, 源语言, 目标语言)` 为键的 SQLite 持久化缓存 |
| 翻译后端 | `src/translators/` | `Translator` 抽象基类；已实现：免费 Google、Cloud Translation API、OpenAI 兼容（RAG + 函数调用，依托 `KnowledgeBase`） |
| 知识库 | `src/knowledge/` | BM25 + 向量混合（RRF）检索；`record_term`、`record_event`、`search` — 进程内翻译器与 MCP 服务器共享 |
| MCP 服务器 | `src/mcp_server.py` | stdio MCP 服务器（`FastMCP`）；向 Claude Desktop、Cursor 等暴露同样的 3 个工具 |
| 配置 | `src/config.py` | `AppConfig` — 类型化 `QSettings` 封装，INI 位于 `%APPDATA%\JustReadIt\config.ini` |
| 调试 UI | `src/ui/` | PySide6 调试窗口 + 窗口选择器 |
| 悬浮窗 | `src/overlay.py` | 置顶透明悬浮窗，冻结模式 |

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

# 安装核心依赖
pip install -e .

# 安装 UI 扩展（PySide6，用于调试窗口）
pip install -e ".[ui]"

# 安装开发工具（pytest、类型存根等）
pip install -e ".[dev]"
```

---

## 使用方法

```powershell
# 启动 PySide6 调试 / 测试窗口
python main.py --debug

# 无界面模式（尚未实现）
python main.py
```

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
- **翻译后端**（`src/translators/`）：实现 `Translator` 抽象基类。

所有文件均以 `from __future__ import annotations` 开头。类型标注全面使用 PEP 604（`X | None`）和 PEP 585 泛型。完整代码风格指南见 `.github/copilot-instructions.md`。

---

## 许可证

本源代码受 **Mozilla Public License, v. 2.0** 约束。
如未随本文件收到 MPL 副本，可从 https://mozilla.org/MPL/2.0/ 获取。

完整许可证文本见 [`LICENSE`](LICENSE)。