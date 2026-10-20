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
import hashlib
import uuid
import urllib.parse
import urllib.request
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
    "SUVORK5CYII="
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


def _find_opus_dir():
    """⚠️ sentencepiece 的 C++ 层打不开中文路径（会报 NOT_FOUND），
    所以 opus 模型必须优先 ASCII 路径，不能让工作区中文目录排在前面。"""
    cands = [
        os.environ.get("SLM_OPUS_EN_ZH_DIR", ""),
        "D:" + chr(92) + "slm-models" + chr(92) + "opus_en_zh",   # 纯 ASCII 优先
        os.path.join(_app_dir(), "models", "opus_en_zh"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "opus_en_zh"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            # 命中中文路径时不采用（除非确实是 ASCII）
            try:
                c.encode("ascii")
            except UnicodeEncodeError:
                continue
            return c
    # 全都没命中 ASCII 路径，退回原查找（报错信息里能看到）
    return _find_model_dir("opus_en_zh")


OPUS_DIR = _find_opus_dir()

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
TRANSLATOR_CHOICES = ["auto", "local", "deepseek", "youdao"]
TRANSLATOR = "auto"

# ---- GPU 加速（可选）----
# ⚠️ 重要事实：sherpa-onnx 的 pip 包是 CPU-only 编译，
#    provider="cuda" 会静默回退 CPU，SenseVoice 无法 GPU 加速。
#    能吃到 GPU 的只有 ctranslate2 后端：Whisper 识别 + opus-mt 本地翻译。
GPU_ACCEL = False          # 界面可切换；只对 whisper 引擎与本地翻译生效

# ---- 有道智云（第二云端引擎）----
# 申请：https://ai.youdao.com/  → 创建应用 → 拿 appKey / appSecret
# 签名 v3：sha256(appKey + input + salt + curtime + appSecret)
YOUDAO_API = "https://openapi.youdao.com/api"
YOUDAO_APP_KEY = ""
YOUDAO_APP_SECRET = ""
YOUDAO_DOMAIN = "general"      # general / medicine / computers / finance / game
CONF_PATH = os.path.join(os.path.expanduser("~"), ".hearizon_sync.json")

# ================================================================ 语言矩阵
# 每项：(界面显示名, SenseVoice 代码, Whisper 代码)
#   SenseVoice 模型原生支持：中 / 英 / 日 / 韩 / 粤
#   ⚠️ 但本版 sherpa-onnx 的 language 参数无效，日/韩锁定不住（会混淆），
#      所以实际走向：中/英/粤 → SenseVoice；日/韩/德/法/意/西 → Whisper
#   德 / 法 / 意 / 西 SenseVoice 模型本身不支持
INPUT_LANGS = [
    ("自动检测", "auto", None),
    ("英语",     "en",   "en"),
    ("中文",     "zh",   "zh"),
    ("日语",     "ja",   "ja"),
    ("韩语",     "ko",   "ko"),
    ("粤语",     "yue",  "yue"),
    ("德语",     None,   "de"),
    ("法语",     None,   "fr"),
    ("意大利语", None,   "it"),
    ("西班牙语", None,   "es"),
]
INPUT_LANG_NAMES = [x[0] for x in INPUT_LANGS]
# 这些语言必须走 Whisper。原因分两类：
#   ① 德/法/意/西：SenseVoice 模型本身不支持
#   ② 日/韩：实测 sherpa-onnx 1.13.8 的 from_sense_voice(language=...) 参数【无效】
#      （传入 ko 仍输出 en 标签），模型永远走自动检测，韩语会被误判成日语。
#      Whisper 的 language 参数可靠，所以日韩也归到这边。
SV_UNSUPPORTED = {"日语", "韩语", "德语", "法语", "意大利语", "西班牙语"}

# 目标语言：(界面显示名, 有道代码, 本地 opus-mt 是否支持)
OUTPUT_LANGS = [
    ("中文",       "zh-CHS", True),
    ("English",    "en",     False),
    ("日本語",     "ja",     False),
    ("한국어",     "ko",     False),
    ("Deutsch",    "de",     False),
    ("Français",   "fr",     False),
    ("Italiano",   "it",     False),
    ("Español",    "es",     False),
]
OUTPUT_LANG_NAMES = [x[0] for x in OUTPUT_LANGS]

# 识别结果语言 → 中文名（给界面显示用）
LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "yue": "粤语", "de": "德语", "fr": "法语", "it": "意大利语",
    "es": "西班牙语", "auto": "自动",
}

# ---- 流式识别参数 ----
STREAM_ENABLED   = True     # 主模式：边说边出字
STREAM_WINDOW_S  = 6.0      # 每次重识别只取最近 6 秒（覆盖大多数口语句子）
STREAM_INTERVAL  = 0.32     # 每 320ms 触发一次
STREAM_JITTER    = 2        # 临时结果新增字符数 ≤ 此值则不刷新（去抖）
STREAM_MIN_S     = 0.30     # 短于此不出临时结果

# ---- 有道语言对映射（目标语言名 → 有道代码）----
def output_code(name):
    for disp, code, _ in OUTPUT_LANGS:
        if disp == name:
            return code
    return "zh-CHS"


def input_codes(name):
    """返回 (sensevoice_code, whisper_code)"""
    for disp, sv, wh in INPUT_LANGS:
        if disp == name:
            return sv, wh
    return "auto", None

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
# ================================================================ 配置持久化
def load_conf():
    """读取本地配置（有道 key 等）"""
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(d):
    try:
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def apply_conf():
    """把配置写进全局变量"""
    global YOUDAO_APP_KEY, YOUDAO_APP_SECRET, YOUDAO_DOMAIN, GPU_ACCEL
    c = load_conf()
    YOUDAO_APP_KEY = c.get("youdao_app_key", "") or ""
    YOUDAO_APP_SECRET = c.get("youdao_app_secret", "") or ""
    YOUDAO_DOMAIN = c.get("youdao_domain", "general") or "general"
    GPU_ACCEL = bool(c.get("gpu_accel", False))


# ================================================================ GPU 探测
def cuda_probe():
    """真实探测 CUDA 是否可用（不只是「有显卡」，还要 CUDA 运行时齐全）

    ctranslate2 需要 cublas64_12.dll / cudnn 等。只装驱动、没装 CUDA Toolkit
    的机器上 get_cuda_device_count() 会返回 1，但实际加载会失败。
    返回 (可用, 说明)
    """
    try:
        import ctranslate2
    except Exception as e:
        return False, f"ctranslate2 不可用：{e}"
    try:
        cnt = ctranslate2.get_cuda_device_count()
    except Exception:
        cnt = 0
    if cnt <= 0:
        return False, "未检测到 NVIDIA 显卡"
    # 必须真跑一次推理：创建 Translator 是懒加载，不碰 CUDA 就不会报缺 DLL
    try:
        import sentencepiece as spm
        tr = ctranslate2.Translator(OPUS_DIR, device="cuda", compute_type="int8_float16")
        sp_src = spm.SentencePieceProcessor(
            model_file=os.path.join(OPUS_DIR, "source.spm"))
        toks = sp_src.encode("hello", out_type=str) + ["</s>"]
        tr.translate_batch([toks], beam_size=1, max_decoding_length=16)
        del tr
        return True, "CUDA 可用"
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "cublas" in low or "cudnn" in low or "cudart" in low:
            return False, "缺少 CUDA 12 运行时（cublas64_12.dll / cudnn），需安装 CUDA Toolkit 12.x"
        return False, msg[:90]


# ================================================================ 文本有效性（修「句号被翻译」）
_PUNCT_ONLY = set("。．.，,！!？?；;：:、…—－-~～·“”‘’「」（）()[]【】{}《》<> ")



def is_meaningful(text):
    """识别结果是否值得翻译：只有标点/空白的一律丢弃"""
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    # 去掉所有标点与空白后还有没有实际内容
    for ch in t:
        if ch not in _PUNCT_ONLY:
            return True
    return False


# ================================================================ 有道智云翻译（第二云端引擎）
class YoudaoTranslator:
    """有道智云文本翻译 API

    签名 v3：sha256(appKey + input + salt + curtime + appSecret)
    ⚠️ input 是「截断文本」不是原文：
         len(q) <= 20 → input = q
         len(q) >  20 → input = q[:10] + str(len(q)) + q[-10:]
    """

    def __init__(self, on_status=None):
        self.on_status = on_status or (lambda s: None)

    def available(self):
        return bool(YOUDAO_APP_KEY and YOUDAO_APP_SECRET)

    def _sign(self, q, salt, curtime):
        n = len(q)
        inp = q if n <= 20 else q[:10] + str(n) + q[-10:]
        raw = YOUDAO_APP_KEY + inp + salt + curtime + YOUDAO_APP_SECRET
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def translate(self, text, target_lang="中文", on_chunk=None, timeout=20):
        if not text.strip():
            return ""
        if not self.available():
            return "[未配置有道 Key]"

        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))
        params = {
            "q": text,
            "from": "auto",
            "to": output_code(target_lang),
            "appKey": YOUDAO_APP_KEY,
            "salt": salt,
            "sign": self._sign(text, salt, curtime),
            "signType": "v3",
            "curtime": curtime,
        }
        if YOUDAO_DOMAIN and YOUDAO_DOMAIN != "general":
            params["domain"] = YOUDAO_DOMAIN

        url = YOUDAO_API + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HearizonSync/1.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return f"[有道翻译失败: {e}]"

        code = str(data.get("errorCode", "0"))
        if code != "0":
            msg = {
                "101": "缺少必填参数", "102": "不支持的语言", "103": "翻译文本过长",
                "108": "应用 ID 无效", "110": "无相关服务", "202": "签名校验失败",
                "203": "访问 IP 不在白名单", "401": "账户已欠费",
                "411": "访问频率受限", "412": "长请求频率受限",
            }.get(code, "未知错误")
            return f"[有道 {code}: {msg}]"

        tr = data.get("translation") or []
        out = tr[0] if tr else ""
        if on_chunk and out:
            on_chunk(out)
        return out


