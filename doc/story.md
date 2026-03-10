# JustReadIt — 项目 Story

## 背景

目标游戏为某基于 Light.VN 引擎制作的 RPG 视觉小说，作者未对国际化作任何处理。Light.VN 并非为复杂 RPG 场景设计，主流翻译工具均存在明显缺陷：

| 工具 | 问题 |
|---|---|
| MTool / RenpyThief | 无法正常嵌入翻译 |
| LunaTranslator / Textractor | 基于 hook 文本 API，无法定位文字在屏幕上的位置 |
| WindowTranslator | OCR 精度不足，日语假名误识一字即可导致语义完全偏转；翻译选项冗杂，LLM 翻译模式无上下文 |

本项目目标：结合本地 OCR 与可插拔翻译后端，实现准确、低延迟、有上下文的悬停翻译。

---

## 启动参数

程序启动时必须指定**目标进程**（PID 或进程名，二选一）。所有模块共享同一个 `GameTarget` 对象，由它统一维护：

| 字段 | 类型 | 用途 |
|---|---|---|
| `pid` | `int` | Frida attach；`AllowSetForegroundWindow` |
| `hwnd` | `int` | `SetForegroundWindow` |
| `window_rect` | `Rect` | 窗口在虚拟屏幕坐标系中的位置（含负坐标），用于定位遮罩窗口 |
| `capture_rect` | `Rect` | `window_rect` 裁剪到所在 monitor 并转为 monitor 本地坐标，直接传给 dxcam `region=` |
| `dxcam_output_idx` | `int` | 窗口所在 monitor 对应的 dxcam output 索引，传给 `Capturer(output_idx=…)` |

`GameTarget` 在启动时由 `EnumWindows` 枚举属于目标 PID 的**主窗口**（可见、非子窗口、面积最大者）得到 HWND，随后调用 `GetWindowRect` 取屏幕坐标。进程消失或窗口句柄失效时抛出明确错误。

> 不自动扫描进程列表猜测目标：Light.VN 游戏往往以通用进程名运行，自动猜测容易误挂。

---

## 核心思路

- 用 **Frida hook** 游戏文本 API，获取界面所有文本的原始内容（注意：hook 结果是各翻译区的文本集合，区间顺序不稳定，但单个翻译区内部文本顺序可信）
- 用 **两层 OCR** 定位并高质量识别屏幕文字；OCR 的 capture region 始终限制在 `GameTarget.window_rect` 内
- 将 OCR 结果与 hook 结果做 **Levenshtein Distance 交叉校正**：匹配失败时直接用 OCR 结果送翻译
- 翻译结果以 **翻译区域截图 phash** 为 key 进行缓存，避免重复翻译同一内容

---

## 两层 OCR 分工 → 单层 Windows OCR

经实测，Windows OCR 在游戏渲染字体（半透明对话框、描边/阴影文字）上的识别精度**优于** manga-ocr。manga-ocr 基于漫画扫描图像训练，对 VN 游戏的屏幕渲染字体适应性差，加之启动慢、资源占用高，因此**放弃 manga-ocr，全程使用 Windows OCR**。

| 层 | 实现 | 职责 |
|---|---|---|
| 唯一 OCR 层 | Windows OCR（系统内置） | 全屏扫描，返回带文本的 bounding box；用于文字定位、范围判断、识别文本内容 |

---

## 工作循环

### 普通悬停模式

```
等待鼠标大幅移动后悬停
  → 用 Windows OCR 对鼠标附近小范围快速检测
    → 无文字：结束，返回等待
    → 有文字：
        → 对翻译区域截图计算 phash
          → 命中缓存：直接显示翻译遮罩
          → 未命中：
              → Windows OCR 全屏扫描，获取所有 bounding box
              → 根据空间分布确定翻译范围（可扩展规则链，见下文）
              → 与 hook 结果做 Levenshtein 匹配
                  - 匹配成功：用 hook 结果（更干净）送翻译
                  - 匹配失败：直接用 OCR 结果送翻译
              → 调用翻译插件（见下文）
              → 以 phash 缓存翻译结果
        → 在触发位置显示翻译遮罩
```

### Freeze 模式

用于自动播放、对话快速切换、鼠标固定按钮等悬停模式无法触发的场景：

