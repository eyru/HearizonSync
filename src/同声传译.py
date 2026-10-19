#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同声传译 —— 麦克风实时字幕翻译
============================================================
工作流：
    麦克风 → VAD 切段 → Whisper 识别 → DeepSeek 翻译 → 屏幕字幕

设计要点：
    1. 麦克风持续采集，用「语音活动检测」自动切句（不是固定时长切）
    2. 识别与翻译在后台线程跑，界面永不卡死
    3. 译文流式输出，说一句翻一句
    4. 全部本地识别（Whisper），只有翻译那一步走 DeepSeek API

用法：
    双击「启动同传.bat」，或 .venv\\Scripts\\python.exe 同声传译.py
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
C_BG      = "#14151a"
C_PANEL   = "#1e2028"
C_TEXT    = "#e8eaed"
C_DIM     = "#8b90a0"
C_SRC     = "#9aa4b8"        # 原文颜色
C_DST     = "#7dd3fc"        # 译文颜色
C_ACCENT  = "#38bdf8"
C_GREEN   = "#4ade80"
C_YELLOW  = "#fbbf24"
C_RED     = "#f87171"
C_LISTEN  = "#22c55e"


# ================================================================ 本地翻译器
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
            f"你是专业同声传译。把用户给的内容翻译成{target_lang}。\n"
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
        self.title("同声传译")
        self.geometry("1080x760")
        self.minsize(900, 640)
        self.configure(bg=C_BG)

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
    def _build_ui(self):
        # ===== 顶部控制条 =====
        top = tk.Frame(self, bg=C_PANEL, height=62)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)

        tk.Label(top, text="麦克风", bg=C_PANEL, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(14, 4), pady=18)
        self.dev_var = tk.StringVar()
        self.dev_cb = ttk.Combobox(top, textvariable=self.dev_var, width=34,
                                   state="readonly", font=("Microsoft YaHei UI", 9))
        self.dev_cb.pack(side=tk.LEFT, pady=18)
        self._refresh_devices()

        tk.Button(top, text="刷新", command=self._refresh_devices,
                  bg=C_PANEL, fg=C_ACCENT, relief=tk.FLAT, bd=0,
                  font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side=tk.LEFT, padx=6)

        tk.Label(top, text="译成", bg=C_PANEL, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(10, 4))
        self.lang_var = tk.StringVar(value="中文")
        ttk.Combobox(top, textvariable=self.lang_var, width=7, state="readonly",
                     values=["中文", "English", "日本語", "한국어"],
                     font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        tk.Label(top, text="翻译", bg=C_PANEL, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(10, 4))
        ttk.Combobox(top, textvariable=self.tr_mode, width=9, state="readonly",
                     values=TRANSLATOR_CHOICES,
                     font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        tk.Label(top, text="引擎", bg=C_PANEL, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(10, 4))
        self.model_var = tk.StringVar(value=ENGINE)
        cb = ttk.Combobox(top, textvariable=self.model_var, width=12, state="readonly",
                          values=ENGINE_CHOICES, font=("Microsoft YaHei UI", 9))
        cb.pack(side=tk.LEFT)
        cb.bind("<<ComboboxSelected>>", self._on_model_change)

        self.btn_start = tk.Button(top, text="▶ 开始同传", command=self._toggle,
                                   bg=C_GREEN, fg="#06210f", relief=tk.FLAT, bd=0,
                                   font=("Microsoft YaHei UI", 11, "bold"),
                                   width=11, cursor="hand2")
        self.btn_start.pack(side=tk.LEFT, padx=(14, 8), pady=12)

        self.status_lbl = tk.Label(top, text="● 未开始", bg=C_PANEL, fg=C_DIM,
                                   font=("Microsoft YaHei UI", 9))
        self.status_lbl.pack(side=tk.LEFT, padx=6)

        # 音量条
        self.level_canvas = tk.Canvas(top, width=130, height=12, bg=C_BG,
                                      highlightthickness=0)
        self.level_canvas.pack(side=tk.LEFT, padx=10)
        self.level_bar = self.level_canvas.create_rectangle(
            0, 0, 0, 12, fill=C_LISTEN, outline="")

        # ===== 字幕区（上下分栏）=====
        body = tk.Frame(self, bg=C_BG)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(8, 0))

        # 原文
        src_box = tk.Frame(body, bg=C_PANEL)
        src_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 6))
        tk.Label(src_box, text="原文（识别结果）", bg=C_PANEL, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=12, pady=(8, 0))
        self.src_text = scrolledtext.ScrolledText(
            src_box, bg="#1a1c22", fg=C_SRC, relief=tk.FLAT, bd=0,
            font=("Microsoft YaHei UI", 15), wrap=tk.WORD, height=8,
            insertbackground=C_TEXT, spacing1=4, spacing3=4)
        self.src_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 12))
        self.src_text.tag_configure("ts", foreground="#5a6070",
                                    font=("Consolas", 9))

        # 译文
        dst_box = tk.Frame(body, bg=C_PANEL)
        dst_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        tk.Label(dst_box, text="译文", bg=C_PANEL, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=12, pady=(8, 0))
        self.dst_text = scrolledtext.ScrolledText(
            dst_box, bg="#16181e", fg=C_DST, relief=tk.FLAT, bd=0,
            font=("Microsoft YaHei UI", 19, "bold"), wrap=tk.WORD, height=8,
            insertbackground=C_TEXT, spacing1=6, spacing3=6)
        self.dst_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 12))
        self.dst_text.tag_configure("ts", foreground="#4a5060",
                                    font=("Consolas", 9))
        self.dst_text.tag_configure("partial", foreground="#5b7a99")
        # 流式译文的锚点：每次重画前从这里删到末尾，保证不重复不残留
        self.dst_text.mark_set("partial_start", "end-1c")
        self.dst_text.mark_gravity("partial_start", tk.LEFT)

        # ===== 底部 =====
        bottom = tk.Frame(self, bg=C_PANEL, height=52)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=8)
        bottom.pack_propagate(False)

        self.stat_lbl = tk.Label(bottom, text="", bg=C_PANEL, fg=C_DIM,
                                 font=("Consolas", 9))
        self.stat_lbl.pack(side=tk.LEFT, padx=8)

        for text, cmd in [
            ("清空", self._clear),
            ("保存记录", self._save),
            ("测麦克风", self._test_mic),
        ]:
            tk.Button(bottom, text=text, command=cmd, bg="#374151", fg="white",
                      relief=tk.FLAT, bd=0, font=("Microsoft YaHei UI", 9),
                      width=10, cursor="hand2").pack(side=tk.RIGHT, padx=4, pady=10)

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