# ================================================================ 云端翻译器（DeepSeek）
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

    ⚠️ 语言覆盖差异（v1.1.0 输入语言选择的依据）：
        SenseVoice 原生：中 / 英 / 日 / 韩 / 粤
        Whisper  额外：德 / 法 / 意 / 西 等
    ⚠️ GPU：sherpa-onnx 的 pip 包为 CPU-only 编译，SenseVoice 无法 GPU；
        Whisper 走 ctranslate2，可吃 CUDA。
    """

    def __init__(self, on_status=None, engine=None):
        self.on_status = on_status or (lambda s: None)
        self.engine = engine or ENGINE
        self.lang_name = "自动检测"
        self._sv = None
        self._whisper = None
        self._whisper_gpu = None      # 记录当前模型是不是 GPU 版
        self._lock = threading.Lock()

    # ---------------- 引擎 / 语言 ----------------
    def set_engine(self, name):
        if name == self.engine and (self._sv is not None or self._whisper is not None):
            return
        self.engine = name
        self._sv = None
        self._whisper = None

    def set_language(self, display_name):
        """设置输入语言；返回是否被迫切换了引擎"""
        self.lang_name = display_name
        sv_code, _ = input_codes(display_name)
        forced = False
        if display_name in SV_UNSUPPORTED:
            # SenseVoice 不支持该语言 → 强制 Whisper
            if self.engine == "sensevoice":
                self.engine = "small"
                self._sv = None
                self._whisper = None
                forced = True
        else:
            # 从 whisper 切回时，如果当前是 whisper 且用户没手动选过，保持
            pass
        # 语言变了，Whisper 需要重建（language 参数在识别时传，不用重建）
        return forced

    def reset(self):
        self._sv = None
        self._whisper = None

    # ---------------- 加载 ----------------
    def load(self):
        sv_code, wh_code = input_codes(self.lang_name)

        if self.engine == "sensevoice":
            if self._sv is not None:
                return
            if sv_code is None:
                # 该语言 SenseVoice 不支持，降级到 whisper
                self.engine = "small"
                self._whisper = None
                return self.load()
            self.on_status("正在加载 SenseVoice 模型...")
            import sherpa_onnx
            if not os.path.exists(SV_MODEL):
                raise FileNotFoundError(
                    f"找不到 SenseVoice 模型：{SV_MODEL}\n请先下载（见使用说明）")
            t0 = time.time()
            self._sv = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=SV_MODEL,
                tokens=SV_TOKENS,
                num_threads=CPU_THREADS,
                use_itn=True,
                language=sv_code if sv_code else "auto",
                provider="cpu",          # sherpa-onnx 为 CPU-only 构建
                debug=False,
            )
            self.on_status(f"SenseVoice 已就绪（{time.time()-t0:.1f}s）")
        else:
            if self._whisper is not None and self._whisper_gpu == GPU_ACCEL:
                return
            size = self.engine if self.engine in ("base", "small") else "small"
            use_gpu = bool(GPU_ACCEL)
            dev = "cuda" if use_gpu else "cpu"
            ctype = "float16" if use_gpu else COMPUTE_TYPE
            mb = {"base": "约 145 MB", "small": "约 480 MB"}.get(size, "")
            self.on_status(f"正在加载 Whisper {size}（{dev}{'' if use_gpu else '，' + ctype}，首次需下载{mb}）...")
            from faster_whisper import WhisperModel
            try:
                self._whisper = WhisperModel(size, device=dev, compute_type=ctype,
                                             cpu_threads=CPU_THREADS)
            except Exception as e:
                self.on_status(f"GPU 加载失败（{e}），回退 CPU")
                self._whisper = WhisperModel(size, device="cpu",
                                             compute_type=COMPUTE_TYPE,
                                             cpu_threads=CPU_THREADS)
                use_gpu = False
            self._whisper_gpu = use_gpu
            self.on_status(f"Whisper {size} 已就绪（{'GPU' if use_gpu else 'CPU'}）")

    # ---------------- 识别 ----------------
    def transcribe(self, audio):
        """返回 (文本, 语言码, 情感, 音频事件)"""
        self.load()
        with self._lock:
            if self.engine == "sensevoice" and self._sv is not None:
                return self._transcribe_sv(audio)
            return self._transcribe_whisper(audio)

    def _transcribe_sv(self, audio):
        s = self._sv.create_stream()
        s.accept_waveform(SAMPLE_RATE, audio)
        self._sv.decode_stream(s)
        r = s.result

        def clean(tag):
            if not tag:
                return ""
            return tag.replace("<|", "").replace("|>", "").strip()

        return (r.text or "").strip(), clean(r.lang), clean(r.emotion), clean(r.event)

    def _transcribe_whisper(self, audio):
        _, wh_code = input_codes(self.lang_name)
        lang_arg = wh_code if wh_code else None
        segs, info = self._whisper.transcribe(
            audio,
            language=lang_arg,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text = "".join(x.text for x in segs).strip()
        lang = info.language or (wh_code or "")
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

    def __init__(self, on_sentence, on_level, on_status, device=None,
                 on_partial=None):
        super().__init__(daemon=True)
        self.on_sentence = on_sentence
        self.on_level = on_level
        self.on_status = on_status
        self.on_partial = on_partial        # 流式：临时结果回调
        self.device = device
        self._stop = threading.Event()

        self.buf = []                 # 当前句子的音频块
        self.speech_ms = 0            # 已累积的语音时长
        self.silence_ms = 0           # 尾部静音时长
        self.active = False           # 是否在说话
        self._last_partial = 0.0      # 上次触发流式识别的时间
        self._tail = deque(maxlen=int(STREAM_WINDOW_S * 1000 / BLOCK_MS) + 4)

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
            self._tail.append(block)
            self.speech_ms += BLOCK_MS
            # ---- 流式：每 STREAM_INTERVAL 秒把「最近 STREAM_WINDOW_S 秒」送一次临时识别 ----
            if self.on_partial and STREAM_ENABLED:
                if self.speech_ms / 1000.0 >= STREAM_MIN_S:
                    now = time.time()
                    if now - self._last_partial >= STREAM_INTERVAL:
                        self._last_partial = now
                        seg = np.concatenate(self._tail) if self._tail else None
                        if seg is not None and len(seg) > 0:
                            self.on_partial(trim_to_valley(seg))
        else:
            if self.active:
                self.buf.append(block)      # 尾部静音也留着，识别更准
                self._tail.append(block)
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
            self._tail.clear()
            self.speech_ms = 0
            self.silence_ms = 0
            self.active = False
            self._last_partial = 0.0
            if audio is not None and speech >= VAD_MIN_MS:
                self.on_sentence(audio)

    def stop(self):
        self._stop.set()


# ================================================================ 流式工具
def trim_to_valley(seg, rate=None, search_ms=260, step_ms=10):
    """把片段起点挪到最近的「能量低谷」，避免窗口边界正好切断一个词。

    只在头部 search_ms 范围内找最安静的位置，保证不会大量丢音频。
    """
    rate = rate or SAMPLE_RATE
    step = int(rate * step_ms / 1000)
    n = int(rate * search_ms / 1000)
    if len(seg) <= n or step <= 0:
        return seg
    head = seg[:n]
    best_i, best_v = 0, 1e9
    for i in range(0, len(head) - step, step):
        v = float(np.mean(head[i:i + step] ** 2))
        if v < best_v:
            best_v, best_i = v, i
    return seg[best_i:] if best_i > 0 else seg


# ================================================================ 流式识别线程
class StreamWorker(threading.Thread):
    """滚动重识别：只处理「最新」片段，自动跳过积压，保证界面实时性。

    实测（Ryzen 9 9955HX，4 秒窗口）：单次约 56ms，
    每 320ms 触发 → 约 17% 单核占用，首字延迟 ≈ 320ms。
    """

    def __init__(self, recognizer, on_partial):
        super().__init__(daemon=True)
        self.recognizer = recognizer
        self.on_partial = on_partial
        self._latest = None
        self._last = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def push(self, audio):
        with self._lock:
            self._latest = audio        # 覆盖：只保留最新，天然跳过积压

    def clear(self):
        with self._lock:
            self._latest = None
            self._last = ""

    def run(self):
        while not self._stop.is_set():
            with self._lock:
                a = self._latest
                self._latest = None
            if a is None:
                time.sleep(0.04)
                continue
            try:
                text, lang, _emo, _evt = self.recognizer.transcribe(a)
                text = text.strip()
                if not text or not is_meaningful(text):
                    continue
                # 窗口已满说明句子超过 6 秒，前面还有内容被截掉 → 加省略号提示
                if len(a) >= int(STREAM_WINDOW_S * SAMPLE_RATE * 0.97):
                    text = "… " + text
                # 去抖：内容完全没变才跳过（否则正常增长会被挡住）
                if text == self._last:
                    continue
                self._last = text
                self.on_partial(text, lang)
            except Exception:
                pass

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

        apply_conf()                                          # 读取有道 key / GPU 设置

        self.capture = None
        self.recognizer = Recognizer(on_status=self._status_threadsafe)
        self.translator = Translator()                       # DeepSeek 云端
        self.local_tr = LocalTranslator(on_status=self._status_threadsafe)  # opus-mt 本地
        self.youdao_tr = YoudaoTranslator(on_status=self._status_threadsafe)  # 有道云端

        self.work_q = queue.Queue()      # 待处理的音频
        self.ui_q = queue.Queue()        # 待更新的界面消息
        self.stream_worker = StreamWorker(self.recognizer, self._on_partial_text)

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
        """侧栏 + 主内容区（v1.1.0：真实 logo、输入语言、GPU 开关、设置入口）"""
        shell = tk.Frame(self, bg=C_BG)
        shell.pack(fill=tk.BOTH, expand=True)

        # ============ 左侧栏 ============
        side = tk.Frame(shell, bg=C_SIDEBAR, width=252)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)

        # --- 品牌区（真实 logo 图标）---
        brand = tk.Frame(side, bg=C_SIDEBAR)
        brand.pack(fill=tk.X, padx=16, pady=(18, 16))

        self._logo_img = None
        try:
            import base64
            raw = base64.b64decode(_ICON_B64)
            big = tk.PhotoImage(data=raw)
            self._logo_img = big.subsample(2, 2)       # 64 → 32
            tk.Label(brand, image=self._logo_img, bg=C_SIDEBAR,
                     bd=0).pack(side=tk.LEFT)
        except Exception:
            cv = tk.Canvas(brand, width=32, height=32, bg=C_SIDEBAR,
                           highlightthickness=0)
            cv.pack(side=tk.LEFT)
            cv.create_oval(1, 1, 31, 31, fill=C_ACCENT, outline="")

        tb = tk.Frame(brand, bg=C_SIDEBAR)
        tb.pack(side=tk.LEFT, padx=(9, 0))
        tk.Label(tb, text="Hearizon Sync", bg=C_SIDEBAR, fg=C_TEXT,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(tb, text="实时字幕翻译", bg=C_SIDEBAR, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")

        # 设置齿轮
        tk.Button(brand, text="⚙", command=self._open_settings,
                  bg=C_SIDEBAR, fg=C_DIM, relief=tk.FLAT, bd=0,
                  font=("Segoe UI", 13), cursor="hand2", width=2,
                  activebackground=C_CARD, activeforeground=C_TEXT
                  ).pack(side=tk.RIGHT, padx=(4, 0))

        # --- 开始 / 停止 ---
        self.btn_start = tk.Button(
            side, text="▶  开始同传", command=self._toggle,
            bg=C_ACCENT, fg="#03211A", relief=tk.FLAT, bd=0,
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2", activebackground=C_ACCENT2,
            activeforeground="#03211A")
        self.btn_start.pack(fill=tk.X, padx=16, pady=(0, 8), ipady=9)

        # --- 状态行 ---
        st = tk.Frame(side, bg=C_SIDEBAR)
        st.pack(fill=tk.X, padx=18, pady=(0, 2))
        self.status_dot = tk.Canvas(st, width=7, height=7, bg=C_SIDEBAR,
                                    highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, pady=4)
        self._dot = self.status_dot.create_oval(0, 0, 7, 7,
                                                fill=C_FAINT, outline="")
        self.status_lbl = tk.Label(st, text="未开始", bg=C_SIDEBAR, fg=C_DIM,
                                   font=("Microsoft YaHei UI", 9), anchor="w")
        self.status_lbl.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        # 跑马灯状态（超长文字滚动显示）
        self._marquee_job = None
        self._marquee_src = ""
        self._marquee_pos = 0

        # --- 音量条 ---
        lv = tk.Frame(side, bg=C_SIDEBAR)
        lv.pack(fill=tk.X, padx=18, pady=(4, 0))
        self.level_canvas = tk.Canvas(lv, height=4, bg=C_SIDEBAR,
                                      highlightthickness=0)
        self.level_canvas.pack(fill=tk.X)
        self.level_canvas.create_rectangle(0, 0, 400, 4, fill=C_BORDER, outline="",
                                           tags="track")
        self.level_bar = self.level_canvas.create_rectangle(
            0, 0, 0, 4, fill=C_LISTEN, outline="")
        self.level_canvas.bind(
            "<Configure>",
            lambda e: self.level_canvas.coords("track", 0, 0, e.width, 4))

        def divider(pady=11):
            tk.Frame(side, bg=C_BORDER, height=1).pack(
                fill=tk.X, padx=16, pady=pady)

        divider(14)

        # --- 字段封装 ---
        def field(label, widget_factory, pady=(0, 8)):
            box = tk.Frame(side, bg=C_SIDEBAR)
            box.pack(fill=tk.X, padx=16, pady=pady)
            tk.Label(box, text=label, bg=C_SIDEBAR, fg=C_DIM,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(0, 3))
            w = widget_factory(box)
            w.pack(fill=tk.X)
            return w

        # ① 输入语言（源）
        self.in_lang_var = tk.StringVar(value="自动检测")
        cbin = field("输入语言（识别）", lambda p: ttk.Combobox(
            p, textvariable=self.in_lang_var, state="readonly",
            values=INPUT_LANG_NAMES, font=("Microsoft YaHei UI", 9)))
        cbin.bind("<<ComboboxSelected>>", self._on_input_lang_change)

        # ② 麦克风
        self.dev_var = tk.StringVar()
        self.dev_cb = field("音频输入", lambda p: ttk.Combobox(
            p, textvariable=self.dev_var, state="readonly",
            font=("Microsoft YaHei UI", 9)))
        self._refresh_devices()

        # ③ 目标语言
        self.lang_var = tk.StringVar(value="中文")
        cbo = field("译成（目标）", lambda p: ttk.Combobox(
            p, textvariable=self.lang_var, state="readonly",
            values=OUTPUT_LANG_NAMES, font=("Microsoft YaHei UI", 9)))

        # ④ 翻译引擎
        self.tr_mode = tk.StringVar(value=TRANSLATOR)
        cbt = field("翻译引擎", lambda p: ttk.Combobox(
            p, textvariable=self.tr_mode, state="readonly",
            values=TRANSLATOR_CHOICES, font=("Microsoft YaHei UI", 9)))
        cbt.bind("<<ComboboxSelected>>", self._on_translator_change)

        # ⑤ 识别引擎
        self.model_var = tk.StringVar(value=ENGINE)
        cbm = field("识别引擎", lambda p: ttk.Combobox(
            p, textvariable=self.model_var, state="readonly",
            values=ENGINE_CHOICES, font=("Microsoft YaHei UI", 9)),
            pady=(0, 6))
        cbm.bind("<<ComboboxSelected>>", self._on_model_change)

        # ⑥ GPU 开关（一行两件：GPU 加速 + 刷新设备）
        row = tk.Frame(side, bg=C_SIDEBAR)
        row.pack(fill=tk.X, padx=16, pady=(0, 10))
        self.gpu_var = tk.BooleanVar(value=bool(GPU_ACCEL))
        tk.Checkbutton(row, text="GPU 加速", variable=self.gpu_var,
                       command=self._on_gpu_toggle, bg=C_SIDEBAR, fg=C_TEXT2,
                       selectcolor=C_CARD, activebackground=C_SIDEBAR,
                       activeforeground=C_ACCENT, font=("Microsoft YaHei UI", 8),
                       bd=0, highlightthickness=0,
                       cursor="hand2").pack(side=tk.LEFT)
        tk.Button(row, text="刷新设备", command=self._refresh_devices,
                  bg=C_CARD, fg=C_DIM, relief=tk.FLAT, bd=0,
                  font=("Microsoft YaHei UI", 8), cursor="hand2",
                  activebackground=C_CARD_HOV, activeforeground=C_TEXT2
                  ).pack(side=tk.RIGHT, ipadx=6, ipady=2)

        divider(9)

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
                      anchor="w").pack(fill=tk.X, padx=12, pady=0, ipady=4)

        # --- 底部统计（多行，修「显示不全」）---
        self.stat_lbl = tk.Label(side, text="", bg=C_SIDEBAR, fg=C_FAINT,
                                 font=("Microsoft YaHei UI", 8),
                                 justify="left", anchor="w")
        self.stat_lbl.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(6, 12))

        # ============ 右侧主区 ============
        main = tk.Frame(shell, bg=C_BG)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head = tk.Frame(main, bg=C_BG, height=48)
        head.pack(fill=tk.X, padx=22)
        head.pack_propagate(False)
        tk.Label(head, text="字幕", bg=C_BG, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side=tk.LEFT, pady=14)
        self.hint_lbl = tk.Label(head, text="点左侧「开始同传」开始监听",
                                 bg=C_BG, fg=C_FAINT,
                                 font=("Microsoft YaHei UI", 9))
        self.hint_lbl.pack(side=tk.LEFT, padx=12, pady=14)

        wrap = tk.Frame(main, bg=C_BG)
        wrap.pack(fill=tk.BOTH, expand=True, padx=22, pady=(2, 16))

        def card(parent, title, color, size, bold=False, expand=True, pady=(0, 10)):
            outer = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                             highlightthickness=1)
            outer.pack(fill=tk.BOTH, expand=expand, pady=pady)
            hd = tk.Frame(outer, bg=C_PANEL)
            hd.pack(fill=tk.X, padx=16, pady=(11, 0))
            d = tk.Canvas(hd, width=5, height=5, bg=C_PANEL, highlightthickness=0)
            d.pack(side=tk.LEFT, pady=5)
            d.create_oval(0, 0, 5, 5, fill=color, outline="")
            tk.Label(hd, text=title, bg=C_PANEL, fg=C_DIM,
                     font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=6)
            t = scrolledtext.ScrolledText(
                outer, bg=C_PANEL, fg=color, relief=tk.FLAT, bd=0,
                font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
                wrap=tk.WORD, height=6, insertbackground=C_TEXT,
                spacing1=6, spacing3=6, padx=6, pady=4,
                selectbackground=C_ACCENT, selectforeground="#03211A")
            t.pack(fill=tk.BOTH, expand=True, padx=16, pady=(7, 14))
            t.tag_configure("ts", foreground=C_FAINT, font=("Consolas", 8))
            return t

        self.src_text = card(wrap, "原文 · 识别结果", C_SRC, 14, pady=(0, 10))
        # 流式临时结果：灰色斜体
        self.src_text.tag_configure("partial", foreground="#5E7A72",
                                    font=("Microsoft YaHei UI", 14, "italic"))
        # 流式临时结果的锚点：每次重画前从这里删到末尾
        self.src_text.mark_set("partial_start", "end-1c")
        self.src_text.mark_gravity("partial_start", tk.LEFT)
        self.dst_text = card(wrap, "译文", C_DST, 18, bold=True, pady=(0, 0))
        self.dst_text.tag_configure("partial", foreground="#2A7A5E")
        self.dst_text.mark_set("partial_start", "end-1c")
        self.dst_text.mark_gravity("partial_start", tk.LEFT)

    # ------------------------------------------------------------ 设置 / 切换事件
    def _on_input_lang_change(self, _evt=None):
        name = self.in_lang_var.get()
        forced = self.recognizer.set_language(name)
        if forced:
            self.model_var.set("small")
            self._status_threadsafe(
                f"「{name}」SenseVoice 不支持，已自动切到 Whisper")
        else:
            self._status_threadsafe(f"输入语言：{name}")
        # 重连时用新语言（运行中则提示重启生效）
        if self.running:
            self._status_threadsafe("语言已切换，重新开始同传后生效")

    def _on_translator_change(self, _evt=None):
        mode = self.tr_mode.get()
        if mode == "youdao" and not self.youdao_tr.available():
            self._status_threadsafe("有道未配置 appKey/appSecret，请点右上角 ⚙ 设置")
        elif mode == "local":
            src, _ = input_codes(self.in_lang_var.get())
            tgt = self.lang_var.get()
            if not (src in ("en", None, "auto") and tgt == "中文"):
                self._status_threadsafe("本地模型只支持「英语→中文」，其他语言对请用云端")
            else:
                self._status_threadsafe("已切到本地翻译")
        elif mode == "deepseek" and not self.translator.available():
            self._status_threadsafe("未找到 DeepSeek API Key")
        else:
            self._status_threadsafe(f"翻译引擎：{mode}")

    def _on_gpu_toggle(self):
        global GPU_ACCEL
        want = bool(self.gpu_var.get())
        if not want:
            GPU_ACCEL = False
            c = load_conf(); c["gpu_accel"] = False; save_conf(c)
            self.recognizer.reset()
            self._status_threadsafe("已关闭 GPU，全部走 CPU")
            return

        # 开启前真实探测，避免「勾了却没生效」的假象
        self._status_threadsafe("正在检测 CUDA 运行时...")

        def probe():
            ok, msg = cuda_probe()
            self.ui_q.put(("gpu_result", ok, msg))

        threading.Thread(target=probe, daemon=True).start()

    def _apply_gpu_result(self, ok, msg):
        global GPU_ACCEL
        if ok:
            GPU_ACCEL = True
            c = load_conf(); c["gpu_accel"] = True; save_conf(c)
            self.recognizer.reset()
            self._status_threadsafe("GPU 已开启（Whisper 识别 + 本地翻译生效）")
        else:
            GPU_ACCEL = False
            self.gpu_var.set(False)
            c = load_conf(); c["gpu_accel"] = False; save_conf(c)
            self.recognizer.reset()
            self._status_threadsafe(f"GPU 无法启用：{msg}")
            messagebox.showwarning(
                "GPU 无法启用",
                f"{msg}\n\n"
                "说明：\n"
                "• 本机已检测到 NVIDIA 显卡，但缺少 CUDA 运行时\n"
                "• 需要安装 CUDA Toolkit 12.x（约 3 GB）才能启用\n"
                "• SenseVoice 识别为 CPU 模型，本身已足够快（RTF 0.02）\n"
                "• 不装也不影响使用")

    def _open_settings(self):
        """设置弹窗：有道 Key / VB-Cable 引导 / GPU 说明"""
        win = tk.Toplevel(self)
        win.title("设置")
        win.configure(bg=C_BG)
        win.geometry("560x560")
        win.transient(self)
        win.grab_set()

        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)

        def title(t):
            tk.Label(pad, text=t, bg=C_BG, fg=C_TEXT,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 4))

        def hint(t, color=C_DIM):
            tk.Label(pad, text=t, bg=C_BG, fg=color, justify="left", anchor="w",
                     wraplength=500, font=("Microsoft YaHei UI", 9)
                     ).pack(anchor="w", pady=(0, 8))

        # ---- 有道 ----
        title("有道智云（第二云端翻译引擎）")
        hint("到 ai.youdao.com 创建「文本翻译」应用，把 appKey / appSecret 填在这里。\n"
             "支持医学等领域翻译（对听力/言语康复文献更准）。")
        f1 = tk.Frame(pad, bg=C_BG); f1.pack(fill=tk.X, pady=(0, 6))
        tk.Label(f1, text="appKey", bg=C_BG, fg=C_DIM, width=10, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        e_key = tk.Entry(f1, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                         relief=tk.FLAT, font=("Consolas", 10))
        e_key.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        e_key.insert(0, YOUDAO_APP_KEY)

        f2 = tk.Frame(pad, bg=C_BG); f2.pack(fill=tk.X, pady=(0, 6))
        tk.Label(f2, text="appSecret", bg=C_BG, fg=C_DIM, width=10, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        e_sec = tk.Entry(f2, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                         relief=tk.FLAT, font=("Consolas", 10), show="•")
        e_sec.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        e_sec.insert(0, YOUDAO_APP_SECRET)

        f3 = tk.Frame(pad, bg=C_BG); f3.pack(fill=tk.X, pady=(0, 10))
        tk.Label(f3, text="领域", bg=C_BG, fg=C_DIM, width=10, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        dom_var = tk.StringVar(value=YOUDAO_DOMAIN)
        ttk.Combobox(f3, textvariable=dom_var, state="readonly", width=16,
                     values=["general", "medicine", "computers", "finance", "game"],
                     font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        def save_youdao():
            global YOUDAO_APP_KEY, YOUDAO_APP_SECRET, YOUDAO_DOMAIN
            YOUDAO_APP_KEY = e_key.get().strip()
            YOUDAO_APP_SECRET = e_sec.get().strip()
            YOUDAO_DOMAIN = dom_var.get()
            c = load_conf()
            c.update({"youdao_app_key": YOUDAO_APP_KEY,
                      "youdao_app_secret": YOUDAO_APP_SECRET,
                      "youdao_domain": YOUDAO_DOMAIN})
            ok = save_conf(c)
            self._status_threadsafe("有道 Key 已保存" if ok else "保存失败")
            win.destroy()

        tk.Button(pad, text="保存有道设置", command=save_youdao,
                  bg=C_ACCENT, fg="#03211A", relief=tk.FLAT, bd=0,
                  font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2"
                  ).pack(fill=tk.X, ipady=6, pady=(0, 14))

        # ---- VB-Cable 引导 ----
        title("想翻译电脑声音（网课 / 视频）？")
        hint("当前只能录麦克风。要录系统声音，装一个免费的虚拟声卡即可：\n"
             "1) 下载 VB-Cable（vb-audio.com/Cable）并安装\n"
             "2) 重启后在左侧「音频输入」里选「CABLE Output」\n"
             "3) 把系统播放设备设为「CABLE Input」")
        n_cable = self._cable_count()
        hint(("✅ 已检测到虚拟声卡：" + str(n_cable) + " 个，直接在「音频输入」里选即可")
             if n_cable else "（当前未检测到虚拟声卡，装好后点「刷新设备」）",
             C_ACCENT if n_cable else C_FAINT)

        # ---- GPU 说明 ----
        title("GPU 加速说明")
        hint("• Whisper 识别、本地 opus-mt 翻译：可用 GPU（需 NVIDIA 显卡）\n"
             "• SenseVoice 识别：官方 pip 包为 CPU 编译，无法 GPU，但它本身就极快（RTF 0.02）\n"
             "• 开启后下次识别会重新加载模型")

        tk.Button(pad, text="关闭", command=win.destroy,
                  bg=C_CARD, fg=C_TEXT2, relief=tk.FLAT, bd=0,
                  font=("Microsoft YaHei UI", 9), cursor="hand2"
                  ).pack(fill=tk.X, ipady=5, pady=(10, 0))

    def _cable_count(self):
        try:
            n = 0
            for d in sd.query_devices():
                if d["max_input_channels"] > 0:
                    nm = d["name"].upper()
                    if "CABLE" in nm:
                        n += 1
            return n
        except Exception:
            return 0

    def _on_partial_text(self, text, lang):
        """流式临时结果 → 界面（灰色斜体，不翻译）"""
        self.ui_q.put(("partial", text, lang))

    def _refresh_devices(self):
        names = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    nm = d["name"]
                    up = nm.upper()
                    tail = ""
                    if "CABLE" in up:
                        tail = "  【系统声音】"
                    elif "STEREO MIX" in up or "立体声混音" in nm:
                        tail = "  【系统声音】"
                    names.append(f"[{i}] {nm[:36]}{tail}")
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
        self._set_status("启动中...", C_YELLOW)

        # 识别模型后台加载
        threading.Thread(target=self._warmup, daemon=True).start()

        # 音频采集（流式：同时把最新片段喂给临时识别线程）
        self.capture = AudioCapture(
            on_sentence=lambda a: self.work_q.put(a),
            on_level=self._on_level,
            on_status=self._status_threadsafe,
            device=dev,
            on_partial=self.stream_worker.push,
        )
        self.capture.start()

        # 流式识别线程
        if not self.stream_worker.is_alive():
            self.stream_worker = StreamWorker(self.recognizer, self._on_partial_text)
            self.stream_worker.start()

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
        if self.stream_worker:
            self.stream_worker.clear()
        self.btn_start.configure(text="▶ 开始同传", bg=C_GREEN, activebackground="#86efac")
        self._set_status("已停止", C_DIM)
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
        # ⚠️ 修：只识别出「。」「.」这类标点时直接丢弃，不送翻译
        if not is_meaningful(text):
            self.ui_q.put(("status", "识别结果无实义（纯标点），已跳过"))
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

        # 本地模式：只用 opus-mt（仅英语→中文）
        # ⚠️ src 为 None 表示「SenseVoice 不支持该语言」（德/法/意/西），必须拦截
        if mode == "local":
            src, _ = input_codes(self.in_lang_var.get())
            if src not in ("en", "auto") or target != "中文":
                return f"[本地模型只支持英语→中文，当前是「{self.in_lang_var.get()}→{target}」，请改用云端]", "本地"
            try:
                return self.local_tr.translate(text, target, on_chunk), "本地"
            except Exception as e:
                return f"[本地翻译失败: {e}]", "本地"

        # 云端模式：只用 DeepSeek
        if mode == "deepseek":
            if not self.translator.available():
                return "[未配置 API Key]", "云端"
            return self.translator.translate(text, target, on_chunk), "云端"

        # 有道模式：只用有道
        if mode == "youdao":
            if not self.youdao_tr.available():
                return "[未配置有道 Key，点右上角 ⚙ 设置]", "有道"
            return self.youdao_tr.translate(text, target, on_chunk), "有道"

        # auto 模式：DeepSeek → 有道 → 本地，逐级降级
        if self.translator.available():
            try:
                r = self.translator.translate(text, target, on_chunk)
                if r and not r.startswith("[翻译失败"):
                    return r, "云端"
            except Exception:
                pass
        if self.youdao_tr.available():
            try:
                r = self.youdao_tr.translate(text, target, on_chunk)
                if r and not r.startswith("[有道"):
                    return r, "有道(降级)"
            except Exception:
                pass
        # 最后降级到本地（仅英→中）
        try:
            src, _ = input_codes(self.in_lang_var.get())
            if src not in ("en", "auto") or target != "中文":
                return "[翻译失败：云端不可用，且本地模型只支持英→中]", "无"
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
            self._set_status(msg[1], C_ACCENT)
        elif kind == "gpu_result":
            _, ok, msg = msg
            self._apply_gpu_result(ok, msg)
        elif kind == "partial":
            # 流式临时结果：灰色斜体，覆盖上一次临时内容（不翻译）
            _, text, lang = msg
            t = self.src_text
            try:
                t.delete("partial_start", "end-1c")
            except Exception:
                pass
            t.mark_set("partial_start", "end-1c")
            t.mark_gravity("partial_start", tk.LEFT)
            t.insert(tk.END, text, "partial")
            t.see(tk.END)
        elif kind == "src":
            # 正式结果：先清掉流式临时行
            try:
                self.src_text.delete("partial_start", "end-1c")
            except Exception:
                pass
            self.src_text.mark_set("partial_start", "end-1c")
            self.src_text.mark_gravity("partial_start", tk.LEFT)
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
        """多行显示，避免侧栏里被截断"""
        st = self.stats
        dur = int(time.time() - self.t_start) if self.t_start else 0
        self.stat_lbl.configure(
            text=(f"句数 {st['sentences']}　运行 {dur//60:02d}:{dur%60:02d}\n"
                  f"原文 {st['chars_in']} 字　译文 {st['chars_out']} 字"))

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

    # ------------------------------------------------------------ 状态栏跑马灯
    MARQUEE_MAX = 11          # 侧栏能显示的中文字数上限

    def _set_status(self, text, color=None):
        """短文本直接显示；长文本滚动（修「提示显示不全」）"""
        if self._marquee_job is not None:
            try:
                self.after_cancel(self._marquee_job)
            except Exception:
                pass
            self._marquee_job = None
        if color is not None:
            self.status_lbl.configure(fg=color)

        if len(text) <= self.MARQUEE_MAX:
            self.status_lbl.configure(text="● " + text)
            self._marquee_src = ""
            return

        # 超长 → 循环滚动
        self._marquee_src = "● " + text + "　　◆　　"
        self._marquee_pos = 0
        self._marquee_tick()

    def _marquee_tick(self):
        src = self._marquee_src
        if not src:
            return
        L = self.MARQUEE_MAX + 2
        doubled = src + src
        seg = doubled[self._marquee_pos:self._marquee_pos + L]
        self.status_lbl.configure(text=seg)
        self._marquee_pos = (self._marquee_pos + 1) % len(src)
        self._marquee_job = self.after(150, self._marquee_tick)

    def _log(self, text, color=None):
        # ⚠️ 打包成窗口程序后 sys.stdout 为 None，print() 会抛
        #    AttributeError 让程序在构造阶段直接退出 —— 必须保护
        try:
            if sys.stdout is not None:
                print(text)
        except Exception:
            pass
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
    # 窗口模式（PyInstaller --windowed）下没有控制台，
    # sys.stdout / sys.stderr 是 None；任何 print 或第三方库写日志都会崩。
    # 这里统一兜底到空设备，保证启动稳定。
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