```
用户按下 Freeze 快捷键
  → 通过 DXGI Desktop Duplication 捕获当前画面（与普通 OCR 同一路径，避免 D3D 窗口黑帧）
  → 移除 hook 进程窗口的焦点
  → 将截图作为置顶遮罩显示（遮罩持有焦点，拦截所有鼠标事件）
  → 用户正常悬停在遮罩上触发翻译（流程同普通模式）
  → 用户右键点击遮罩关闭 Freeze
      → 调用 AllowSetForegroundWindow(pid) 授权，将焦点还给 hook 进程窗口
```

> **注意**：焦点归还必须使用 `AllowSetForegroundWindow` 或 `AttachThreadInput` + `SetFocus`，直接调用跨进程 `SetForegroundWindow` 在 Windows 限制下通常无效。

---

## 翻译插件

翻译后端设计为可插拔，按场景选用：

| 插件 | 计费 / 模型 | 适用场景 |
|---|---|---|
| Cloud Translation API | 按字符计费，成本低 | UI、菜单、短文本 |
| OpenAI 接入点 | 按 token 计费 | 对话、剧情长文本 |

OpenAI 插件行为：
- 用户可在界面上配置全局 system prompt（如角色设定、翻译风格）
- 每次翻译时附带**前文摘要**，避免多轮对话中因上下文缺失导致理解偏差
- 摘要由插件内置 Agent 维护，可复用工具层提供的缓存能力

---

## 翻译范围检测

根据 bounding box 的空间分布，通过**可扩展规则链**（如 `List[RangeDetector]`）确定最终翻译范围，便于后续针对特定游戏布局添加适配。

内置规则优先级从高到低：

1. **段落**：行宽一致、行高一致、紧密间距 → 扩展到整个段落
2. **表格行**：行高一致，同横轴存在 2-3 个底边基本对齐的 box → 扩展到整行
3. **单 box（默认）**：鼠标最近的单个 bounding box

规则链按顺序检测，首个匹配项生效；未匹配任何规则时回退到默认单 box。

---

## Hook 文本清洗

hook 抓取结果含控制符、重复内容，送 Levenshtein 匹配前必须清洗。

- 清洗逻辑通过**可扩展规则链**实现（如 `List[Cleaner]`），便于后续针对具体游戏添加适配
- 基础规则至少包含：去除控制字符、去除重复行、trim 空白

---

## 技术栈

**主语言：Python**

- Windows OCR、Frida Python 绑定生态完整
- 操作 Windows API 能力足够（`ctypes` / `pywin32`）

**不选 C# / Rust 的原因**

- C#：NuGet 包管理在此场景下配置成本高
- Rust：hook 注入全为 `unsafe`，维护成本过高；暂无实际性能瓶颈证据

---

## 屏幕捕获

必须走 **DXGI Desktop Duplication API**（或 DWM shared surface），不能使用 `BitBlt` / `PrintWindow`。Light.VN 使用 DirectX 渲染，后者在硬件加速窗口上只能抓到黑帧。

---

## 实现注意事项

### Windows OCR 语言包

`WindowsOcr` 默认使用 `ja`（日语）识别器。若未安装日语 OCR Capability，将抛出 `MissingOcrLanguageError`，并附带明确的安装提示。

**安装方式**（需管理员权限，~6 MB，仅 OCR 数据，不改变系统语言或 UI）：

```powershell
# 使用提供的脚本（推荐）
powershell -ExecutionPolicy Bypass -File scripts\install_ja_ocr.ps1

# 或手动执行
Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"
```

脚本位于 `scripts/install_ja_ocr.ps1`，包含管理员权限检测、已安装检测、安装、验证及错误提示。安装后无需重启即可使用。

---

## 屏幕捕获

---

## 协议

项目本身：**MPL-2.0**

主要依赖协议确认：

| 依赖 | 协议 | 与 MPL-2.0 的关系 |
|---|---|---|
| winrt（winrt-Windows.*） | MIT | Microsoft 官方维护，兼容，无问题 |
| Frida | wxWindows Library Licence 3.1 | 与 MPL 不冲突；**分发二进制时需提供源码或编译工具链**，发布安装包时须留意 |

> MPL-2.0 为文件级弱 copyleft，对第三方依赖无传染性，只要求被修改的 MPL 文件本身继续以 MPL 发布。

---

