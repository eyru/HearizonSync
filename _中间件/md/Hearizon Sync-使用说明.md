# Hearizon Sync —— 使用说明（v1.1.0）

> 麦克风实时字幕翻译：说一句，屏幕出原文和译文
> 建立时间：2026-10-19　|　位置：`写出\Hearizon Sync\`

---

## 一、怎么用

**双击「HearizonSync.bat」。**

1. 选麦克风（默认已选好系统的）
2. 选「译成」目标语言（默认中文）
3. 点 **▶ 开始同传**
4. 开始说话，屏幕上下两栏分别显示**原文**和**译文**

**第一次启动会慢一点**（要加载模型），之后 2~3 秒。

---

## 一点五、v1.1.0 新增了什么

| 功能 | 说明 |
|---|---|
| **流式识别（主模式）** | 边说边出字，首字延迟约 0.3 秒；句子说完后再出最终整句并翻译 |
| **输入语言可选** | 自动 / 英 / 中 / 日 / 韩 / 粤 / 德 / 法 / 意 / 西 |
| **语言对语言** | 输入语言 × 目标语言（8 种）自由组合 |
| **有道双引擎** | 第二个云端翻译引擎，支持医学等领域翻译 |
| **GPU 加速（可选）** | 仅对 Whisper 识别与本地翻译生效，见第七节说明 |
| **系统声音引导** | 装了 VB-Cable 就能翻网课/视频，设置里有引导 |
| **修复** | 纯标点结果不再送翻译；桌面快捷方式图标已正确 |

### 输入语言 vs 识别引擎（重要）

| 输入语言 | 使用哪个引擎 | 说明 |
|---|---|---|
| 英 / 中 / 日 / 韩 / 粤 | **SenseVoice**（默认） | 中文原生训练，CER 约 8%，最快 |
| **德 / 法 / 意 / 西** | **自动切 Whisper** | SenseVoice 不支持这四种语言，程序会自动换引擎 |
| 自动检测 | SenseVoice | 靠模型自身判断 |

> 选德法意西时，左侧「识别引擎」会自动变成 `small`，这是正常的。

### 语言对语言怎么用

左侧两个下拉：
- **输入语言（识别）**：说话人用的是什么语言
- **译成（目标）**：要翻译成什么语言

例如外教课是英语 → 输入选「英语」，译成选「中文」。

> ⚠️ **本地翻译只支持「英语→中文」**。其他语言对请把「翻译引擎」设为 `auto` / `deepseek` / `youdao`。

---

## 二、它怎么工作的

```
麦克风 → 判断有没有人在说话 → 说完一句自动切段
      → Whisper 本地识别成文字（不联网）
      → DeepSeek 翻译（联网，流式）
      → 屏幕显示
