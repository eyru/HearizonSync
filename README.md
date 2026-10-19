<div align="center">

<img src="icon.png" width="120" alt="同声传译">

# 同声传译 · RealTimeInterpreter

**麦克风实时字幕翻译 —— 说完一句，屏幕出原文和译文**

本地识别（SenseVoice）· 本地/云端双翻译引擎 · 断网可用

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)]()
[![Python](https://img.shields.io/badge/python-3.12-green)]()
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE.txt)

作者：**何瑞程**（[@eyru](https://github.com/eyru)）

</div>

---

## 这是什么

一个 Windows 桌面软件：打开麦克风，它把听到的话**实时转成文字并翻译**，上下两栏分别显示原文和译文。

**典型用途**：外教课、国际会议、英文讲座、看生肉视频。

### 核心特性

| 特性 | 说明 |
|---|---|
| 🎙 **本地语音识别** | SenseVoice-Small（阿里达摩院），中文 CER 约 8%，语音不上传 |
| 🌐 **双翻译引擎** | 本地 opus-mt（断网可用）/ 云端 DeepSeek（质量更高）/ auto 自动降级 |
| ⚡ **实时** | 识别 RTF 0.02（比实时快 50 倍），本地翻译 0.03 秒/句 |
| 🈶 **语言自动检测** | 中/英/日/韩/粤，**不会被强制指定带偏** |
| 🎭 **情感与音频事件** | 附带输出说话人情绪、笑声/掌声等标签 |
| 📝 **记录导出** | 一键保存原文+译文到 txt |

### 版本

**当前 v1.0.1** —— 界面重做为侧边栏布局（Claude / DSH 风格，深绿主题），修复窗口图标与安装协议编码。

---

## 快速开始

### 方式一：安装包（推荐）

1. 到 [Releases](../../releases) 下载 `同声传译-安装包-1.0.1.exe`（约 366 MB）
2. 双击安装（默认装到 `C:\Program Files\同声传译`）
3. 从开始菜单或桌面启动

**安装包已包含全部依赖和模型，不需要装 Python。**

### 方式二：从源码运行

```bash
git clone https://github.com/eyru/realtime-interpreter.git
cd realtime-interpreter

# 需要 Python 3.12
python -m venv .venv
.venv\Scripts\activate
pip install sherpa-onnx ctranslate2 sentencepiece sounddevice numpy faster-whisper

# 下载模型（见下方「模型下载」）
# 然后运行
python src/同声传译.py
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

双击 `同声传译.exe`（或 `启动同传.bat`），界面：

```
┌─────────────────────────────────────────────────────┐
│ 麦克风 [下拉]  译成 [中文▾]  翻译 [auto▾] 引擎 [sensevoice▾] │
│                              ▶ 开始同传    ● 状态  音量▮▮  │
├─────────────────────────────────────────────────────┤
│ 原文（识别结果）                                      │
│ [12:00:01] (英语) Okay class, today we will...       │
├─────────────────────────────────────────────────────┤
│ 译文                                                 │
│ [12:00:01] 同学们，今天我们将...                       │
└─────────────────────────────────────────────────────┘
```

### 三个下拉怎么选

**「引擎」= 语音识别**

| 选项 | 中文 CER | 速度 | 建议 |
|---|---|---|---|
| `sensevoice`（默认） | **约 8%** | RTF 0.02 | 就用这个 |
| `base` | 约 25% | RTF 0.20 | Whisper 回退 |
| `small` | 约 20% | RTF 0.53 | Whisper 回退 |

**「翻译」= 翻译引擎**

| 选项 | 联网 | 成本 | 质量 | 速度 |
|---|---|---|---|---|
| `auto`（默认） | 有网走云端，失败自动降级 | ¥0 或极少 | 看情况 | 看情况 |
| `local` | ❌ **完全离线** | **¥0** | 中等 | **0.03 秒** |
| `deepseek` | ✅ 需要 | 约 ¥0.001/千字 | **高** | 0.9 秒 |

**「译成」= 目标语言**：中文 / English / 日本語 / 한국어

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
| **只支持麦克风** | 系统声音（网课/视频）需虚拟声卡如 VB-Cable |
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
| [DeepSeek API](https://platform.deepseek.com/) | 云端翻译 | 商业服务 |

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

- [使用说明](docs/同声传译-使用说明.docx) —— 详细操作、排错
- [技术选型分析](docs/同声传译-技术选型分析.docx) —— 引擎对比、改进清单

---

## 许可证

[MIT](LICENSE.txt) © 2026 何瑞程

第三方组件遵循其原有许可证，详见 [LICENSE.txt](LICENSE.txt)。