## 开发历程
- 不就是把 OCR 和 hook 结合嘛，windows 快速 ocr 然后用更高准确率的专用 ocr，最后和 hook 结果比对拿到正确文本，不就齐活了？
- OCR
  - 不对，这 manga-ocr 识别准确率怎么还不如 windows 自带的日文 ocr
  - 不对，聚合算法恐怕没我想的那么简单
  - 不对，怎么识别区域歪了
- Hook
  - 怎么尝试 hook Windows 的文字输出 API 时啥都没找到？噢跨平台引擎自己搞了文字渲染
  - 怎么连 FreeType 都不带
  - 全量 hook .pdata 指定的函数，看参数就完事了
  - 卧槽 49k 各函数，游戏直接炸了。frida 太重了，切 minhook 并参考 Textractor
  - 怎么 minhook 也 hook 炸了？噢 Textractor 也会 hook 炸，不是我的问题
  - 别一次全 hook，渐进式地分批 hook、分批禁用 hook，这样活跃的 hook 不足之前的 1/10，这样可以尽量减少对游戏的干扰
  - 怎么还在炸？噢还是太多了，得在 dll 里就做抑制
  - 不对，为啥输出当前文本的函数地址一直在变？
  - 不对，有些文本好像始终没显示？噢草，一直显示最高 score 的文本了，应该显示当前文本并更新 score 的
  - 不对，函数地址还是一直在变？我得看看内存啥样
  - 不对，一次发言里的两句话竟然是分两次输出的，而且 hook 到的两句话出现的函数不仅不一样，涉及这两句话的函数数量都不一样，这 TM 啥情况？我得看看内存
  - 不对，这两句话在内存里到处出现，而且没有明显相近的，这玩意怕不是在一个复杂结构体里
  - 欸对，我之前 hook 到的出现都是 r11 之类的寄存器，这些也不是函数入参寄存器啊，TM 是函数调用者的局部变量！难怪不稳定
  - 哦我懂了，得找指针链，只看真函数参数，然后顺着参数里的指针找结构体，然后在结构体里找成员的指针和成员的成员的指针
  - MD，性能垮了，尝试通过聚合、主函数识别等机制改善性能

## TODO

### 基础设施
- [x] 初始化项目结构（`src/`、`tests/`、配置文件）
- [x] 配置 Python 环境与依赖管理（`pyproject.toml`）
- [x] 实现 `GameTarget`：接受 PID 或进程名，推导主窗口 HWND 与 `window_rect`（`src/target.py`）

### 屏幕捕获
- [x] 实现基于 DXGI Desktop Duplication 的屏幕捕获模块
- [x] 验证在 Light.VN 窗口上無黑帧

### OCR
- [x] 集成 Windows OCR（`winrt-Windows.Media.Ocr`），实现带文字内容的 bounding box 返回（经实测精度优于 manga-ocr，不再使用 manga-ocr）
- [x] 实现翻译范围检测可扩展规则链（内置规则：段落 / 表格行 / 单 box）

### 调试界面
- [x] 实现 `src/ui/debug_window.py`：PySide6 调试窗口，含窗口选择、截图预览 + bbox 叠加、Windows OCR / Hook / 翻译面板
- [x] 实现 `src/ui/window_picker.py`：鼠标点击选择目标进程（Win32 GetAsyncKeyState 轮询）
- [x] 实现 `main.py --debug` 入口

### Hook
- [x] 用 Frida 实现 Light.VN 文本 API hook
- [x] 实现可扩展清洗规则链（基础规则：去控制符、去重复、trim）

### 校正与缓存
- [ ] 实现 OCR 结果与 hook 结果的 Levenshtein Distance 匹配
- [ ] 实现翻译区域截图 phash 计算与缓存层

### 翻译插件
- [ ] 定义翻译插件接口（`Translator` ABC，`src/translators/base.py`）
- [ ] 实现 Cloud Translation API 插件
- [ ] 实现 OpenAI 接入点插件（含前文摘要 Agent、全局 prompt 配置）

### 遮罩与交互
- [ ] 实现翻译结果置顶遮罩窗口（透明背景，文字叠加）
- [ ] 实现悬停触发逻辑（鼠标大幅移动后悬停检测）
- [ ] 实现 Freeze 模式：快捷键触发、截图遮罩、右键关闭
- [ ] 实现焦点归还逻辑（`AllowSetForegroundWindow` + `SetForegroundWindow`）

### 发布
- [ ] 整理 Frida wxWindows Licence 3.1 的分发合规要求
- [ ] 编写用户文档（安装、GPU 配置、翻译插件选择）