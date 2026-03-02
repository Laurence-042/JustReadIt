# 项目背景

有个神人作者拿 Light.VN 做了个超棒的 RPG 黄油，但套了壳，也根本没考虑国际化

因为这个引擎就不是拿来做复杂 RPG 的，主流翻译软件（MTool/RenpyThief）都无法正常嵌入翻译。

而 LunaTranslator、Textractor 等基于 hook 文本 API 的方式同样效果不佳，主要是因为其系统基本在复刻 RPGMaker，信息全挤着，用这种方式根本对不上哪行是哪行

WindowTranslator 是不错，但是其 OCR 准确度不算很高，而日语一个片假名搞歪了意思就完全不同，而且还有各种口语反转，以至于翻译效果不佳。另外，其提供的翻译选项颇为冗杂，而且其调翻译服务的方式也过于离散（如果选 LLM 翻译，LLM 似乎拿不到上下文，只能硬翻）

因此，我在考虑做一个结合本地 OCR 和可插拔翻译后端的工具

# 核心思路

结合 LunaTranslator 和 WindowTranslator 两者的优势

hook 文本 API，并清洗得到界面上的所有文本

然后 OCR 识别文字

将`可能识别出错的 OCR 结果`和`可能存在控制符、重复的 hook 抓取结果`交叉比对

如果找到了匹配段，就翻译匹配段

如果没找到，就翻译 OCR 结果

# 具体工作循环

OCR 分两层：
- **Windows OCR**：免费、速度快，负责全屏扫描并返回 bounding box，用于定位文字和判断文字范围，本身识别质量不作要求
- **manga-ocr**：专门针对漫画/游戏场景微调的日文 OCR 模型，本地运行，负责对裁出的实际文字区域进行高质量识别

它的工作流程大致是

- 等待鼠标位置大幅移动后悬停触发
- 获取屏幕画面
- 获取指针位置
- 用 Windows OCR 对鼠标附近小范围做快速检测，判断指针位置是否存在文本
  - 是
    - 用当前屏幕截图的感知哈希（phash）检查是否有缓存
      - 是
        - 将缓存作为翻译结果
      - 否
        - 用 Windows OCR 进行全屏扫描，获取所有文字的 bounding box
        - 根据 bounding box 的空间分布确定翻译范围
          - 默认只翻译鼠标最近的单个 bounding box
          - 如果满足表格行特征（行高一致，同横轴存在另外 2-3 个底边基本对齐的 bounding box），自动扩展到整行
          - 如果满足段落特征（行宽一致、行高一致、紧密间距），自动扩展到整个段落
        - 用 manga-ocr 对裁出的文字区域进行识别，得到文本
        - 和 hook 抓取的结果交叉比对，找到 OCR 对应的部分（不一定完全对应，这一步就是为了用 hook 结果保证目标区域 OCR 结果出现瑕疵也能修复）
        - 根据设置选择翻译插件，如有必要，翻译插件可以自行集成工具提供的缓存 Agent
          - Cloud Translation API（按字符计费，费用低，适合 UI/菜单等短文本）
          - OpenAI 接入点（缓存翻译结果，用户可以在界面上配置总 prompt，而翻译过程中会连带之前的摘要一起发送，来避免对话中多次翻译缺乏上下文导致理解偏差；适合对话场景）
        - 以 phash 为 key 缓存翻译结果
    - 将结果作为遮罩显示在触发位置
  - 否
    - 结束，开始工作循环

# 项目协议

MPL-2.0

# 关于选型

## 技术栈

优先 Python、C#或 Rust

Python 的 OCR 等库生态比较好，而且操作 Windows API 的能力也不算太弱，但性能可能是个问题

C#主要的优势在于 Windows API 支持极佳，不管是显示遮罩还是调 Windows 提供的 OCR 都十分方便，但是其包管理属是有点难绷

Rust 是当前两者都有难以解决的问题时才会考虑选择的，Rust 的运行效率和社区生态都不错，但是我担心 hook 这种行为全是 unsafe 导致写和维护都巨麻烦

## OCR
Google 有拍照翻译功能，但那个没有公开 API，Cloud Translation 只接受文本。而工作循环中会高频 OCR，不能用 Vision 硬跑。

所以更务实的方案是本地用 manga-ocr 做高质量日文识别，再把文本发给翻译服务。

manga-ocr 应该是 Apache 的 License，和 MPL 兼容

不过这个用 CPU 恐怕跑起来延迟过高，和翻译延迟加起来就没法接收，所以尽可能用 GPU

## Hook

准备使用 Frida 搓，因为 Textractor 和 LunaTranslator 都是 GPL-3.0，而且看项目结构颇有其他人很难复用的态势

Frida 和 Python/Node.js 似乎兼容性不错而且也能实现往 Windows 程序注入的功能，其 wxWindows Library Licence, Version 3.1 应该也和 MPL 建通