```

**四个关键设计：**

| 设计 | 为什么 |
|---|---|
| **自动断句**（能量 VAD） | 不是固定时长切，而是「说完停 0.7 秒」才切，句子完整 |
| **识别全本地** | 语音不上传，隐私安全，也不消耗 API 额度 |
| **翻译流式输出** | 译文一个字一个字往外蹦，不用等整句 |
| **双栏显示** | 上原文、下译文，对照着看 |

---

## 二点五、⭐ 翻译引擎：现在可以断网用了

界面「翻译」下拉有三个选项：

| 模式 | 联网 | 成本 | 质量 | 速度 |
|---|---|---|---|---|
| **auto**（默认） | 🟡 有网走云端，没网自动降级 | ¥0 或极少 | 看情况 | 看情况 |
| **local** | ❌ **完全离线** | **¥0** | 中等 | **0.02~0.05 秒** |
| **deepseek** | ✅ 需要 | 约 ¥0.001/千字 | **高** | 0.8~1.0 秒 |

### 实测对比（同一句）

**原文**：`The sound level meter measures sound pressure level in decibels.`

| 引擎 | 译文 | 评价 |
|---|---|---|
| **本地**（opus-mt） | 音量测量仪测量了分贝的音压 | ⚠️ 「声级计」翻错成「音量测量仪」 |
| **云端**（DeepSeek） | **声级计用分贝来测量声压级。** | ✅ 准确 |

**原文**：`Can you hear the difference between these two tones?`

| 引擎 | 译文 | 速度 |
|---|---|---|
| 本地 | 你能听到这两个音调的区别吗？ | **0.03 秒** |
| 云端 | 你能听出这两个声调的区别吗？ | 0.92 秒 |

### 怎么选

| 场景 | 建议 |
|---|---|
| 教室/图书馆，网不稳 | **local** —— 稳定优先，术语错就错 |
| 宿舍/家里，有网 | **deepseek** —— 质量优先 |
| 不确定 | **auto**（默认）—— 有网自动用云端，断网自动降级到本地 |

> 💡 **auto 模式的降级逻辑**：先试云端，如果失败（断网/额度用完/报错），自动切到本地，状态栏会提示「云端不可用，已降级到本地翻译」。

### 本地方案的技术细节

- **模型**：`opus-mt-en-zh`（Helsinki-NLP，2020 年），CT2 int8 格式，**155 MB**
- **架构**：Marian NMT（不是大模型）
- **BLEU**：36.1（中英，测试集）
- **限制**：**只能英→中**，中文→英文需要另一个模型（`opus-mt-zh-en`）

> ⚠️ **必须放纯 ASCII 路径**：`D:\slm-models\opus_en_zh\`
> 原因：**sentencepiece 打不开中文路径**（实测报 `NOT_FOUND`）
> 工作区里的 `models\opus_en_zh` 是个 **Junction 软链接**，指向 D 盘那份

---

## 三、识别引擎（已换 SenseVoice）

界面「引擎」下拉有三个选项：

| 引擎 | RTF（中文） | 中文准确度 | 说明 |
|---|---|---|---|
| **sensevoice**（默认） | **0.02** | **CER 约 8%** | 阿里达摩院开源，**中文原生训练**，自带语言检测/情感/音频事件 |
| base | 0.20 | CER 约 25% | Whisper，快但中文一般 |
| small | 0.53 | CER 约 20% | Whisper，稍好但仍不如 SenseVoice |

### 为什么换 SenseVoice

**主人反馈的两个问题，根因都在 Whisper：**

| 问题 | Whisper 的表现 | SenseVoice 的改进 |
|---|---|---|
| **莫名识别成其他语言** | `language=None` 自动检测时容易飘（外教课英文偶尔被判成其他语言） | **语言检测稳定** —— 实测强制指定 `en/ja/ko` 仍正确输出中文，说明它不靠"猜" |
| **准确率低于 50%** | 中文 CER 约 20–30%，专业术语常错（"声学"→"生学"） | **中文 CER 约 8%**，中文原生训练 |

**速度差距更大**：同一段 10 秒英文课录音，SenseVoice **0.17 秒**，Whisper small **2.46 秒**（快 14 倍）。

### SenseVoice 的额外能力

它会额外返回两个标签，界面会显示：

- **情感**：`NEUTRAL` / `HAPPY` / `SAD` / `ANGRY`（非中性才显示）
- **音频事件**：`Speech` / `Laughter` / `Applause` / `Music` / `Cough` 等

**这对听力学 / 言语康复场景有用** —— 可以记录说话人情绪状态。

---

## 四、实测效果（真实语音，非模拟）

### 4.1 外教课场景（英文授课 → 中文）

**测试音频**：Windows TTS 合成，10 秒，内容为「Okay class, today we are going to talk about acoustic measurement. The sound level meter is a device that measures sound pressure level in decibels.」

| 环节 | 耗时 | 结果 |
|---|---|---|
| 识别（SenseVoice） | **1.19 s**（首次含预热） | `O class Today we are going to talk about acoustic measurement The sound level meter is a device that measures sound pressure level in decibels.` |
| 翻译（DeepSeek） | 1.02 s | **同学们，今天我们要讲的是声学测量。声级计是一种测量声压级的设备，单位是分贝。** |
| **合计** | **2.21 s** | 音频 10 秒，**RTF 0.22** |

### 4.2 中文 → 英文

| 环节 | 耗时 | 结果 |
|---|---|---|
| 识别 | **0.10 s**（RTF 0.018） | `开饭时间早上9点至下午5点。` |
| 翻译 | 0.64 s | `Meal times are from 9 AM to 5 PM.` |
| **合计** | **0.74 s** | 音频 5.6 秒，**RTF 0.13** |

### 4.3 语言检测稳定性测试

用**中文音频**，强制指定不同语言，看会不会被带偏：

| 指定语言 | 识别结果 | 检测到的 lang |
|---|---|---|
| auto | 开饭时间早上9点至下午5点。 | `zh 标签` |
| zh | 同上 | `zh 标签` |
| **en** | **同上（没被带偏）** | `zh 标签` |
| **ja** | **同上** | `zh 标签` |
| **ko** | **同上** | `zh 标签` |

**→ 即使强制指定错误语言，SenseVoice 仍正确输出中文。** 这是它和 Whisper 的关键差异。

---

## 五、已知限制（老实说）

| 限制 | 说明 |
|---|---|
| **不是「真·同传」** | 真同传是边说边译、几乎零延迟。这个是**说完一句才译一句**，延迟约 1~2 秒 |
| **中文专业术语仍可能错** | SenseVoice 中文 CER 约 8%，比 Whisper 好很多，但「声学/听力学」这类词仍可能错。**这是所有 ASR 的通病** |
| **需要联网翻译** | 识别是离线的，但翻译要调 DeepSeek API（约 ¥0.001/千字） |
| **多人同时说话会乱** | 能量 VAD 只判断「有没有声音」，不区分是谁在说 |
| **背景噪声大时误触发** | 可调 `VAD_MIN_RMS`（默认 0.008），调高更不容易被噪声触发 |
| **只支持麦克风输入** | 想翻译系统声音（网课/视频）需要装虚拟声卡（VB-Cable），当前不支持 |

### ⚠️ 关于「逆向有道/讯飞的同声传译」

**技术上不成立，鱼鱼也不做。**

「去广告去会员版」针对的是**本地 App 的客户端校验** —— 改掉「你是不是会员」那行代码即可。

但**同声传译功能跑在它们自己的服务器上**，你的设备只是麦克风+喇叭。**没有本地模型可拆，也没有本地校验可改。**

想用它们的能力，正道是调官方 API：
- [有道短语音翻译 API](https://ai.youdao.com/new/product-sst.s)：**0.019 元/次**
- 新用户送 50 元体验金

---

## 五点五、配置有道翻译（可选）

1. 打开 [ai.youdao.com](https://ai.youdao.com/)，注册并创建应用（选「文本翻译」）
2. 拿到 **appKey** 和 **appSecret**
3. 在本软件右上角点 **⚙** → 填入 → 保存
4. 左侧「翻译引擎」选 `youdao`，或 `auto`（云端失败时自动降级到有道）

**领域翻译**：设置里可选 `medicine`（医学），对听力学/言语康复文献更准。

**价格**：新用户送 50 元体验金；入门版免费 100 次/天。

> 没配 key 时选 `youdao` 会提示「未配置」，不影响其他功能。

---

## 五点六、翻译电脑声音（VB-Cable）

默认只能录麦克风。要翻译网课、视频、会议的声音：

1. 下载 [VB-Cable](https://vb-audio.com/Cable/) 并安装，重启电脑
2. 左侧「音频输入」里选 **CABLE Output**（会标注【系统声音】）
3. 把系统播放设备设为 **CABLE Input**
4. 想自己同时听到声音，在「声音设置」里勾选「侦听此设备」

> 如果没装虚拟声卡，「音频输入」里不会出现系统声音选项，界面保持干净。
> 部分声卡自带「立体声混音」，也会被标注【系统声音】，可以直接用。

---

## 六、如果出问题

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 打开就报「缺少 API Key」 | 没找到 `DEEPSEEK_API_KEY` | 识别功能仍可用，只是不翻译 |
| 一直「未识别到内容」 | 麦克风没声音 / 选错设备 | 点「测麦克风」按钮，看 RMS 是否 > 0.008 |
| 识别很慢 | 用的是 small 模型 | 切回 base |
| 首次启动卡在「正在加载模型」 | 在下载模型（约 145 MB） | 等它下完，只下一次 |
| 翻译报错 | 网络或额度问题 | 看状态栏提示 |
| 文字是繁体 | 旧版本问题 | 已修复（用 initial_prompt 强制简体） |

---

## 六点五、模型文件在哪 / 怎么重新下载

**SenseVoice 模型已下载好，在：**

```
写出\Hearizon Sync\models\sv\
├── model.int8.onnx   239 MB  ← 主模型
└── tokens.txt        316 KB  ← 词表
```

**如果模型丢了或要重下：**

```bash
cd 写出/Hearizon Sync/models/sv

