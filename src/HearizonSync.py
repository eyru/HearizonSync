#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hearizon Sync —— 麦克风实时字幕翻译
============================================================
工作流：
    麦克风 → VAD 切段 → SenseVoice 识别 → 本地/云端翻译 → 屏幕字幕

设计要点：
    1. 麦克风持续采集，用「语音活动检测」自动切句（不是固定时长切）
    2. 识别与翻译在后台线程跑，界面永不卡死
    3. 译文流式输出，说一句翻一句
    4. 识别全本地（SenseVoice），翻译可选本地或云端

用法：
    双击「HearizonSync.bat」，或 .venv\\Scripts\\python.exe HearizonSync.py
"""

import os
import sys
import json
import queue
import threading
import time
import traceback
from collections import deque
from datetime import datetime

# ---- 必须在 import faster_whisper 之前设置（国内镜像 + 关闭 xet）----
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# ================================================================ 配置
SAMPLE_RATE   = 16000        # Whisper 要求 16 kHz
CHANNELS      = 1
BLOCK_MS      = 30           # 每次读取 30 ms
BLOCK_SIZE    = int(SAMPLE_RATE * BLOCK_MS / 1000)

# VAD 参数（能量门限法，简单可靠）
VAD_MIN_RMS   = 0.008        # 低于此 RMS 认为是静音
VAD_HANG_MS   = 700          # 说完后静音多久算一句结束
VAD_MIN_MS    = 400          # 太短的不送识别（防咳嗽声）
VAD_MAX_MS    = 12000        # 太长强制切断（防一直不停）

# ---- 识别引擎（实测数据，Ryzen 9 9955HX）----
#   sensevoice : RTF 0.02  ← 默认，中文原生训练，自带语言检测
#   base       : RTF 0.20  Whisper，中文一般
#   small      : RTF 0.53  Whisper，中文稍好但仍不如 SenseVoice
ENGINE        = "sensevoice"
ENGINE_CHOICES = ["sensevoice", "base", "small"]
CPU_THREADS   = 16
COMPUTE_TYPE  = "int8"

# ================================================================ 内嵌图标
# 64x64 PNG 的 base64（窗口图标用，避免依赖外部文件）
_ICON_B64 = (
    "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"
)

# ================================================================ 路径解析
# 打包成 exe 后 __file__ 指向临时解压目录，模型需要另找。
# 查找顺序：环境变量 → exe 同目录/models → 源码目录/models → D:\slm-models
def _app_dir():
    """程序所在目录（打包后是 exe 目录，源码时是 .py 目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _find_model_dir(name):
    """按优先级查找模型目录，返回存在的那个"""
    candidates = [
        os.environ.get(f"SLM_{name.upper()}_DIR", ""),
        os.path.join(_app_dir(), "models", name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", name),
        "D:" + chr(92) + "slm-models" + chr(92) + name,
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # 都找不到时返回首选路径（报错信息里能看到）
    return os.path.join(_app_dir(), "models", name)


APP_DIR = _app_dir()
SV_DIR   = _find_model_dir("sv")
OPUS_DIR = _find_model_dir("opus_en_zh")

# SenseVoice 模型路径
SV_MODEL  = os.path.join(SV_DIR, "model.int8.onnx")
SV_TOKENS = os.path.join(SV_DIR, "tokens.txt")

# Whisper 回退用的 prompt（SenseVoice 不需要）
ZH_PROMPT = "以下是普通话简体中文的转写，涉及声学、听力学、噪声测量。"
EN_PROMPT = "The following is English speech about acoustics, audiology and noise."

# ---- 语言锁定 ----
# 主人反馈「会莫名识别成其他语言」。默认锁定中文优先，可切 auto。
#   zh      : 只认中文（外教课中译中时用 auto）
#   auto    : 自动检测（默认）
#   en/ja/ko: 锁定单一语言
SV_LANGUAGE = "auto"

# ---- 本地翻译（opus-mt，断网可用）----
# 模型在 models/opus_en_zh/（CT2 格式，155 MB）
# ⚠️ sentencepiece 打不开中文路径，所以查找顺序里 D:\slm-models 优先
#    （OPUS_DIR 已在上面通过 _find_model_dir 解析）

# 翻译引擎：
#   local    : opus-mt 本地，断网可用、零成本、快（质量中等）
#   deepseek : DeepSeek 云端，质量高、需联网
#   auto     : 有网用 DeepSeek，没网自动降级到本地
TRANSLATOR_CHOICES = ["auto", "local", "deepseek"]
TRANSLATOR = "auto"

# 识别结果语言 → 中文名（给界面显示用）
LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "yue": "粤语", "auto": "自动",
}

# 配色
# ---- 配色：Claude / DSH 风格 · 图标深绿主题 ----
# 主色取自图标背景 (1,110,89) → #016E59，提亮为 #10A37F 系
C_BG       = "#0A0F0D"        # 最底层（窗口）
C_SIDEBAR  = "#0D1512"        # 侧边栏
C_PANEL    = "#111B17"        # 主面板
C_CARD     = "#16241F"        # 卡片 / 输入区
C_CARD_HOV = "#1C2E27"        # 卡片悬停
C_BORDER   = "#1F332C"        # 边框
C_BORDER2  = "#2A443B"        # 强边框

C_TEXT     = "#E8F0EC"        # 主文字
C_TEXT2    = "#B8C9C2"        # 次文字
C_DIM      = "#6E8880"        # 弱文字
C_FAINT    = "#445953"        # 极弱

C_ACCENT   = "#10A37F"        # 强调（绿）
C_ACCENT2  = "#2DD4BF"        # 强调亮（青绿）
C_SRC      = "#8FA8A0"        # 原文
C_DST      = "#4ADE9B"        # 译文（亮绿）

C_GREEN    = "#10A37F"        # 开始按钮
C_YELLOW   = "#EAB308"
C_RED      = "#DC2626"
C_LISTEN   = "#2DD4BF"

# ================================================================ 本地翻译器# ================================================================ 本地翻译器
class LocalTranslator:
    """opus-mt 本地翻译（CT2 + sentencepiece），断网可用

    实测（Ryzen 9 9955HX）：
        "Can you hear the difference between these two tones?"
        → 你能听到这两个音调的区别吗？   0.027s
    """

    def __init__(self, on_status=None):
        self.on_status = on_status or (lambda s: None)
        self._tr = None
        self._sp_src = None
        self._sp_tgt = None
        self._lock = threading.Lock()

    def available(self):
        return os.path.exists(os.path.join(OPUS_DIR, "model.bin"))

    def load(self):
        if self._tr is not None:
            return
        if not self.available():
            raise FileNotFoundError(
                f"找不到本地翻译模型：{OPUS_DIR}\n请先下载（见使用说明）")
        self.on_status("正在加载本地翻译模型...")
        import ctranslate2
        import sentencepiece as spm
        t0 = time.time()
        self._tr = ctranslate2.Translator(OPUS_DIR, device="cpu",
                                          compute_type="int8")
        self._sp_src = spm.SentencePieceProcessor(
            model_file=os.path.join(OPUS_DIR, "source.spm"))
        self._sp_tgt = spm.SentencePieceProcessor(
            model_file=os.path.join(OPUS_DIR, "target.spm"))
        self.on_status(f"本地翻译已就绪（{time.time()-t0:.1f}s）")

    def translate(self, text, target_lang="中文", on_chunk=None):
        """注意：opus-mt 是单语言对模型，这里只有 en→zh"""
        if not text.strip():
            return ""
        self.load()
        with self._lock:
            tokens = self._sp_src.encode(text, out_type=str)
            # ⚠️ Marian 模型必须在源端加 EOS，否则输出会重复乱码
            tokens = tokens + ["</s>"]
            res = self._tr.translate_batch(
                [tokens], beam_size=1,
                repetition_penalty=1.0,
                max_decoding_length=256)
            out = res[0].hypotheses[0]
            if out and out[0] == "</s>":
                out = out[1:]
            result = self._sp_tgt.decode(out)
        if on_chunk:
            on_chunk(result)
        return result


# ================================================================ 翻译器
class Translator:
    """调用 DeepSeek API 做翻译（流式）"""

    API_URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, api_key=None, model="deepseek-chat"):
        self.api_key = api_key or self._load_key()
        self.model = model

    @staticmethod
    def _load_key():
        """从 DSH 凭据里读，避免让用户再填一次"""
        # 1) 环境变量
        k = os.environ.get("DEEPSEEK_API_KEY")
        if k:
            return k.strip()
        # 2) DSH 凭据文件
        p = os.path.expanduser(r"~\.dsh\.credentials.yaml")
        try:
            for line in open(p, encoding="utf-8"):
                if line.strip().startswith("DEEPSEEK_API_KEY:"):
                    v = line.split(":", 1)[1].strip().strip('"\'')
                    if v:
                        return v
        except Exception:
            pass
        return None

    def available(self):
        return bool(self.api_key)

    def translate(self, text, target_lang="中文", on_chunk=None, timeout=30):
        """流式翻译；on_chunk(增量文本) 用于边收边显示"""
        if not text.strip():
            return ""
        if not self.api_key:
            return "[未配置 API Key]"

        import urllib.request

        sys_prompt = (
            f"你是专业同声传译员。把用户给的内容翻译成{target_lang}。\n"
            "要求：\n"
            "1. 只输出译文，不要任何解释、不要引号、不要重复原文\n"
            "2. 保持口语自然，像真人同传那样说人话\n"
            "3. 如果原文是语气词/半句话，就译成对应的口语\n"
            "4. 专有名词保留原文或按通用译法"
        )
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            "stream": True,
            "temperature": 0.3,
            "max_tokens": 1000,
        }).encode("utf-8")

        req = urllib.request.Request(
            self.API_URL, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        out = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)
                        delta = d["choices"][0]["delta"].get("content", "")
                    except Exception:
                        continue
                    if delta:
                        out.append(delta)
                        if on_chunk:
                            on_chunk(delta)
        except Exception as e:
            return f"[翻译失败: {e}]"

        return "".join(out)


