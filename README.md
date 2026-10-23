<div align="center">

<img src="icon.png" width="120" alt="Hearizon Sync">

# Hearizon Sync

**麦克风实时字幕翻译 —— 说完一句，屏幕出原文和译文**

本地识别（SenseVoice）· 本地/云端双翻译引擎 · 断网可用

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)]()
[![Python](https://img.shields.io/badge/python-3.12-green)]()
[![License](https://img.shields.io/badge/license-GPL--3.0%20%2B%20Commercial-blue)](LICENSE.txt)

作者：**何瑞程**（[@eyru](https://github.com/eyru)）

</div>

---

## 这是什么

**Hearizon Sync** 是一个 Windows 桌面软件：打开麦克风，它把听到的话**实时转成文字并翻译**，上下两栏分别显示原文和译文。

名字取自 **Hear + Horizon + Sync** —— 听见地平线之外的声音。

**典型用途**：外教课、国际会议、英文讲座、看生肉视频。

### 核心特性

| 特性 | 说明 |
|---|---|
| 🎙 **本地语音识别** | SenseVoice-Small（阿里达摩院），中文 CER 约 8%，语音不上传 |
| 🌐 **双翻译引擎** | 本地 opus-mt（断网可用）/ 云端 DeepSeek（质量更高）/ auto 自动降级 |
| ⚡ **实时** | 识别 RTF 0.02（比实时快 50 倍），本地翻译 0.03 秒/句 |
| ⚡ **流式字幕** | **边说边出字**，首字延迟约 0.3 秒（先说后译，整句仍走完整识别） |
| 🌍 **输入语言可选** | 自动 / 英 / 中 / 日 / 韩 / 粤 / 德 / 法 / 意 / 西，**语言对语言**自由组合 |
| 🧠 **三引擎翻译** | DeepSeek / **有道智云**（支持医学等领域）/ 本地 opus-mt，可逐级降级 |
| 🎭 **情感与音频事件** | 附带输出说话人情绪、笑声/掌声等标签 |
| 📝 **记录导出** | 一键保存原文+译文到 txt；SRT 字幕使用真实时间轴 |
| 🎨 **现代界面** | 圆角控件 / 自绘下拉 / 现代滚动条；3 套字幕配色 + 贴底 / 贴顶 |
| 🧰 **系统托盘** | 最小化到托盘后台运行，双击恢复；图标区分「正在同传 / 已停止」 |
| 🩹 **自动纠错（beta）** | 识别结果最小修正（本地规则 + 云端 LLM），侧栏可开关 |

### 版本

**当前 v2.1.1** —— 新增**自动纠错（beta，可选开关）**；修复「保存记录 / 导出 SRT」在 Program Files 安装下无法写入（现统一保存到「文档\Hearizon Sync」）；托盘图标区分「正在同传 / 已停止」，右键菜单随状态变化。

**v2.0.1** —— 全新 UI：圆角按钮、自绘下拉选择器、现代滚动条、侧栏滚动；字幕 3 套配色与贴底/贴顶；系统托盘最小化；SRT 真实时间轴导出；关于页彩蛋；安装向导改为正式中文 EULA。双许可（GPL-3.0 + 商业许可）不变。

**v1.2.0** —— 字幕字号热调节（Ctrl+ / −）、快捷键、SRT 字幕导出、无声提醒、首次使用引导、彩蛋；并完成**双许可改造（GPL-3.0 + 商业许可）**。

**v1.1.0** —— 流式识别（主模式）、输入语言选择、有道双引擎、GPU 可选加速；修复纯标点被翻译、快捷方式图标、窗口模式启动崩溃等问题。

---

## 快速开始

### 方式一：安装包（推荐）

1. 到 [Releases](../../releases) 下载 `HearizonSync-Setup-2.1.1.exe`（约 366 MB）
2. 双击安装（默认装到 `C:\Program Files\Hearizon Sync`）
3. 从开始菜单或桌面启动

**安装包已包含全部依赖和模型，不需要装 Python。**

### 方式二：从源码运行

```bash
git clone https://github.com/eyru/hearizon-sync.git
cd hearizon-sync

# 需要 Python 3.12
python -m venv .venv
.venv\Scripts\activate
pip install sherpa-onnx ctranslate2 sentencepiece sounddevice numpy faster-whisper

# 下载模型（见下方「模型下载」）
python src/HearizonSync.py
```

---

## 模型下载

程序需要两个模型，**安装包已内置**；从源码运行需手动下载。

### SenseVoice（语音识别，239 MB）

```bash
mkdir -p models/sv && cd models/sv
BASE="https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
curl -L -o model.int8.onnx "$BASE/model.int8.onnx"
curl -L -o tokens.txt "$BASE/tokens.txt"
```

### opus-mt-en-zh（本地翻译，155 MB）

```bash
mkdir -p models/opus_en_zh && cd models/opus_en_zh
BASE="https://hf-mirror.com/gaudi/opus-mt-en-zh-ctranslate2/resolve/main"
for f in model.bin shared_vocabulary.json source.spm target.spm vocab.json config.json; do
  curl -L -o "$f" "$BASE/$f"
done
```

> ⚠️ **本地翻译模型必须放在纯 ASCII 路径**
> `sentencepiece` 的 C++ 层打不开中文路径，会报 `NOT_FOUND`。
> 默认查找顺序：`环境变量 SLM_OPUS_EN_ZH_DIR` → `程序目录/models/opus_en_zh` → `D:\slm-models\opus_en_zh`

---

## 使用

双击 `HearizonSync.exe`（或 `HearizonSync.bat`），界面：

```
┌────────────┬──────────────────────────────────┐
│ ● Hearizon  │  字幕                             │
│   Sync      │  ┌────────────────────────────┐  │
│ [▶ 开始同传]│  │ ● 原文 · 识别结果          │  │
│ ● 未开始    │  │ [12:00:01] (英语) Okay...  │  │
│ 输入电平 ▬▬ │  └────────────────────────────┘  │
│ ──────────  │  ┌────────────────────────────┐  │
│ 麦克风 [▾] │  │ ● 译文                     │  │
│ 刷新设备    │  │ 同学们，今天我们要讲的是... │  │
│ 译成   [▾] │  └────────────────────────────┘  │
│ 翻译引擎[▾]│                                   │
│ 识别引擎[▾]│                                   │
│ ──────────  │                                   │
│ 测麦克风    │                                   │
│ 保存记录    │                                   │
│ 清空字幕    │                                   │
└────────────┴──────────────────────────────────┘
```

### 语言对怎么选

左侧有两个下拉：

- **输入语言（识别）** —— 说话人用的是什么语言
- **译成（目标）** —— 要翻译成什么语言

例如外教课是英语 → 输入选「英语」，译成选「中文」。

| 输入语言 | 实际使用的引擎 | 原因 |
|---|---|---|
| 自动 / 中文 / 英语 / 粤语 | **SenseVoice** | 中文原生训练，CER 约 8%，最快 |
| **日语 / 韩语** / 德语 / 法语 / 意大利语 / 西班牙语 | **Whisper** | SenseVoice 的 `language` 参数在本版 sherpa-onnx 中**实测无效**，日韩会被误判成日语；德法意西模型本身不支持 |

> 选到需要 Whisper 的语言时，「识别引擎」下拉会自动变成 `small`，状态栏会提示。

### 翻译引擎怎么选

| 模式 | 联网 | 成本 | 说明 |
|---|---|---|---|
| `auto`（默认） | 有网走云端 | ¥0 或极少 | **DeepSeek → 有道 → 本地** 逐级降级 |
| `local` | ❌ 完全离线 | ¥0 | **仅支持「英语→中文」**（opus-mt 是单语言对模型） |
| `deepseek` | ✅ | 约 ¥0.001/千字 | 质量最高 |
| `youdao` | ✅ | 免费 100 次/天 | 支持 `medicine` 等领域翻译 |

### 有道智云怎么配

1. 到 [ai.youdao.com](https://ai.youdao.com/) 创建「文本翻译」应用
2. 拿到 **appKey** / **appSecret**
3. 点软件右上角 **⚙** 填入并保存
4. 「翻译引擎」选 `youdao`，或保持 `auto`（云端失败时自动降级到有道）

> 领域选 `medicine` 对听力学 / 言语康复文献更准。新用户送 50 元体验金。

### GPU 加速（可选）

勾选左侧「GPU 加速」后，程序会**先真实检测** CUDA 运行时：

| 情况 | 结果 |
|---|---|
| 有 NVIDIA 显卡 + 装了 CUDA Toolkit 12.x | ✅ 启用（Whisper 识别、本地翻译走 GPU） |
| 只有驱动没装 CUDA Toolkit | ❌ 提示缺少 `cublas64_12.dll` 并自动取消勾选 |
| SenseVoice 识别 | **始终 CPU**（官方 pip 包是 CPU 编译，无法 GPU） |

> SenseVoice 本身比实时快 50 倍（RTF 0.02），**不需要 GPU**。GPU 主要帮 Whisper。
> 想启用需单独安装 [CUDA Toolkit 12.x](https://developer.nvidia.com/cuda-downloads)（约 3 GB），不装不影响使用。

### 翻译电脑声音（网课 / 视频）

默认只能录麦克风。要录系统声音：

1. 下载 [VB-Cable](https://vb-audio.com/Cable/) 并安装，重启电脑
2. 「音频输入」里选 **CABLE Output**（会标注【系统声音】）
3. 把系统播放设备设为 **CABLE Input**

> 部分声卡自带「立体声混音」，也会被标注【系统声音】，可以直接用。
> 没装虚拟声卡时界面保持干净，不会多出杂乱选项。

---

## 实测数据

**硬件**：AMD Ryzen 9 9955HX（16 核 32 线程）+ RTX 5070 Ti Laptop（12 GB）

### 识别（同一段 10 秒英文课录音）

| 引擎 | 耗时 | RTF |
|---|---|---|
| **SenseVoice** | **0.17 s** | **0.018** |
| Whisper small | 2.46 s | 0.247 |

### 翻译（同一句）

**原文**：`The sound level meter measures sound pressure level in decibels.`

| 引擎 | 译文 |
|---|---|
| 本地（opus-mt） | 音量测量仪测量了分贝的音压 ⚠️ |
| 云端（DeepSeek） | **声级计用分贝来测量声压级。** ✅ |

**原文**：`Can you hear the difference between these two tones?`

| 引擎 | 译文 | 耗时 |
|---|---|---|
| 本地 | 你能听到这两个音调的区别吗？ | **0.03 s** |
| 云端 | 你能听出这两个声调的区别吗？ | 0.92 s |

### 语言检测稳定性

用**中文音频**强制指定不同语言：

| 指定 | 结果 | 检测到 |
|---|---|---|
| auto / zh / **en** / **ja** / **ko** | 开饭时间早上9点至下午5点。（**全部相同**） | 全部 `zh` |

**→ 即使强制指定错误语言也不被带偏。** 这是 SenseVoice 和 Whisper 的本质差异：**Whisper 靠猜，SenseVoice 的语言是显式输出头**。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **不是真·同传** | 是「说完一句译一句」，延迟 1~2 秒。真·边说边译需流式识别+增量翻译，复杂度高得多 |
| **专业术语仍可能错** | SenseVoice CER 约 8%，「声学/听力学」这类词仍可能错。**这是所有 ASR 的通病** |
| **本地翻译只能英→中** | opus-mt 是单语言对模型，中→英需另一个模型 |
| **本地翻译术语会错** | 768M 的 2020 年 NMT，比不了大模型 |
| **多人同时说话会乱** | 能量 VAD 不区分说话人 |
| **只支持麦克风** | 系统声音（网课/视频）需虚拟声卡如 VB-Cable（见上文引导） |
| **本地翻译只能英→中** | 其他语言对必须走云端（有道/DeepSeek） |
| **日/韩走 Whisper** | 本版 sherpa-onnx 的 SenseVoice `language` 参数无效，为避免误判只能换引擎 |
| **云端翻译需 API Key** | 用 `local` 模式则完全不需要 |

---

## 技术栈

| 组件 | 用途 | 许可 |
|---|---|---|
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | SenseVoice 推理（纯 ONNX，不需 torch） | Apache-2.0 |
| [SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice) | 语音识别模型（阿里达摩院） | Apache-2.0 |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | opus-mt 翻译推理 | MIT |
| [OPUS-MT](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh) | 本地翻译模型（Helsinki-NLP） | CC-BY-4.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Whisper 回退识别 | MIT |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | 音频采集 | MIT |
| [DeepSeek API](https://platform.deepseek.com/) | 云端翻译（主） | 商业服务 |
| [有道智云](https://ai.youdao.com/) | 云端翻译（备，支持医学领域） | 商业服务 |

### 架构

```
麦克风
  ↓ sounddevice 采集（16 kHz）
能量 VAD 自动断句（说完静音 700 ms）
  ↓
SenseVoice 本地识别 ──→ 原文 + 语言 + 情感
  ↓
翻译引擎（auto / local / deepseek）
  ├─ local    : opus-mt（CT2 + sentencepiece）
  └─ deepseek : DeepSeek API（流式）
  ↓
双栏字幕显示
```

---

## 常见问题

**Q：识别一直显示「未识别到内容」？**
A：点「测麦克风」按钮，看 RMS 是否 > 0.008。可能是麦克风权限或选错设备。

**Q：首次启动很慢？**
A：要加载 239 MB 识别模型，约 2~3 秒。之后快。

**Q：本地翻译输出重复乱码？**
A：Marian 模型**必须在源端加 `</s>`**，代码已处理。如果自己改代码要注意这点。

**Q：`sentencepiece` 报 NOT_FOUND 但文件存在？**
A：**中文路径问题**。把模型移到纯 ASCII 路径（如 `D:\slm-models\`）。

**Q：想翻译系统声音（网课/视频）？**
A：当前只支持麦克风。需要装 [VB-Cable](https://vb-audio.com/Cable/) 虚拟声卡，然后选它作为输入设备。

---

## 相关文档

- [使用说明](docs/Hearizon%20Sync-使用说明.docx) —— 详细操作、排错
- [技术选型分析](docs/Hearizon%20Sync-技术选型分析.docx) —— 引擎对比、改进清单

---

## 许可证

本项目采用**双许可（Dual Licensing）**模式 —— 你可以按需选择其一：

| 方案 | 协议 | 适用场景 |
|---|---|---|
| **A · 开源社区版** | [GPL-3.0](LICENSE.txt) | 个人学习、研究、自用；或衍生作品同样以 GPL-3.0 开源发布 |
| **B · 商业许可** | [Commercial License](COMMERCIAL-LICENSE.md) | 闭源商业产品集成、专有分发、SaaS 等无法满足 GPL-3.0 义务的场景 |

© 2026 何瑞程 (He Ruicheng)

> ⚠️ **版本说明**：v1.0.0 ~ v1.1.0 的历史版本曾以 **MIT** 发布，已获得副本者其 MIT 授权继续有效且不可撤销。双许可自 2026-10-22 起适用于此后发布的版本。
>
> 第三方组件（sherpa-onnx / SenseVoice / CTranslate2 / OPUS-MT / faster-whisper / Whisper / sounddevice / NumPy / Python）遵循其原有许可证，详见 [LICENSE.txt](LICENSE.txt) 末尾。
>
> 本软件**不是医疗器械**，不得作为助听器或听力诊断设备使用或宣称。