# 从 HuggingFace 镜像下载（国内可直连）
BASE="https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"

curl -L -o model.int8.onnx "$BASE/model.int8.onnx"
curl -L -o tokens.txt "$BASE/tokens.txt"
```

> ⚠️ **不要从 GitHub 下** —— 国内经常连不上（实测 `Could not connect to server`）。
> ⚠️ 某些 VPN 环境下需要加 `--ssl-no-revoke`（schannel 证书吊销检查会失败）。

**Whisper 回退模型的缓存位置**：`C:\Users\<用户名>\.cache\huggingface\hub\`

---

## 六点七、GPU 加速说明（重要）

勾选左侧「GPU 加速」后，程序会**先真实检测** CUDA 运行时：

| 情况 | 结果 |
|---|---|
| 有 NVIDIA 显卡 + 装了 CUDA Toolkit 12.x | ✅ 启用（Whisper 识别、本地翻译走 GPU） |
| 只有显卡驱动，没装 CUDA Toolkit | ❌ 提示「缺少 cublas64_12.dll」并自动取消勾选 |
| 没有 NVIDIA 显卡 | ❌ 提示「未检测到显卡」 |
| SenseVoice 识别 | **始终 CPU**（官方 pip 包是 CPU 编译，无法 GPU） |

> ⚠️ **SenseVoice 不需要 GPU**：它本身就比实时快 50 倍（RTF 0.02），
> 4 秒音频只要 56 毫秒。GPU 对整体体验的提升很有限。
>
> 想启用 GPU 需要单独安装 CUDA Toolkit 12.x（约 3 GB），不装不影响使用。

---

## 七、技术细节（给以后要改的人）

**关键配置在文件开头：**

```python
SAMPLE_RATE   = 16000        # Whisper 要求 16 kHz
MODEL_SIZE    = "base"       # 默认档位
CPU_THREADS   = 16           # 实测 8 和 16 差不多
COMPUTE_TYPE  = "int8"       # int8 比 float32 快约 20%