# ================================================================ 识别引擎
class Recognizer:
    """双引擎识别：SenseVoice（默认，快且中文强）+ Whisper（回退）

    实测对比（Ryzen 9 9955HX，同一段音频）：
        SenseVoice  RTF 0.02   中文原生，自带语言检测/情感/音频事件
        Whisper base RTF 0.20  多语言混合训练，中文一般
        Whisper small RTF 0.53 中文稍好，但仍不如 SenseVoice
    """

    def __init__(self, on_status=None, engine=None):
        self.on_status = on_status or (lambda s: None)
        self.engine = engine or ENGINE
        self._sv = None          # sherpa-onnx recognizer
        self._whisper = None     # faster-whisper model
        self._lock = threading.Lock()

    # ---------------- 引擎切换 ----------------
    def set_engine(self, name):
        if name == self.engine and (self._sv is not None or self._whisper is not None):
            return
        self.engine = name
        self._sv = None
        self._whisper = None

    # ---------------- 加载 ----------------
    def load(self):
        if self.engine == "sensevoice":
            if self._sv is not None:
                return
            self.on_status("正在加载 SenseVoice 模型...")
            import sherpa_onnx
            if not os.path.exists(SV_MODEL):
                raise FileNotFoundError(
                    f"找不到 SenseVoice 模型：{SV_MODEL}\n"
                    "请先下载（见使用说明第七节）")
            t0 = time.time()
            self._sv = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=SV_MODEL,
                tokens=SV_TOKENS,
                num_threads=CPU_THREADS,
                use_itn=True,
                language=SV_LANGUAGE,
                debug=False,
            )
            self.on_status(f"SenseVoice 已就绪（{time.time()-t0:.1f}s）")
        else:
            if self._whisper is not None:
                return
            size = self.engine
            mb = {"base": "约 145 MB", "small": "约 480 MB"}.get(size, "")
            self.on_status(f"正在加载 Whisper {size}（首次需下载{mb}）...")
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(
                size, device="cpu", compute_type=COMPUTE_TYPE,
                cpu_threads=CPU_THREADS,
            )
            self.on_status(f"Whisper {size} 已就绪")

    # ---------------- 识别 ----------------
    def transcribe(self, audio):
        """audio: float32 numpy @16kHz
        返回 (文本, 语言码, 情感, 音频事件)
        """
        self.load()
        with self._lock:
            if self.engine == "sensevoice":
                return self._transcribe_sv(audio)
            return self._transcribe_whisper(audio)

    def _transcribe_sv(self, audio):
        s = self._sv.create_stream()
        s.accept_waveform(SAMPLE_RATE, audio)
        self._sv.decode_stream(s)
        r = s.result
        # SenseVoice 返回形如 '<|zh|>' 的标签，去掉尖括号
        def clean(tag):
            if not tag:
                return ""
            return tag.replace("<|", "").replace("|>", "").strip()
        text = (r.text or "").strip()
        lang = clean(r.lang)
        emo = clean(r.emotion)
        evt = clean(r.event)
        return text, lang, emo, evt

    def _transcribe_whisper(self, audio):
        segs, info = self._whisper.transcribe(
            audio,
            language=None,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text = "".join(x.text for x in segs).strip()
        lang = info.language or ""
        # 中文再跑一遍，强制简体 + 领域词
        if lang == "zh" and text:
            segs2, _ = self._whisper.transcribe(
                audio, language="zh", beam_size=1, vad_filter=False,
                condition_on_previous_text=False, initial_prompt=ZH_PROMPT,
            )
            t2 = "".join(x.text for x in segs2).strip()
            if t2:
                text = t2
        return text, lang, "", ""


# ================================================================ 音频采集 + VAD
class AudioCapture(threading.Thread):
    """持续采集麦克风，用能量 VAD 切句，把句子丢进队列"""

    def __init__(self, on_sentence, on_level, on_status, device=None):
        super().__init__(daemon=True)
        self.on_sentence = on_sentence
        self.on_level = on_level
        self.on_status = on_status
        self.device = device
        self._stop = threading.Event()

        self.buf = []                 # 当前句子的音频块
        self.speech_ms = 0            # 已累积的语音时长
        self.silence_ms = 0           # 尾部静音时长
        self.active = False           # 是否在说话

    def run(self):
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="float32", blocksize=BLOCK_SIZE, device=self.device,
            ) as stream:
                self.on_status("麦克风已打开，开始监听")
                while not self._stop.is_set():
                    block, overflowed = stream.read(BLOCK_SIZE)
                    if self._stop.is_set():
                        break
                    mono = block[:, 0].copy()
                    self._process(mono)
        except Exception as e:
            self.on_status(f"麦克风错误: {e}")

    def _process(self, block):
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        self.on_level(rms)

        voiced = rms > VAD_MIN_RMS

        if voiced:
            self.active = True
            self.silence_ms = 0
            self.buf.append(block)
            self.speech_ms += BLOCK_MS
        else:
            if self.active:
                self.buf.append(block)      # 尾部静音也留着，识别更准
                self.silence_ms += BLOCK_MS

        # 触发切句
        cut = False
        if self.active and self.silence_ms >= VAD_HANG_MS:
            cut = True
        elif self.active and self.speech_ms >= VAD_MAX_MS:
            cut = True

        if cut:
            audio = np.concatenate(self.buf) if self.buf else None
            speech = self.speech_ms
            self.buf = []
            self.speech_ms = 0
            self.silence_ms = 0
            self.active = False
            if audio is not None and speech >= VAD_MIN_MS:
                self.on_sentence(audio)

    def stop(self):
        self._stop.set()


# ================================================================ 主界面
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hearizon Sync")
        self.geometry("1180x780")
        self.minsize(980, 680)
        self.configure(bg=C_BG)
        self._set_window_icon()

        self.capture = None
        self.recognizer = Recognizer(on_status=self._status_threadsafe)
        self.translator = Translator()                       # DeepSeek 云端
        self.local_tr = LocalTranslator(on_status=self._status_threadsafe)  # opus-mt 本地
        self.tr_mode = tk.StringVar(value=TRANSLATOR)        # auto/local/deepseek

        self.work_q = queue.Queue()      # 待处理的音频
        self.ui_q = queue.Queue()        # 待更新的界面消息

        self.running = False
        self.worker = None
        self.stats = {"sentences": 0, "chars_in": 0, "chars_out": 0}
        self.t_start = None

        self._build_ui()
        self._check_env()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._pump_ui)

    # ------------------------------------------------------------ UI
    def _set_window_icon(self):
        """设置窗口左上角图标。

        Windows 上最可靠的是 iconbitmap（走 WM_SETICON/ICON_SMALL），
        iconphoto 作为补充（某些 Tk 版本对 PNG 支持更好）。
        """
        import base64
        import tempfile

        # ---- 1) 首选：iconbitmap + 真实 .ico 文件 ----
        ico_candidates = [
            os.path.join(_app_dir(), "app.ico"),
            os.path.join(getattr(sys, "_MEIPASS", ""), "app.ico"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"),
        ]
        for ico in ico_candidates:
            if ico and os.path.exists(ico):
                try:
                    self.iconbitmap(default=ico)
                    self.iconbitmap(ico)
                    return
                except Exception:
                    pass

        # ---- 2) 兜底：把内嵌 PNG 写到临时 .ico 再用 ----
        try:
            raw = base64.b64decode(_ICON_B64)
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            with open(tmp, "wb") as f:
                f.write(raw)
            from PIL import Image
            ico_tmp = tmp.replace(".png", ".ico")
            Image.open(tmp).save(ico_tmp, format="ICO",
                                 sizes=[(16,16),(32,32),(48,48),(64,64),(256,256)])
            self.iconbitmap(ico_tmp)
            return
        except Exception:
            pass

        # ---- 3) 最后：iconphoto（注意 data 要传 base64 字符串，不是 bytes）----
        try:
            self._icon_img = tk.PhotoImage(data=_ICON_B64)
            self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build_ui(self):
        """Claude / DSH 风格：左侧栏 + 主内容区"""
        # 整体：左栏 220px + 右侧自适应
        shell = tk.Frame(self, bg=C_BG)
        shell.pack(fill=tk.BOTH, expand=True)

        # ============ 左侧栏 ============
        side = tk.Frame(shell, bg=C_SIDEBAR, width=228)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)

        # --- 品牌区 ---
        brand = tk.Frame(side, bg=C_SIDEBAR)
        brand.pack(fill=tk.X, padx=18, pady=(22, 26))

        logo = tk.Canvas(brand, width=34, height=34, bg=C_SIDEBAR,
                         highlightthickness=0)
        logo.pack(side=tk.LEFT)
        logo.create_oval(1, 1, 33, 33, fill=C_ACCENT, outline="")
        logo.create_text(17, 17, text="H", fill="#03211A",
                         font=("Segoe UI", 16, "bold"))

        tb = tk.Frame(brand, bg=C_SIDEBAR)
        tb.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(tb, text="Hearizon Sync", bg=C_SIDEBAR, fg=C_TEXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(tb, text="实时字幕翻译", bg=C_SIDEBAR, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")

        # --- 开始 / 停止 ---
        self.btn_start = tk.Button(
            side, text="▶  开始同传", command=self._toggle,
            bg=C_ACCENT, fg="#03211A", relief=tk.FLAT, bd=0,
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2", activebackground=C_ACCENT2,
            activeforeground="#03211A")
        self.btn_start.pack(fill=tk.X, padx=18, pady=(0, 8), ipady=9)

        # --- 状态行 ---
        st = tk.Frame(side, bg=C_SIDEBAR)
        st.pack(fill=tk.X, padx=20, pady=(0, 4))
        self.status_dot = tk.Canvas(st, width=7, height=7, bg=C_SIDEBAR,
                                    highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, pady=4)
        self._dot = self.status_dot.create_oval(0, 0, 7, 7,
                                                fill=C_FAINT, outline="")
        self.status_lbl = tk.Label(st, text="未开始", bg=C_SIDEBAR, fg=C_DIM,
                                   font=("Microsoft YaHei UI", 9))
        self.status_lbl.pack(side=tk.LEFT, padx=6)

        # --- 音量条 ---
        lv = tk.Frame(side, bg=C_SIDEBAR)
        lv.pack(fill=tk.X, padx=20, pady=(6, 0))
        tk.Label(lv, text="输入电平", bg=C_SIDEBAR, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        self.level_canvas = tk.Canvas(lv, height=5, bg=C_SIDEBAR,
                                      highlightthickness=0)
        self.level_canvas.pack(fill=tk.X, pady=(4, 0))
        self.level_canvas.create_rectangle(0, 0, 400, 5, fill=C_BORDER, outline="",
                                           tags="track")
        self.level_bar = self.level_canvas.create_rectangle(
            0, 0, 0, 5, fill=C_LISTEN, outline="")
        self.level_canvas.bind(
            "<Configure>",
            lambda e: self.level_canvas.coords("track", 0, 0, e.width, 5))

        # --- 分隔线 ---
        def divider():
            f = tk.Frame(side, bg=C_BORDER, height=1)
            f.pack(fill=tk.X, padx=18, pady=16)

        divider()

        # --- 设置区 ---
        def field(label, widget_factory):
            box = tk.Frame(side, bg=C_SIDEBAR)
            box.pack(fill=tk.X, padx=18, pady=(0, 12))
            tk.Label(box, text=label, bg=C_SIDEBAR, fg=C_DIM,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(0, 4))
            w = widget_factory(box)
            w.pack(fill=tk.X)
            return w

        # 麦克风
        self.dev_var = tk.StringVar()
        self.dev_cb = field("麦克风", lambda p: ttk.Combobox(
            p, textvariable=self.dev_var, state="readonly",
            font=("Microsoft YaHei UI", 9)))
        self._refresh_devices()

        tk.Button(side, text="刷新设备", command=self._refresh_devices,
                  bg=C_CARD, fg=C_DIM, relief=tk.FLAT, bd=0,
                  font=("Microsoft YaHei UI", 8), cursor="hand2",
                  activebackground=C_CARD_HOV, activeforeground=C_TEXT2
                  ).pack(fill=tk.X, padx=18, pady=(0, 14), ipady=5)

        # 译成
        self.lang_var = tk.StringVar(value="中文")
        field("译成", lambda p: ttk.Combobox(
            p, textvariable=self.lang_var, state="readonly",
            values=["中文", "English", "日本語", "한국어"],
            font=("Microsoft YaHei UI", 9)))

        # 翻译
        field("翻译引擎", lambda p: ttk.Combobox(
            p, textvariable=self.tr_mode, state="readonly",
            values=TRANSLATOR_CHOICES, font=("Microsoft YaHei UI", 9)))

        # 识别引擎
        self.model_var = tk.StringVar(value=ENGINE)
        cb = field("识别引擎", lambda p: ttk.Combobox(
            p, textvariable=self.model_var, state="readonly",
            values=ENGINE_CHOICES, font=("Microsoft YaHei UI", 9)))
        cb.bind("<<ComboboxSelected>>", self._on_model_change)

        divider()

        # --- 操作按钮 ---
        for text, cmd in [
            ("测麦克风", self._test_mic),
            ("保存记录", self._save),
            ("清空字幕", self._clear),
        ]:
            tk.Button(side, text=text, command=cmd,
                      bg=C_SIDEBAR, fg=C_TEXT2, relief=tk.FLAT, bd=0,
                      font=("Microsoft YaHei UI", 9), cursor="hand2",
                      activebackground=C_CARD, activeforeground=C_TEXT,
                      anchor="w").pack(fill=tk.X, padx=14, pady=1, ipady=6)

        # --- 底部统计 ---
        self.stat_lbl = tk.Label(side, text="", bg=C_SIDEBAR, fg=C_FAINT,
                                 font=("Consolas", 8), justify="left")
        self.stat_lbl.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=14)

        # ============ 右侧主区 ============
        main = tk.Frame(shell, bg=C_BG)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 顶部标题条 ---
        head = tk.Frame(main, bg=C_BG, height=52)
        head.pack(fill=tk.X, padx=24, pady=(0, 0))
        head.pack_propagate(False)
        tk.Label(head, text="字幕", bg=C_BG, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side=tk.LEFT, pady=16)
        self.hint_lbl = tk.Label(head, text="点左侧「开始同传」后开始监听",
                                 bg=C_BG, fg=C_FAINT,
                                 font=("Microsoft YaHei UI", 9))
        self.hint_lbl.pack(side=tk.LEFT, padx=12, pady=16)

        # --- 字幕卡片容器 ---
        wrap = tk.Frame(main, bg=C_BG)
        wrap.pack(fill=tk.BOTH, expand=True, padx=24, pady=(4, 20))

        def card(parent, title, color, size, bold=False, expand=True, pady=(0, 10)):
            outer = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                             highlightthickness=1)
            outer.pack(fill=tk.BOTH, expand=expand, pady=pady)

            hd = tk.Frame(outer, bg=C_PANEL)
            hd.pack(fill=tk.X, padx=18, pady=(13, 0))
            d = tk.Canvas(hd, width=5, height=5, bg=C_PANEL, highlightthickness=0)
            d.pack(side=tk.LEFT, pady=6)
            d.create_oval(0, 0, 5, 5, fill=color, outline="")
            tk.Label(hd, text=title, bg=C_PANEL, fg=C_DIM,
                     font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=7)

            t = scrolledtext.ScrolledText(
                outer, bg=C_PANEL, fg=color, relief=tk.FLAT, bd=0,
                font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
                wrap=tk.WORD, height=6, insertbackground=C_TEXT,
                spacing1=6, spacing3=6, padx=6, pady=4,
                selectbackground=C_ACCENT, selectforeground="#03211A")
            t.pack(fill=tk.BOTH, expand=True, padx=18, pady=(8, 16))
            t.tag_configure("ts", foreground=C_FAINT, font=("Consolas", 8))
            return t

        self.src_text = card(wrap, "原文 · 识别结果", C_SRC, 14, pady=(0, 10))
        self.dst_text = card(wrap, "译文", C_DST, 18, bold=True, pady=(0, 0))
        self.dst_text.tag_configure("partial", foreground="#2A7A5E")
        self.dst_text.mark_set("partial_start", "end-1c")
        self.dst_text.mark_gravity("partial_start", tk.LEFT)

    def _refresh_devices(self):
        names = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    names.append(f"[{i}] {d['name'][:40]}")
        except Exception:
            pass
        self.dev_cb["values"] = names
        if names and not self.dev_var.get():
            try:
                default = sd.default.device[0]
                pick = next((n for n in names if n.startswith(f"[{default}]")), names[0])
            except Exception:
                pick = names[0]
            self.dev_var.set(pick)

    def _picked_device(self):
        s = self.dev_var.get()
        if s.startswith("["):
            try:
                return int(s[1:s.index("]")])
            except Exception:
                return None
        return None

    # ------------------------------------------------------------ 环境检查
    def _check_env(self):
        if not self.translator.available():
            self._log("未找到 DEEPSEEK_API_KEY，翻译功能不可用", C_RED)
            messagebox.showwarning(
                "缺少 API Key",
                "没有找到 DEEPSEEK_API_KEY。\n\n"
                "翻译功能需要它。设置方法：\n"
                "  1. 设置系统环境变量 DEEPSEEK_API_KEY\n"
                "  2. 或写进 ~/.dsh/.credentials.yaml\n\n"
                "识别功能不需要 key，可以先试。")
        else:
            self._log("已找到 DeepSeek API Key", C_GREEN)

    def _on_model_change(self, _evt=None):
        """切识别引擎：卸载旧模型，下次识别时重新加载"""
        name = self.model_var.get()
        if self.running:
            self._stop()
        self.recognizer.set_engine(name)
        self._status_threadsafe(f"已切换引擎为 {name}，下次开始会重新加载")

    # ------------------------------------------------------------ 开关
    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        dev = self._picked_device()
        self.running = True
        self.t_start = time.time()
        self.btn_start.configure(text="■ 停止", bg=C_RED, activebackground="#ef4444")
        self.status_lbl.configure(text="● 启动中...", fg=C_YELLOW)

        # 识别模型后台加载
        threading.Thread(target=self._warmup, daemon=True).start()

        # 音频采集
        self.capture = AudioCapture(
            on_sentence=lambda a: self.work_q.put(a),
            on_level=self._on_level,
            on_status=self._status_threadsafe,
            device=dev,
        )
        self.capture.start()

        # 处理线程
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def _warmup(self):
        try:
            self.recognizer.load()
            self._status_threadsafe("识别模型已就绪，请开始说话")
        except Exception as e:
            self._status_threadsafe(f"模型加载失败: {e}")

    def _stop(self):
        self.running = False
        if self.capture:
            self.capture.stop()
            self.capture = None
        self.btn_start.configure(text="▶ 开始同传", bg=C_GREEN, activebackground="#86efac")
        self.status_lbl.configure(text="● 已停止", fg=C_DIM)
        self._log("已停止")

    # ------------------------------------------------------------ 后台处理
    def _worker_loop(self):
        while self.running:
            try:
                audio = self.work_q.get(timeout=0.3)
            except queue.Empty:
                continue
            if not self.running:
                break
            try:
                self._handle_sentence(audio)
            except Exception:
                self._log("处理出错:\n" + traceback.format_exc(), C_RED)

    def _handle_sentence(self, audio):
        t0 = time.time()

        # 1) 识别
        self.ui_q.put(("status", "识别中..."))
        text, lang, emo, evt = self.recognizer.transcribe(audio)
        t_asr = time.time() - t0

        if not text.strip():
            self.ui_q.put(("status", "未识别到内容"))
            return

        ts = datetime.now().strftime("%H:%M:%S")
        self.ui_q.put(("src", ts, text, lang, emo, evt))
        self.stats["sentences"] += 1
        self.stats["chars_in"] += len(text)

        # 2) 翻译（流式）
        target = self.lang_var.get()
        self.ui_q.put(("status", "翻译中..."))

        # 先插一行占位
        self.ui_q.put(("dst_begin", ts, ""))

        chunks = []
        def on_chunk(d):
            chunks.append(d)
            self.ui_q.put(("dst_chunk", "".join(chunks)))

        t1 = time.time()
        full, used = self._do_translate(text, target, on_chunk)
        t_mt = time.time() - t1

        self.ui_q.put(("dst_end", full))
        self.stats["chars_out"] += len(full)

        total = time.time() - t0
        self.ui_q.put(("status",
                       f"识别 {t_asr:.1f}s + 翻译 {t_mt:.1f}s = {total:.1f}s"))

    def _do_translate(self, text, target, on_chunk):
        """按当前模式选择翻译引擎，返回 (译文, 实际用的引擎名)"""
        mode = self.tr_mode.get()

        # 本地模式：只用 opus-mt
        if mode == "local":
            try:
                return self.local_tr.translate(text, target, on_chunk), "本地"
            except Exception as e:
                return f"[本地翻译失败: {e}]", "本地"

        # 云端模式：只用 DeepSeek
        if mode == "deepseek":
            if not self.translator.available():
                return "[未配置 API Key]", "云端"
            return self.translator.translate(text, target, on_chunk), "云端"

        # auto 模式：有网用云端，失败自动降级到本地
        if self.translator.available():
            try:
                r = self.translator.translate(text, target, on_chunk)
                if r and not r.startswith("[翻译失败"):
                    return r, "云端"
            except Exception:
                pass
        # 降级
        try:
            self._status_threadsafe("云端不可用，已降级到本地翻译")
            return self.local_tr.translate(text, target, on_chunk), "本地(降级)"
        except Exception as e:
            return f"[翻译失败: {e}]", "无"

    # ------------------------------------------------------------ UI 消息泵
    def _pump_ui(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                self._apply(msg)
        except queue.Empty:
            pass
        self.after(60, self._pump_ui)

    def _apply(self, msg):
        kind = msg[0]
        if kind == "status":
            self.status_lbl.configure(text="● " + msg[1], fg=C_ACCENT)
        elif kind == "src":
            _, ts, text, lang, emo, evt = msg
            tag = LANG_NAMES.get(lang, lang or "?")
            extra = ""
            if emo and emo.upper() not in ("NEUTRAL", "EMO_UNKNOWN", "UNKNOWN", ""):
                extra += f" [{emo}]"
            self.src_text.insert(tk.END, f"[{ts}] ", "ts")
            self.src_text.insert(tk.END, f"({tag}){extra} ", "ts")
            self.src_text.insert(tk.END, f"{text}\n")
            self.src_text.see(tk.END)
        elif kind == "dst_begin":
            _, ts, _ = msg
            self.dst_text.insert(tk.END, f"[{ts}] ", "ts")
            self.dst_text.mark_set("partial_start", "end-1c")
            self.dst_text.mark_gravity("partial_start", tk.LEFT)
        elif kind == "dst_chunk":
            # 删掉上次的部分译文，重画（只删到末尾，不动历史）
            self.dst_text.delete("partial_start", "end-1c")
            self.dst_text.insert(tk.END, msg[1], "partial")
            self.dst_text.see(tk.END)
        elif kind == "dst_end":
            self.dst_text.delete("partial_start", "end-1c")
            self.dst_text.insert(tk.END, msg[1] + "\n")
            self.dst_text.see(tk.END)
        self._update_stats()

    def _update_stats(self):
        s = self.stats
        dur = int(time.time() - self.t_start) if self.t_start else 0
        self.stat_lbl.configure(
            text=f"句数 {s['sentences']}  |  原文 {s['chars_in']} 字  |  "
                 f"译文 {s['chars_out']} 字  |  已运行 {dur//60:02d}:{dur%60:02d}")

    def _on_level(self, rms):
        # 从音频线程调用，只改一个矩形，开销极小
        try:
            w = int(min(1.0, rms / 0.15) * 130)
            self.level_canvas.coords(self.level_bar, 0, 0, w, 12)
            color = C_LISTEN if rms > VAD_MIN_RMS else "#3a4048"
            self.level_canvas.itemconfig(self.level_bar, fill=color)
        except Exception:
            pass

    def _status_threadsafe(self, text):
        self.ui_q.put(("status", text))

    def _log(self, text, color=None):
        # 简化：状态栏 + 控制台
        print(text)
        if color == C_RED:
            self.status_lbl.configure(text="● " + text[:40], fg=C_RED)

    # ------------------------------------------------------------ 按钮
    def _clear(self):
        self.src_text.delete("1.0", tk.END)
        self.dst_text.delete("1.0", tk.END)
        self.dst_text.mark_set("partial_start", "end-1c")
        self.dst_text.mark_gravity("partial_start", tk.LEFT)
        self.stats = {"sentences": 0, "chars_in": 0, "chars_out": 0}
        self.t_start = time.time() if self.running else None
        self._update_stats()

    def _save(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"同传记录_{ts}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("===== 原文 =====\n")
                f.write(self.src_text.get("1.0", tk.END))
                f.write("\n===== 译文 =====\n")
                f.write(self.dst_text.get("1.0", tk.END))
            messagebox.showinfo("已保存", path)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _test_mic(self):
        """录 3 秒，看能不能收到声音"""
        def run():
            try:
                dev = self._picked_device()
                rec = sd.rec(int(3 * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                             channels=1, dtype="float32", device=dev)
                sd.wait()
                rms = float(np.sqrt(np.mean(rec.astype(np.float64) ** 2)))
                peak = float(np.abs(rec).max())
                msg = (f"录音 3 秒：\nRMS = {rms:.4f}\n峰值 = {peak:.4f}\n\n"
                       f"{'✅ 有声音' if rms > VAD_MIN_RMS else '⚠️ 几乎是静音，检查麦克风权限/设备选择'}")
                self.after(0, lambda: messagebox.showinfo("麦克风测试", msg))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("测试失败", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _on_close(self):
        self.running = False
        if self.capture:
            self.capture.stop()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