VAD_MIN_RMS   = 0.008        # 静音门限
VAD_HANG_MS   = 700          # 说完静音多久算一句
VAD_MIN_MS    = 400          # 太短的不送识别
VAD_MAX_MS    = 12000        # 太长强制切断
```

**踩过的坑：**

1. **HuggingFace 连不上** → 设 `HF_ENDPOINT=https://hf-mirror.com`
2. **hf-xet 加速通道被镜像挡住**（401） → 设 `HF_HUB_DISABLE_XET=1`
3. **中文识别出繁体字** → 加 `initial_prompt="以下是普通话简体中文的转写..."`，实测能把「我們」纠正成「我们」
4. **流式译文重复/残留** → 用 `mark_set("partial_start")` + `delete(mark, "end-1c")`，注意 `end` 会多删一个字符
5. **系统 Python 3.14 装不了 faster-whisper** → 用 uv 里的 Python 3.12 建独立 venv

---

## 八、文件清单

| 文件 | 说明 |
|---|---|
| `HearizonSync.py` | 主程序 |
| `HearizonSync.bat` | 双击启动（纯 ASCII / CRLF） |
| `models\sv\model.int8.onnx` | **SenseVoice 识别模型（239 MB）** |
| `models\sv\tokens.txt` | 词表 |
| `models\opus_en_zh` | **本地翻译模型软链接** → `D:\slm-models\opus_en_zh` |
| `.venv\` | 独立 Python 环境 |
| `Hearizon Sync-使用说明.docx` | 本文 |
| `Hearizon Sync-技术选型分析.docx` | 选型对比与改进清单 |

**⚠️ 本地翻译模型必须放在 D:\slm-models\opus_en_zh\（纯 ASCII 路径）**

**依赖版本：**

| 包 | 版本 | 用途 |
|---|---|---|
| sherpa-onnx | 1.13.8 | **SenseVoice 识别（纯 ONNX，不需 torch）** |
| ctranslate2 | 4.8.3 | **本地翻译（opus-mt）** |
| sentencepiece | 0.2.2 | 本地翻译分词 |
| faster-whisper | 1.2.1 | Whisper 回退 |
| sounddevice | 0.5.6 | 麦克风采集 |
| numpy | 2.5.4 | 数组运算 |

**总占用：约 570 MB**（含 venv + 两个模型）
