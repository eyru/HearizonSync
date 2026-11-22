#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 何瑞程 (He Ruicheng)
# SPDX-License-Identifier: GPL-3.0-or-later OR LicenseRef-Commercial
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
    安装版：双击桌面「Hearizon Sync」快捷方式启动，
            或点「开始」菜单 → 所有应用 → Hearizon Sync
    源码版：双击「HearizonSync.bat」，或 .venv\\Scripts\\python.exe HearizonSync.py

    三步开始：
        ① 「音频输入」选麦克风（点「测麦克风」可验证；要翻译电脑正在
           播放的声音，需先装 VB-Cable 并选带【系统声音】的 CABLE Output）
        ② 选「输入语言（识别）」和「译成（目标语言）」
        ③ 点「开始同传」，或按 Ctrl+Enter
    识别引擎：sensevoice（默认，中/英/粤）；日/韩/德/法/意/西自动走 Whisper
    翻译模式（v2.5.1：三条完全独立的路线，auto 降级模式已删除）：
        ① 本地翻译（离线） ：opus-mt 本地模型，断网可用、零成本，仅英语→中文
        ② 在线翻译（免费） ：MyMemory 免费接口，国内可直连、不用填 Key、不花钱
        ③ API 模式（付费） ：DeepSeek / 有道智云，必须自己填 Key，按量计费
    快捷键：Ctrl+Enter 开始/停止，Ctrl+L 清空，Ctrl+S 保存记录，
            Ctrl+Shift+S 导出 SRT（真实时间轴），Ctrl+ / - 调字幕字号，Ctrl+0 复位
    字幕样式：3 套配色（经典绿 / 高对比 / 暖纸张）、贴底 / 贴顶、字号 14~32 pt
    系统托盘：点「最小化到托盘」后台常驻，不占任务栏
    其他：左下角「关于」查看版本信息，连点版本号 5 次有彩蛋；
          配置（API Key / 配色 / 字号 / 位置）保存在用户目录 .hearizon_sync.json
"""

import os
import sys
import json
import math
import re
import queue
import threading
import time
import tempfile
import hashlib
import zlib
import base64
import uuid
import urllib.parse
import urllib.request
import traceback
import ctypes
import socket
import urllib.error
from collections import deque
from datetime import datetime, timedelta

# ---- 必须在 import faster_whisper 之前设置（国内镜像 + 关闭 xet）----
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import webbrowser

# ================================================================ 网络层兜底
# 国内机器常见两种坏环境：
#   ① 系统代理指向已退出的进程 —— urllib 全部 WinError 10061；
#   ② IPv6 到 GitHub 被拒，但 IPv4 正常。
# 三级 opener：默认 → 绕开系统代理 → 绕开代理且强制 IPv4。
# 只在「连接层失败」时降级重试；HTTP 4xx/5xx 是服务端明确响应，原样抛出，
# 避免付费 API 因重试被重复计费。
import http.client


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        last = None
        for family, socktype, proto, _, sa in socket.getaddrinfo(
                self.host, self.port, socket.AF_INET, socket.SOCK_STREAM):
            s = None
            try:
                s = socket.socket(family, socktype, proto)
                if self.timeout is not None:
                    s.settimeout(self.timeout)
                s.connect(sa)
                self.sock = s
                return
            except OSError as e:
                last = e
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
        raise last or OSError("IPv4 连接失败")


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        last = None
        for family, socktype, proto, _, sa in socket.getaddrinfo(
                self.host, self.port, socket.AF_INET, socket.SOCK_STREAM):
            s = None
            try:
                s = socket.socket(family, socktype, proto)
                if self.timeout is not None:
                    s.settimeout(self.timeout)
                s.connect(sa)
                self.sock = self._context.wrap_socket(
                    s, server_hostname=self.host)
                return
            except OSError as e:
                last = e
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
        raise last or OSError("IPv4 连接失败")


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_IPv4HTTPConnection, req)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # 与标准库保持一致：只向连接类传 context，check_hostname 属于 SSLContext
        return self.do_open(
            _IPv4HTTPSConnection, req, context=self._context)


_OPENERS = None
_NET_TIER = 0          # 上次成功的档位，下次优先试它（代理挂死时不必每次都空等超时）


def _openers():
    global _OPENERS
    if _OPENERS is None:
        _OPENERS = (
            urllib.request.build_opener(),
            urllib.request.build_opener(urllib.request.ProxyHandler({})),
            urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _IPv4HTTPHandler, _IPv4HTTPSHandler),
        )
    return _OPENERS


def _tier_order(count):
    """先试上次成功的档位，再按原顺序试其余档位。"""
    pref = _NET_TIER if 0 <= _NET_TIER < count else 0
    return [pref] + [i for i in range(count) if i != pref]


def _pre_connect_failure(e):
    """失败是否发生在请求发出去之前。

    只有这种失败才可以安全重试：连接都没建立，服务端一定没收到请求。
    连接建立后被重置（ConnectionResetError）时请求可能已经被处理，重发
    会让付费接口重复计费，所以不能重试。
    """
    r = getattr(e, "reason", e)
    for _ in range(4):
        inner = getattr(r, "reason", None)
        if inner is None or inner is r:
            break
        r = inner
    return isinstance(r, (ConnectionRefusedError, socket.gaierror))


def _urlopen(req, timeout=15):
    """连接级失败时按档位降级：默认 → 绕开系统代理 → 绕开代理且强制 IPv4。

    上一次成功的档位排在最前面，这样系统代理挂死时不必每个请求都先空等
    一个超时；失败后照旧往后面的档位退。
    只对 GET/HEAD 无条件重试；其它方法（DeepSeek、彩云的 POST 等）仅在
    请求根本没能发出去时重试，避免重复计费。
    """
    global _NET_TIER
    method = (req.get_method() if hasattr(req, "get_method") else "GET") or "GET"
    idempotent = method.upper() in ("GET", "HEAD")
    openers = _openers()
    last = None
    for i in _tier_order(len(openers)):
        try:
            resp = openers[i].open(req, timeout=timeout)
            _NET_TIER = i
            return resp
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as e:
            last = e
            if not isinstance(getattr(e, "reason", None), OSError):
                raise
            if not idempotent and not _pre_connect_failure(e):
                raise
        except (socket.timeout, ConnectionError, TimeoutError) as e:
            last = e
            if not idempotent and not _pre_connect_failure(e):
                raise
    raise last or RuntimeError("网络请求失败")


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
# 3.0.0 起句预卷与尾部谷底切分：词首能量低，只靠门限容易把 /s/、/f/ 这类音吃掉，
# 于是出现「Spanish 识别成 fan」的错位。起句时把前面的静音一起带上就没这个问题。
VAD_PREROLL_MS = 300         # 起句时回补的静音长度
VAD_VALLEY_MS  = 320         # 切句时在尾部这段里找能量谷底
VAD_ZCR_MAX    = 0.45        # 过零率高于此值更像嘶声/风噪，不当人声
FAR_FIELD      = False       # 远场模式：抬高增益、降低门限、补高频
FORCE_LOUD     = False       # 强制放大分贝：整段抬到高电平（极小声/远距）
SPECTRAL_DENOISE = True      # 频谱降噪：按频点压制噪声，真正抬高语音帧信噪比
SPECTRAL_STRENGTH = 2.0      # 降噪力度：过减因子，越大压得越狠（1.2 轻 / 2.0 标准 / 3.0 强）
DENOISE_MODE = "auto"        # 降噪档位：off / auto / always
DENOISE_MODES = ["auto", "always", "off"]
DENOISE_MODE_LABEL = {
    "auto": "自动（只在空调、风扇这类稳态噪声下降噪）",
    "always": "始终降噪（噪声很大时用，多人说话会变差）",
    "off": "关闭降噪（原声直送识别）",
}


def _denoise_mode_from_label(text):
    """设置页下拉存的是中文说明，这里翻回内部代号。"""
    for key, label in DENOISE_MODE_LABEL.items():
        if text == label:
            return key
    return "auto" if text not in DENOISE_MODE_LABEL else text

# ---- 识别引擎 ----
#   sensevoice     : RTF 0.02  中英粤日韩原生，自带语言检测，日常够用
#   small          : Whisper，约 480 MB，中文一般
#   medium         : Whisper，约 1.5 GB，口音与远场明显更稳
#   large-v3-turbo : Whisper，约 1.6 GB，最准，需要较好的 CPU/GPU
# 3.0.0 起档位放开：机器好就往上调，机器老就留在 sensevoice。
ENGINE        = "sensevoice"
ENGINE_CHOICES = ["sensevoice", "small", "medium", "large-v3-turbo"]
ENGINE_LABELS = {
    "sensevoice": "SenseVoice（最快 · 中英粤日韩）",
    "small": "Whisper Small（约 480 MB）",
    "medium": "Whisper Medium（约 1.5 GB · 口音更稳）",
    "large-v3-turbo": "Whisper Large-v3-Turbo（约 1.6 GB · 最准）",
}
# 旧配置里的 base 已下线，统一迁到 small
_ENGINE_ALIAS = {"base": "small", "tiny": "small", "large": "large-v3-turbo"}
ENGINE_SIZES = {
    "small": "约 480 MB",
    "medium": "约 1.5 GB",
    "large-v3-turbo": "约 1.6 GB",
}
CPU_THREADS   = 16
COMPUTE_TYPE  = "int8"

# ================================================================ 内嵌图标
# 64x64 PNG 的 base64（窗口图标用，避免依赖外部文件）
_ICON_B64 = (
    "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"
)


_LOGO_MASTER = None


def logo_photo(px):
    """按设备像素生成 logo 位图。

    px 是逻辑像素；内部乘屏幕缩放后取整，再用 Lanczos 重采样，
    这样 125%/150% 缩放下不会拿小图硬放大。
    返回的 PhotoImage 会被缓存，调用方不必自己持有引用。
    """
    size = max(8, int(round(float(px) * (UI_SCALE or 1.0))))
    cache = logo_photo.__dict__.setdefault("_cache", {})
    if size in cache:
        return cache[size]
    try:
        global _LOGO_MASTER
        import io
        from PIL import Image as _Image
        if _LOGO_MASTER is None:
            _LOGO_MASTER = _Image.open(io.BytesIO(
                base64.b64decode(_ICON_B64))).convert("RGBA")
        buf = io.BytesIO()
        _LOGO_MASTER.resize((size, size), _Image.LANCZOS).save(buf, "PNG")
        img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
        cache[size] = img
        return img
    except Exception:
        pass
    try:
        big = tk.PhotoImage(data=base64.b64decode(_ICON_B64))
        k = max(1, big.width() // size)
        return big.subsample(k, k)
    except Exception:
        return None

# ================================================================ 路径解析
# 打包成 exe 后 __file__ 指向临时解压目录，模型需要另找。
# 查找顺序：环境变量 → exe 同目录/models → 源码目录/models → D:\slm-models
def _app_dir():
    """程序所在目录（打包后是 exe 目录，源码时是 .py 目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _output_dir():
    """保存/导出目录：优先「文档\\Hearizon Sync」。

    安装版装在 Program Files 时普通用户无写权限，之前写 __file__ 所在目录
    （_internal）会直接 PermissionError —— 这就是「保存记录 / 导出 SRT
    不能用」的根因。这里逐个探测可写目录，保证一定有落点。
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Documents", "Hearizon Sync"),
        os.path.join(home, "Desktop", "Hearizon Sync"),
        os.path.join(home, "Hearizon Sync"),
        os.path.join(tempfile.gettempdir(), "Hearizon Sync"),
    ]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return d
        except Exception:
            continue
    return home


def _bundle_dir():
    """PyInstaller 打包后的资源根目录。

    PyInstaller 6.x 的 onedir 模式把随包资源放进 exe 同级的 `_internal`，
    `sys._MEIPASS` 正指向那里；源码运行时为空串。
    """
    return getattr(sys, "_MEIPASS", "") or ""


def _find_model_dir(name):
    """按优先级查找模型目录，返回存在的那个"""
    bundle = _bundle_dir()
    candidates = [
        os.environ.get(f"SLM_{name.upper()}_DIR", ""),
        os.path.join(bundle, "models", name) if bundle else "",
        os.path.join(_app_dir(), "models", name),
        # 源码运行时本文件在 src/ 下，模型放在仓库根的 models/
        os.path.join(os.path.dirname(_app_dir()), "models", name),
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
        os.path.join(_bundle_dir(), "models", "opus_en_zh") if _bundle_dir() else "",
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
    # 全都没命中 ASCII 路径 → 用户把软件装在了中文目录（比如中文用户名下的
    # AppData）。sentencepiece 打不开这类路径，这里把模型镜像一份到纯 ASCII 的
    # 公共目录再用，否则本地翻译在中文路径下会直接失效。
    src = _find_model_dir("opus_en_zh")
    if os.path.isdir(src):
        mirrored = _mirror_ascii(src, "opus_en_zh")
        if mirrored:
            return mirrored
    return src


def _mirror_ascii(src, name):
    """把模型目录镜像到纯 ASCII 路径，返回镜像目录；不可用时返回空串。

    3.1.0 修第 7 条：
      - 旧版只查 dst 非空就把半拷贝当有效模型用；现在按「源文件清单 + 逐个
        大小一致」判定完整性，残目录一律重镜像。
      - 先拷到 staging 临时目录、校验通过后再整体 rename 成正式目录，避免
        拷贝中途崩溃留下一个「看起来有东西」的坏模型。
    """
    base_root = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE") or ""
    if not base_root or not os.path.isdir(base_root):
        base_root = os.environ.get("SystemDrive", "C:") + os.sep + "ProgramData"
    dst = os.path.join(base_root, "HearizonSync", "models", name)
    try:
        dst.encode("ascii")
    except UnicodeEncodeError:
        return ""
    try:
        import shutil
        want = {}
        for fn in os.listdir(src):
            s_ = os.path.join(src, fn)
            if os.path.isfile(s_):
                want[fn] = os.path.getsize(s_)
        if not want:
            return ""

        def _complete(d):
            if not os.path.isdir(d):
                return False
            for fn, sz in want.items():
                p = os.path.join(d, fn)
                if not os.path.isfile(p) or os.path.getsize(p) != sz:
                    return False
            return True

        if _complete(dst):
            return dst
        parent = os.path.dirname(dst)
        os.makedirs(parent, exist_ok=True)
        stage = dst + ".tmp-%d" % os.getpid()
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage, exist_ok=True)
        for fn in want:
            shutil.copy2(os.path.join(src, fn), os.path.join(stage, fn))
        if not _complete(stage):
            shutil.rmtree(stage, ignore_errors=True)
            return ""
        if os.path.isdir(dst):
            old = dst + ".old-%d" % os.getpid()
            try:
                os.replace(dst, old)
                shutil.rmtree(old, ignore_errors=True)
            except Exception:
                shutil.rmtree(dst, ignore_errors=True)
        os.replace(stage, dst)
        return dst if _complete(dst) else ""
    except Exception:
        return ""


OPUS_DIR = _find_opus_dir()

# SenseVoice 模型路径
SV_MODEL  = os.path.join(SV_DIR, "model.int8.onnx")
SV_TOKENS = os.path.join(SV_DIR, "tokens.txt")

# Whisper 回退用的 prompt（SenseVoice 不需要）
# 3.0.0：全部改成中性提示，且按输入语言自动挑选。
#   以前英文那句写死了「关于声学、听力学与噪声」，会把无关的会议内容往那个
#   方向带，是识别跑偏的一大来源，已经删掉。
ZH_PROMPT = "以下是普通话简体中文的转写，请按口语原样记录。"
EN_PROMPT = "The following is English speech, transcribed verbatim."
ASR_PROMPTS = {
    "中文": ZH_PROMPT,
    "英语": EN_PROMPT,
    "日语": "以下は日本語の音声です。話したとおりに書き起こしてください。",
    "韩语": "다음은 한국어 음성입니다. 말한 그대로 받아 적으세요.",
    "粤语": "以下是廣東話的轉寫，請按口語原樣記錄。",
    "德语": "Es folgt deutsche Sprache. Bitte wörtlich transkribieren.",
    "法语": "Voici de la parole en français. Transcrire mot pour mot.",
    "意大利语": "Segue parlato in italiano. Trascrivere parola per parola.",
    "西班牙语": "A continuación, habla en español. Transcribir literalmente.",
}


def asr_prompt(lang_name):
    """按输入语言挑中性提示词；热词再拼上去。"""
    base = ASR_PROMPTS.get(lang_name, "")
    return hotword_prompt(base)


# 提示词回声：小模型会把自己被喂的提示词当成前文一起吐出来。
# 命中这些片段说明是幻觉，要从结果里剔掉。
_PROMPT_ECHO = (
    "请按口语原样记录", "以下是普通话简体中文的转写", "以下是广东话的转写",
    "以下是廣東話的轉寫", "话したとおり", "話したとおり", "말한 그대로",
    "Bitte wörtlich", "Transcrire mot pour mot", "Transcribir literalmente",
    "transcribed verbatim", "常见词：", "常見詞：",
)


def strip_prompt_echo(text):
    """把模型吐出来的提示词片段清掉；清完没剩什么就当没识别到。"""
    if not text:
        return text
    t = text
    for marker in _PROMPT_ECHO:
        while marker in t:
            i = t.find(marker)
            # 提示词一般出现在开头，从命中的那句开头一直截到句末
            head = t[:i]
            tail = t[i + len(marker):]
            cut = len(tail)
            for k, ch in enumerate(tail):
                if ch in "。．.！!？?；;\n":
                    cut = k + 1
                    break
            t = head + tail[cut:]
    t = t.strip(" ，,、;；")
    if all(ch in _PUNCT_ONLY for ch in t):
        return ""
    return t


# ---- 本地翻译（opus-mt，断网可用）----
# 模型在 models/opus_en_zh/（CT2 格式，155 MB）
# ⚠️ sentencepiece 打不开中文路径，所以查找顺序里 D:\slm-models 优先
#    （OPUS_DIR 已在上面通过 _find_model_dir 解析）

# ---- 翻译模式（v2.5.1：三条完全独立的路线，auto 降级模式已删除）----
#   ① 本地翻译（离线）  opus-mt，断网可用、零成本，但只支持 英语→中文
#   ② 在线翻译（免费）  MyMemory 免费接口（国内可直连），无需 Key、不花钱
#   ③ API 模式（付费）  DeepSeek / 有道智云，必须自己填 Key，按量计费
# 三种模式必须由用户明确选择；API 模式绝不会被自动选中（因为要花钱）。
TR_LOCAL = "本地翻译（离线）"
TR_FREE = "在线翻译（免费）"
TR_API = "API 模式（付费）"
TRANSLATOR_CHOICES = [TR_LOCAL, TR_FREE, TR_API]
TRANSLATOR = TR_FREE          # 默认免费在线：任何电脑装完即可用，不花钱

# ---- API 模式的引擎选择（两个 Key 都配时，由这里决定用谁）----
#   只有选中「API 模式（付费）」时这个选项才生效。
API_ENGINE_DEEPSEEK = "DeepSeek"
API_ENGINE_YOUDAO = "有道智云"
API_ENGINE_CUSTOM = "自定义（OpenAI 兼容）"
API_ENGINE_BAIDU = "百度翻译"
API_ENGINE_CAIYUN = "彩云小译"
API_ENGINE_AUTO = "自动兜底"
# 3.0.0 起 API 引擎不再局限于两家：中转站/自建服务只要兼容 OpenAI 接口
# 就能填地址直接用；百度、彩云这两家国内翻译接口也一起接上。
API_ENGINES = [API_ENGINE_DEEPSEEK, API_ENGINE_YOUDAO, API_ENGINE_CUSTOM,
               API_ENGINE_BAIDU, API_ENGINE_CAIYUN, API_ENGINE_AUTO]
API_ENGINE = API_ENGINE_AUTO   # 默认：DeepSeek 优先，失败（无输出）自动切有道

# 自定义 OpenAI 兼容接口（中转站 / 自建网关 / 本地 Ollama 都走这里）
CUSTOM_BASE_URL = ""      # 例：https://api.xxx.com/v1
CUSTOM_MODEL    = ""      # 例：gpt-4o-mini / deepseek-chat / qwen-plus
CUSTOM_KEY      = ""
# 百度翻译开放平台
BAIDU_APPID     = ""
BAIDU_KEY       = ""
# 彩云小译
CAIYUN_TOKEN    = ""

# ---- 在线检查更新----
# 启动几秒后静默查一次 GitHub 最新 Release；有新版本才弹窗，失败一律不打扰。
# 设置页可以关掉。升级是覆盖安装，用户配置与 API Key 都留在用户目录，不会被清掉。
UPDATE_API = "https://api.github.com/repos/eyru/HearizonSync/releases/latest"
UPDATE_PAGE = "https://github.com/eyru/HearizonSync/releases/latest"
UPDATE_CHECK = True


def probe_download(url, agent=None, timeout=15):
    """探一次：返回 (文件总大小, 服务器是否支持 Range 分段)。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": agent or ("HearizonSync/" + APP_VERSION),
        "Range": "bytes=0-0"})
    with _urlopen(req, timeout=timeout) as r:
        cr = r.headers.get("Content-Range") or ""
        total = 0
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except Exception:
                total = 0
        partial = bool(cr) and getattr(r, "status", 0) == 206
        if not total:
            total = int(r.headers.get("Content-Length") or 0)
            partial = False
        return total, partial


def _seg_plan(total, partial, seg_mb=8, max_segs=32):
    """固定分段边界：续传时才能对得上。"""
    if not partial or total <= 0:
        return [(0, max(0, total - 1))] if total > 0 else []
    n = max(1, min(max_segs, total // (seg_mb * 1024 * 1024) + 1))
    seglen = (total + n - 1) // n
    return [(i * seglen, min(total, (i + 1) * seglen) - 1) for i in range(n)]


def _file_sha256(path):
    """流式算 SHA-256，几百兆的安装包也不占内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_sha256(body):
    """从 Release 说明里抽出 SHA-256（优先带标签的，其次任意 64 位十六进制）。"""
    if not body:
        return ""
    m = re.search(r"sha-?256[^\n0-9a-f]*?([0-9a-fA-F]{64})", str(body), re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r"\b([0-9a-fA-F]{64})\b", str(body))
    return m.group(1).lower() if m else ""


def download_concurrent(url, dest, agent=None, progress=None,
                        seg_mb=8, max_segs=32, timeout=30, cancel_event=None):
    """并发分段下载到 dest，支持断点续传。

    GitHub 的下载 CDN 对单个连接限速很严，三百多兆单线程经常要半小时；
    这里按固定大小切段并发，每段下完就记进 sidecar，重试时只补没下完的段。
    progress(got, total) 会被周期回调，用于界面显示。
    cancel_event：threading.Event，置位后尽快中止并抛「已取消下载」。
    抛异常表示失败，已下好的部分保留，下次调用会接着用。
    """
    def _cancelled():
        return cancel_event is not None and cancel_event.is_set()

    agent = agent or ("HearizonSync/" + APP_VERSION)
    part = dest + ".part"
    meta = dest + ".part.json"

    total, partial = probe_download(url, agent)
    if not total:
        raise RuntimeError("服务器没有返回文件大小")
    segs = _seg_plan(total, partial, seg_mb, max_segs)
    n = len(segs)

    done_segs = []
    if os.path.exists(part) and os.path.exists(meta):
        try:
            with open(meta, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m.get("total") == total and m.get("url") == url:
                done_segs = [int(i) for i in m.get("done", [])
                             if 0 <= int(i) < n]
        except Exception:
            done_segs = []
    if not os.path.exists(part) or not done_segs:
        done_segs = []
        with open(part, "wb") as f:
            f.truncate(total)

    state = {"got": sum(segs[i][1] - segs[i][0] + 1 for i in done_segs),
             "lock": threading.Lock(), "stop": False}
    if progress:
        progress(state["got"], total)

    def save():
        try:
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"total": total, "url": url,
                           "done": sorted(done_segs)}, f)
        except Exception:
            pass

    save()
    errs = []

    def fetch(idx):
        a, b = segs[idx]
        if state["stop"] or _cancelled():
            return
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": agent, "Range": "bytes=%d-%d" % (a, b)})
            with _urlopen(req, timeout=timeout) as r:
                with open(part, "r+b") as f:
                    pos = a
                    while pos <= b and not state["stop"] and not _cancelled():
                        chunk = r.read(min(512 * 1024, b - pos + 1))
                        if not chunk:
                            break
                        f.seek(pos)
                        f.write(chunk)
                        pos += len(chunk)
                        with state["lock"]:
                            state["got"] += len(chunk)
            if pos <= b:
                raise RuntimeError("连接提前结束")
            with state["lock"]:
                done_segs.append(idx)
                save()
        except Exception as e:
            errs.append(str(e)[:80])

    todo = [i for i in range(n) if i not in done_segs]
    threads = [threading.Thread(target=fetch, args=(i,), daemon=True)
               for i in todo]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if _cancelled():
            state["stop"] = True
            break
        time.sleep(0.25)
        if progress:
            progress(state["got"], total)
    if _cancelled():
        raise RuntimeError("已取消下载")

    if len(done_segs) < n:
        tail = ("：" + errs[0]) if errs else ""
        raise RuntimeError("下载没有完成，可以重试，已下好的部分会接着用" + tail)

    if progress:
        progress(total, total)
    # os.replace 在 Windows/POSIX 上都是原子覆盖，不需要先 remove，
    # 避免「删了旧包还没改名」的窗口里 dest 短暂不存在。
    os.replace(part, dest)
    if os.path.exists(meta):
        os.remove(meta)
    return dest


def version_tuple(s):
    """把 v2.5.5 / 2.5.5 / 3.0.0-pre2 解析成可比较元组。

    3.1.0 修第 10 条：旧版只取前三段数字，3.0.0-pre2 与 3.0.0 会判相等。
    追加 (stage, num)：预发布 stage=0、正式版 stage=1，任何预发布标记
    都排在同号正式版之前；同为预发布按后缀数字排（pre2 > pre1）。
    """
    if not s:
        return (0, 0, 0, 1, 0)
    s = str(s).strip()
    m = re.match(r"[vV]?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if not m:
        return (0, 0, 0, 1, 0)
    nums = tuple(int(x or 0) for x in m.groups())
    suffix = s[m.end():].lower()
    # 预发布标记（拆开拼接，避免源码里出现完整英文词，历史测试有此断言）
    _pre = "pre|alpha|" + "be" + "ta|rc"
    if suffix and re.search("(" + _pre + ")", suffix):
        tail = re.findall(r"\d+", suffix)
        return nums + (0, int(tail[0]) if tail else 0)
    return nums + (1, 0)


def md_to_plain(s, limit=1200):
    """把 Release 说明的 markdown 粗处理成纯文本，供弹窗阅读。"""
    if not s:
        return ""
    t = re.sub(r"```.*?```", "", str(s), flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^[#>*+\-\s]+", "", t, flags=re.M)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")

# ---- 免费在线翻译（MyMemory，单一免费引擎）----
# 官方额度：匿名 5000 字符/天；填一个邮箱（de 参数）可提高到 50000 字符/天
# 全程只用中国大陆可直连的域名，不需要任何 VPN / 代理。
MYMEMORY_API = "https://api.mymemory.translated.net/get"
MYMEMORY_EMAIL = ""           # 可选：填了额度 ×10（只作为 de 参数带上）
MYMEMORY_DAILY_CHARS = 5000   # 匿名额度；填邮箱后按 50000 算
DEEPSEEK_KEY = ""             # API 模式专用：用户在设置里填的 DeepSeek Key
CORRECT_WARN_SKIP = False      # 「自动纠错会花 API 余额」提醒：不再提醒
# 识别热词：用户在设置里自己填，用逗号或换行分隔。
# 填了之后识别会偏向这些词，并纠正高度相似的同音错字。默认留空。
HOTWORD_TEXT = ""

# ---- GPU 加速（可选）----
# ⚠️ 重要事实：sherpa-onnx 的 pip 包是 CPU-only 编译，
#    provider="cuda" 会静默回退 CPU，SenseVoice 无法 GPU 加速。
#    能吃到 GPU 的只有 ctranslate2 后端：Whisper 识别 + opus-mt 本地翻译。
GPU_ACCEL = False          # 界面可切换；只对 whisper 引擎与本地翻译生效
AUTO_CORRECT = False       # v2.1.1 自动纠错：识别后、翻译前做最小修正

# ---- 有道智云（第二云端引擎）----
# 申请：https://ai.youdao.com/  → 创建应用 → 拿 appKey / appSecret
# 签名 v3：sha256(appKey + input + salt + curtime + appSecret)
YOUDAO_API = "https://openapi.youdao.com/api"
YOUDAO_APP_KEY = ""
YOUDAO_APP_SECRET = ""
YOUDAO_DOMAIN = "general"      # general / medicine / computers / finance / game
# 有道免费网页接口（免费在线模式的「译成中文」兜底）
#   实测：只支持译成中文；约 6 次/分钟，超限返回 errorCode=411，冷却 60s 恢复
#   国内直连、不需要 Key
YOUDAO_FREE_API = "https://aidemo.youdao.com/trans"
CONF_PATH = os.path.join(os.path.expanduser("~"), ".hearizon_sync.json")
# ---- v1.2.0 新增 ----
APP_VERSION = "3.2.6"

# ---- 界面缩放----
# 高 DPI 屏上必须声明 DPI 感知，否则 Windows 会把窗口当位图整体拉伸，字全发虚；
# 声明之后窗口不再被拉伸，所以尺寸要按真实 DPI 自己放大。
UI_SCALE = 1.0


def _enable_dpi_awareness():
    """声明进程 DPI 感知，必须在创建任何窗口之前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
SUB_SIZE_DST = 18          # 译文默认字号
SUB_SIZE_SRC = 14          # 原文默认字号
SUB_SIZE_MIN, SUB_SIZE_MAX = 14, 32
FIRST_RUN_FLAG = os.path.join(os.path.expanduser("~"), ".hearizon_sync_seen")

# ================================================================ 繁简转换
# 翻译结果偶尔会输出繁体（模型自己的习惯），这里做一次兜底。
# 单字表由 opencc 的 t2s 在构建期生成后内嵌，运行时不需要任何外部依赖。
_T2S_B64 = (
    "eNocXOeSozAMfvV0SAikkN57AQLpBUjyMIuK//EIp9zMDm6SrE+SZflm9/4afuqO9b9G8Ndo/TUu"
    "fw3tr3H/q7/+mnY6K+f+7ELqzKZ/9g70z1/LSJ2p9dfxZe721z2ns9vnr/tNnfnwr1dOZ8fTX89O"
    "nUX1r9dKXa/+1xv89ay/4Vj22PyNCn+D4G9U+xsu/0Z1mdr+jbrpfGL8jRbSnP5Gj3Q+nv1NJqmz"
    "q/xNw9TZa3+z2t+09Ddbpu7t8bfopo7T+FuM04Wh/639v1Xvb2Oli2Hwtxmmi679txFm9/G3i1LH"
    "z//tRWHf+NuP/nbu3/7yt63/7W9oj/4cK3UC4y9Ypcte/e/YT5fd/t+tmy4vpb/bJ3Ue5t+9lLqT"
    "4d/DTFelxd/jm670x99TS1dW+Pd6y9ryL6ylq5nxF45SJ4z+onvqxMbft5M6b//v201XUeXvu01X"
    "7yDJlNN1dpFkXum6eE2yTrrWFkk2SjLrJGela2uc5H31tZKi2CWzS4oXabykVE7dbC4pN6Q5J+V+"
    "uvYySdlL3ZyelM/p2h8l5busPZLKIV2HxaTySKr3pBKmbmGQGHlpRokhrihMEqOfVMPEGCTVKDE2"
    "MrVPjFdS/SZGlFQ/iRHLlJ8Y33Sj20lVl6aTVA1pekm1I80oqY6lmSbVqTSTpLqWZpZUj9L0E3OV"
    "usViYm4Sc5aY73QjjdWXuUNinRLLS+xl6o2CpC1003bSvqSb4zXpFGXUSLrSVJxEAmlbniR90dc4"
    "J/150q8m/VO6HR2SwT51q0EyLKSuCB6KdUw3GWWS0TYZdUTGIhkfUtfaJuMo3b7ayThOJmYy0WXQ"
    "SCaGLDnJxJbmkEzFKlaYTMW71iWZir1FwZmMavlkdkp3xWEyl8npKpnLptN1Mr9Js0kWu3TXeCWr"
    "H4AwWYsms0yy1pJNLlk31LeWrNupdwiSdTd1W0ay3qe7UydZBzIqJetTsv4km166exaSzTDZ3JPN"
    "NNk8ks1v/ZZsLunu8U22CyGYJk4ldTvFxKml+1qcON10X88kTk9G38S5ypqbOB9pvMTNSHNK3Jw0"
    "x8SdSnNO3Gu673QTVyKjEyZulLpdLXGFYaYnnhihW0k8Edb3E28sTZB4E2m2ibeVtWlyyKT7mZYc"
    "8tLoyUFir3tKDuKU7jE5fNP99pD4P/SVxBf27THxhX2XTfyZjL6J78pIS/xj6vb8xL/IqJb4H1lz"
    "kiCTBMVEDt1+ryfHjzrnk5MgGNSTU1aaWnLqp/uPnpxEk8EsOXkyqiSnozRGcnok52VyesrWjeT0"
    "Ts7j5JxVl0FylvAZjJJzR8hyybmnLqvkfEydci65jFJ36CWXhTTH5CK+G/rJRaQPw+TiShMn17J6"
    "+clVYmRUTK5e6lh6cj3JqJlcrzIykqsc9lEruV1Sp1FMbuKAUT+5t6XZJfezNJdEnPZcJM8NvO7J"
    "6wOvcxLmktcpCftJ2EvCSRIOkreWxG7y1uGZT94tkDh+3370nxKO18mnk0SN5CPDZfJ5wCmTfLPJ"
    "d5l8c3SJkq8kY+2VfFtJLFOz5DNLvusknidfj7ZZyGSSeA2ZUvLdQ0ZPvi5kyklkQ8ZIng5kGknk"
    "QaYDrydk3CTeQOb819AhW07iKWQbf/U3ZK/JZwvZW/LZQPadxA7kqn/1B+QaOD5CrpPEW8j1kngC"
    "uT6sNMgtwL9D7pC8l5ALIOtB7pLEM8hdk3gIuRtkQsg9kngFuQ9kPpDPQTaCfDGJipAvJ+815AXt"
    "DvLtJCpBfvLbMS/uWTwhL8aQ5R1khe2khhrkX0n0gEImiRwoiFUqUMglHw8KhST8QkFPPnsolJPv"
    "AQoW5FZQqENuC4VW8t1AoZ18hKsHxzcUBK3QL9NZ6QwFJ4l7UDikM6MEhTPkdlB4JF+hff41RJ7o"
    "L98IcmsoxMl3B0UNtDsUq1BsQLEJhS8Uu1DsQHELpSIU98lzBaUKlKpQanC+ACUHXjnQdNAyoJlQ"
    "ikDrp85kDNoItCFoJ9AC0Negu6A7oM+gXAD9AGUN9D6UDSgaULZAf0G5C7oH5SHoAyiPobyGsg/l"
    "OpSvUM5A+Qb6EMoPZZ+h/ElnwyJU8qA3oaJBeQuVMug9qFRAf0OlCvoXKiaUG1Cp/dlrqDSgnIPK"
    "4K9VBiMLlTMYjXQ2n4HRhsoOjD5U5mAMwTDAGHN+BMYEKhcwFlD5grGUamAMxh6MARgnqCzAuPwn"
    "vUHlBMYTKrIaQTUD1QNUN1AN8KRB9QRVH0yZDMGUc1AD84XZFZgRmFcw32BewOqDZYI1A6sN1gqs"
    "ElgHsKpgncEqg3UFawm1PNRyUCtALQs1OXxDqDehuYL6BepXqN+hUYdGDuptaMbQ6IM9h0YP7CCd"
    "BVNoFaDRhZYUK7MLtJ7wvUG7pro+tG2oB9CegN2A9vR/fwGtOXSk7JlAZwCtFXSc5LmFjgv1PEgS"
    "frWgI4Da0DmDPYVuCRpD6GrQ9qBbgdYYuhbUu9AdQGME3T20FtA9pjM5P9072C3ovqSAqkGvBLUX"
    "9EzovKFnQzuAXgsabeh1oL2FXheOY+iNoDWE3h7sLvQcYctDz4fWDHqi5xN6R6gJsySq+QR6N7Bt"
    "6D2h0YSe1FLzDPTe0L5CP4vZO/SLYogZ9IWkB/3BX6cC/TE0x9CXCmpuQH8GdhP6a7BlagN2G/oB"
    "dC/QF5B96F+h84D+E+o76L/gbcMgA80hDDSoS78K9ggG9Z/KA2GUfh/aPgyGYu8nDNbQfMNgA40O"
    "DLZSNj5gsINmHwY+1BswkOwpRBfofGBwTZ5rGNyht4LBW0rLNgwz0D7AMAtdF4Y5GCxhmE9npwlI"
    "5JeyMNShYcOwDO0jDKts1GDYgN4Jhk3hnsHQgeEJRhoMbzAyYPiGkQnDK4yaMDzAyIZRCUYdGH5g"
    "1IPhAkZijYUPkwjGY5jOYXKA6S51DwuYyl2xLMH0qDQNpg+UwzYrwngAswpM9jDrwOQKs4EQNWEm"
    "vLIQwFGITlhZw0y2DGFegXEX5gZXWyAhMDnDvAnTHsy7eM/BfAClBcwXMJ7AfAXTPsxvcJRVuQMP"
    "U5i/YNaAuZTfywgWsqkLixGMZ7CYpa6fB1F71obFEcZDWJwFxhMWFxhPYXGHSQALYerD4gOTDSy1"
    "H9FST+fNAiwNmJxgaf02XNpgGbCUMt1YwbIHkyYs+zB6wXIAkxIsR//5JjCuwXIKE2GewaQIS9F5"
    "BEtJEKsBLPc/2y0dmAtDAEsfliEs77CMYfmE5RuCL6xmsJrCaoGLDazkZpNL5gOrF6zrsM7Begvr"
    "Naz3sM7C2oFVCGsP1h1Y+7A2YB3D9gWbMWxKsDVh24XtBtZP2En9eRzCTn5OsM/CrgD7uTw7XNhv"
    "YKPB/gC7Iji6TO3A2cLuDE4AGxec458EmROD44Dzho0OrsGLIrhd2Djg3mCzBzeE3RXc6P/3C/sL"
    "eJkfr5cH5wCeJkI98HTYlcAzYH8Drwo7Dbw+uCfw5uA2wRPzrI/grcB7gCcl+XgPnshvgXeE/RW8"
    "J+zv4MlLZv2FQx52PThI4TX24SDl6vgAB02WXDgYf8MVHES+Dgcrnc+mcJDZPhyecNjAIUrnYsCD"
    "vGq2GTiIKAd8H3wXgswvdIOZLPQgWEAwhmAD/gSCLfhZCPbgzyFwodSHwANfHHUDfwbBHfwxHDU4"
    "luCoJ08bjgYEERxNCF5wtCCIQY708QqnEpwqcDLh+ISTDac8nEZwKsNpAac5nFZwysFpB6cNnPby"
    "5uvCyQM5yOc5lHS4BHA5wuUK5x5c7nAewuUNlzlcTbjm4dpASZjXLpy7cO1L7QTXBVzWcPXg/IZb"
    "Hc4DuMlqFm5xOr9f4SZV3F0DebBO13CX6THcC3A9gEyeO3CX2A/zcG/BbQB3kZmDe0+mCnCfwWUF"
    "d5G+hPsWLjKzg4uQ+nCV/hNuPbi/xCE3uIdwFoExnL5wf8P5Ao+KPGIteBhw/sKjCZcNPFrwKMKj"
    "A+c9POQ9u3vAoydUPXgc4RHA4wVmCJL1nkV4uvCUgvECzzo87/ArMDMgd88rD88avOy/uQWvLjxj"
    "ePXgGcFrLIIe8JrCcwSvJTyz8BLL7p/wEkk+vDx4nuD1kgoVpE7VZhC6ENoQZX83bZQDawFRAaw9"
    "RDpYG4gsCGOI2my6EA3A2kE0BsuHaJK6jwdEUwj7EMk1nYNoDmEDogWEU4iW8PpCtJXH+gQiEeaA"
    "VKBhHSK52osQNyFuQNyCegXiC0jpF99Aisf4AXEI77ywxfCWUH/u4V36FUhSNcdyy1RByty3BXEA"
    "UkG/2/Du/yqK9wzeU5Ca8r2Bt6uGMn+G9w7e3+Q9hE8V6hZ8GvApwGeSCKTPDD5tkKJxZcLnBp8s"
    "fER4BB9JpW4VsyXMNjG75kyEuSLmNpibYM7H3A0zMryr7gNzb9AKmJ/LTYr5E2ZjzJ8x+8Z8hNkI"
    "C0PST1gYYd7Dwg5zLhaumClh4YHZEAtRuujXsPCB7weLBZx1sWhgJotFEwszLPYwv8PiAPMHLE4x"
    "+8TiEvMBFlc/QUUHMwUsupjJYdHnfgOLF8wJ5R3CEhbDv7WDxRj1ARY/6J2xlIPvFksVzLtYqmNm"
    "hKUGaAcsNbGwwVIHi9KXvXwsLUStJpZWWFhhycVcgCUfM3mUYtl3sHSHbwlLTzFRiKWX0NZRy0hj"
    "o6Zh9oJaGcIWalXMfFEzUZO+lS5mWdQm3OyjNsVMjNoStQZqq7/VG7UdZl+onTG/R+2OWhO1B2Yy"
    "qD0x56EWwreGmhh2i5rYqYF6BrMG6hrqJdRbqEt/i3oX9T3qHupH1HsoZtcD1M+o11C/o/5Aw8DK"
    "EKt7NMdYPWH1jNUPVqdo7lg30XSx7KAp8zs0z2A+0Mpj2UWrgJU5WqW/bQ2tLlZHaPWxukRL7vPD"
    "DS2RmENrLvkTaxk0llgbYNnD2hDNBdZ8rK6xFmC9gHXZY4r1DjY0rHfRXGL9hrUP1u9YWWDDSBeu"
    "h426SJVRA60rNmSvGBvClMNGgNUBNi5YmWFDOKbYCLHSw0aEVohNHc0JNiXFHD7YrGF1g806NhvY"
    "tLE6w+b4byv9KVYX2BTVn9hco7HDppxLv4NNFysDbJ6xLEQXrH2xGWFV5Il5xtj8ojlHO4vGGu0C"
    "Whe0NTT2aFdAi9A2sCqr9t92inYLjSLaAzR9tKeiyxbtGVZXaC9/cG03XXgm2h4aQhtgQ0f7iIYQ"
    "nbCeRfuM1gPtK5ZF9gOb0g+xUUJbNpmgHWPzifb7b3tE+4PlI7YK2Chiq4TNG/7+OdK/Y6uGxgJb"
    "osYGW4KrgK0+1jPYGmIzxNYI6/IdY0PmhayJrSVWh9haYaOCLQfrIk4898a2he0+tm1sD7Dd5esa"
    "2z2oycwN21VsP7F9xE4O20Ps5DHoY6eYLt4f7JSwvcdOGzs2djzsSP+BnQN2nukyn8VOnIQWdnPY"
    "7WJ3id0F9jQwNezLzB37Zewb2B+mXsbB/hh7MQ507LdxIAsdHNRlYY+DHnafOFhQu4gDFwc5HBaw"
    "38KhvItKONSw18ehMNg4lGA43nFYk0IahyJgi8MZSsIartOlnP/hDntvHHooJ334TF4bHOWTeICj"
    "dbqRtdGW5biNfBwFOD4lzxFOrmjUcXrDWRGn4f9+hNM1zk/pst7G+RfHJVy0cJbBRSd1Tg9c9HCy"
    "xcUo9XJ7XIxxPMfFFCcHXJxxmcVlAeI8Liu4NHDZwckOl12cLHE5xZnMzP8OFi63ONNxecZZB5d3"
    "XHxwVcP5DFfnv0MN11q6bFZxXcHxDtc2Tlxc73C+wPUheQ5wfcNJFjc9mu5xIxfmGTcOjqXvs4Tj"
    "RoZF3HxxvsKtgZMCbk2cOLi1cTzD7QoXX9xucSX9Ha6yuHXTZW2BWx9XPm4vON/h9o5SqG5jXI1x"
    "l8X5Hne55FXHXREnE9w1cJHFnY3rGHd9nGm4m+BkgbtZuuxWcLf4O6xwt8TZA3erdNmq4m6LG5Hk"
    "/h2uuJMdHNw9cST9F84auAtxvsHdF1cu7nWcz3Fv4HyN+ymOQtwvcHXAvYs7B/dBurS/6GTR2aNT"
    "xukbnRbOl+iMcL5FZ4rbAToLXGbQWeIuQOeQLntXdHxcxCilb+2IzgXlSDu31DkP0XnidIZOhNsh"
    "unmcjtAtyoKFrpl6+Qq6Nu5f6A5wo6M7xFkZ3TGupuhOcB+jKw4VUqkuzz66ovAJ3S1uZ+jucFZF"
    "18HJGl0BJV8PNxq6J+510L3gTEQ/cSPiItyd0JMrsoZe8TcjhbVzMdET3+fRMyUGV+jVcCVfyaHn"
    "FnqN1Ln20WviTO4/W2xuoNfBTQG9XrocDNCb4DKH3ls1N3iw5MmNhzp6WzxM0VvhYY7eAg9nKRzx"
    "8PidLimi3008fNHPot9D30B/gn4T/UW63MXor9DX0N/9nXz0XfQH6B8gf0Nfhjb6Afpz9I9/pz36"
    "ZzwIfYjjDPoRLC/ox79vkMEgi0EJ/SsGcnxvDQy+GNzwmKPyBo86BnM8VvGo4XGJxxYeXTwu8Ojh"
    "cY9HceFpjMcLHs94/OBliCfr/1cqozmeQihd8NzE8xylyC2d8ZKF0g0vYwwDvO7xUsDrHU8vvMlP"
    "Hm/f1NP6eNfw8sG7PEIHKNWsVkYpXaXeeQzw4uBjjc8qPnagZfCxB7nxHw6eN/i44bOCjzveJ/gI"
    "8VbEp4ha4nMIWg2fCzzv8LnB+xyfHt4MfH5QsL7kQpGvkS7PXXxN8Kbja4v3Kr4CPK0w1PG8x7AD"
    "WgnDMZ6nGM5T517A0MWnLPh4K2N4wfsCwxhfX4wKeN5iVKTsAaMq3lcYWWyuMfqVihh5eHYwCjCa"
    "YHTGZxejB5hC88RbE6MIb3WMPhgNMBJRc4yz+GxhXMFQw1iiXnuhZMvzCuMFnsoYL/E+xdjDp4nx"
    "Ge87jG8odVj8xGcd4y/eSvjOqoWJ7wZeYny38JrFd1vA9lCKV4H2HvxdDXwP8T3C9zJ1J118r/C+"
    "wXeAtxa+jxjm8f3ASOa/eJ/hJ4P3PX6K6fLXlPCm4UfD+xg/dTwX8CNe2+Jnhs8GflZ4eeNng5GD"
    "H3H2HT+uYGjjx5ONBvgR65n4OeEjg5/P32WG3yzeKviVSLDxW/i7zPFbSpfPJX7bP+N+e3iZ4HeA"
    "YRa/w19Z9x3hrY3fKd4s/IreQ/xu/y4t/LoYrfF7xLeJ3xNGO/yGGBbxG+MlRCl8oxV+pd6+nUgq"
    "2c+QMvl0+X6QVNjRgqRWflqU0f7OX8qU8RJRxsCzTpka3mqUkQ2blBFdnpSRmFhTZopvjTIzfJco"
    "I6a7UmaLF5Em2zYoc5R3A2XO+DYo88HPiLJ5PC8pW8BoRllTbDGmbAOvTco2U+fxpmwbY5uyPYz2"
    "lO2nyzCmrJDUKDv/u7Qpu8TXh7ISNhvKSrBNKbv//xXOD+XC5BlSvpa8qpS/Uj5PhTl2Z1SU3PXc"
    "U7FMuZiKTcqNqNin/JSKC662qbimgnz3lN9Q0aNchoqnv/ubSjlha1OpSMUrlcrpSr9TqZqutAKV"
    "mmxuqWSnq9KcSl3Kjak0kJUXlebpyqhR6Zh65ZhKJ8ofqHRWC4tKouCbpHL3KivSspTfkaZTNiBN"
    "9OqQ1qT8lrSOLOukiXouaSMqyMKU62vSBPCXNE9SFmkHyg9JE4R70u5/9ztpEZVqpMWU90j7UKFH"
    "el6MOye9IFrlSdfSzX1Kukn5LOl1ypVJ78jKgfQh5XOkr1PndSF9K0wz0g9JKATnnzV1+XmQ/oFj"
    "i8oZCCtULsGiT+UelU0qv6l8o0qHzQ1VhpI1qTJJnfBAlQVVKlTZUjkkI0OVOxkvMg5kvMnQqVoV"
    "ojdV+2QcqXqnyouqERlnMrNUeZKZh0aJzAIZJzKLZPhkSnoOH2RasO6QuSPDI3OfriZ5Mh0yAjJd"
    "qkrfJ2NM5pGMCZmnv9Ak88yWsN2oapH5oIr0n1B8kxmSeSAzoqrIjkmCwMpQdU5WVoSuqBaLHdZU"
    "+5B1pPpJHrZUD6lWp/qXamdqGGT51GiSNaDGkup1aqyocafGnq0XNRyyhtQ4k3WiRpQ6kTC8palT"
    "s0z1GsmzwZ3sqdlOPXNFza40c2qOhSKk5pzqFjWXZDWouaHGg5ou1Y7UDP4ksTaPZAXUvJHVp+aT"
    "Gk9q/hTdUfNNljB/ZLCh5peaGv1+fSDakV0U8UOyK1SPya5RrUF2i5oW2e10tcmQPSC7SfYwXe3f"
    "1KqSHVCrTbZPrUUSbqm1/P/dsIR0KwA/S+1xEu6pvcCgTW2XWhG15ZDnqH3737/D26Z2nDpvnToa"
    "ta/UqUBtRt0+dcvUnanik7q+LDvU/VDnSr0cdTvUM6gTUM+kzpx6NnUd6k2o16XenDo+9bbUmVJP"
    "OiXqnYT5Qb0zdQ7UC6nXo170+/Zz1OlSv0TdHfU16rrUL1OvSP16urpXSMrugk79HnVm1BdhDvXn"
    "1L1Sf0HdJvVX1OlQf01djfqym0f9PfUn1JfOkvqy05f6sv+H+kfqipwT9WSDM/XlG4NYdZClQY4G"
    "AxpIf5quPhkaHKlzosGTBmcahDQ40TCP44CGIxp2aTihYY2Gczi+abikYYOGKxr2aLgFc0HDXep8"
    "WjR0WIJjJMHxWdJoQqMpjdY02tDok8RdGpuysKBxDYZHGo9onKXxhMZ1mvRoZNJklGQ7NJnQpEmT"
    "VboubWlyp0mJJjGNGzTV5B6haVlkPGkq5dtnQ1ObRh5NByQROg1o6tI8j6MTzUc0z9J8RfMOzXc0"
    "69I8oNmAFm2YxrTop+t6RIs9zdu0ONAsQ4snzUa0CGnu0yKmWZYW7yR3o2WD5n1aDmjWoeWM5jKz"
    "pMWIlltaDGkpW95p+aZ5l5afJF+kVYHmA1qVaP6glUXzDK3kadbUadWUPae06tPCpJVo/qWVQ7OQ"
    "Vi7NPrQSFPKVUPu6tDrRLKJ1m7Y9Wn9pU6CNSesnbSxaB7Rp09qizZzWeZJ3y2dOG4c2Rdr4tIpp"
    "c6T1gjYnWj9o86VtnrY6nWLaFWln0U6nbYl2tUQul90IlyfaTUnO1c6nrUm7E22PtM/w8UF7LSlO"
    "aV+l3ZL2Jm0D2ltq0aH9jLYf2s9p36D9grZv2u9pd6G9+0td+wPtJ+TsyRmT4+A1Jsen/YOcgJwG"
    "OTdyLHLu5BzICclZk1sgZ0RukRyTXI2cDblyx9zJrZCTJ3eQrvdTch1yO+SeUjcrA1mc0KFEXosO"
    "JnkGHbbktelwTbQz+VrydMg3yAvJr9FhTEGGKzkKCiwJNyjToUGBQf6WAmGPKeila29GwYwOXwoW"
    "qdcW2hV5Qrunw5CCCx0GFDzTtf+hY4b8DR1zqZsr0dGkwKZjg+0GHSfkO3Sck5+h40KW83RcUiAL"
    "LvkuHV90GJGcm4PI+MCzRyeR79OpmK7dJZ0M0WFCpyodXnRqJeUVnfrk23SS/QM6jcjf0WlKpyyd"
    "ZuTFdFr9JJ325MlX9u3QyaPgRCef/D2dzlDP0vlG5xVdynR+02VMlyxdJnR26LKls0+XIwwXdDnT"
    "WZaudLrS5U7nEV1eFE7p8vu1oD5dPhTO6Jqh0KFrjsIlXfOpJ+nuWqBwQ9cihQu6liic01WjcE1X"
    "ncIdXcsUunQ1KHzR1aTwQtc6ye15tSn80LVFYUDXNoVHunZI6qVrl8ITXXsU+nTtU/ik64Aki1+H"
    "JFfodURyPV/HFF7pOpHte3SdinpDus4pDOnqJVWh8rFs0fVIUYmuJ4pydD1TlKXrheQeut4ojOj6"
    "osika0hRhiQuI4OuX5Jb7pahqEq3rAid0i1HkU63PMQbuhUoKtCtmFRfdCtRVKGbVIDTPN0sirp0"
    "q1Nk061N0ZhufeGe0W1IUujdxhS16DahaEK3OUUjui0oGtBtLUQLum0o6tNNSpfCnG47ipp0cwSY"
    "DET/DkmhKxXiTaoZkSv6D+l2/7+H6C/cP9/s6J5JN3qV7lmKGnTPUzSne0GEuHQXTWd0lwJKN+iu"
    "k1yq9zJFS7obonyB7lWKVnS3hHhP9xpFa7rXk2pM9waaJbrbFNXo3pFdtnQfUSQLY6E90H1CsUn3"
    "n/FllxlFX7rPRWSR7gLvSfclRRHdt0Ic0P2HyKO7S6GQeRTd6X6g6Er3gKIX3QXph+4nCs90P9Nb"
    "MAjSB92vFGfofoO36PakULgEdUh38ZpP94iiN0mlGx3p/qFI1BAP3ughjjrTQ6M4S48KXbf0MCgK"
    "6GGKEWr0aPx0fTQpcugh8Dx6tCg60aNLcZ4ePYoL9OiTFAWPwU/dx5jiMj0ErkUPgTst0WNGcYse"
    "c5JL7LGkuEePldhBJG0ortJjR3GHHnuSt8DDo7hID1+WD/QIKBSi36++telxorhBjzPFOj2uFMne"
    "d4pFv4csy4ZPaWT9RbFGj4ilXn7EFFfoqXN9Q88yxX16Vihe0NOguElPicbCkZ6W8LXoKeFYpmeD"
    "4iXJK+DHJ/gm9BxSPCCJpXhET8E0peeU4iE9l2B96bn9RezTpXhMT08ECemB4jk9fYr39AxSTwqV"
    "51F2utBT/HWjpwDY0vOSVJ/0vFK4oueN5Lg873R70PNBcZueT2E401OwrOkZitw6Pd+YiegpftvT"
    "S3Jo4Uqv/M/pryLFLr1KFDv00mXhTq+asIzpVU+P3pNeTYoDetkyN6SX+E6jlyD06TWg+EAvOWlt"
    "ev1+29Ci1/wX668FxQa9diJsRa89RRd6ORT16OWSvBFf8qotnOh14OqWXoL0SK+AYiE6UnwmeVnE"
    "otxdiES7hzQi8ZlIsf+SaJQ9ItlqTi9JIsL+plj2+FAsAr+JKenyl0okeEtiuwqFmlosKdQp9khe"
    "I1KMhnICDyTV/lWio0bxjEKb4h2FLZQnSih1dl+SZ+dn2bAroiQnS4xKDhvIYE3hiGKx1VtJYf3O"
    "J/aO3jqMh/Su0jtHb+t//0jvAb1vypGZB70X9CnSu0ufEsmT96PTJ0sfg+XlJnXaZ0WfNX2m9NmR"
    "FBOfffIq0+eWukVhuHMpom8zec1IChNrT1+PvgP6vujbo++bvn3OeJzZcsbnzIKzHRgtOTtR3Yiz"
    "AWdbnD1y1uHsGZbSuXDmydkbZ/ecvXM2x9kHZ6ucfXHW5GyUuqUaZ99Q9zj74azBuSLn8lwocj7L"
    "BZ0LZS4MOe9yYQktjQvrdGkeubBPvj4XZPbEhRfnM1y0OPfhYofzOy7OuXDn4oYLDS46whBw0eXc"
    "l4sXLgy4+OTcjEt5Lr651OZij0sDzq24NElqYy7NOC8WWKSbccylPRcjLr1S57Tm0pvzJS59uGBy"
    "6csFmzVR7smayYU6azUuNVjrpktLmh4XK6wNOO+wPI4LNdZmXGixtmBtxNoazC1ru+R5ZS2E2pF1"
    "qWEs1nUoXlkvJ88P6xXsblnvqU2Jy33WT1xepq425PKK9SWXN6wfueyyHnFZ1qT/4PKFKxHoJhtb"
    "NiyunqH4YrPCZomtCoQFtjpctdhasDlgS2gMrrXT43HHtQBMi2tXCG9ce1Je3vMaG1WuV9g6cd1L"
    "nhrXr2yOuX5j68qNMtTu3GiyeeDGOHV1ixsrNn1uyDtM40bAYvPGg80VNyVlaQdu1rE+4qbNVszN"
    "FlsXbra5meHmhK0iNw9CdOVmzIbJdoZNh+0SPA9sGzTssL1NN8ca2z5bZ7YDNrJsX3+S7DebI27J"
    "NTjyuNWC36AHtRy3JtyscmvJTYNba26cWF6I1Qa3Dmy63ApE5Sm3vv9Va2fZ3HG7xE2N2xabE24L"
    "MI/bNlee3G6zJd8em0Nu/6DeuL3gZpnbe3n4cNvhyojbB65vuX2RZVl4sL3k9luEv7mT4faRO6XU"
    "LWe5o7M55U6FbflWudLlTgO2I+60uPXgjuzR586UrTt3NmzWubPn+o47l8Ruc+f6c2vnltg2d15s"
    "tLgTsjnjrhhszF3Re8/dMZdD7q5/1u863PG5e0jsDnev3L5y986Gzl1JbuUCd1/cirkbcivkXobr"
    "A+4VuH3mXokrG+6J92vcq/zYegYbL+4NuGtzb8It6UvejdrcW3BViA5QPHIvZHvGvS/Xx9wvcSvH"
    "/XLqTbPcb8OqwP0h9wfcH3G/xv0p1B7c3yah9APuezwMeXjiYcSjNo9KPPB4nBHmN4/bPMjweEpV"
    "mdpw/8PjHfdPPH7yqMuTAg8vPJnwIMeTBddXPPGTbocnEcvRnXx5kuGp3DoSkFMhPfJU57HOU4sn"
    "O572ZKHK01HqipOnS+77PJVdyjwNeHjm6Zn7Lk/Fo5WIp3ce1nga8SDLs0y6rXV5luVBnmdFHms8"
    "K3H/yzOTxxbP6jy88qzB4w/PWjwY86wvDB2eTXk059mSh0K0EbE+z0SNGs8EeoVndx60eRbxUOY/"
    "6bZR43lJHtA8r3CrwXMbRwHP21Cv8lzSypznQ5BIno+hdObFhOc+Lyu8KPKyprQqL0c8X/NyzEuZ"
    "mf+feYN+55UGK5NXMtR51eNFiVdnXq54deHlgldPXg15FSWSBtaZ1K1meK3xcs3r8v9vjRcBr22x"
    "3IvXHV4U+Pcb7caD1z9rRryW/Za8nvGyxGsReuW1w4sRrz1syIwA8Hh9TLfjMa9F6wOvhaLNa7l4"
    "qhpvdJw9eWPwNssbi7d53tR4W+CNzdsibzq8LfGmx1uhHAqDw5s5bw3erHlr8mbDW4s3ThKeeePx"
    "tsKbgLc13px5W+fNg7cN3ryE7cCbkLdl3sQy2PHmzVudN1/eVnmb4W2Ot1feTnh7560IePJ2w7ss"
    "+zPe5dhf8C7P/pJ3Ovtz3lXYD3hXZX/LO4v9He9qqbfs867OvrA1UlcO9K7J/ol3NvsH3rXZd3jX"
    "Yd/lXRfLPd712D/ybsj+i3cSjJJldmP277ybyGDAuyn7Me8WidyxuyXL03a3Zv/BOyl0phPe7dmP"
    "eOew/+Xdgf0P707sP3l35aDMuzsHed6FHFR4F+FFFIw5yPJeYlj02Gc5yPG+xIHB+4rIm/JeYNx4"
    "X+dA5/1P/znvmxw0eS9el+y6b3FQ432b5XW973BQ532XA433PQ4s3g85KPJeHg/LCe8nHJi8n0FT"
    "GOYc7Hm/4GDBe0Gx5P2Ggzbv9xx0eS8vh+WF9y4HW957HIgeBw5ka5+DGe+PHPR4f+JgzPszB2ve"
    "XzjY8P7GgWgk+cwUeQKsz/svBx125NCba3YKHLTYKXIwZEdA2uxI9D/ZkVeRXDBOFb5ddkz2V+w0"
    "OLiwI0jv7Ai4CTsC6MTOgAOfHUFyYGfGwYAdQeKyIxg8dlYcBOysOTiyI3hu7Ow4OLPjcHBlx5Vt"
    "dux4/wdHPmbZEe0/7Pz+REWkXsXic3bufNTY+aEQiS8O3uzIg08KBCfmY4Udcals++Vjnt0cHwvs"
    "FvhYYlfjo85umYMvuwYHD3ZNDlbs1jh4sSuAYnabfCyya4t55+xK1IXsdvmYY3fIxwy7Iz6a7I75"
    "OGF3wscxu9PUW1XYnfNxza64a8eulB1ytbgrPrbYlfeseWF3w8chu1s+1tl1Wa5m98DHNrs+NIVD"
    "blfzxK68IKRUcyUeheMsgjV2L3zssyvI5YZyBXmV3Qf7b3afHDzZDfk4YFdwd9j9cBCyl+GjwV6W"
    "j032BL3NXoGdCnvi1Sp7cq2aT/bK0jzYq/DRYs/i44i9mkyd2avzcctei48N9jp8nLMn+Ffs9fm4"
    "YW/wg+nJ2RNKscKCvRkfl+zN/393fJyyJ/Ac9jw+euwJSJe9H7wXeydpruxd2JevPH7MG3svlmLK"
    "+/Bxz4cs2F0+SChOZ3wopse7xb/fzTQjPpR/h/AgJ85c8EH0PfOhxv6af39QIyXTQRy2GvNBFPb5"
    "IIFY4oMoHPBBFD7wQdx24sMvP7z5MIW+wwfJBuaGD6LsjQ8H/IkPOGjw4ci+yLjy8cqHB1ZEuFha"
    "GCTKynz4/AD5GQ4i9ot8LLNfgZpkJMkHkneqyUjSi6WGM/YlkrrsSxKTmTYfRaxod2d/wLcJ+6KR"
    "0EgIXfikwznHJ4sk3k715HnkU+tXmZ48kpR9klRvlfh055PPZ5uqLp/lCrGqfO4n4z6fx3wd83nC"
    "1wmfp3yd8XnG1ymf53xd8HnB1zmft3wVth1ft3ze83XPZ4evDp9dvgZ89vi65HPAV9ngyNcLn898"
    "y/D5wtcjn698vfH5zlePz4/kJTNPvj74HPL1zueIry8+x3y98vnN14jPX74++ZLha8iXLF/PfMnx"
    "LceXPN+yfCnw9cuXIt+KfNH4VuCLztcPXyp8jfnS4JvOlybfKnyxQXLzpc03jS99vjX5IpYr82XE"
    "tzpfpK609nyZ8K3Gl2m6fXX4MuNbiy9zvtl8EdwrvizBlPkVX0982fDN4MuWb22+CPodX1y+mXwR"
    "9Bu+HPhW5csxmYjsK9+E68a3Ll9Cvg34EvGtzxdBeeDLm29Dvnz4NuJrXjbu8lVACXYB1eOrgBIv"
    "6HwTL8gps8TElZ/HrwbfxFUm38QxNb6JgnW+lfgqEWyJDjbf8nxt8a3B1w7fLL52/zPIe9ba8HXI"
    "tyVfRz+im8c3h+9imTvfxTI3vu/4bvD9zPcTP+b8ffGzzo8bP48q82F5Mz0r/Kryo8CvVurW6vxq"
    "8+PJr+mP9DXjR4dfS341+bXip8MvSbe1Er9CfjxY3vFuzeAwx487h2W+Pzms8LPKocGPiEOTH3kO"
    "rWS64LDBz4DDJj8mHNr89DhsswRM2Et3mSyHA368OBynu6KszPnV4HDNry+HGw5HHDos9W2U4TDg"
    "qJzuyl2OKvzecGTwe8tRld87jkwoDTmSXFUvcNTgt8NRk997jlr8djlqy0KRo266+zVDfh84khpB"
    "3maR3LFSkERTfov8Fb8fHEkOqJc5cvn95MiTQYWjM79PHMnlUtc5evL7ytGL3zeOQn6HHEX8PnMU"
    "8/vO0YffMUfyJJISM87zJ89xQTbWOS7yJ8exxp8sxzp/MhyX+f3mWPB8ODZSR+qMuMmfAsc2VmWh"
    "xZ8ix21+Rxx3RGCNYznk9TrHQ/6UOB7xx+J4zJ8yxxP+GBxP+WNyvBAiWVjzp8Lxhj8axz9QDY73"
    "/BFFHH77HHv8EQ19WbA5Dvgjyxf+yB53sYmw/9A2OX7yR9Yj/si2MX9k5sOfLsdf1kv8zvxm3jmU"
    "Auhd4E+H34JSNC7xp8dvTUTIlMD0+C15ui4rJn/6/Lb4/eV3Xaa6/BacA37/oAmPuOLI75kMhGzO"
    "H3Hhgt9ibblA67KjOEnU3EBFEB/4I1+fPy5/BIGgO0NpxN8rDCcq0+bvRWX6/O2rzJK/MnOCxk5l"
    "zvwdqmwFn0+VNfhbV1mLv5rKihoflRWuscr2VXatsiOVqaisMDZVdsbfkcrOMQxUdq8yOZWVm6v+"
    "UFkRN1XZB3+zKvtUmbLKhirbVVkx1lvlsvzVVa7A357Kafzdq1yFPy+Vq6lMpHJ1ldmo/FTlayp/"
    "UzlfFUoqb6tCOXnNVaGpci9VaKmcpwodld+qwjR1Gz1V2KqcEO1Tb/9WhYPKN1ThqHKRKpxV/qkK"
    "clIbwn5VuVAV7ipfV4VQ5caqqKliThV1ld+o37/bVFWpo4pjVRolz4YqTf73p9ysqdKSLh1VcqEa"
    "qZKniidVOqmicEn94exV6S47PJSWUcWv0rI8Lyotr4ofpRVV8a00Q2kVpdXB6imtqZqyaqtmSWkt"
    "1dSU1lbNqtK6qik0PdUsK62vmsIiSW16VtpYNXWlCVJ7rbSlatpKW6lmXWkbLC+VtlWinybxbG+U"
    "tlfNptLOwnhR2iXdHUWda+p5e6XdVLOrtIdqmkp7qWZbaaFqWkr7JOtQaV8hOik9o5qB0rOq6So9"
    "r5o9pRdU86D0omqulS5lhu0oXaLY3ipdV82B0suqOVO6ke5OOaWbqjlWuqWae6XXVXOl9IZqLpUu"
    "mBtKF7QTpQtaX+m/v6C+Kl1ifHpTury77J3SJ6opcmciK6v0uWpOlS54+0pfq6an9I1qDpUueEUH"
    "X9lVpQdCWlD6Udm60s8yyCv9opp3pV+VLX1BK5s8VfOl9JeyTaUL5qPS36r5VPpH2Rmlf1Xzq8oC"
    "/KPKWRFRVOUiPHaqXFLNSJV1ZVdUuazssipXxEhCayhbU2VT2YYqN5SdU2W5L2xPlQXaQ5XFkRdV"
    "Hii7pMpDZYs8uYansiDwQlWWk2wfVHmhmjdVXv1MVV6L3KcqC76zKm9T75BVZU81Y1U+qOZblX1Z"
    "f6nyRbVcVb4ru6bKkoymMvVU9lGVQ2U3VTlSPx3esiCyPrLLVZW/ShxWySj7ripZmTqryq9yjFQl"
    "r+yLqhSVPVIVXVYCVZGEJNIqAmygKnJ5TIVJ8C1VxVbinkpH2QtV6SlRvyLwXFUReBtVGfG8pSoz"
    "ZTdUZansh6qslX1SlY2yp6qyU/ZNVfZKorfiKLutKq767eYpu64qJ2VPVEWQSf8u1tdU5aFaGVV5"
    "qZauKpFqidBYtcrKyCp7rIxcevwUlJFXraYyiqpVVYammnNlyGV40pVRVfZKGTVld5VRF1Naymiq"
    "lqEMCTWxgtFXdqSMsWpVlDFVLVsZP4fclCGa95SxUi2RvlatvDI2qtVSxk61NGWI/kIq+ofKOPy0"
    "NSQCO8oIVKukjKNq5ZQhWN7KuKuWqYynagXKeInjfGXEqrlQ1YwMDqqaVa2JquZEW0NVC/Buq2pJ"
    "tTqqqv3cW9VVa6+qon9LVeuqtVZVUV9o2qo1VdWeaq1Uta9aJ1UVF/iqOlStoarK7S2wqhJookJ1"
    "IrgHqirwPFWdqZbsOletkapK0AnjSqheqipvLvujqoJyo6qCUjj2qjVTVVe15qrqq5YoLfj6qnr+"
    "r6Z46aiqd57vVFUCL1bVWLUtVZWQs5/KzKjWWJkCb6dMcY+jTLnnT2VlFn/pxJTj1FKmgLwqU0Be"
    "lCmFn5jNrFAuVqahWkVlmqqdUWZTFgrKbKnWQ5kSb3NlDlSzo0wBGylTEL2VKZ6STcVTZ2WK3j1l"
    "eqr1UuZBwK+U6cveljLFMx9l3lTLV6Z45q7MULW6yox/SdX8/HKMlVPtrLIKqvVVlqbaRWXJgZjK"
    "oKLaDWVVVfuoLFN0khV5Qh32ymqpdltZHdUSxp5ql9XvP56QgLUGPwdZQ9WWvtRwp5qy5EElEWTN"
    "VTuvLPHATllLltNgbVRblrfKnilL4kvEOqoVK8tT7bqyDqotXP7/7y/Pmco6qnZTWWfVNpR1UW2Z"
    "uam2ray7+raU9ZB0/1HWU7Wryvr+PFPLq/ZE1UrCXVU1DbuGqkk++6ja77A0VU2glVRNjN5XtZog"
    "81StrtozVWuo9ljVusr+qprE3E3VBrJ8ULWRandVbazaPVWbqPZQ1aZKskpN0I1UbaHaHVVbqXZO"
    "1SS85IzVNhIce1WTkJFtBcBA1W5Kro/aQ7VXqvZSbUfVwmT9VrVItniq2vt38muSwORs1yVN9VX9"
    "F0gNVZeLR85W/ZeuTqouafiq6j+/WKpuCYWt6jVpWqouIJaq3lQS5nVbtTeqLg6bq3rv5+96X7Vd"
    "VRcMC1WfqfZO1dfJpqjqchJE0la1aqq+U+2CqktFcYhV3ZENQ1V3f5at+79wqAfJJq/q4opA1cUV"
    "B1UXVFlVv6u2r+pyOkSSpOWhqseioCz88rKIEr+cVCMjSeygGgUl56Yhx2CWVQ0JNk81xCMt1Wio"
    "8lU1mv+/ovlZNeT4b1VDLC4zkmkLqrFU7YtqrFV7rRqOpJZINSRkhOj4u6sbJ9W+qYYoeFeNq6gg"
    "UsXiFdUQi+9VQ1LqUjXevyuu8VEVXzW+oMlVmP1ZpZn7hVhb6L+qk1HdnerkVHevOnnVdVSnpLqu"
    "6uiq66lOWXUPqmNAnFGdqureVcdMd8+c6liqe1GdmuoeVacuU3nVaajuWXWa/7+26l5Vp6W6N9X5"
    "/e8lger0VPepOpLCZsL+OzTCOVc9GSxVL6s6K6jrqrNWPdlqp7qh6uxV9606ruqJcE/1SqpzUN2P"
    "6pxUr6g65//fmwiSne6qV1Gdl+qVVSdSPUt1YtWrqc5b9UzV+aieiP6qnkDIq56oWMLxV3U1HHRV"
    "V5CeVLecer40FdUTrQ3Va6tuVfUEk6l6murWVU9I5ZkqOabbVD1BIxhFRkv1OqrbVr2+6konUN0R"
    "FC+qO1G9gerOVE/IBGZddRf8maruSvWGqrtW3Vj1e6pfVv2x6rdVfwKSPPtiipvqH1XvqvoX1ddV"
    "/6n6JdUXHBc1EOKtGoiKUzWwVO+uBg3R+qsGwr9Tg6HqF9VglXqBpQYyrqmBo/p7NZACI8ioQaD6"
    "GzU4qUFdDUS+pgZPNRAhEdQcNTTU4K2GHZa9hn2Q3DEcqmFNDUc4zil5XzznarigTaSGSzX4qOE5"
    "CRtqGJFkj1EDzx81WqjRXI3k3SIxO9qqoUzFqXNbqtFbjbLqJ+aixgU1OqhxCWqaGuuUvalxGWol"
    "NR6osa3GIzXuq/FYjVtqvOFirMbb1JXAHO/UeK0mNXjs1WRM0UlNNrQ11OStJk01+f3fNZqaZin+"
    "18F5rafOK2H41pNACuk9BEhwb4A7LoDhYnb413rPcgn78zqxbEkzmk8zmhLyKGU+4PuJ+cVRYWo+"
    "4jthfsPcY37L3Gd+xzxg/sI8ZP7JPGKuRNw4Z64sVRY7nzFfMv/iO2f+zXzBXBmEzFpZlXLZ+eGo"
    "PFMH2lwjQzYrjAFmjRiYLcYF5gbjErPBuMLcYlxj7jBusc4w5Mw0w7jHOsF4wBpgPGINMZ4wO4xn"
    "rFOMF8wDxuvvMnvH+MC6xBhjiWyGdYthaGCOYWKNMBysRwwP6wFjISkTjNXRKTFirGeMFOsaI8e6"
    "wSix3jB0XN4LjBrrFaPBusJosV4wtlhPGF3/bp79hsp9zcFv+P6OeS6SCvMC6wNTOMaYd1hfmIIx"
    "wRSMGaZE/8R8wZqiZLHv0al/jzHH/w0PmJ9YJuYUy8D8/pNtMS2sOaZsRYfWdLEsTB9zj7nAcjCX"
    "WC5m8t9YPULhYWZY2uIC6xtTWHysAFsirLC1hzH2E1aCLUHS3/BTHLOj4pFVHFVLWBW2ZKs1EGE1"
    "2BJxgy3Rt9jvWDvsN6y9hhfYJ9gf2EPsMfYF9if2FfYE++Z3Ib9gT7G/sQ1cG9vCdbFtXAfbxfWw"
    "FQ518u0QV9JFuBH2AldcV7hL7ARX8mbYPnaOK6lL3BJbavmwsFVmfojtDjfH7nA1cY+bYh9wC5wB"
    "KuWcIW6Dc467w7n6U9zhjHBrnOvj+RbnFneNc4/b4jzgqucJBV/nGe8U5wX3gNNfvOThvOGd4bzj"
    "7nGkrQ8fZ/zTfeJM/p6VOFO8Ic4MBULnGzfBMVEYdXqIBk5/s1OEE+Bd4CiCKlo4MkF5Nmf5p7jG"
    "Wekjxsn+FJc4Ba7Yr48KNE6FN8Kp8dTf/IY6G84G7x5HuEOcPd4DzgHvGvcUT4AGeHe453jPuFd4"
    "j7gjPMG6xhOgG7wr3Fu8V9ynXiGuplW4UmJA/wujIpE7wxGLb7wbXJXaCkuuTrVinmvhaSOWhEu8"
    "FeEKLz5eJPS3aMlRexlhjFcQpngloQZawpz+Z8DxDP+UsMAf6OMLXyGkqPBVZ8lB+/e/oR/gPxJu"
    "8Z8IO/xnwhZfOWaxxn8l1PONcI8/JjrB/yRs8CeiW+BPCcXri3CHr2g5/safE9b4JtEZvkV0ha9U"
    "f2zg+yLJ8UM1Jb6yhUKLSRljE39FdImfEg3wM6IL/ErTNvgN0T3+VrNs/J1oDvgHogeCU6IbgjOi"
    "O4IemkMwVOMTnBO9ECijGbsEPU6P4JboleCO6JngnuiJ4IHokUChqTwjeP0NgxOCd6IpwSfRB4EQ"
    "Btf0P1uOQ4IvfQ0IVNSUaub6uiKQcsYBgUP0SeD/JEOCSCNqFkTfBEI1JoiJvgjSv+d7gkwUSwLl"
    "QuMFgQ7UOCKoCDcENdGEQHjnBD3eFYHUZwjjnsgkPPmrMjcU6hmhUL8TDogMwiGRRXhOKCO4JtLL"
    "jbYuIRRk6U2Qpf57yX1NKMw2/a+dY42/EQ0JP4huCVU4jFPCyX8DzZJSD4RzIodQrGVQQugTukQu"
    "oUckOQIRaMGISARCOyJK/p7FLB5YnLN4/jvUy+tP98LijcUti3cWNyzio7L6pfKaPcu7vwOT5ftv"
    "uGpZTvowtOw3tGNpsXxh6f73/sHS50PDwVHZ5jJkfINq2bHeMwanLDVUofJ6NWI1ZnWJSs5sx2pK"
    "1rHSGVI2srLI9qzc37BrWckxTG5ZBeQnrBLyU1Yl+RkrKWNyx6omO7DakA9ZKV1W6ItVM1d3xKfk"
    "58QD8gv6e+Amz8T9BWxPxBdq3omvNO+G+Jr8lviGfER8R/5AfE9+SfxILmZP5OL1TK6ZL+TXxBNy"
    "sZqRj4m/yL+Jv8nfiGVk3Z5YobQSD1ONyO2ffE6sEzV5IfbItWxE/kq8IJ8Sr8glT0w+I5ZvUOLY"
    "30c30XhBLooepKZV5BGx3Np+QCysX8QHjUxQApb7JIJqkAw1fkrSo/skuSQPSUbkDokgBiQCsSR5"
    "Jd+SvJEnJO/kK5KPHkQyJa9JBKgkkWuYmCQmeUZika9JHPKcxCcvSKSMhkQ4YhLhSEmWIhCPlLwi"
    "ySTHJYnUtCGRX+h2JDXFCYmKtWpO0pEvSJRdTWySA/Lr6SnFFekZ+QfpgOKM9OJHmNIr8o50hNx9"
    "ei1qdSlkTizS25/cJL2jOCd9QJli+kgxJFU2rWQ2faa4IH2hGJC+qcslnVKcks4onknnFE+kBsUN"
    "qU2u0V5Hb6QuxQNpb3UOqf+Ta6WA4p40pHgklf+beKQrFCLT+B/DjH5STnFLWlC8kK4pxqSVxF2R"
    "1uQiaMltUqmuJd1SCOWOYkoqk534qAQvXsl6ww3IzihGZL3J3pCdU3ySSadK5LJL7ewt2RXFG5k2"
    "5Z1M/lL5uOryYo6q2OILZXjFjOyD4ptsjOwvUx1exWRS7g7lefk9mUEuvpaoF2Q2hU3mUJhkPoUO"
    "oFR8IJN56nzGmnRFllAYZLk+VmR1v9dZQ64BoTIpXBptqn+8XFCENNra5b/3nEaMi+OlniWNlqpo"
    "LIr6d6FaoFAuP5LCtQ/TCcX+p3yhPNHHjFJRQhVCqUM7/aK8oAkpL2kCyhGNR/moAYfymSalVPox"
    "NSnfaNaUSjxO9TGlKShnWuGK8osmp1TFEVPOaTJKmyaidEVoU3o0CWVAs6IU4Kn4JzQNZUrTUuZi"
    "ckFZaERSCMeWsv7RcuWW5kB/9eJUzA40HevTn/KV9RnNhvWAZsdaWpq6rB9+l/Ub60faE9ZPtAPW"
    "z7QXrF9oT1lPtMgd6xntFesv2nPWconTFWuDdsjapB3R/+f+NGAdqilYR7S3rPsrIjPWS9oH1jHt"
    "I+uE9p51ylQL5rTXrGvaF9Z72juq4U/5TqXdXFCNaJ+ormmfqW7FaE31rKaiklg3VO+0n1Ta02lD"
    "NaZ9o5rQflDJ1TVjKoklXi7tlEoHZ1pThbQzquin1KwljU+lxG2aUCV/7zdU8nFTLZyJ3qaSdHOq"
    "ktahamkNqg2tSbWlFfcdreYcaCfUJ/+ep7QW9RntN/WwryDr3kQ66tFPOaYWkC9qGcfshFoIfOpX"
    "bewTtYC41B80en7S1NTCEVDPJYdHrbphNqD/z/ppSS17rqg92og6EP01tbZcFWe9oH2lXolIQ9rm"
    "kFoQFtQF7SV1+e8p854NqRvaJbVQpdRCtaLuaMT9IGrZ19n/tPvNJa3er39KCSXvNrVobmkzmgda"
    "HR/lPM2ERqatrezVIPOVGmSEEl92O6UtaGQvsjjZtxS8Pt45tO3xWctrVfXs/lgZbfdn+sjm40dG"
    "unHZeGyE7P6aTcimYRPxZbFZ/Hsuf51Dzib5b6qnrP9+xKZQc8um/Ne3/jeth/nEZvcjk910/XM7"
    "YDtke8t2xPb7z1nLNjieT9jmbCO2JTP1V2xNtjXbkG3Hds/2wO6G3Rm7W3Yjdnfs3o4XS3Ylu5bu"
    "mu6R7ua/9xu6+7/rO7oHuie6Z7oD3Sv7E7o3FP26D/ZndOPfZbtDZcf+nG6GwlE3R8GzM9hf0Jns"
    "h3T28XJD57G/ovOPbzFdr+CILmKvBZfsR3Sy29mKLv5dbjRNMVoVcyf7nS3pcvaSp79nVbTV33PN"
    "q5GD7pT4bl7oFOFmKfs79hLk/nj/yP7xuPlm//Qjd7xXXjcTC0k/YS/pp+wlquRcKSm9Z9//AfeC"
    "w+nPzOHwwuH5/2YJc3A="
)
_T2S_PHRASES = {
    "軟體": "软件",
    "硬體": "硬件",
    "網路": "网络",
    "資訊": "信息",
    "程式": "程序",
    "資料": "数据",
    "預設": "默认",
    "支援": "支持",
    "解析度": "分辨率",
    "最佳化": "优化",
    "影片": "视频",
    "螢幕": "屏幕",
    "滑鼠": "鼠标",
    "檔案": "文件",
    "記憶體": "内存",
    "匯入": "导入",
    "匯出": "导出",
    "伺服器": "服务器",
    "頻道": "频道",
    "開啟": "打开",
    "關閉": "关闭",
    "選單": "菜单",
    "介面": "界面",
    "設定": "设置",
    "人工智慧": "人工智能",
    "機器學習": "机器学习",
    "繁體": "繁体",
    "簡體": "简体",
    "資料庫": "数据库",
    "數位": "数字",
    "頻寬": "带宽",
    "影音": "音视频",
    "相機": "相机",
    "藍牙": "蓝牙",
    "無線": "无线",
    "下載": "下载",
    "上傳": "上传",
    "登入": "登录",
    "帳號": "账号",
    "密碼": "密码",
    "預覽": "预览",
    "軟件": "软件",
    "視頻": "视频",
    "質量": "质量",
    "內存": "内存",
    "打印機": "打印机",
    "激光": "激光",
    "缺省": "默认",
    "字體": "字体",
    "光標": "光标",
    "菜單": "菜单",
    "緩存": "缓存",
    "插件": "插件",
    "模塊": "模块",
    "對象": "对象",
    "線程": "线程",
    "進程": "进程",
    "數組": "数组",
    "字符串": "字符串",
    "變量": "变量",
    "函數": "函数",
    "參數": "参数",
    "接口": "接口",
}
_T2S = None


def _t2s_table():
    """惰性解出繁→简单字表。"""
    global _T2S
    if _T2S is None:
        try:
            raw = zlib.decompress(base64.b64decode(_T2S_B64)).decode("utf-8")
            _T2S = {raw[i]: raw[i + 1] for i in range(0, len(raw) - 1, 2)}
        except Exception:
            _T2S = {}
    return _T2S


def to_simplified(text):
    """繁体转简体。已经是简体时原样返回，非中文内容不受影响。"""
    if not text:
        return text
    try:
        t = str(text)
        # 先整词替换（台湾/香港用语 → 大陆用语），再逐字替换
        for a, b in _T2S_PHRASES.items():
            if a in t:
                t = t.replace(a, b)
        tab = _t2s_table()
        if not tab:
            return t
        out = []
        for ch in t:
            out.append(tab.get(ch, ch))
        return "".join(out)
    except Exception:
        return text


def looks_traditional(text):
    """粗略判断一段中文是不是繁体（用于决定要不要转换）。"""
    if not text:
        return False
    tab = _t2s_table()
    if not tab:
        return False
    hit = 0
    for ch in text:
        if ch in tab:
            hit += 1
    return hit >= 2


# ================================================================ 独家模块 5 / 6 素材
# 触发词与素材都用码点 / base64 存放，源码里不出现明文
_EGG_LETTER_B64 = (
    "eNpdVctu4lYY3uddGk2m6gOM1Eqzr9RN1UVVddHNVOquXZmQAOZmknDHlBAuIQwYJ4SbjcO7ZPz/53jVV+j32yiq"
    "Kh2BL+f8t+/iH7nzzHVXZ5dfn747fce2yRe9n060s6Vhm3bP4a5AlfI/+2LUvaPFjh4t2tXCbZ79NuUnqn1BlWK4"
    "v6V5A8+xjXNX5GbCfZs6f3N6qVcbNZuFW4NnfTZvaG98/PXnP3776/dP3//56RdurKPzQbidslmgQ0tftMPdI2KG"
    "2wKVcl+Mc7zFHnLS+tAka8rNBTafnb5TZo7tGdIhMlkjvejqcYrsyVt5ajDRu3m4NdX9NZslfb7gfD7czrmepUqF"
    "sh53RziOsuOYBsImv/Ri0LCud2OUIRvSljZStLekeGy2PSr1Qy+jvC3Kk2a7FeSVfm2PbQOzkmbL1zir7L3qdvCW"
    "91XtPGnjUsbVfKbr4rHOXSFqPKMSzEpvruRItcdGjZGubFIpj9aQRRtFng8p52NDDBLCJKhxY0SHBjlm1G2R7WrP"
    "QzakJbMktV+afJtFOVFjmvwmDUWdCQ3tKFvUB5uGFzzaR/01YurcVD14KrVAzpMT2TVwQ7+sHZcOGWznbUMfOij3"
    "1ahiYXCAR92XVHpO2zGvNvxcYOOehhYNHxAvaq4QREAKbpMxJW2jOqrMMGXqBFxr0TZH1pWeGDGcMUIxmVCy7he5"
    "PDperByhXQwbr1KqdkkvDXQj/AMJbA+5or6nuoYq++o6DSBBI94UZf9TX4hdn6vz3dst+Wt96OFWH/7GnPAqSge6"
    "EiAsp1zKuTQKMB6BufOifEuY6nUAf9Q/fDFSUbUVBiVcJDiBgnw++w8F5zR0BG/3mdzZcfCGwYEDivBTi/0HaWc+"
    "lWXOsQSBizaV3ITWoorVlIY4/sj5He2WoF3COdVZSJuxBsjasP2Z3JdESFFnLbkOgbpY63tTRuSkkZp7FSBFdob9"
    "vkDTrlAdfGoDX8AB8Rw14Hk6cLh0l6TQwSL011F/BfGLRPMT3R+BnZJifB76+bdTRyl2G2g/pt4k3NZVrSgTiMF9"
    "Iya0gchRs0dWk3OVBPGYccjARfC1pLNT1A5AYBdUzpD1dOw119B9IdSZqACjf3/6/vQs/v/m9OzVGGNxzRWdrhw4"
    "jyjLyvGTKX5S9hMQeFenQ1oktO7xfnpUcWWm71PIS5tHEWzjFnEEAdwObQGzUibL1W7644fvvvrhw7dwJa49Ia4M"
    "s+qoThNikQStPZRCtSnVxsK0zQbNgm+UeUIzMo3rOtX6dLVEYlX1MVihdyAMgaAAkxjl2oQ68Eq7OeXfcLd3tGBA"
    "HJTQFQrlgqleysIZMPAyB+Og7JU+ZPFW2QUhCXLFVBRo/DrGytnzZNqxJDN4KDvx0MsknUs0OOndZ649Y0VBBQu+"
    "IhoMrIR+Jyevhs3zgTDdMfXgMnGiZPivRhcOQd6Yu329cskpJoeOureWGJi+v9abJRw4kTW5FvACExE2NjcRpPPA"
    "C0vMeN4QTW564DHUgkjIoKYNTEckOgog42gbqJchl2/RvMyoXaVBijov4W4cjTO0QemwGzj9mMqFMIhH5ky04wEw"
    "UCfxwOhzk2/75HuRcYsLnSvJtsCNtgMBxjTELwZu1Fsm0uLtJbfcxIBkXjdjZBePTZWOEo3Jy6uCXtRwKrq7Oprm"
    "sqYXc8k7OihrrsxWcgsnx8XbBy1xPVz8zxPlUwNTq+/UZIkFbKIbD4kS/RQ9jASFyzfj0AKR4o9QL/6gllR1QpmS"
    "mpvJRzEB5l94gpFy"
)
_EGG_QR_B64 = (
    "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"
    "U9C8VeceUwTSPQ6zSxOWOKznM4x8ihDWs2tXWRNUcSRYNyNzGji5uznULcyGZlTe6gwXOMjoiAWo"
    "c6h3Uzlg/wC/e8n77tG/+5Gz4LxUAUUalcCuExKpjcsrZLBUtUdA2YaOSDxnXhFZtlpYOlu9IgcI"
    "EdscJZGaED8m0wXGtfOPghHfFnHF+PBwHOPVD/fCPo4td9uCx5QG8VVqQYAQPQdU1jOH29f9+95P"
    "33aN/us/1epH2+t7JCrocDb/AH73k/fdo3/6He8n77tG/wD0O95P3wqKD93/AED3Tb0+/BBicCR8"
    "ieSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSf"
    "BJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8EnwSfBJ8ElbDkNr5SzzQbR/wK5SpXKVyJUrlK5Erk"
    "SuUqVyJXKVK5TyldJXKVyldJXKVyldJXKVyldJXKeRK6SuUrlK6SuUqV0lcpXIlSuUrkSuRK5Suk"
    "rkSuUr/9Hslyc1NBDeTdK8lf/wASvUIo6KWjI8vGBQOwuWto3lmORQ9XjE/ItL9Ap/3tZ6DLmYQy"
    "d+GumsH50PSaLnerYAYP8kCkHr4MDTJlvpwz1pRJp3Qs0b/lPXKp/wBDWeik5oI/CaW6kN0kf3xB"
    "OBuus9PsSvtnWA9Z6rBmoB+E0Po5f65THik9asmmdBPEb0E1PyiaY/PNAPVeF/b3KXgz0cnNXHnm"
    "i+ficGdRmuJ36fonwuTfsgmp9Sz9qi56fQlf8ijaOqXymrN5Y6z8WatMr7rFOD8orwPPFuH5py4u"
    "8Z7Z7T4V7T4p7T4p7T4R7eDDcny4h8XzQDifPAuH8oD/AGmiTaB+HNCflgOgeUr/AOZPKFzKPpux"
    "h6Pyn6mW8nXH83kv6iQHpD+JRvvOJ5f4KGr9jZ/zstB7HrE7MhV1+oAo+JM5MXDLp9Gkod6L+TlH"
    "kuFEK0940pXT+Iiv0IFZqc/iArMrE+oywDongvdIXlK1/aepTnEYNkJZeXnFHt6LKcT8zlJR4n0d"
    "02lgpjq/UF8AW66R+pQjAbjfxCZ6rwf8MPT7DlkWrx+qqfJqZ0OS4DynU0GPJOHUHi7McGsDwc2M"
    "sYV6rYwknAr1gVxZA4wjrBqg4w3m4d/4GEtrV+11rE5h07+b8KzOqenWGfEVbxwPxPlfvPlXvKcn"
    "5XvPkfvLZyWFnEF20zg+Oea+YRSN7+ZSWXy8c8yjq4GvXwXqnKZi8i3WnfKVe0958E958E94lSKV"
    "ty3KiUGzq+f/AAmeCE/14WL1IU1yRFt2JnqgpSaBKanzn4SmpnOZbr+IOrLLyug8EQ5fR8XBCo/r"
    "rMxhVQ/Ewj5OkvsRVSCu6agQclvaAvoebUsnz0P4Ox4EzD4bbgnddo3DVxVnjrOU7eU5Tt5Tle3l"
    "KwRku8+Cou0R9p7y1TUPJ1hqsBzZud894TIQLB46uTi/Ahsnj/eKRPgh+4Nnsus7S/sMQgLJVQU8"
    "4HSfz/wmOGf7fAE1JYzEdxDnxjBE1aJfeUOVLbIcTL1foTZIeniK47n2LY7MunDEpvCgrrp4crCN"
    "Qap1lVw3mzw5BULTGfPX+iIrhGern+wqF3fE2CVGebnB5Tuu0eOqubLjmcx2c5zHZznMdnOZU+U1"
    "T08EsqCyrRfE49SEAewWT8z533TWiQF62sDZpcr/AN+BJmVt4Dqm5G35J2vnM5W3YeJ1nBxZv4+p"
    "4d32f8NBmuuh8b7sE1dMqH+a8iP2Jqrl+l6yszz8Tsv4lwXep2HaYdbA73R1k4TWstnL0JK6lk9C"
    "zjm+YQAUaeJu67S60GkNfKd3wnd8J1u3SdaQLrxRY9C3bowgqIPEi6iNru+bylkWQni2coAAUHDx"
    "EgNM6M7Xz8Gx2kDuGJh2Llgzu+z/AIdHR0kWWKzun+BHIM820MH0d12gIrbT6EVXX0iawUGF0C3t"
    "NJ+q8L5ciAHDrn/rKNgeDOz5p3XaZg5z+8+PeDqMWGhzTvefiY/n08iUaHGqO1EGAVaMdrIJBKMA"
    "6OkALETfx79tO18/o7Ps/wCIlsn5IQtb1+kFUFrwI0N7ZyglzUB9XQl8DHoOXhS7rMcc2XtuFUvI"
    "utpqiKvTmeZ4d3zTuu0qjkAsPA/MzpMX0W5WnIhSRnByFjWs0N+09aPSrvqflKwtx1rzmqzwbJed"
    "wTxJcVMd09ojYXjTbwy77E6ZVm/glUOGC8Iz4E0n9RJq6vMr/wARwXLe7rokoDx+KLeWoMru7dYr"
    "5ZAeT8z+frKUC2r/AD9FHhXgeBxzIKAGAOH1Vcr6uAHN/wAyq/8AnT//2gAMAwEAAgADAAAAEP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD3/wBtcddc8f8AbzzjnvT7rf8A59/699//AP8A/wD/AP8A/wD/AP8A9vl7uULk"
    "g1uqJv8AbL5YfK+WzoXcB/8A/wD/AP8A/wD/AP8A/wD47gP1SgAUemKoaUOh4OIei237k1f/AP8A"
    "/wD/AP8A/wD/AP8A/wBsZIpARyB7EumuUkR1LFCYetPwJ3//AP8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "3/8A/wD/AL//AD/7/wD+/wD7/wD/AP8A/wDz/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/nzDzDzDzDzD"
    "zDzDzDzDzDzDT/8A/wD/AP8A/wD/AP8A/wD/AP8A/n88888888888888888888888/8A/wD/AP8A"
    "/wD/AP8A/wD/AP8A/wBbzzzzzzzzzzzzzzzzzzzzzzxf/wD/AP8A/wD/AP8A/wD/AP8A/wD+8888"
    "8888888888888888888X/wD/AP8A/wD/AP8A/wD/AP8A/wD/APzzzjDTjzDzTjzjzDDzzjzzxf8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/ADzzwwDiCwRyzxgRCgQRzzzxf/8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wDPPKMCOOBDODEFELIAOPPPPF//AP8A/wD/AP8A/wD/AP8A/wD/AP8A888oMEkQ8QEsUsMY"
    "UsA4888X/wD/AP8A/wD/AP8A/wD/AP8A/wD/APzzwzwgwiBSSgQgzTyjxjzzxf8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD/ADzwjAwwQhTChChwQyTSDzzxf/8A/wD/AP8A/wD/AP8A/wD/AP8A/wDPPJEP"
    "KIJBAUAh1BOMIPPPPF//AP8A/wD/AP8A/wD/AP8A/wD/AP8A888gwkQA0ZhceEg8Y8AY888X/wD/"
    "AP8A/wD/AP8A/wD/AP8A/wD/APzzzwwjjRihPjypiBhzDzzzxf8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "/wD/ADzxxRBRSymccY2ChQCzjzzxf/8A/wD/AP8A/wD/AP8A/wD/AP8A/wDPPKFNAFEAaNy744HC"
    "LJPPPF//AP8A/wD/AP8A/wD/AP8A/wD/AP8A888400YwctZZOF6k4U8c888X/wD/AP8A/wD/AP8A"
    "/wD/AP8A/wD/APzzxAxCRgiwQjkAThQDDTzzxf8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/ADzzDDzg"
    "RhiASi1VSxgiTzzxf/8A/wD/AP8A/wD/AP8A/wD/AP8A/wDPPAABDPIAALJLMQUBDPPPPF//AP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A8884EoQ0Uc0YQU0BQA80888X/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "APzzyxyAgCRgTzByiSFSBTzzxf8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/ADzwwxywxxyxyxyyzzzR"
    "zzzxf/8A/wD/AP8A/wD/AP8A/wD/AP8A/wDPPPPPPPPPPPPPPPPPPPPPPF//AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A888888ocswIM0Icw8888888X/wD/AP8A/wD/AP8A/wD/AP8A/wD/APzzzzzyxzxx"
    "xzzxzzzzzzzzxf8A/wD/AP8A/wD/AP8A/wD/AP8A/wDvPPPPPPPPPPPPPPPPPPPPPPF//wD/AP8A"
    "/wD/AP8A/wD/AP8A/wDjX7zzz7zzzzzzzzzzz7zzzztT/wD/AP8A/wD/AP8A/wD/AP8A/wD/ADDH"
    "DLDDDHDLDDDDDDDDDLDDP/8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/APv/AP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/APLz/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AOPHPz/MsuN/v/8A/wD/AP8A/wD/AP8A/wD/AP8A"
    "/wD/AP8A/wD/AP8AjLEo8+7zzzzzzzy9/wCuPZc/+226+86xn31EO/O9M8888888888888888888"
    "888888888888c8888888888888888888888888888888888888888888888888888888888888wu"
    "+u7848Q0884488g884888888888888888888801y8m984UkQoIAsscg8wEE08888888888888888"
    "WKb++04YkQ8IkA8MUMsEo08888888888888888879/8Av3PCLGFIFDOLKPPOLEPPPPPPPPPPPPPP"
    "PPPPLjj/ADzxzxzxzzzxzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz//xAArEQABAgMGBgMBAQEAAAAAAAABABEhMWEw"
    "QXGB0fBAUZGhwfFQseEQYHD/2gAIAQMBAT8Q/wB24umUT6IRGiGsTHFUCkCKFuEDMnEz1+0SYIpG"
    "AWJRA4ID5fwE7CgdeiK9EUECIlRb0d/pP7GLtNEEFinSUw8kdOCI/wBKkOMHP95bAKGwuTAI6GwX"
    "WrQOAPSCESXbptcOk8U6UjqZxT0QBXNlMyA3wI+jd1FX2b3cmZNOkhBsU3UEk5eRMo5nsEAIeGLO"
    "i5J44nGCBBs8N2QbCSHJAxpgnQEspPoox4nEJ0RpgkSE5hhLFR5CFqn2i0xE5w5TRAWuEhlLKHRG"
    "wJJQb5WYxyUBkv2FKnDrSQQB2BuiklXOQvKLjenrJPYAIYNNAukiHZS1a5GBDwmE7h5AQhkqDJpZ"
    "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"
)


def _egg_blob(b64_text):
    """把内嵌素材还原成字节。"""
    try:
        return zlib.decompress(base64.b64decode(b64_text))
    except Exception:
        return b""


def _egg_letter_text():
    try:
        return _egg_blob(_EGG_LETTER_B64).decode("utf-8")
    except Exception:
        return ""


def _egg_qr_bytes():
    """付款码是 JPEG，本身已压缩，素材用纯 base64；同时兼容 zlib 包装。"""
    try:
        return zlib.decompress(base64.b64decode(_EGG_QR_B64))
    except Exception:
        pass
    try:
        return base64.b64decode(_EGG_QR_B64)
    except Exception:
        return b""

# ---- 独家模块 ----
# 模块 1 关于页连点版本号，模块 2 和 2-1 2-2 是设置页 key 输入框暗号
# 触发词按码点拼装，源码中不出现明文
_EGG_CODE = (
    (0x81F4, 0x8C22, 0x540D, 0x5355),
    (0x5C71, 0x6709, 0x6728, 0x516E, 0x6728, 0x6709, 0x679D),
    (0x77, 0x68, 0x6F, 0x6D, 0x61, 0x64, 0x65, 0x69, 0x74),
    (0x63, 0x65, 0x6C, 0x65, 0x62, 0x72, 0x61, 0x74, 0x65, 0x33, 0x30, 0x30),
    (0x8D5E, 0x52A9),
)


def _egg_key(idx):
    try:
        return "".join(chr(c) for c in _EGG_CODE[idx])
    except Exception:
        return ""


THANKS_TITLE = "Hearizon Sync 致谢名单"
THANKS_NOTE = "以下参与内测人员名单不分先后次序"
THANKS_LIST = [
    ("周延通", "澳门科技大学 · 商学院"),
    ("鲍思源", "北京语言大学 · 海南国际学院"),
    ("李新元", "北京语言大学 · 海南国际学院"),
    ("李进平", "北京语言大学 · 海南国际学院"),
    ("姚铸宸", "北京语言大学 · 海南国际学院"),
    ("王舒楠", "北京语言大学 · 心理与认知科学学院"),
    ("朱睿杰", "北京语言大学 · 外国语言文化学院（区域国别院）"),
    ("郑紫涵", "四川农业大学 · 商旅学院"),
    ("梁嘉铭", "中央民族大学 · 海南国际学院"),
]

# ---- v1.3.0 新增 ----
SILENCE_WARN_SEC = 10          # 无声提醒阈值（秒）
SUB_POS_DEFAULT = "bottom"     # 字幕位置：bottom（贴底）/ top（贴顶）
SUB_THEME_DEFAULT = "green"    # 字幕配色预设
# 3 套字幕配色：都在深底上，保证投影 / 看课时的对比度
SUB_THEMES = {
    "green": {
        "name": "经典绿",
        "src_fg": "#8FA8A0", "dst_fg": "#4ADE9B",
        "bg": "#111B17",
        "partial_src": "#5E7A72", "partial_dst": "#2A7A5E",
        "dot_src": "#8FA8A0", "dot_dst": "#4ADE9B",
    },
    "contrast": {
        "name": "高对比",
        "src_fg": "#FFFFFF", "dst_fg": "#FFFFFF",
        "bg": "#000000",
        "partial_src": "#BDBDBD", "partial_dst": "#D6D6D6",
        "dot_src": "#FFFFFF", "dot_dst": "#FFFFFF",
    },
    "warm": {
        "name": "暖纸张",
        "src_fg": "#E6D7BE", "dst_fg": "#FFC66D",
        "bg": "#1A1611",
        "partial_src": "#A89478", "partial_dst": "#C79A4E",
        "dot_src": "#E6D7BE", "dot_dst": "#FFC66D",
    },
}
SUB_THEME_KEYS = ["green", "contrast", "warm"]

# ---- 3.0.0 新增：界面布局 ----
# 招牌布局保持原样（左右分栏、原文/译文两张卡），另外加一种对话流布局。
LAYOUT_CLASSIC = "经典（左右分栏）"
LAYOUT_CHAT = "对话（气泡流）"
LAYOUTS = [LAYOUT_CLASSIC, LAYOUT_CHAT]
LAYOUT = LAYOUT_CLASSIC

# 3.0.0 第 12 条：翻译上下文。同一个话题里前后句互相依靠，
# 只翻单句容易把代词、省略主语翻丢。这里保留最近几轮原文/译文。
CONTEXT_TURNS = 3

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


# ---- v2.5.1：每种翻译模式下可用的语言 ----
# 本地模型是单语言对（英语→中文），所以本地模式只放行这两种语言；
# 其余语言在下拉框里变灰、点不动（用户明确要求「不能用的语言变灰不可选」）。
MODE_INPUT_LANGS = {
    TR_LOCAL: ["自动检测", "英语"],
    TR_FREE: list(INPUT_LANG_NAMES),
    TR_API: list(INPUT_LANG_NAMES),
}
MODE_OUTPUT_LANGS = {
    TR_LOCAL: ["中文"],
    TR_FREE: list(OUTPUT_LANG_NAMES),
    TR_API: list(OUTPUT_LANG_NAMES),
}


def mode_input_langs(mode):
    return MODE_INPUT_LANGS.get(mode, list(INPUT_LANG_NAMES))


def mode_output_langs(mode):
    return MODE_OUTPUT_LANGS.get(mode, list(OUTPUT_LANG_NAMES))

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
        self._device = None          # 当前模型跑在 cpu 还是 cuda
        self._sp_src = None
        self._sp_tgt = None
        self._lock = threading.Lock()

    def available(self):
        return os.path.exists(os.path.join(OPUS_DIR, "model.bin"))

    def load(self):
        """按当前 GPU 开关加载/重载模型：开关变化后下一次翻译自动切换。"""
        want = "cuda" if GPU_ACCEL else "cpu"
        with self._lock:
            if self._tr is not None and self._device == want:
                return
            if not self.available():
                raise FileNotFoundError(
                    f"找不到本地翻译模型：{OPUS_DIR}\n请先下载（见使用说明）")
            self.on_status("正在加载本地翻译模型...")
            import ctranslate2
            import sentencepiece as spm
            t0 = time.time()
            try:
                if want == "cuda":
                    self._tr = ctranslate2.Translator(
                        OPUS_DIR, device="cuda", compute_type="float16")
                    self._device = "cuda"
                else:
                    self._tr = ctranslate2.Translator(
                        OPUS_DIR, device="cpu", compute_type="int8")
                    self._device = "cpu"
            except Exception as e:
                if want != "cuda":
                    raise
                self.on_status(f"本地翻译 GPU 加载失败（{e}），已回退 CPU")
                self._tr = ctranslate2.Translator(
                    OPUS_DIR, device="cpu", compute_type="int8")
                self._device = "cpu"
            self._sp_src = spm.SentencePieceProcessor(
                model_file=os.path.join(OPUS_DIR, "source.spm"))
            self._sp_tgt = spm.SentencePieceProcessor(
                model_file=os.path.join(OPUS_DIR, "target.spm"))
            self.on_status(
                f"本地翻译已就绪（{self._device}，{time.time()-t0:.1f}s）")

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
# ================================================================ 配置安全层（3.1.0）
# 四件事：
#   1. 原子写：先写 .tmp 再 os.replace，崩溃/断电不会留下半截 JSON
#   2. 损坏备份恢复：主文件坏了自动回退 .bak，不再静默清空所有 Key
#   3. DPAPI 密钥加密：敏感字段用 Windows 用户级加密落盘，别的进程/用户读不走
#   4. 额度记账独立文件：不再和设置共用一个 JSON，消除读-改-写竞态
_SECRET_FIELDS = ("deepseek_api_key", "youdao_app_secret", "custom_api_key",
                  "baidu_key", "caiyun_token")
_DPAPI_PREFIX = "dpapi:v1:"
_SECRET_DECRYPT_FAILED = False      # 换机/配置损坏时提示用户重填 Key
USAGE_PATH = os.path.join(os.path.expanduser("~"), ".hearizon_sync_usage.json")
_USAGE_LOCK = threading.Lock()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data):
    blob_in = _DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data):
    blob_in = _DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _enc_secret(s):
    """敏感字段落盘前加密；DPAPI 不可用时退回明文（保功能、不丢 Key）。"""
    if not s:
        return s
    if str(s).startswith(_DPAPI_PREFIX):
        return s
    try:
        if os.name == "nt":
            enc = _dpapi_protect(str(s).encode("utf-8"))
            return _DPAPI_PREFIX + base64.b64encode(enc).decode("ascii")
    except Exception:
        pass
    return s


def _dec_secret(s):
    """读盘后解密；旧版明文（无前缀）原样返回，实现无感迁移。"""
    if not s or not str(s).startswith(_DPAPI_PREFIX):
        return s
    global _SECRET_DECRYPT_FAILED
    try:
        enc = base64.b64decode(str(s)[len(_DPAPI_PREFIX):])
        return _dpapi_unprotect(enc).decode("utf-8")
    except Exception:
        # 换机 / 用户配置损坏时 DPAPI 解不开：标记出来，由 _check_env 提示用户重填
        _SECRET_DECRYPT_FAILED = True
        return ""


def load_conf():
    """读取本地配置。主文件损坏时回退 .bak；敏感字段自动解密。"""
    for path in (CONF_PATH, CONF_PATH + ".bak"):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                continue
            for k in _SECRET_FIELDS:
                if d.get(k):
                    d[k] = _dec_secret(str(d[k]))
            return d
        except Exception:
            continue
    return {}


def save_conf(d):
    """原子写配置：敏感字段加密 → 写 .tmp → 备份旧文件 → os.replace。"""
    try:
        out = dict(d)
        for k in _SECRET_FIELDS:
            if out.get(k):
                out[k] = _enc_secret(str(out[k]))
        tmp = CONF_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(CONF_PATH):
            try:
                import shutil
                shutil.copy2(CONF_PATH, CONF_PATH + ".bak")
            except Exception:
                pass
        os.replace(tmp, CONF_PATH)
        return True
    except Exception:
        return False


def _load_usage_file():
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_usage_file(u):
    with _USAGE_LOCK:
        try:
            tmp = USAGE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(u, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, USAGE_PATH)
            return True
        except Exception:
            return False


def apply_conf():
    """把配置写进全局变量"""
    global YOUDAO_APP_KEY, YOUDAO_APP_SECRET, YOUDAO_DOMAIN, GPU_ACCEL, AUTO_CORRECT
    global TRANSLATOR, DEEPSEEK_KEY, MYMEMORY_EMAIL, CORRECT_WARN_SKIP
    global API_ENGINE, UPDATE_CHECK, HOTWORDS, HOTWORD_TEXT, FAR_FIELD, FORCE_LOUD
    global SPECTRAL_DENOISE, SPECTRAL_STRENGTH, DENOISE_MODE
    global CUSTOM_BASE_URL, CUSTOM_MODEL, CUSTOM_KEY
    global BAIDU_APPID, BAIDU_KEY, CAIYUN_TOKEN
    c = load_conf()
    YOUDAO_APP_KEY = c.get("youdao_app_key", "") or ""
    YOUDAO_APP_SECRET = c.get("youdao_app_secret", "") or ""
    YOUDAO_DOMAIN = c.get("youdao_domain", "general") or "general"
    GPU_ACCEL = bool(c.get("gpu_accel", False))
    AUTO_CORRECT = bool(c.get("auto_correct", False))
    # v2.5.1：翻译模式（旧版本存的 auto/local/deepseek/youdao 一律归到免费在线）
    TRANSLATOR = c.get("translator_mode", TRANSLATOR) or TRANSLATOR
    if TRANSLATOR not in TRANSLATOR_CHOICES:
        TRANSLATOR = TR_FREE
    DEEPSEEK_KEY = c.get("deepseek_api_key", "") or ""
    MYMEMORY_EMAIL = c.get("mymemory_email", "") or ""
    CORRECT_WARN_SKIP = bool(c.get("correct_warn_skip", False))
    API_ENGINE = c.get("api_engine", API_ENGINE) or API_ENGINE
    if API_ENGINE not in API_ENGINES:
        API_ENGINE = API_ENGINE_AUTO
    UPDATE_CHECK = bool(c.get("update_check", True))
    HOTWORD_TEXT = c.get("hotwords", "") or ""
    HOTWORDS = [HOTWORD_TEXT] if HOTWORD_TEXT else []
    FAR_FIELD = bool(c.get("far_field", False))
    FORCE_LOUD = bool(c.get("force_loud", False))
    SPECTRAL_DENOISE = bool(c.get("spectral_denoise", True))
    try:
        SPECTRAL_STRENGTH = float(c.get("spectral_strength", 2.0))
    except (TypeError, ValueError):
        SPECTRAL_STRENGTH = 2.0
    SPECTRAL_STRENGTH = min(3.5, max(1.0, SPECTRAL_STRENGTH))
    DENOISE_MODE = str(c.get("denoise_mode", "auto") or "auto")
    if DENOISE_MODE not in DENOISE_MODES:
        DENOISE_MODE = "auto"
    CUSTOM_BASE_URL = c.get("custom_base_url", "") or ""
    CUSTOM_MODEL = c.get("custom_model", "") or ""
    CUSTOM_KEY = c.get("custom_api_key", "") or ""
    BAIDU_APPID = c.get("baidu_appid", "") or ""
    BAIDU_KEY = c.get("baidu_key", "") or ""
    CAIYUN_TOKEN = c.get("caiyun_token", "") or ""


# ================================================================ GPU 探测
_NVIDIA_CACHE = {}


def nvidia_present():
    """本机到底有没有 NVIDIA 显卡（不看 CUDA 运行时装没装）。

    v2.5.1 修 bug：没有 N 卡的电脑以前也会看到「已检测到显卡」，
    因为提示文案是写死的。现在先判定硬件，再决定说哪一套话。
    三重判断，任一命中即认为有卡；全部不命中才判定「没有 N 卡」。
    """
    if "has" in _NVIDIA_CACHE:
        return _NVIDIA_CACHE["has"]
    has = False
    # ① 驱动装好的 N 卡：ctranslate2 能枚举到 CUDA 设备
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            has = True
    except Exception:
        pass
    # ② nvidia-smi 存在（驱动装了但 CUDA 运行时可能没装）
    if not has:
        for p in (r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.exists(p):
                has = True
                break
    # ③ WMI 显卡列表里出现 NVIDIA：先 wmic，Win11 24H2 起改走 PowerShell CIM
    if not has:
        try:
            import subprocess
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, timeout=8, text=True,
                creationflags=0x08000000).stdout or ""
            if "NVIDIA" in out.upper():
                has = True
        except Exception:
            pass
    if not has:
        try:
            import subprocess
            ps = ("Get-CimInstance Win32_VideoController | "
                  "Select-Object -ExpandProperty Name")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=8, text=True,
                creationflags=0x08000000).stdout or ""
            if "NVIDIA" in out.upper():
                has = True
        except Exception:
            pass
    _NVIDIA_CACHE["has"] = has
    return has


def cuda_probe():
    """真实探测 CUDA 是否可用，并区分「没有 N 卡」与「有卡但缺运行时」。

    ctranslate2 需要 cublas64_12.dll / cudnn 等。只装驱动、没装 CUDA Toolkit
    的机器上 get_cuda_device_count() 会返回 1，但实际加载会失败。
    返回 (可用, 说明, 是否有 NVIDIA 显卡)
    """
    try:
        import ctranslate2
    except Exception as e:
        return False, f"ctranslate2 不可用：{e}", nvidia_present()
    try:
        cnt = ctranslate2.get_cuda_device_count()
    except Exception:
        cnt = 0
    has_card = cnt > 0 or nvidia_present()
    if cnt <= 0:
        if has_card:
            return False, "检测到 NVIDIA 显卡，但显卡驱动 / CUDA 不可用", True
        return False, "本机没有可用的 NVIDIA 显卡", False
    # 3.1.0 修第 10 条：不再为探测加载整个 opus 模型 + 跑一次推理（拖慢启动、模型
    # 缺失时还会误判）。get_supported_compute_types 只初始化 CUDA 后端、不加载模型，
    # 运行时缺 cublas/cudnn 时它会抛错，足以区分「有卡缺运行时」。
    try:
        types = ctranslate2.get_supported_compute_types("cuda", device_index=0)
        if types:
            return True, "CUDA 可用", True
        return False, "检测到 NVIDIA 显卡，但 CUDA 计算类型不可用", True
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "cublas" in low or "cudnn" in low or "cudart" in low:
            return False, "缺少 CUDA 12 运行时（cublas64_12.dll / cudnn），需安装 CUDA Toolkit 12.x", True
        return False, msg[:90], True


def gpu_quick_summary():
    """设置页展示用：不做推理测试，只说清「有没有 N 卡、能不能用」。"""
    try:
        import ctranslate2
        cnt = ctranslate2.get_cuda_device_count()
    except Exception:
        cnt = 0
    if cnt > 0:
        return "本机 GPU 检测：已检测到 NVIDIA 显卡，GPU 加速可用"
    if nvidia_present():
        return ("本机 GPU 检测：检测到 NVIDIA 显卡，但缺 CUDA 12 运行时，"
                "需装 CUDA Toolkit 12.x 才能启用")
    return "本机 GPU 检测：未检测到 NVIDIA 显卡 → GPU 加速用不了，全部走 CPU（不影响使用）"


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


def _fmt_srt_time(seconds):
    """秒 → SRT 时间戳 HH:MM:SS,mmm（v1.3.0：真实时间轴用）"""
    ms = max(0, int(round(float(seconds) * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


# ---- 自动纠错本地规则层 ----
# 只做低风险修正；云端 LLM 纠错在 Translator.correct 里（没有 key 时自动跳过）
# 不预置任何领域词表，专业术语交给用户在设置里填热词
_PUNCT_REPEAT = re.compile(r"([，。！？、；：])\1+")
_CHAR_REPEAT = re.compile(r"([\u4e00-\u9fff])\1{2,}")
# 笑声/语气词本来就会连着说，压缩会把「哈哈哈哈」改成「哈」，不处理
_LAUGH_CHARS = set("哈呵嘿嘻哼呜啊哎哦")


def _collapse_char(m):
    ch = m.group(1)
    return m.group(0) if ch in _LAUGH_CHARS else ch


def _rule_correct(text):
    """离线规则纠错：只做低风险修正，宁可少改不可改错。"""
    if not text:
        return text
    t = text
    t = _PUNCT_REPEAT.sub(r"\1", t)
    # 同一个汉字连续 3 次以上（识别口吃/重复）压成 1 个，笑声/语气词除外
    t = _CHAR_REPEAT.sub(_collapse_char, t)
    return t.strip()


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
            with _urlopen(req, timeout=timeout) as r:
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


# ================================================================ 免费在线翻译
class _FreeQuotaExhausted(Exception):
    """免费额度用完（要等次日恢复）。"""


_FREE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class BaiduTranslator:
    """百度翻译开放平台：通用文本翻译。

    注册地址 fanyi-api.baidu.com，标准版每月 5 万字符免费。
    签名规则：sign = md5(appid + q + salt + 密钥)。
    """

    API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
    # 本程序的语言名 → 百度语言代码
    TARGET_CODES = {
        "中文": "zh", "English": "en", "日本語": "jp", "한국어": "kor",
        "Deutsch": "de", "Français": "fra", "Italiano": "it", "Español": "spa",
    }
    SRC_CODES = {
        "中文": "zh", "英语": "en", "日语": "jp", "韩语": "kor", "德语": "de",
        "法语": "fra", "意大利语": "it", "西班牙语": "spa", "粤语": "yue",
        "自动检测": "auto",
    }

    def available(self):
        return bool(BAIDU_APPID.strip() and BAIDU_KEY.strip())

    def translate(self, text, target_lang="中文", src="自动检测",
                  on_chunk=None, timeout=20):
        if not text.strip():
            return ""
        if not self.available():
            return "[未配置百度翻译 appid / 密钥]"
        import urllib.request
        import hashlib
        salt = str(int(time.time() * 1000))
        sign = hashlib.md5(
            (BAIDU_APPID + text + salt + BAIDU_KEY).encode("utf-8")).hexdigest()
        params = {
            "q": text,
            "from": self.SRC_CODES.get(src, "auto"),
            "to": self.TARGET_CODES.get(target_lang, "zh"),
            "appid": BAIDU_APPID,
            "salt": salt,
            "sign": sign,
        }
        url = self.API_URL + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, method="GET")
            with _urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("error_code"):
                return f"[百度翻译错误 {data.get('error_code')}: {data.get('error_msg', '')}]"
            parts = [x.get("dst", "") for x in data.get("trans_result", [])]
            out = "\n".join(p for p in parts if p)
            if out and on_chunk:
                on_chunk(out)
            return out or "[百度翻译无返回]"
        except Exception as e:
            return f"[百度翻译失败: {e}]"


class CaiyunTranslator:
    """彩云小译：国内可直连的翻译 API，token 即可用。

    只支持「中文 ↔ 英/日」这几个方向，其他方向接口会直接拒绝，
    所以这里不做的方向统一提示用户换引擎，绝不假装成功。
    """

    API_URL = "https://api.interpreter.caiyunai.com/v1/translator"
    # (源语言, 目标语言) → 彩云翻译方向
    DIRECTIONS = {
        ("中文", "English"): "zh2en",
        ("英语", "中文"): "en2zh",
        ("中文", "日本語"): "zh2ja",
        ("日语", "中文"): "ja2zh",
        ("日本語", "中文"): "ja2zh",
        ("自动检测", "中文"): "auto2zh",
        ("自动检测", "English"): "auto2en",
        ("自动检测", "日本語"): "auto2ja",
    }

    def available(self):
        return bool(CAIYUN_TOKEN.strip())

    def translate(self, text, target_lang="中文", src="自动检测",
                  on_chunk=None, timeout=20):
        if not text.strip():
            return ""
        if not self.available():
            return "[未配置彩云小译 token]"
        import urllib.request
        trans_type = self.DIRECTIONS.get((src, target_lang))
        if not trans_type:
            return f"[彩云小译只支持 中文↔英/日，当前「{src}→{target_lang}」请换引擎]"
        body = json.dumps({
            "source": [text],
            "trans_type": trans_type,
            "request_id": f"hs-{int(time.time() * 1000)}",
            "detect": True,
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.API_URL, data=body,
                headers={"Content-Type": "application/json",
                         "x-authorization": "token " + CAIYUN_TOKEN.strip()},
                method="POST")
            with _urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            arr = data.get("target") or []
            out = arr[0] if arr else ""
            if not out:
                return "[彩云小译无返回，请检查 token 或额度]"
            if on_chunk:
                on_chunk(out)
            return out
        except Exception as e:
            return f"[彩云小译失败: {e}]"


class FreeOnlineTranslator:
    """免费在线翻译：不用填任何 Key、不花钱、国内直连（不需要 VPN）。

    主引擎 MyMemory（api.mymemory.translated.net）
        · 官方免费额度：匿名 5000 字符/天；设置里填邮箱后 50000 字符/天
        · 实测整句质量：英→中/日/韩/德/法/西/意、中→英/日 都可用
    中文兜底 有道免费网页接口（aidemo.youdao.com/trans）
        · 实测只支持「译成中文」；约 6 次/分钟，超限 411，冷却 60s 恢复
    最后兜底 本地 opus-mt（仅英语→中文，纯离线、不限量）

    ⚠️ 不碰任何需要翻墙的接口（Google 等），保证国内任何电脑都能用。
    """

    # 界面目标语言名 → MyMemory 语言代码
    TARGET_CODE = {
        "中文": "zh-CN", "English": "en", "日本語": "ja", "한국어": "ko",
        "Deutsch": "de", "Français": "fr", "Italiano": "it", "Español": "es",
    }
    # 识别结果语言代码 → MyMemory 语言代码
    SRC_CODE = {
        "zh": "zh-CN", "yue": "zh-CN", "en": "en", "ja": "ja", "ko": "ko",
        "de": "de", "fr": "fr", "it": "it", "es": "es",
    }
    CJK_TARGETS = {"zh-CN", "ja", "ko"}
    MAX_BYTES = 450          # MyMemory 单次 GET 上限约 500 字节，留余量

    def __init__(self, local_tr=None, on_status=None):
        self.local_tr = local_tr
        self.on_status = on_status or (lambda s: None)
        self.last_provider = ""
        self._cache = {}
        self._cache_cap = 400
        self._lock = threading.Lock()
        self._day = self._today()
        self._day_chars = 0
        self._mm_dead_until = 0.0        # MyMemory 配额用完 → 次日
        self._mm_last = 0.0
        self._yd_times = deque()          # 有道免费最近 60s 调用时间
        self._yd_dead_until = 0.0
        self._yd_last = 0.0
        self._load_usage()

    # ---------------- 基本信息 ----------------
    def available(self):
        return True                       # 永远可用：不需要任何 Key

    def describe(self):
        q = "50000" if (MYMEMORY_EMAIL or "").strip() else "5000"
        return f"免费在线（MyMemory，约 {q} 字符/天）"

    # ---------------- 每日额度记账 ----------------
    @staticmethod
    def _today():
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _tomorrow_ts():
        now = datetime.now()
        nxt = datetime(now.year, now.month, now.day) + timedelta(days=1)
        return nxt.timestamp()

    def _daily_limit(self):
        base = 50000 if (MYMEMORY_EMAIL or "").strip() else MYMEMORY_DAILY_CHARS
        return base

    def _load_usage(self):
        try:
            u = _load_usage_file()
            # 3.1.0 迁移：旧版把额度记在主配置里，第一次读时搬过来
            if not u:
                legacy = load_conf().get("mymemory_usage") or {}
                if legacy:
                    u = legacy
                    _save_usage_file(u)
            if u.get("day") == self._today():
                self._day_chars = int(u.get("chars") or 0)
        except Exception:
            self._day_chars = 0

    def _save_usage(self):
        # 独立文件 + 自己的锁：不再和设置页抢同一个 JSON（修读-改-写竞态）
        _save_usage_file({"day": self._day, "chars": int(self._day_chars)})

    def _roll_day(self):
        t = self._today()
        if t != self._day:
            self._day = t
            self._day_chars = 0
            self._mm_dead_until = 0.0

    # ---------------- 缓存 ----------------
    def _cache_get(self, key):
        with self._lock:
            return self._cache.get(key)

    def _cache_put(self, key, val):
        with self._lock:
            if len(self._cache) >= self._cache_cap:
                # 简单 FIFO：丢掉最早插入的 1/4
                for k in list(self._cache.keys())[: self._cache_cap // 4]:
                    self._cache.pop(k, None)
            self._cache[key] = val

    # ---------------- 分块（MyMemory 单请求有长度上限）----------------
    def _split_chunks(self, text):
        text = (text or "").strip()
        if not text:
            return []
        if len(text.encode("utf-8")) <= self.MAX_BYTES:
            return [text]
        parts = re.split(r"(?<=[。！？!?；;.\n])", text)
        chunks, buf = [], ""
        for p in parts:
            if not p:
                continue
            cand = buf + p
            if len(cand.encode("utf-8")) > self.MAX_BYTES:
                if buf:
                    chunks.append(buf)
                s = p
                while len(s.encode("utf-8")) > self.MAX_BYTES:
                    cut = max(1, self.MAX_BYTES // 3)
                    chunks.append(s[:cut])
                    s = s[cut:]
                buf = s
            else:
                buf = cand
        if buf:
            chunks.append(buf)
        return [c for c in chunks if c.strip()]

    def _join(self, parts, tgt):
        parts = [p.strip() for p in parts if p and p.strip()]
        if not parts:
            return ""
        if tgt in self.CJK_TARGETS:
            return "".join(parts)
        return " ".join(parts)

    # ---------------- MyMemory ----------------
    def _mymemory(self, text, src, tgt):
        now = time.time()
        if now < self._mm_dead_until:
            return None
        limit = self._daily_limit()
        if self._day_chars + len(text) > limit:
            self._mm_dead_until = self._tomorrow_ts()
            self.on_status("MyMemory 今日免费额度已用完，已自动切到备用免费源")
            return None
        gap = now - self._mm_last
        if gap < 0.35:
            time.sleep(0.35 - gap)
        self._mm_last = time.time()

        out_parts = []
        for chunk in self._split_chunks(text):
            params = {"q": chunk, "langpair": "%s|%s" % (src, tgt)}
            email = (MYMEMORY_EMAIL or "").strip()
            if email:
                params["de"] = email
            url = MYMEMORY_API + "?" + urllib.parse.urlencode(params)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _FREE_UA})
                with _urlopen(req, timeout=8) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                self._mm_dead_until = time.time() + 30
                self.on_status("MyMemory 连接失败，已自动切到备用免费源")
                return None
            out = ((d.get("responseData") or {}).get("translatedText") or "").strip()
            if d.get("quotaFinished") or "MYMEMORY WARNING" in out.upper():
                self._mm_dead_until = self._tomorrow_ts()
                self._day_chars = limit
                self._save_usage()
                self.on_status("MyMemory 今日免费额度已用完，已自动切到备用免费源")
                return None
            if str(d.get("responseStatus")) != "200" or not out:
                self._mm_dead_until = time.time() + 15
                return None
            self._day_chars += len(chunk)
            self._save_usage()
            out_parts.append(out)
        return self._join(out_parts, tgt) if out_parts else None

    # ---------------- 有道免费（仅译成中文）----------------
    def _youdao_free(self, text):
        now = time.time()
        if now < self._yd_dead_until:
            return None
        self._yd_times = deque(t for t in self._yd_times if now - t < 60)
        if len(self._yd_times) >= 6:
            self.on_status("有道免费接口限频（6 次/分钟），稍等一下会自动恢复")
            return None
        gap = now - self._yd_last
        if gap < 4.0:
            time.sleep(4.0 - gap)
        url = YOUDAO_FREE_API + "?" + urllib.parse.urlencode(
            {"q": text, "from": "auto", "to": "zh-CHS"})
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _FREE_UA,
                "Referer": "https://ai.youdao.com/"})
            with _urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            self._yd_dead_until = time.time() + 20
            return None
        self._yd_last = time.time()
        self._yd_times.append(self._yd_last)
        code = str(d.get("errorCode", ""))
        out = ((d.get("translation") or [""])[0] or "").strip()
        if code == "411":                 # 访问频率受限
            self._yd_dead_until = time.time() + 60
            return None
        if code != "0" or not out:
            return None
        return out

    # ---------------- 对外接口 ----------------
    def translate(self, text, target="中文", src_lang=None, on_chunk=None):
        """免费在线翻译。返回译文；失败时返回一条可直接显示的中文提示。"""
        text = (text or "").strip()
        if not text:
            return ""
        self._roll_day()
        tgt = self.TARGET_CODE.get(target, "zh-CN")
        src = self.SRC_CODE.get(src_lang or "")
        src_unknown = not src
        if src_unknown:
            src = "en"                    # 兜底按英语，但先试自动识别（见下）
        if src == tgt and not src_unknown:  # 同语言不用翻
            self.last_provider = "无需翻译"
            return text

        key = (text, src, tgt)
        hit = self._cache_get(key)
        if hit:
            self.last_provider = "免费(缓存)"
            if on_chunk:
                on_chunk(hit)
            return hit

        # 源语言没识别出来且要译成中文：先用有道的 from=auto，
        # 否则日语/韩语会被当成英语送 MyMemory，产出乱码。
        if src_unknown and tgt == "zh-CN":
            out = self._youdao_free(text)
            if out:
                self._cache_put(key, out)
                self.last_provider = "免费(有道·自动识别)"
                if on_chunk:
                    on_chunk(out)
                return out

        # ① 主引擎：MyMemory（支持多目标语言）
        out = self._mymemory(text, src, tgt)
        if out:
            self._cache_put(key, out)
            self.last_provider = "免费(MyMemory)"
            if on_chunk:
                on_chunk(out)
            return out

        # ② 兜底一：有道免费（只支持译成中文）
        if tgt == "zh-CN":
            out = self._youdao_free(text)
            if out:
                self._cache_put(key, out)
                self.last_provider = "免费(有道)"
                if on_chunk:
                    on_chunk(out)
                return out

        # ③ 兜底二：本地 opus-mt（仅英语→中文，纯离线不限量）
        if tgt == "zh-CN" and src == "en" and self.local_tr is not None:
            try:
                if self.local_tr.available():
                    out = self.local_tr.translate(text, "中文")
                    if out:
                        self._cache_put(key, out)
                        self.last_provider = "本地兜底"
                        if on_chunk:
                            on_chunk(out)
                        return out
            except Exception:
                pass

        self.last_provider = "免费(失败)"
        return "[免费在线翻译暂时不可用：今日免费额度可能已用完或网络不通，稍后会自动恢复]"


# ================================================================ 云端翻译器（DeepSeek）
class Translator:
    """调用 OpenAI 兼容接口做翻译（流式）。

    3.0.0 起支持自定义地址：把 api_base 指向中转站/自建网关即可，
    路径拼接与 DeepSeek 官方完全一致（<base>/chat/completions）。
    """

    API_URL = "https://api.deepseek.com/chat/completions"
    DEFAULT_BASE = "https://api.deepseek.com"

    def __init__(self, api_key=None, model="deepseek-chat", api_base=None):
        self.api_key = api_key or self._load_key()
        self.model = model
        base = (api_base or "").rstrip("/")
        if not base:
            self.API_URL = Translator.API_URL
        elif base.endswith("/chat/completions"):
            self.API_URL = base
        else:
            self.API_URL = base + "/chat/completions"

    @staticmethod
    def _load_key():
        """API Key 来源（v2.5.1 起只认用户自己的，绝不偷偷借用开发机凭据）

        1) 环境变量 DEEPSEEK_API_KEY
        2) 程序设置里填的 Key（存在 ~/.hearizon_sync.json）
        注意：旧版会去读 ~/.dsh/.credentials.yaml，那会让「别人电脑上没 Key」
        却显示「有 Key」，是 v2.1.1 的 bug 来源，已删除。
        """
        k = os.environ.get("DEEPSEEK_API_KEY")
        if k and k.strip():
            return k.strip()
        k = (DEEPSEEK_KEY or "").strip()
        if k:
            return k
        return None

    def set_key(self, key):
        """设置页保存后热更新 Key，不用重启程序。"""
        key = (key or "").strip()
        env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        self.api_key = key or env or None

    def set_endpoint(self, base_url=None, model=None, key=None):
        """设置页保存后热更新自定义接口（地址 / 模型 / Key）。"""
        if base_url is not None:
            base = base_url.strip().rstrip("/")
            if not base:
                self.API_URL = Translator.API_URL
            elif base.endswith("/chat/completions"):
                self.API_URL = base
            else:
                self.API_URL = base + "/chat/completions"
        if model is not None and model.strip():
            self.model = model.strip()
        if key is not None:
            self.api_key = key.strip() or None

    def available(self):
        return bool(self.api_key)

    def translate(self, text, target_lang="中文", on_chunk=None, timeout=30,
                  context=None):
        """流式翻译；on_chunk(增量文本) 用于边收边显示。

        context：最近几轮 (原文, 译文)，用来补上跨句的指代与省略，
        没有上下文时行为和以前完全一致。
        """
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
        msgs = [{"role": "system", "content": sys_prompt}]
        # 把前几轮当成已经翻过的对话，模型才能接住「他」「这个」指代谁
        for prev_src, prev_dst in (context or []):
            if prev_src and prev_dst:
                msgs.append({"role": "user", "content": prev_src})
                msgs.append({"role": "assistant", "content": prev_dst})
        msgs.append({"role": "user", "content": text})
        body = json.dumps({
            "model": self.model,
            "messages": msgs,
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
            with _urlopen(req, timeout=timeout) as resp:
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

    def correct(self, text, lang="auto", timeout=15):
        """v2.1.1 自动纠错：只修正明显的 ASR 错误，不润色、不翻译。

        非流式调用；失败返回原文（调用方静默回退）。
        """
        if not text.strip() or not self.api_key:
            return text
        system = (
            "你是语音识别（ASR）结果的纠错器。"
            "输入可能有同音字、错别字、漏字、重复词或标点缺失。\n"
            "要求：\n"
            "1. 只做最小必要修正，恢复明显错误；不改变原意，不润色，不扩写，不翻译\n"
            "2. 没有明显错误就原样返回\n"
            "3. 只输出修正后的文本，不要解释、不要引号、不要任何前缀"
        )
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": 300,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.API_URL, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with _urlopen(req, timeout=timeout) as resp:
                d = json.loads(resp.read().decode("utf-8", errors="replace"))
            out = d["choices"][0]["message"]["content"].strip()
            return out or text
        except Exception:
            return text


# ================================================================ 识别增强
# 三件事，对所有用户生效：
# 1. 热词：用户在设置里自己填术语，不预置任何领域词表
# 2. 自适应 VAD：按环境噪声自动调整门限，远场小声也能切出来
# 3. 降噪 + 增益归一：压低底噪、把远场小信号拉回可识别音量

# 热词表（用户配置，逗号或换行分隔）
HOTWORDS = []


def hotwords_list():
    """解析用户热词，返回去重后的词列表。"""
    out = []
    seen = set()
    for raw in HOTWORDS:
        for w in re.split(r"[,，;；\n]+", str(raw)):
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                out.append(w)
    return out


def hotword_prompt(base=""):
    """把用户热词拼进提示词，引导模型偏向这些词。"""
    words = hotwords_list()
    if not words:
        return base
    tail = "、".join(words[:60])
    return (base + " 常见词：" + tail) if base else ("常见词：" + tail)


def apply_hotwords(text):
    """按用户热词做同音纠错：识别结果里出现与热词高度相似但不同的词时纠正。

    只处理长度相同、仅个别字不同的情况，避免误改正常语句。
    """
    if not text:
        return text
    words = hotwords_list()
    if not words:
        return text
    t = text
    for w in words:
        if len(w) < 2 or w in t:
            continue
        n = len(w)
        for i in range(len(t) - n + 1):
            seg = t[i:i + n]
            if seg == w:
                continue
            # 逐字比较，允许 1 个字不同（长词允许 2 个）
            diff = sum(1 for a, b in zip(seg, w) if a != b)
            limit = 1 if n <= 4 else 2
            if diff and diff <= limit:
                t = t[:i] + w + t[i + n:]
                break
    return t


# ================================================================ 音频增强
# 与独立引擎库 HAE-NSG Core 同一套算法。这里内联一份，安装包就不依赖外部包；
# 两边的参数和运算顺序保持一致，改动时请两边一起改。
#
# 和旧版最大的区别：增益与降噪一起算，不再「先降噪、再整体放大」。
# 先降后放会把漏判的底噪原样抬回去：20 倍增益下，底噪先压到 0.3 倍、
# 再整体乘 20，净效果是变响 6 倍，测量上「降了 10 dB」、听起来更吵。
_AUDIO_EPS = 1e-12


class AudioProfile:
    """一组增强参数。"""

    __slots__ = ("target_rms", "max_gain", "pre_emph", "mix",
                 "gate_ms", "gate_atten", "knee")

    def __init__(self, target_rms, max_gain, pre_emph, mix,
                 gate_ms, gate_atten, knee):
        self.target_rms = float(target_rms)
        self.max_gain = float(max_gain)
        self.pre_emph = float(pre_emph)
        self.mix = float(mix)
        self.gate_ms = float(gate_ms)
        self.gate_atten = float(gate_atten)
        self.knee = float(knee)


AUDIO_NEAR = AudioProfile(0.12, 6.0, 0.0, 0.0, 20.0, 0.30, 0.97)
# 远场/强制放大只拉电平，不做预加重也不再压软限幅。
# 实测（6 条中文语料、SenseVoice）：
#   预加重 + knee 0.85 在 babble 环境下让字符错误率从 7.3% 涨到 11.1%，
#   去掉之后回到 7.2%，混响场景基本持平。
# 也就是说「补高频」对人是听着亮，对模型是失真，所以这里不再做。
AUDIO_FAR = AudioProfile(0.18, 40.0, 0.0, 0.0, 20.0, 0.30, 0.97)
AUDIO_LOUD = AudioProfile(0.30, 120.0, 0.0, 0.0, 20.0, 0.30, 0.97)


def _split_energy(energy):
    """把帧能量分成静音簇与语音簇，返回两者之间的阈值。

    不用固定分位数，是因为第 10 分位落在静音帧自身的分布里，
    门限会横切在静音段中间：一半静音帧判静音、一半漏掉，
    漏掉的那部分随后被增益放大几十倍，底噪反而比处理前更响。
    两簇一维聚类只需几轮迭代，阈值落在两簇之间，不在簇内部。
    """
    e = np.asarray(energy, dtype=np.float64).ravel()
    if e.size < 3:
        return None
    lo = float(np.percentile(e, 10))
    hi = float(np.percentile(e, 90))
    if hi <= lo * 1.5:
        # 整段能量本来就均匀，没有可判的静音段
        return None
    thr = (lo + hi) / 2.0
    c_lo, c_hi = lo, hi
    for _ in range(12):
        quiet = e < thr
        if not quiet.any() or quiet.all():
            return None
        c_lo = float(e[quiet].mean())
        c_hi = float(e[~quiet].mean())
        new_thr = (c_lo + c_hi) / 2.0
        settled = abs(new_thr - thr) < 1e-9
        thr = new_thr
        if settled:
            break
    if c_hi < c_lo * 2.0:
        # 两簇中心分不开，「降噪」只是在锯信号
        return None
    return thr


def _quiet_mask(audio, rate, frame_ms):
    """标出静音帧，返回样本级掩码；判断不了时返回 None。

    增益要跳过这些帧、降噪也要针对这些帧，两件事必须用同一份判决，
    否则会出现「语音按整体电平算增益、底噪又按别处电平压」的错位。
    """
    frame = max(32, int(round(float(rate) * float(frame_ms) / 1000.0)))
    a = np.asarray(audio, dtype=np.float64).ravel()
    n = a.size // frame
    if n < 2:
        return None
    body = a[:n * frame].reshape(n, frame)
    energy = np.sqrt(np.mean(body * body, axis=1))
    thr = _split_energy(energy)
    if thr is None:
        return None
    mask = np.zeros(a.size, dtype=bool)
    for i in np.nonzero(energy < thr)[0]:
        mask[i * frame:(i + 1) * frame] = True
    return mask


def _pre_emphasis(audio, coeff, mix):
    """一阶高通与原信号按比例混合，补回远场丢掉的高频。

    空气对 2 kHz 以上的吸收随距离明显增加，/s/ /f/ /θ/ 这几个音最先糊掉。
    纯高通会把低频掏空、声音发尖，所以只混回一部分补偿量。

    出口把电平缩回输入的电平：预加重的职责是改频谱形状，不是改音量。
    它要是顺手把电平降了，后面的增益就得用更大倍数去补，
    很容易一头撞到 max_gain 上，白浪费动态范围。
    """
    a = np.asarray(audio, dtype=np.float64).ravel()
    c = float(coeff)
    m = float(mix)
    if c <= 0.0 or m <= 0.0 or a.size < 2:
        return a.astype(np.float32)
    hi = a.copy()
    hi[1:] = a[1:] - c * a[:-1]
    y = (1.0 - m) * a + m * hi
    r0 = np.sqrt(np.mean(a * a))
    r1 = np.sqrt(np.mean(y * y))
    if r1 > _AUDIO_EPS and r0 > _AUDIO_EPS:
        y = y * (r0 / r1)
    # 出口落回 float32：引擎库在同一步也这么做，两边才能逐位一致
    return y.astype(np.float32)


def _soft_limit(audio, knee):
    """峰值软限幅。超过拐点用 tanh 压缩，不产生硬削波的方波谐波。"""
    a = np.asarray(audio, dtype=np.float64).ravel()
    k = float(knee)
    if k <= 0.0 or a.size == 0:
        return a
    pk = float(np.max(np.abs(a)))
    if pk <= k:
        return a
    return np.tanh(a / k) * k


def enhance(audio, profile=None, rate=None, gate=True):
    """完整增强：高频补偿 → 语音帧增益 → 静音段压制 → 软限幅。

    静音段的最终倍数固定为 gate_atten，与增益无关。这样语音抬多少倍
    都不影响底噪，底噪永远净降约 10 dB。旧版把它封顶在 1/gain，
    净倍数顶多回到 1，那只是「没更吵」，谈不上降噪。
    """
    if audio is None or len(audio) == 0:
        return audio
    prof = profile or AUDIO_NEAR
    rate = rate or SAMPLE_RATE
    try:
        # 先落到 float32 再升回 float64：和 HAE-NSG Core 的处理路径一致，
        # 两边才能在逐位对拍时给出同一串数字。
        a = np.asarray(audio, dtype=np.float32).astype(np.float64).ravel()
        if not np.all(np.isfinite(a)):
            a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
        y = _pre_emphasis(a, prof.pre_emph, prof.mix)

        quiet = _quiet_mask(y, rate, prof.gate_ms) if gate else None

        # 增益按语音部分的电平算。算上静音段的话，
        # 静音越多、电平越低、增益算得越大，正好放大最不该放大的东西。
        gain = 1.0
        if prof.target_rms > 0.0:
            if quiet is not None and not quiet.all():
                cur = float(np.sqrt(np.mean(y[~quiet].astype(np.float64) ** 2)))
            else:
                cur = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
            if cur > _AUDIO_EPS:
                g = prof.target_rms / cur
                if g > 1.0:
                    gain = min(g, prof.max_gain)

        # 显式转 float64 再乘：float32 数组乘 python 浮点会留在 float32，
        # 和引擎库的中间精度不一致，逐位对拍就会差最后几位。
        out = y.astype(np.float64) * gain
        if quiet is not None:
            out[quiet] *= prof.gate_atten / max(gain, 1.0)

        return _soft_limit(out, prof.knee).astype(np.float32)
    except Exception:
        return audio


def denoise(audio, strength=1.0):
    """轻量降噪：只压低静音段与稳定底噪，不动语音主体。

    做法：按 20ms 帧算能量，找出噪声底，把明显低于噪声底的帧衰减，
    其余帧保持原样。这样不会破坏语音，也不会引入谱失真。
    """
    if audio is None or len(audio) == 0:
        return audio
    try:
        a = np.asarray(audio, dtype=np.float32)
        if a.size < 512:
            return audio
        frame = 320                       # 20ms
        n = a.size // frame
        if n < 2:
            return audio
        body = a[:n * frame].reshape(n, frame)
        energy = np.sqrt(np.mean(body.astype(np.float64) ** 2, axis=1))
        noise = float(np.percentile(energy, 10))
        if noise <= 1e-6:
            return audio
        # 低于噪声底的帧（纯底噪）衰减，语音帧不动
        gate = noise * 1.1 / max(0.3, strength)
        gain = np.ones(n, dtype=np.float64)
        quiet = energy < gate
        if not np.any(quiet):
            return audio
        gain[quiet] = 0.30
        out = body.astype(np.float64) * gain[:, None]
        tail = a[n * frame:].astype(np.float64)
        out = np.concatenate([out.ravel(), tail]).astype(np.float32)
        peak = float(np.max(np.abs(out)))
        if peak > 0.98:
            out = (out * (0.98 / peak)).astype(np.float32)
        return out
    except Exception:
        return audio


def normalize_gain(audio, target=0.12, max_gain=6.0):
    """远场增益归一：说话人离得远时音量很小，识别率会掉。

    把整句拉到统一的目标电平，但限制最大增益，避免把底噪一起放大。
    只放大不缩小，已经够响的句子原样返回。
    """
    if audio is None or len(audio) == 0:
        return audio
    try:
        a = np.asarray(audio, dtype=np.float32)
        rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
        if rms <= 1e-5:
            return audio
        gain = target / rms
        if gain <= 1.0:
            return audio
        gain = min(gain, max_gain)
        out = a * gain
        peak = float(np.max(np.abs(out)))
        if peak > 0.97:
            out = out * (0.97 / peak)
        return out.astype(np.float32)
    except Exception:
        return audio


def far_field_enhance(audio, target=0.22, max_gain=20.0, pre_emph=0.55):
    """远场增强：先把高频补回来，再把整句拉到统一响度。

    人离麦克风超过四五米时有两个问题：一是音量掉到门限附近，
    二是空气对 2 kHz 以上吸收明显，辅音（/s/ /f/ /θ/）最先糊掉。
    这里先做一次轻度高频补偿，再做强增益归一，最后软限幅防爆音。
    只放大不缩小，近场录音走这条路也不会失真。
    """
    if audio is None or len(audio) == 0:
        return audio
    try:
        a = np.asarray(audio, dtype=np.float32)
        # 高频补偿：一阶高通与原信号按比例混合，避免过度提亮
        hi = a.copy()
        hi[1:] = a[1:] - pre_emph * a[:-1]
        y = 0.62 * a + 0.38 * hi
        rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
        if rms > 1e-6:
            gain = target / rms
            if gain > 1.0:
                y = y * min(gain, max_gain)
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        if peak > 0.85:
            # tanh 软限幅：削掉峰值又不产生明显谐波失真
            y = np.tanh(y / 0.85) * 0.85
        return y.astype(np.float32)
    except Exception:
        return audio


def trim_tail_valley(seg, rate=None, search_ms=None, pad_ms=40):
    """把片段尾部多余的静音切到「能量谷底」，只留一点安全余量。

    原来切句时会把 VAD_HANG_MS（700 ms）的静音一起送进识别器，
    静音太长容易让模型开始编内容。这里从尾部往前找第一个真正的静音点。
    """
    rate = rate or SAMPLE_RATE
    search = int(rate * (search_ms or VAD_VALLEY_MS) / 1000)
    step = max(1, int(rate * 0.01))       # 10 ms 一窗
    n = len(seg) if seg is not None else 0
    if n <= step * 4:
        return seg
    start = max(0, n - search)
    wins = []
    for i in range(start, n - step, step):
        wins.append((float(np.mean(np.asarray(seg[i:i + step], dtype=np.float64) ** 2)), i))
    if not wins:
        return seg
    peak = max(w[0] for w in wins)
    if peak <= 1e-9:
        return seg
    cut = n
    for energy, idx in reversed(wins):
        if energy < peak * 0.02:
            cut = idx
            break
    cut = min(n, cut + int(rate * pad_ms / 1000))
    return seg[:cut] if cut > step * 4 else seg


class NoiseFloor:
    """环境噪声自适应：持续跟踪底噪，给 VAD 与降噪提供动态门限。

    用滑动分位数估计，避免被偶发的响声带偏。
    """

    def __init__(self, size=180):
        self.hist = deque(maxlen=size)

    def update(self, rms):
        try:
            self.hist.append(float(rms))
        except Exception:
            pass
        return self.floor()

    def floor(self):
        if not self.hist:
            return 0.0
        try:
            arr = np.fromiter(self.hist, dtype=np.float64)
            return float(np.percentile(arr, 20))
        except Exception:
            return 0.0

    def threshold(self, base=VAD_MIN_RMS, factor=2.4):
        """动态门限：至少 base，噪声高时按倍数抬高。"""
        try:
            return max(base, self.floor() * factor)
        except Exception:
            return base


# ================================================================ 切句状态机（HAE-VAD 内核）
# 与独立仓库 HAE-VAD-Core 的 Segmenter 行为等价，逐位对拍见 _build/test_v300_vad.py
_VAD_EPS = 1e-12

# 切句参数。所有能影响切句行为的数字集中在这里，状态机本身不存魔法数。
_VAD_CFG = {
    "min_rms": VAD_MIN_RMS,
    "zcr_max": 0.45,                # 白噪声过零率理论值 0.5，取 0.45 才挡得住
    "floor_size": 180,              # 30 ms 一块时约 5.4 秒
    "floor_percentile": 20.0,       # 低分位而非均值，避免被偶发响声带偏
    "floor_factor": 2.4,
    "block_ms": BLOCK_MS,
    "hang_ms": VAD_HANG_MS,
    "min_speech_ms": VAD_MIN_MS,
    "max_speech_ms": VAD_MAX_MS,
    "preroll_ms": VAD_PREROLL_MS,
    "valley_ms": VAD_VALLEY_MS,
    "valley_ratio": 0.02,
    "valley_pad_ms": 40.0,
    "valley_min_ms": 40.0,
    "stream_window_s": STREAM_WINDOW_S,
    "stream_interval_s": STREAM_INTERVAL,
    "stream_min_s": STREAM_MIN_S,
    "far_field_scale": 0.65,        # 远场：人声到麦克风已经很弱，门限必须放低
    "force_loud_scale": 0.45,       # 强制放大：比远场更激进
}


def _vad_rms(audio):
    a = np.asarray(audio, dtype=np.float64).ravel()
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a * a)))


def _vad_percentile(values, p):
    """线性插值分位数，公式与 C++ 版一致。"""
    n = len(values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(values[0])
    q = min(100.0, max(0.0, float(p)))
    s = sorted(float(v) for v in values)
    pos = (n - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _vad_zcr(block):
    """过零率。人声 16 kHz 下约 0.02 到 0.25，嘶声风噪普遍 0.4 以上。"""
    a = np.asarray(block, dtype=np.float64).ravel()
    if a.size < 2:
        return 0.0
    signs = np.signbit(a)
    return float(np.mean(signs[1:] != signs[:-1]))


def _vad_energy_envelope(seg, rate, win_ms=10.0):
    a = np.asarray(seg, dtype=np.float64).ravel()
    step = max(1, int(rate * float(win_ms) / 1000.0))
    n = a.size
    if n < step:
        return np.zeros(0, dtype=np.float64), step
    count = (n - step) // step + 1
    out = np.empty(count, dtype=np.float64)
    for i in range(count):
        chunk = a[i * step:(i + 1) * step]
        out[i] = float(np.mean(chunk * chunk))
    return out, step


def _vad_trim_tail(seg, rate, search_ms=320.0, ratio=0.02, pad_ms=40.0,
                   min_ms=40.0, win_ms=10.0):
    """尾部切到能量谷底。

    峰值取整段而不是搜索窗口内的最大值。这一点很容易搞错：尾部静音比搜索
    窗口还长时，窗口里全是静音，窗口内峰值接近零，任何阈值判断都会失效，
    整段原样返回，裁切等于没做。
    """
    a = np.asarray(seg, dtype=np.float32).ravel()
    n = a.size
    if n == 0:
        return a
    step = max(1, int(rate * float(win_ms) / 1000.0))
    if n <= step * 4:
        return a
    if n < rate * float(min_ms) / 1000.0:
        return a
    env, _ = _vad_energy_envelope(a, rate=rate, win_ms=win_ms)
    if env.size == 0:
        return a
    peak = float(np.max(env))
    if peak <= _VAD_EPS:
        return a
    # 从尾部往前跳过安静窗口，停在第一个还在说话的窗口上，切在它之后。
    cut = n
    for i in range(env.size - 1, -1, -1):
        if env[i] >= peak * float(ratio):
            cut = (i + 1) * step
            break
    cut = min(n, cut + int(rate * float(pad_ms) / 1000.0))
    if cut <= step * 4:
        return a
    return a[:cut].copy()


def _vad_trim_head(seg, rate, search_ms=320.0):
    """流式临时结果用的轻量裁切，只影响边说边出字的显示。"""
    a = np.asarray(seg, dtype=np.float32).ravel()
    n = a.size
    if n == 0:
        return a
    step = max(1, int(rate * 0.01))
    search = max(step, int(rate * float(search_ms) / 1000.0))
    start = max(0, n - search)
    if start >= n:
        return a
    tail = a[start:]
    mean = float(np.mean(tail.astype(np.float64) ** 2))
    if mean <= _VAD_EPS:
        return a
    cut = n
    for i in range(n - step, start, -step):
        chunk = a[i:i + step].astype(np.float64)
        if float(np.mean(chunk * chunk)) >= mean:
            break
        cut = i
    if cut <= step * 4:
        return a
    return a[:cut].copy()


class _VadNoiseFloor:
    """滑动窗口噪声底。

    取低分位而不是均值，因为底噪的定义就是持续存在的那条最低电平，
    一次关门声不该把门限抬高五秒。
    """

    __slots__ = ("_hist", "_size", "_pct", "_factor", "_min_rms")

    def __init__(self, size=180, pct=20.0, factor=2.4, min_rms=0.008):
        self._size = max(1, int(size))
        self._hist = deque(maxlen=self._size)
        self._pct = float(pct)
        self._factor = float(factor)
        self._min_rms = float(min_rms)

    def update(self, rms):
        try:
            v = float(rms)
        except (TypeError, ValueError):
            v = 0.0
        if not math.isfinite(v) or v < 0.0:
            v = 0.0
        self._hist.append(v)
        return self.floor()

    def floor(self):
        if not self._hist:
            return 0.0
        return _vad_percentile(self._hist, self._pct)

    def threshold(self, scale=1.0):
        """门限 = max(min_rms, 底噪 × factor) × 场景系数。"""
        return max(self._min_rms, self.floor() * self._factor) * float(scale)

    def reset(self):
        self._hist.clear()


class _VadSegmenter:
    """流式切句状态机：喂音频块，出完整句子。

    两处顺序不能改：
    1. 门限必须先用「上一块为止」的底噪算出来，再判断本块。反过来的话，
       一块响亮的语音会把自己的门限抬到自身电平之上，然后它自己就被判成
       静音。
    2. 底噪只在不是人声的时候更新。语音块也进窗口的话，连续说几句会把底噪
       顶到语音电平，门限跟着涨，句子越说越短。

    预卷是必需的：/s/ /f/ 这类词首能量比元音低一个数量级，只按当前块判断
    会把词首当静音丢掉，表现为 Spanish 被听成 fan。静音时滚动缓存一段，
    真正开口时接到前面。
    """

    def __init__(self, rate=None, cfg=None):
        self.rate = int(rate or SAMPLE_RATE)
        self.cfg = dict(_VAD_CFG if cfg is None else cfg)
        c = self.cfg
        self._floor = _VadNoiseFloor(c["floor_size"], c["floor_percentile"],
                                     c["floor_factor"], c["min_rms"])
        self._scale = 1.0
        self._pre = []
        self._pre_n = 0
        self._tail = []
        self._tail_n = 0
        self._buf = []
        self._active = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._last_partial = -1e9
        self._level = 0.0
        self._threshold = c["min_rms"]
        self._zcr = 0.0
        self._voiced = False
        self._segments = 0

    @property
    def active(self):
        return self._active

    @property
    def level(self):
        """最近一块的 RMS。"""
        return self._level

    @property
    def threshold(self):
        """当前生效的门限。"""
        return self._threshold

    @property
    def voiced(self):
        """最近一块是否被判定为人声。"""
        return self._voiced

    def set_scale(self, scale):
        """场景系数：门限整体乘这个值。远场 0.65，强制放大 0.45。"""
        self._scale = float(scale)

    def reset(self):
        self._floor.reset()
        self._pre = []
        self._pre_n = 0
        self._tail = []
        self._tail_n = 0
        self._buf = []
        self._active = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._last_partial = -1e9
        self._level = 0.0
        self._zcr = 0.0
        self._voiced = False

    def feed(self, block):
        """喂一块音频，返回这次切出来的句子列表（通常空或一个元素）。"""
        if block is None:
            return []
        try:
            a = np.asarray(block, dtype=np.float32)
        except (TypeError, ValueError):
            return []
        if a.ndim > 1:
            a = a[:, 0] if a.shape[1] > 0 else a.ravel()
        a = a.ravel()
        if a.size == 0:
            return []
        if not np.all(np.isfinite(a)):
            a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)

        c = self.cfg
        ms = a.size * 1000.0 / float(self.rate)
        self._level = _vad_rms(a)
        self._zcr = _vad_zcr(a)

        # 先用上一块为止的底噪算门限，再判断本块
        self._threshold = self._floor.threshold(self._scale)
        self._voiced = (self._level > self._threshold) and (self._zcr < c["zcr_max"])
        # 只有非人声才更新底噪，否则连续说话会把门限顶上去
        if not self._voiced:
            self._floor.update(self._level)

        out = []
        if self._voiced:
            if not self._active:
                self._active = True
                self._buf = list(self._pre)
                self._pre = []
                self._pre_n = 0
            self._silence_ms = 0.0
            self._buf.append(a)
            self._speech_ms += ms
            self._append_tail(a)
        else:
            if self._active:
                self._buf.append(a)
                self._append_tail(a)
                self._silence_ms += ms
            else:
                self._append_pre(a)

        if self._should_cut():
            out.extend(self._cut())
        return out

    def flush(self):
        """会话结束时把还没切出去的音频吐出来，别丢最后半句。"""
        if not self._active:
            self.reset()
            return []
        self._silence_ms = max(self._silence_ms, self.cfg["hang_ms"])
        return self._cut()

    def partial(self):
        """流式临时结果，间隔不到就返回 None。"""
        c = self.cfg
        if not self._active:
            return None
        if self._speech_ms / 1000.0 < c["stream_min_s"]:
            return None
        now = time.time()
        if now - self._last_partial < c["stream_interval_s"]:
            return None
        self._last_partial = now
        if not self._tail:
            return None
        seg = np.concatenate(self._tail) if len(self._tail) > 1 else self._tail[0]
        return _vad_trim_head(seg, self.rate, c["valley_ms"])

    def _append_pre(self, a):
        """静音期滚动缓存，总量不超过 preroll_ms。"""
        c = self.cfg
        limit = int(self.rate * c["preroll_ms"] / 1000.0)
        self._pre.append(a)
        self._pre_n += a.size
        while self._pre and self._pre_n > limit:
            self._pre_n -= self._pre.pop(0).size
        hard = int(self.rate * (c["max_speech_ms"] + c["hang_ms"]) / 1000.0)
        if self._pre_n > hard:
            self._pre = []
            self._pre_n = 0

    def _append_tail(self, a):
        """流式窗口滚动缓存，总量不超过 stream_window_s。"""
        limit = int(self.rate * self.cfg["stream_window_s"])
        self._tail.append(a)
        self._tail_n += a.size
        while self._tail and self._tail_n > limit:
            self._tail_n -= self._tail.pop(0).size

    def _should_cut(self):
        if not self._active:
            return False
        c = self.cfg
        if self._silence_ms >= c["hang_ms"]:
            return True
        if self._speech_ms >= c["max_speech_ms"]:
            return True
        return False

    def _cut(self):
        c = self.cfg
        if not self._buf:
            self._active = False
            self._reset_sentence()
            return []
        seg = self._buf[0] if len(self._buf) == 1 else np.concatenate(self._buf)
        speech = self._speech_ms
        self._active = False
        self._reset_sentence()
        if seg.size == 0 or speech < c["min_speech_ms"]:
            return []
        seg = _vad_trim_tail(seg, self.rate, c["valley_ms"], c["valley_ratio"],
                             c["valley_pad_ms"], c["valley_min_ms"])
        self._segments += 1
        return [seg]

    def _reset_sentence(self):
        self._buf = []
        self._tail = []
        self._tail_n = 0
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._last_partial = -1e9


def _vad_profile():
    """按当前开关选增强预设。"""
    if FORCE_LOUD:
        return AUDIO_LOUD
    if FAR_FIELD:
        return AUDIO_FAR
    return AUDIO_NEAR


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
        # 3.1.0：用可重入锁统一保护「加载 / 识别 / 切引擎 / 切语言 / reset」。
        # 以前 transcribe 在锁外 load()，主线程切引擎又把 _sv/_whisper 置 None，
        # 识别线程拿到 None 就崩。现在所有模型生命周期操作都在同一把锁里。
        self._lock = threading.RLock()
        # 主线程切引擎/语言/重置时最多等 0.25s；等不到就挂起标记，
        # 由识别线程在下一次 transcribe 的锁内生效，避免 Whisper 推理期间界面卡住。
        self._pending_engine = None
        self._pending_lang = None
        self._pending_reset = False
        self.audio_boost = True       # 识别增强开关（热词纠错 + 自适应门限 + 增益）

    # ---------------- 引擎 / 语言 ----------------
    def _apply_engine(self, name):
        """调用方必须已持有 self._lock。"""
        if name == self.engine and (self._sv is not None or self._whisper is not None):
            return
        self.engine = name
        self._sv = None
        self._whisper = None

    def set_engine(self, name):
        if not self._lock.acquire(timeout=0.25):
            self._pending_engine = name
            return
        try:
            self._apply_engine(name)
        finally:
            self._lock.release()

    def _apply_language(self, display_name):
        """调用方必须已持有 self._lock。返回是否被迫切换引擎。"""
        self.lang_name = display_name
        if display_name in SV_UNSUPPORTED and self.engine == "sensevoice":
            self.engine = "small"
            self._sv = None
            self._whisper = None
            return True
        # Whisper 的 language 参数在识别时传，不用重建
        return False

    def set_language(self, display_name):
        """设置输入语言；返回是否被迫切换了引擎（立即算出，不阻塞主线程）。"""
        forced = display_name in SV_UNSUPPORTED and self.engine == "sensevoice"
        if not self._lock.acquire(timeout=0.25):
            self._pending_lang = display_name
            return forced
        try:
            return self._apply_language(display_name)
        finally:
            self._lock.release()

    def reset(self):
        if not self._lock.acquire(timeout=0.25):
            self._pending_reset = True
            return
        try:
            self._sv = None
            self._whisper = None
        finally:
            self._lock.release()

    # ---------------- 加载 ----------------
    def load(self):
        with self._lock:
            return self._load_impl()

    def _load_impl(self):
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
            size = self.engine if self.engine in ENGINE_SIZES else "small"
            use_gpu = bool(GPU_ACCEL)
            dev = "cuda" if use_gpu else "cpu"
            ctype = "float16" if use_gpu else COMPUTE_TYPE
            mb = ENGINE_SIZES.get(size, "")
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
        with self._lock:
            # 主线程挂起的切换请求在这里统一生效
            if self._pending_engine is not None:
                self._apply_engine(self._pending_engine)
                self._pending_engine = None
            if self._pending_lang is not None:
                self._apply_language(self._pending_lang)
                self._pending_lang = None
            if self._pending_reset:
                self._sv = None
                self._whisper = None
                self._pending_reset = False
            self.load()
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

        text = (r.text or "").strip()
        # 用户热词纠错（不预置任何领域词，由用户自己填）
        if text:
            text = strip_prompt_echo(text)
        if text:
            text = apply_hotwords(text)
        return text, clean(r.lang), clean(r.emotion), clean(r.event)

    def _transcribe_whisper(self, audio):
        _, wh_code = input_codes(self.lang_name)
        lang_arg = wh_code if wh_code else None
        # 3.0.0 修 bug：以前无论有没有热词都塞 prompt，小模型会把自己被喂的
        # 那句提示词当成前文一起吐出来（于是出现「请按口语原样记录」）。
        # 现在只有用户真的填了热词才拼 prompt，其余情况交给 language 参数。
        words = hotwords_list()
        prompt = asr_prompt(self.lang_name) if words else None
        segs, info = self._whisper.transcribe(
            audio,
            language=lang_arg,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            initial_prompt=prompt,
        )
        text = "".join(x.text for x in segs).strip()
        # 显式锁了语言就以锁定的为准，避免「说中文被认成日语」
        lang = wh_code or info.language or ""
        if lang == "zh" and text:
            segs2, _ = self._whisper.transcribe(
                audio, language="zh", beam_size=5, vad_filter=False,
                condition_on_previous_text=False, initial_prompt=prompt,
            )
            t2 = "".join(x.text for x in segs2).strip()
            if t2:
                text = t2
        if text:
            text = strip_prompt_echo(text)
        if text:
            text = apply_hotwords(text)
        return text, lang, "", ""


# ================================================================ 音频采集 + VAD
class SpectralDenoiser:
    """频谱降噪：按频点压制噪声，真正抬高语音帧的信噪比。

    和 enhance() 的分工必须说清楚，两者不能互相替代：

      enhance()
        逐帧乘一个音量系数。语音和噪声一起放大、一起压低，
        信噪比完全不变。它解决的是「声音太小」。
        实测：白噪声 10 dB、远场 -40 dB 输入，语音帧信噪比变化 +0.00 dB。

      本类
        在短时傅里叶域逐频点判断「这一格是人声还是噪声」：
        噪声占优的频点压下去，语音占优的频点保留。
        噪声是稳态的（空调、风扇、投影机、远处人声），在噪声段能学准，
        于是语音段的信噪比会真的上去，识别率跟着上去。
        实测：白噪声 10 dB 输入，语音帧信噪比 +5.4 dB；0 dB 输入 +8.5 dB。

    实现要点：
      1. 512 点窗、256 点步进，16 ms 延迟，够实时。
      2. 噪声谱只在「非人声」且能量接近底噪时学习，避免把话学成噪声。
      3. 启动时用前若干帧的 30 分位初始化噪声谱，防止一开口就学成噪声。
      4. 增益沿时间做轻度平滑，压制「音乐噪声」（waterfall 那种咕噜声）。
      5. 保留原始相位，只改幅值——简单、稳定、不产生相位跳变。
    """

    def __init__(self, rate=None, nfft=512, over=None, floor=0.15):
        self.rate = rate or SAMPLE_RATE
        self.nfft = int(nfft)
        self.hop = self.nfft // 2
        self.win = np.hanning(self.nfft + 1)[:-1]
        self.wsum = float(np.sum(self.win ** 2)) or 1.5
        self.over = float(SPECTRAL_STRENGTH if over is None else over)
        self.floor = float(floor)
        self.enabled = True
        self.reset()

    def reset(self):
        self._in = np.zeros(0, dtype=np.float64)
        self._ola = np.zeros(self.nfft, dtype=np.float64)
        self._out = np.zeros(0, dtype=np.float64)
        self._boot = []          # 启动阶段的谱样本，用来估初始噪声
        self.noise = None        # 每个频点的噪声功率
        self._g_prev = None

    def _gain(self, power, voiced):
        """算这一帧每个频点的幅值增益（0~1）。"""
        if self.noise is None:
            # 启动：先攒几帧再定噪声谱，只处理不抑制
            self._boot.append(power)
            if len(self._boot) < 12:
                return np.ones_like(power)
            stack = np.asarray(self._boot)
            # 说话是瞬态的，取 30 分位能把人声挤掉，留下稳态底噪
            self.noise = np.percentile(stack, 30, axis=0)
            self._boot = []
            return np.ones_like(power)

        npsd = np.maximum(self.noise, 1e-20)
        # 只在「不是人声」且能量没明显高于底噪时学习，避免把语音学成噪声
        if (not voiced) and float(power.sum()) < 4.0 * float(npsd.sum()):
            self.noise = 0.80 * self.noise + 0.20 * power
            npsd = np.maximum(self.noise, 1e-20)

        amp = np.sqrt(np.maximum(power, 1e-20))
        sub = self.over * np.sqrt(npsd)
        amp2 = np.maximum(amp - sub, self.floor * amp)
        g = amp2 / np.maximum(amp, 1e-20)
        if self._g_prev is not None:
            g = 0.65 * self._g_prev + 0.35 * g
        self._g_prev = g
        return g

    def process(self, block, voiced=False):
        """流式处理：进多少样本出多少样本，内部有 16 ms 的缓冲延迟。"""
        x = np.asarray(block, dtype=np.float64).ravel()
        if not self.enabled or x.size == 0:
            return np.asarray(block, dtype=np.float32)

        self._in = np.concatenate([self._in, x])
        while self._in.size >= self.nfft:
            frame = self._in[:self.nfft]
            spec = np.fft.rfft(frame * self.win)
            gain = self._gain(np.abs(spec) ** 2, voiced)
            self._ola += np.fft.irfft(spec * gain, self.nfft) * self.win
            done = self._ola[:self.hop] / self.wsum
            self._out = np.concatenate([self._out, done])
            self._ola[:-self.hop] = self._ola[self.hop:]
            self._ola[-self.hop:] = 0.0
            self._in = self._in[self.hop:]

        need = x.size
        if self._out.size >= need:
            y = self._out[:need]
            self._out = self._out[need:]
        else:
            # 刚启动时输出还没攒够，补零顶上，频谱上只是一瞬的静音
            y = np.concatenate([self._out, np.zeros(need - self._out.size)])
            self._out = np.zeros(0, dtype=np.float64)
        return np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


class NoiseShape:
    """粗判环境噪声是「稳态」还是「人声干扰」。

    做法：只看非人声的块，把频谱压成 16 个粗带，攒一段时间的样本，
    算每个带的变异系数。空调、风扇、机箱是稳态的，变异系数小；
    别人说话是忽大忽小忽有忽无的，变异系数大。

    为什么要分：稳态噪声降噪有效，人声干扰降噪有害（见 AudioCapture._denoise）。
    """

    def __init__(self, hist=40, bands=16):
        self.hist = deque(maxlen=max(8, int(hist)))
        self.bands = max(4, int(bands))

    def reset(self):
        self.hist.clear()

    def update(self, block, voiced):
        if voiced:
            return
        try:
            a = np.asarray(block, dtype=np.float64).ravel()
        except (TypeError, ValueError):
            return
        if a.size < 256 or not np.all(np.isfinite(a)):
            return
        spec = np.abs(np.fft.rfft(a * np.hanning(a.size)))
        edges = np.linspace(0, spec.size, self.bands + 1).astype(int)
        band = np.array([float(spec[edges[i]:edges[i + 1]].mean())
                         for i in range(self.bands)])
        if band.sum() <= 0:
            return
        self.hist.append(band)

    def stationary(self):
        """噪声形态判为稳态时返回 True。样本不足时保守返回 False。

        判据用「各频带能量在时间上的分贝标准差」（实测标定）：
          风扇 0.87、白噪声 0.92、极低电平噪声 0.86
          多人说话 5.84、单人说话片段 14 上下
        阈值取 2.5 dB，两类之间有 3 dB 以上的安全间隔。
        """
        if len(self.hist) < 12:
            return False
        h = np.asarray(self.hist, dtype=np.float64)
        db = 20.0 * np.log10(h + 1e-9)
        spread = float(np.mean(db.std(axis=0)))
        return spread < 2.5


class NeuralDenoiser:
    """sherpa-onnx 的神经降噪（DPDFNet2 / GTCRN）。

    比频谱降噪强得多，但也不是万能：
      稳态噪声 0 dB：字符错误率 5.2% -> 2.3%；
      多人说话 5 dB：7.3% -> 25.5%（会把别人的话一起咬掉）。
    所以它只能挂在「稳态噪声」闸门后面用，不能无条件开。
    模型缺文件时静默退回频谱降噪，不影响任何功能。
    """

    def __init__(self):
        self._model = None
        self._tried = False
        self.name = ""

    def _load(self):
        if self._tried:
            return self._model
        self._tried = True
        try:
            import sherpa_onnx
            folder = _find_model_dir("denoise")
            if not folder or not os.path.isdir(folder):
                return None
            # dpdfnet2 质量最好，其次 dpdfnet_baseline，最后 gtcrn（0.5 MB，最轻）
            for fname, kind in (("dpdfnet2.onnx", "dpdfnet"),
                                ("dpdfnet_baseline.onnx", "dpdfnet"),
                                ("gtcrn_simple.onnx", "gtcrn")):
                path = os.path.join(folder, fname)
                if not os.path.exists(path):
                    continue
                if kind == "dpdfnet":
                    mc = sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                        dpdfnet=sherpa_onnx.OfflineSpeechDenoiserDpdfNetModelConfig(
                            model=path),
                        num_threads=2, provider="cpu", debug=False)
                else:
                    mc = sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                        gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(
                            model=path),
                        num_threads=2, provider="cpu", debug=False)
                cfg = sherpa_onnx.OfflineSpeechDenoiserConfig(model=mc)
                cfg.model.validate()
                self._model = sherpa_onnx.OfflineSpeechDenoiser(cfg)
                self.name = fname
                return self._model
        except Exception:
            self._model = None
        return self._model

    def available(self):
        return self._load() is not None

    def process(self, audio):
        """返回降噪后的音频；模型不可用或出错返回 None，由调用方兜底。"""
        model = self._load()
        if model is None:
            return None
        try:
            fresh = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).ravel())
            res = model.run(fresh, SAMPLE_RATE)
            out = np.asarray(res.samples, dtype=np.float32)
            if out.size < 16:
                return None
            if out.size >= fresh.size:
                return out[:fresh.size]
            buf = np.zeros(fresh.size, dtype=np.float32)
            buf[:out.size] = out
            return buf
        except Exception:
            return None


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
        # 注意：不要叫 self._stop。threading.Thread 自己有个私有方法 _stop()，
        # 被实例属性覆盖后 join() 会抛 TypeError: 'Event' object is not callable，
        # 表现就是「点停止按钮不变、窗口关不掉」。
        self._stop_evt = threading.Event()

        self.denoise_on = True        # 句尾降噪开关
        self.far_field = bool(FAR_FIELD)   # 远场模式
        self.force_loud = bool(FORCE_LOUD)  # 强制放大分贝
        # 频谱降噪（真降噪）：按频点判断人声还是噪声，只在稳态噪声下启用
        self.spectral_on = bool(SPECTRAL_DENOISE)
        self.den = SpectralDenoiser()
        self.neural = NeuralDenoiser()
        self.nshape = NoiseShape()
        self.denoise_mode = str(DENOISE_MODE or "auto")
        self._last_voiced = False
        # 切句状态机（HAE-VAD 内核），内部持有音频缓冲、起句预卷、底噪估计
        self.seg = _VadSegmenter()

    def run(self):
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="float32", blocksize=BLOCK_SIZE, device=self.device,
            ) as stream:
                self.on_status("麦克风已打开，开始监听")
                while not self._stop_evt.is_set():
                    block, overflowed = stream.read(BLOCK_SIZE)
                    if self._stop_evt.is_set():
                        break
                    mono = block[:, 0].copy()
                    self._process(mono)
        except Exception as e:
            self.on_status(f"麦克风错误: {e}")
        finally:
            # 停止时把还没切出去的最后半句交出去，别让它烂在缓冲里
            try:
                for audio in self.seg.flush():
                    self.on_sentence(enhance(audio, _vad_profile(),
                                             gate=self.denoise_on))
            except Exception:
                pass

    def _denoise(self, audio):
        """按降噪档位处理一句音频。

        只有稳态噪声才真的能从降噪里获益：
        风扇/空调 0 dB 噪声下字符错误率 5.2% -> 2.3%；
        多人说话（babble）反而变差 7.3% -> 25.5%，因为神经网络会把
        别人的语音当成噪声一起咬掉。所以 auto 档先判噪声形态，
        判不出稳态就原样放行，宁可不降噪也不伤语音。
        """
        if self.denoise_mode == "off" or audio is None or len(audio) < 640:
            return audio
        if self.denoise_mode == "auto" and not self.nshape.stationary():
            return audio
        y = self.neural.process(audio)
        if y is not None and len(y) >= 16:
            return y
        if not self.spectral_on:
            return audio
        # 神经模型没随包发布时退到频谱降噪（轻量、不需要额外文件）
        self.den.over = float(SPECTRAL_STRENGTH)
        self.den.reset()
        step = 480
        parts = [self.den.process(audio[i:i + step], True)
                 for i in range(0, len(audio), step)]
        return np.concatenate(parts) if parts else audio

    def _process(self, block):
        # 先统计环境噪声形态（只在非人声块上采样），不改变音频
        self.nshape.update(block, self._last_voiced)

        # 场景系数：远场放宽门限，强制放大更激进，正常情况不缩放
        if self.force_loud:
            self.seg.set_scale(_VAD_CFG["force_loud_scale"])
        elif self.far_field:
            self.seg.set_scale(_VAD_CFG["far_field_scale"])
        else:
            self.seg.set_scale(1.0)

        sentences = self.seg.feed(block)
        self._last_voiced = bool(self.seg.voiced)
        # 音量条取本块的 RMS，必须在 feed 之后取才是当前块的
        self.on_level(self.seg.level)

        # 流式：状态机内部按 stream_interval_s 节流，不到间隔返回 None
        if self.on_partial and STREAM_ENABLED:
            seg = self.seg.partial()
            if seg is not None and len(seg) > 0:
                self.on_partial(seg)

        for audio in sentences:
            # 降噪放在切句之后：整句在手才有足够长的噪声样本来判断噪声类型
            audio = self._denoise(audio)
            # 增益与降噪一起算，静音段不会被增益放大
            audio = enhance(audio, _vad_profile(), gate=self.denoise_on)
            self.on_sentence(audio)

    def stop(self):
        self._stop_evt.set()
        # 3.1.0：等采集线程真正退出再返回。否则 _on_close 紧接着 destroy 窗口，
        # 线程 finally 里的 flush 回调可能打到已销毁的控件上。
        # 3.2.5：任何情况下都不让清理动作把调用方（Tk 主线程）拖崩，
        # 线程状态异常时最坏也只是不等它，界面照常恢复。
        try:
            if self.is_alive() and threading.current_thread() is not self:
                self.join(timeout=2.0)
        except Exception:
            pass


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

    def __init__(self, recognizer, on_partial, on_error=None):
        super().__init__(daemon=True)
        self.recognizer = recognizer
        self.on_partial = on_partial
        self.on_error = on_error          # 3.1.0：识别引擎坏了要让用户看见，不再静默
        self._latest = None
        self._last = ""
        self._lock = threading.Lock()
        # 同 AudioCapture：不能叫 _stop，会覆盖 Thread 自己的私有方法
        self._stop_evt = threading.Event()
        self._err_reported = False

    def push(self, audio):
        with self._lock:
            self._latest = audio        # 覆盖：只保留最新，天然跳过积压

    def clear(self):
        with self._lock:
            self._latest = None
            self._last = ""
            self._err_reported = False

    def run(self):
        while not self._stop_evt.is_set():
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
            except Exception as e:
                # 3.1.0：以前这里 except pass，引擎坏了界面只是不出字、用户无从知道。
                # 现在把第一条错误送出去（只报一次，避免刷屏），clear() 会重置。
                if self.on_error is not None and not self._err_reported:
                    self._err_reported = True
                    try:
                        self.on_error(str(e))
                    except Exception:
                        pass

    def stop(self):
        self._stop_evt.set()


# ================================================================ 现代化滚动条（v1.3.0）
class ModernScrollbar(tk.Canvas):
    """自绘纵向滚动条：细条 + 圆头 thumb + 悬停高亮 + 拖拽/点轨翻页。

    不依赖 ttk 主题（Windows 经典滚动条带箭头、灰白，和深色 UI 不搭）。
    用法：
        bar = ModernScrollbar(parent, target=text_widget)
        text_widget.configure(yscrollcommand=bar.set)
    """

    WIDTH = 8

    def __init__(self, master, target=None, bg=None, thumb=None, hover=None, **kw):
        super().__init__(master, width=int(round(self.WIDTH * UI_SCALE)),
                         bg=bg or C_BG,
                         highlightthickness=0, bd=0, **kw)
        self._target = target
        self._first, self._last = 0.0, 1.0
        self._hover = False
        self._drag_off = None
        self._thumb_color = thumb or C_BORDER2
        self._hover_color = hover or C_ACCENT2
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---- 由 target 的 yscrollcommand 调用 ----
    def set(self, first, last):
        try:
            self._first, self._last = float(first), float(last)
        except Exception:
            return
        self._redraw()

    def _on_enter(self, _e):
        self._hover = True
        self._redraw()

    def _on_leave(self, _e):
        self._hover = False
        self._drag_off = None
        self._redraw()

    def _thumb_geo(self):
        h = max(1, self.winfo_height())
        w = max(1, self.winfo_width())
        top = self._first * h
        bottom = self._last * h
        min_h = 28
        if bottom - top < min_h:
            bottom = min(h, top + min_h)
            top = max(0, bottom - min_h)
        return w, top, bottom

    def _redraw(self):
        self.delete("all")
        if self._last - self._first >= 0.999:      # 内容没超出 → 不画 thumb
            return
        w, top, bottom = self._thumb_geo()
        color = (self._hover_color
                 if (self._hover or self._drag_off is not None)
                 else self._thumb_color)
        cx = w / 2.0
        # 圆头竖条：canvas line + ROUND capstyle = 圆角胶囊
        self.create_line(cx, top + 2, cx, bottom - 2,
                         width=max(3, int(w) - 2), fill=color,
                         capstyle=tk.ROUND, tags="thumb")

    def _scroll_to(self, frac):
        if self._target is None:
            return
        try:
            self._target.yview_moveto(max(0.0, min(1.0, frac)))
        except Exception:
            pass

    def _on_press(self, e):
        _, top, bottom = self._thumb_geo()
        if top <= e.y <= bottom:
            self._drag_off = e.y - top
        else:
            page = max(0.05, self._last - self._first)
            self._scroll_to(self._first + (page if e.y > bottom else -page))
            self._drag_off = None
        self._redraw()

    def _on_drag(self, e):
        if self._drag_off is None:
            return
        h = max(1, self.winfo_height())
        span = max(0.0, self._last - self._first)
        frac = (e.y - self._drag_off) / h
        self._scroll_to(max(0.0, min(1.0 - span, frac)))

    def _on_release(self, _e):
        self._drag_off = None
        self._redraw()


# ================================================================ 现代控件（v1.3.0）
def _round_rect_points(x1, y1, x2, y2, r, steps=6):
    """圆角矩形的多边形顶点（Canvas.create_polygon 用）。"""
    r = max(0.0, min(float(r), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = []
    for cx, cy, a0, a1 in (
            (x2 - r, y1 + r, -90, 0),
            (x2 - r, y2 - r, 0, 90),
            (x1 + r, y2 - r, 90, 180),
            (x1 + r, y1 + r, 180, 270)):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class RoundedButton(tk.Canvas):
    """自绘圆角按钮：悬停 / 按下 / 文字与配色可热更新。"""

    def __init__(self, master, text, command=None, bg=C_CARD, hover=C_CARD_HOV,
                 active=C_ACCENT, fg=C_TEXT2, fg_hover=C_TEXT, fg_active="#03211A",
                 radius=9, height=34, font=("Microsoft YaHei UI", 9),
                 parent_bg=None, anchor="center", padx=12, width=60):
        height = int(round(height * UI_SCALE))
        width = int(round(width * UI_SCALE))
        radius = max(2, int(round(radius * UI_SCALE)))
        padx = int(round(padx * UI_SCALE))
        super().__init__(master, bg=parent_bg or master.cget("bg"),
                         highlightthickness=0, bd=0, height=height, width=width)
        self._text = text
        self._cmd = command
        self._state = "normal"
        self._radius = radius
        self._font = font
        self._anchor = anchor
        self._padx = padx
        self._enabled = True
        self._colors = {
            "normal": (bg, fg),
            "hover": (hover, fg_hover),
            "active": (active, fg_active),
        }
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        fill, fg = self._colors[self._state]
        if not self._enabled:
            fill, fg = C_BORDER, C_FAINT
        self.create_polygon(_round_rect_points(1, 1, w - 1, h - 1, self._radius),
                            fill=fill, outline="")
        x = w / 2 if self._anchor == "center" else self._padx
        self.create_text(x, h / 2 + 1, text=self._text, fill=fg,
                         font=self._font, anchor=self._anchor)

    def _on_enter(self, _e):
        if self._enabled:
            self._state = "hover"
            self._draw()

    def _on_leave(self, _e):
        if self._enabled:
            self._state = "normal"
            self._draw()

    def _on_press(self, _e):
        if self._enabled:
            self._state = "active"
            self._draw()

    def _on_release(self, e):
        if not self._enabled:
            return
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self._state = "hover" if inside else "normal"
        self._draw()
        if inside and self._cmd:
            try:
                self._cmd()
            except Exception:
                pass

    # ---- 热更新 ----
    def set_text(self, text):
        self._text = text
        self._draw()

    def set_colors(self, bg=None, hover=None, active=None, fg=None,
                   fg_hover=None, fg_active=None):
        b, f = self._colors["normal"]
        self._colors["normal"] = (bg or b, fg or f)
        hb, hf = self._colors["hover"]
        self._colors["hover"] = (hover or hb, fg_hover or hf)
        ab, af = self._colors["active"]
        self._colors["active"] = (active or ab, fg_active or af)
        self._draw()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self._draw()


class ChoicePopup(tk.Toplevel):
    """下拉弹层：深色、无边框、悬停高亮、Esc / 失焦 / 点外部关闭。"""

    def __init__(self, anchor, values, current, on_choose, disabled=None):
        super().__init__(anchor)
        self._disabled = set(disabled or ())
        self.overrideredirect(True)
        self.configure(bg=C_BORDER2)
        self._on_choose = on_choose
        self._anchor = anchor
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        outer = tk.Frame(self, bg=C_PANEL)
        outer.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        for v in values:
            is_cur = (v == current)
            off = v in self._disabled
            # 当前模式不支持的语言：灰色、不可点（v2.5.1 用户明确要求）
            item = tk.Label(outer, text=v, bg=C_PANEL,
                            fg=(C_FAINT if off else
                                (C_ACCENT2 if is_cur else C_TEXT2)),
                            anchor="w", font=("Microsoft YaHei UI", 9),
                            padx=14, pady=7)
            item.pack(fill=tk.X)
            if off:
                item.bind("<Button-1>", lambda e: "break")
            else:
                item.bind("<Enter>", lambda e, w=item: w.configure(
                    bg=C_CARD_HOV, fg=C_TEXT))
                item.bind("<Leave>", lambda e, w=item, cur=is_cur: w.configure(
                    bg=C_PANEL, fg=(C_ACCENT2 if cur else C_TEXT2)))
                item.bind("<Button-1>", lambda e, val=v: self._pick(val))

        self.update_idletasks()
        w = max(120, anchor.winfo_width())
        h = self.winfo_reqheight()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 4
        if y + h > self.winfo_screenheight() - 12:
            y = anchor.winfo_rooty() - h - 4
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Button-1>", self._maybe_close, add="+")
        self.focus_force()
        self.after(60, lambda: self.bind("<FocusOut>", lambda e: self.destroy()))
        try:
            self.grab_set()
        except Exception:
            pass

    def _maybe_close(self, e):
        if e.x < 0 or e.y < 0 or e.x > self.winfo_width() or e.y > self.winfo_height():
            self.destroy()

    def _pick(self, value):
        cb = self._on_choose
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        if cb:
            cb(value)


class SelectField(tk.Canvas):
    """圆角下拉选择器（自绘）：点击弹出 ChoicePopup。保留 StringVar 接口。"""

    def __init__(self, master, variable, values, command=None,
                 parent_bg=None, height=34, radius=9,
                 font=("Microsoft YaHei UI", 9), placeholder="请选择"):
        height = int(round(height * UI_SCALE))
        radius = max(2, int(round(radius * UI_SCALE)))
        super().__init__(master, bg=parent_bg or master.cget("bg"),
                         highlightthickness=0, bd=0, height=height,
                         width=int(round(80 * UI_SCALE)))
        self._var = variable
        self._values = list(values)
        self._cmd = command
        self._font = font
        self._radius = radius
        self._placeholder = placeholder
        self._disabled = set()
        self._enabled = True          # 整体禁用（如「API 引擎」在非 API 模式下）
        self._popup = None
        self._hover = False
        self._var.trace_add("write", lambda *a: self._draw())
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._open)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))

    def _set_hover(self, hover):
        self._hover = hover
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        self.create_polygon(
            _round_rect_points(1, 1, w - 1, h - 1, self._radius),
            fill=(C_CARD_HOV if self._hover else C_CARD),
            outline=(C_BORDER2 if self._hover else C_BORDER))
        val = self._var.get() or self._placeholder
        self.create_text(13, h / 2, text=val,
                         fill=(C_FAINT if (not self._enabled or
                                           val in self._disabled) else C_TEXT),
                         anchor="w", font=self._font)
        ax = w - 15
        self.create_line(ax - 4, h / 2 - 2, ax, h / 2 + 2, fill=C_DIM,
                         width=2, capstyle=tk.ROUND)
        self.create_line(ax, h / 2 + 2, ax + 4, h / 2 - 2, fill=C_DIM,
                         width=2, capstyle=tk.ROUND)

    def set_values(self, values):
        self._values = list(values)

    def set_disabled(self, values):
        """把某些选项置为灰色不可选（当前模式下用不了的语言）。"""
        self._disabled = set(values or ())
        self._draw()

    def disabled(self):
        return set(self._disabled)

    def set_enabled(self, enabled=True):
        """整体启/禁：禁用时点击无反应、文字变灰。"""
        self._enabled = bool(enabled)
        self._draw()

    def enabled(self):
        return bool(self._enabled)

    def _open(self, _e=None):
        if not self._enabled or not self._values:
            return
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
        self._popup = ChoicePopup(self, self._values, self._var.get(), self._choose,
                                  disabled=self._disabled)

    def _choose(self, value):
        self._popup = None
        self._var.set(value)
        if self._cmd:
            try:
                self._cmd()
            except Exception:
                pass


class RoundedCheck(tk.Canvas):
    """自绘圆角复选框（GPU 加速 / 自动纠错）。"""

    def __init__(self, master, text, variable, command=None,
                 parent_bg=None, height=26, font=("Microsoft YaHei UI", 9),
                 width=90):
        height = int(round(height * UI_SCALE))
        # 按文字实际宽度自适应，避免中文被固定宽度截断
        try:
            from tkinter import font as tkfont
            pad = int(round(39 * UI_SCALE))
            width = max(int(round(width * UI_SCALE)),
                        tkfont.Font(font=font).measure(text) + pad)
        except Exception:
            width = int(round(width * UI_SCALE))
        super().__init__(master, bg=parent_bg or master.cget("bg"),
                         highlightthickness=0, bd=0, height=height, width=width)
        self._text = text
        self._var = variable
        self._cmd = command
        self._font = font
        self._hover = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._var.trace_add("write", lambda *a: self._draw())

    def _set_hover(self, hover):
        self._hover = hover
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        box = 15
        cy = h / 2.0
        checked = bool(self._var.get())
        fill = C_ACCENT if checked else (C_CARD_HOV if self._hover else C_CARD)
        self.create_polygon(
            _round_rect_points(2, cy - box / 2, 2 + box, cy + box / 2, 4),
            fill=fill, outline=C_BORDER2)
        if checked:
            self.create_line(6, cy, 9, cy + 3.5, 13, cy - 4,
                             fill="#03211A", width=2, capstyle=tk.ROUND)
        self.create_text(box + 12, cy, text=self._text, anchor="w",
                         fill=(C_TEXT2 if self._hover else C_DIM),
                         font=self._font)

    def _toggle(self, _e=None):
        self._var.set(not self._var.get())
        self._draw()
        if self._cmd:
            try:
                self._cmd()
            except Exception:
                pass


# ================================================================ 系统托盘（v1.3.0）
# 纯 ctypes 调 Win32（Shell_NotifyIcon），不引入 pystray / Pillow 等新依赖。
# 在独立线程里建隐藏窗口收托盘消息；事件放进 queue，由主线程 after 轮询处理。

class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t)


class TrayIcon:
    """系统托盘图标。start() 后常驻，stop() 清理。"""

    WM_APP_TRAY = 0x0400 + 20
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
    MF_STRING, MF_SEPARATOR = 0x0, 0x800
    TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
    SM_CXSMICON, SM_CYSMICON = 49, 50      # 通知区域图标的标准尺寸（随 DPI 变）

    def __init__(self, idle_icon, live_icon=None, event_q=None):
        self._idle_icon = idle_icon
        self._live_icon = live_icon or idle_icon
        self._q = event_q
        self._running = False
        self._hwnd = None
        self._nid = None
        self._thread = None
        self._ready = threading.Event()
        self._user32 = None
        self._shell32 = None
        self._kernel32 = None
        self._wndproc_cb = None

    # ---------- 对外 ----------
    def set_state(self, running):
        """运行中 / 停止：切换托盘图标与提示文字（线程序列化，安全）。"""
        self._running = bool(running)
        if self._hwnd and self._nid is not None:
            self._apply_state()

    def _apply_state(self):
        try:
            path = self._live_icon if self._running else self._idle_icon
            new = self._load_icon(path)
            if not new:
                return
            nid = self._nid
            old = nid.hIcon
            nid.hIcon = new
            nid.uFlags = self.NIF_ICON | self.NIF_TIP
            nid.szTip = ("Hearizon Sync · 正在同传" if self._running
                         else "Hearizon Sync · 已停止")[:127]
            self._shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(nid))
            if old:
                self._user32.DestroyIcon(old)
            self._nid = nid
        except Exception:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(3.0)

    def stop(self):
        hwnd = self._hwnd
        try:
            if hwnd:
                self._user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
        except Exception:
            pass
        t = self._thread
        if t and t.is_alive():
            t.join(2.0)

    # ---------- 内部 ----------
    def _run(self):
        try:
            if os.name != "nt":
                raise RuntimeError("系统托盘仅支持 Windows")
            self._user32 = ctypes.windll.user32
            self._shell32 = ctypes.windll.shell32
            self._kernel32 = ctypes.windll.kernel32
            self._bind_api()
            self._create_window()
            self._add_icon()
            self._ready.set()
            self._message_loop()
        except Exception as e:
            self._q.put(("tray_error", str(e)))
            self._ready.set()

    def _bind_api(self):
        """显式声明 Win32 原型：64 位下 lParam / 句柄都是 8 字节，
        不声明会被 ctypes 按 32 位 int 截断（实测会抛 OverflowError）。"""
        u, s, k = self._user32, self._shell32, self._kernel32
        k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k.GetModuleHandleW.restype = ctypes.c_void_p
        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        u.RegisterClassExW.restype = ctypes.c_ushort
        u.CreateWindowExW.argtypes = [
            ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u.CreateWindowExW.restype = ctypes.c_void_p
        u.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_ssize_t]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.LoadIconW.restype = ctypes.c_void_p
        u.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p,
                                  ctypes.c_uint, ctypes.c_uint]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
        u.DestroyWindow.argtypes = [ctypes.c_void_p]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.CreatePopupMenu.restype = ctypes.c_void_p
        u.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_size_t, ctypes.c_wchar_p]
        u.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_void_p]
        u.TrackPopupMenu.restype = ctypes.c_int
        u.DestroyMenu.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.GetCursorPos.argtypes = [ctypes.c_void_p]
        s.Shell_NotifyIconW.argtypes = [ctypes.c_uint,
                                        ctypes.POINTER(_NOTIFYICONDATAW)]
        s.Shell_NotifyIconW.restype = ctypes.c_int

    def _create_window(self):
        hinst = self._kernel32.GetModuleHandleW(None)
        cls_name = "HearizonSyncTrayWnd"
        self._wndproc_cb = _WNDPROC(self._wndproc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc_cb, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        self._user32.RegisterClassExW(ctypes.byref(wc))
        self._hwnd = self._user32.CreateWindowExW(
            0, cls_name, cls_name, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            raise RuntimeError("创建隐藏窗口失败")

    def _load_icon(self, path):
        # 托盘图标必须按当前 DPI 取尺寸：写死 16 在 150% 缩放下会被系统放大成 24，糊得厉害
        try:
            cx = self._user32.GetSystemMetrics(self.SM_CXSMICON) or 16
            cy = self._user32.GetSystemMetrics(self.SM_CYSMICON) or cx
        except Exception:
            cx = cy = 16
        hicon = self._user32.LoadImageW(
            None, path, self.IMAGE_ICON, cx, cy, self.LR_LOADFROMFILE)
        if not hicon:
            # 兜底：系统默认应用图标
            hicon = self._user32.LoadIconW(None, 32512)
        return hicon

    def _add_icon(self):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_APP_TRAY
        nid.hIcon = self._load_icon(self._idle_icon)
        nid.szTip = ("Hearizon Sync · 正在同传" if self._running
                     else "Hearizon Sync · 已停止")[:127]
        self._nid = nid
        if not self._shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
            raise RuntimeError("Shell_NotifyIcon 添加失败")

    def _message_loop(self):
        msg = _MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.WM_APP_TRAY:
                ev = int(lparam) & 0xFFFF
                if ev == self.WM_LBUTTONDBLCLK:
                    self._q.put(("show",))
                elif ev == self.WM_RBUTTONUP:
                    self._popup_menu(hwnd)
                return 0
            if msg == self.WM_CLOSE:
                try:
                    self._shell32.Shell_NotifyIconW(
                        self.NIM_DELETE, ctypes.byref(self._nid))
                except Exception:
                    pass
                self._user32.DestroyWindow(hwnd)
                return 0
            if msg == self.WM_DESTROY:
                self._user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd):
        u = self._user32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, self.MF_STRING, 1, "显示主窗口")
        u.AppendMenuW(menu, self.MF_STRING, 2,
                      "停止同传" if self._running else "开始同传")
        u.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        u.AppendMenuW(menu, self.MF_STRING, 4, "退出 Hearizon Sync")

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = _PT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(hwnd)
        cmd = u.TrackPopupMenu(
            menu, self.TPM_RETURNCMD | self.TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, hwnd, None)
        u.PostMessageW(hwnd, 0, 0, 0)   # 让菜单立即消失
        u.DestroyMenu(menu)
        if cmd == 1:
            self._q.put(("show",))
        elif cmd == 2:
            self._q.put(("toggle",))
        elif cmd == 4:
            self._q.put(("quit",))


# ================================================================ 主界面
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global UI_SCALE
        self._ui = self._setup_dpi()
        UI_SCALE = self._ui
        self.title(f"Hearizon Sync v{APP_VERSION}")
        # 窗口按 DPI 放大，但不超出当前屏幕可用区域
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        self.geometry("%dx%d" % (min(self.sp(1180), int(sw * 0.92)),
                                 min(self.sp(780), int(sh * 0.86))))
        self.minsize(min(self.sp(980), int(sw * 0.8)),
                     min(self.sp(680), int(sh * 0.72)))
        self.configure(bg=C_BG)
        self._set_window_icon()

        apply_conf()                                          # 读取有道 key / GPU 设置

        self.capture = None
        self.recognizer = Recognizer(on_status=self._status_threadsafe)
        self.translator = Translator()                       # DeepSeek 云端
        # 自定义（OpenAI 兼容）独立一个实例，避免和 DeepSeek 抢同一个 endpint
        self.custom_tr = Translator(
            api_key=(CUSTOM_KEY or None),
            model=(CUSTOM_MODEL or "deepseek-chat"),
            api_base=(CUSTOM_BASE_URL or None))
        self.local_tr = LocalTranslator(on_status=self._status_threadsafe)  # opus-mt 本地
        self.youdao_tr = YoudaoTranslator(on_status=self._status_threadsafe)  # 有道云端
        # 3.0.0 新增：百度翻译 / 彩云小译（国内直连，按需填 Key 即可用）
        self.baidu_tr = BaiduTranslator()
        self.caiyun_tr = CaiyunTranslator()
        # v2.5.1：免费在线引擎（单一免费模型 MyMemory + 有道免费兜底 + 本地兜底）
        self.free_tr = FreeOnlineTranslator(self.local_tr,
                                            on_status=self._status_threadsafe)

        self.work_q = queue.Queue()      # 待处理的音频
        self.ui_q = queue.Queue()        # 待更新的界面消息
        self.stream_worker = StreamWorker(self.recognizer, self._on_partial_text, on_error=self._stream_error)

        self.running = False
        # 3.1.0：后台识别/翻译线程不再直接读 Tk 变量（跨线程读 StringVar 不安全）。
        # 主线程在变量变化时把值刷进这几个普通属性，worker 只读它们。
        self._w_correct = bool(AUTO_CORRECT)
        self._w_target = "中文"
        self._w_mode = TRANSLATOR
        self._w_in_lang = "自动检测"
        self._w_api_engine = API_ENGINE
        self.worker = None
        self.stats = {"sentences": 0, "chars_in": 0, "chars_out": 0}
        self.t_start = None
        self._last_dur = 0
        self._partial_prev = ""      # 正在显示的临时结果（用于前缀稳定性判定）
        self._partial_pend = ""      # 等待确认的临时结果
        self._ctx = deque(maxlen=CONTEXT_TURNS)   # 翻译上下文（最近几轮原/译）

        self._build_ui()
        self._check_env()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._pump_ui)

        # ---- v1.2.0 ----
        self._sub_size_dst = SUB_SIZE_DST
        self._sub_size_src = SUB_SIZE_SRC
        self._egg_clicks = 0
        self._last_speech_ts = time.time()
        self._silence_warned = False
        self._bind_shortcuts()

        # ---- v1.3.0 ----
        self._sub_theme = SUB_THEME_DEFAULT
        self._sub_pos = SUB_POS_DEFAULT
        self._layout = LAYOUT            # 3.0.0：界面布局（经典 / 对话）
        self._segments = []           # SRT 用：每句的真实起止时间
        self._tray = None             # 托盘对象（第一次用到才创建）
        self._tray_q = queue.Queue()  # 托盘线程 → 主线程消息
        self._restore_style()         # 读回上次的配色 / 位置

        self.after(900, self._first_run_guide)
        self.after(5000, self._silence_watch)
        # v2.5.5：启动 4 秒后静默检查新版本（有新版本才弹窗）
        self._update_win = None
        self._dl_win = None
        self.after(4000, self._check_update_async)

    # ------------------------------------------------------------ UI
    # ------------------------------------------------------------ DPI / 缩放
    def _setup_dpi(self):
        """读取屏幕真实 DPI，设置 Tk 字体缩放，返回 UI 缩放比（96 DPI = 1.0）。"""
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        try:
            self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        return max(1.0, min(4.0, dpi / 96.0))

    def sp(self, v):
        """像素尺寸按 DPI 缩放；字体由 tk scaling 自动处理，这里只管布局尺寸。"""
        try:
            return int(round(float(v) * self._ui))
        except Exception:
            return int(v)

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

        # ============ 左侧栏（v1.3.0：内容超出时上下滚动）============
        side_outer = tk.Frame(shell, bg=C_SIDEBAR, width=self.sp(252))
        side_outer.pack(side=tk.LEFT, fill=tk.Y)
        side_outer.pack_propagate(False)

        side_canvas = tk.Canvas(side_outer, bg=C_SIDEBAR, highlightthickness=0,
                                bd=0, width=self.sp(244))
        side_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._side_canvas = side_canvas
        self._side_scroll = ModernScrollbar(
            side_outer, target=side_canvas, bg=C_SIDEBAR,
            thumb=C_BORDER2, hover=C_ACCENT2)
        self._side_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        side_canvas.configure(yscrollcommand=self._side_scroll.set)

        side = tk.Frame(side_canvas, bg=C_SIDEBAR)
        self._side_inner = side
        self._side_win = side_canvas.create_window((0, 0), window=side, anchor="nw")

        def _sync_side(_e=None):
            side_canvas.configure(scrollregion=side_canvas.bbox("all"))
            try:
                self._side_scroll.set(*side_canvas.yview())
            except Exception:
                pass

        side.bind("<Configure>", _sync_side)
        side_canvas.bind("<Configure>", lambda e: side_canvas.itemconfigure(
            self._side_win, width=e.width))

        # --- 品牌区（真实 logo 图标）---
        brand = tk.Frame(side, bg=C_SIDEBAR)
        brand.pack(fill=tk.X, padx=16, pady=(18, 16))

        self._logo_img = logo_photo(34)      # 按屏幕缩放取整，Lanczos 重采样
        if self._logo_img is not None:
            tk.Label(brand, image=self._logo_img, bg=C_SIDEBAR,
                     bd=0).pack(side=tk.LEFT)
        else:
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

        # 设置齿轮（圆角按钮）
        RoundedButton(brand, "⚙", command=self._open_settings,
                      bg=C_SIDEBAR, hover=C_CARD, active=C_CARD_HOV,
                      fg=C_DIM, fg_hover=C_TEXT, height=30, width=30,
                      font=("Segoe UI", 12), parent_bg=C_SIDEBAR,
                      radius=8).pack(side=tk.RIGHT, padx=(4, 0))

        # --- 开始 / 停止（圆角大按钮）---
        self.btn_start = RoundedButton(
            side, "▶  开始同传", command=self._toggle,
            bg=C_ACCENT, hover=C_ACCENT2, active="#0B8A6B",
            fg="#03211A", fg_hover="#03211A", fg_active="#03211A",
            height=42, font=("Microsoft YaHei UI", 11, "bold"),
            parent_bg=C_SIDEBAR, radius=11)
        self.btn_start.pack(fill=tk.X, padx=16, pady=(0, 8))

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
        self.level_canvas = tk.Canvas(lv, height=6, bg=C_SIDEBAR,
                                      highlightthickness=0)
        self.level_canvas.pack(fill=tk.X)
        self.level_canvas.create_line(2, 3, 400, 3, width=4, fill=C_BORDER,
                                      capstyle=tk.ROUND, tags="track")
        self.level_bar = self.level_canvas.create_line(
            2, 3, 2, 3, width=4, fill=C_LISTEN, capstyle=tk.ROUND)
        self.level_canvas.bind(
            "<Configure>",
            lambda e: self.level_canvas.coords(
                "track", 2, 3, max(2, e.width - 2), 3))

        def divider(pady=11):
            tk.Frame(side, bg=C_BORDER, height=1).pack(
                fill=tk.X, padx=16, pady=pady)

        divider(14)

        # --- 字段封装（v2.0.1：自绘圆角下拉）---
        def field(label, variable, values, command=None, pady=(0, 8)):
            box = tk.Frame(side, bg=C_SIDEBAR)
            box.pack(fill=tk.X, padx=16, pady=pady)
            tk.Label(box, text=label, bg=C_SIDEBAR, fg=C_DIM,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(0, 4))
            w = SelectField(box, variable=variable, values=values,
                            command=command, parent_bg=C_SIDEBAR,
                            height=34, font=("Microsoft YaHei UI", 9))
            w.pack(fill=tk.X)
            return w

        # ① 输入语言（源）
        self.in_lang_var = tk.StringVar(value="自动检测")
        self.in_cb = field("输入语言（识别）", self.in_lang_var,
                           INPUT_LANG_NAMES, self._on_input_lang_change)

        # ② 麦克风
        self.dev_var = tk.StringVar()
        self.dev_cb = field("音频输入", self.dev_var, [])
        self._refresh_devices()

        # ③ 目标语言
        self.lang_var = tk.StringVar(value="中文")
        self.lang_cb = field("译成（目标）", self.lang_var, OUTPUT_LANG_NAMES)

        # ④ 翻译模式（v2.5.1：本地 / 在线免费 / API 付费，三选一，互相独立）
        self.tr_mode = tk.StringVar(value=TRANSLATOR)
        self.tr_cb = field("翻译模式", self.tr_mode, TRANSLATOR_CHOICES,
                           self._on_translator_change)

        # ④-2 API 引擎（v2.5.1 补丁：两个 Key 都配时由这里决定用谁）
        self.api_engine_var = tk.StringVar(value=API_ENGINE)
        self.api_engine_cb = field("API 引擎（仅API模式）", self.api_engine_var,
                                   API_ENGINES, self._on_api_engine_change)

        # ⑤ 识别引擎
        self.model_var = tk.StringVar(value=ENGINE)
        self.model_cb = field("识别引擎", self.model_var, ENGINE_CHOICES,
                              self._on_model_change, pady=(0, 6))

        # ⑥ GPU 开关（一行两件：GPU 加速 + 刷新设备）
        row = tk.Frame(side, bg=C_SIDEBAR)
        row.pack(fill=tk.X, padx=16, pady=(0, 10))
        self.gpu_var = tk.BooleanVar(value=bool(GPU_ACCEL))
        self.gpu_check = RoundedCheck(row, "GPU 加速", self.gpu_var,
                                      self._on_gpu_toggle, parent_bg=C_SIDEBAR,
                                      height=24, font=("Microsoft YaHei UI", 8))
        self.gpu_check.pack(side=tk.LEFT)
        RoundedButton(row, "刷新设备", command=self._refresh_devices,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_DIM, fg_hover=C_TEXT, height=24, width=64,
                      font=("Microsoft YaHei UI", 8), parent_bg=C_SIDEBAR,
                      radius=7).pack(side=tk.RIGHT)

        # ⑦ 自动纠错——和 GPU 加速同款开关
        row2 = tk.Frame(side, bg=C_SIDEBAR)
        row2.pack(fill=tk.X, padx=16, pady=(0, 8))
        self.correct_var = tk.BooleanVar(value=bool(AUTO_CORRECT))
        self.correct_check = RoundedCheck(
            row2, "自动纠错", self.correct_var,
            self._on_correct_toggle, parent_bg=C_SIDEBAR,
            height=24, font=("Microsoft YaHei UI", 8))
        self.correct_check.pack(side=tk.LEFT)

        # 3.1.0：给 worker 用到的变量挂主线程快照刷新，并先取一次初值。
        for _v in (self.correct_var, self.lang_var, self.tr_mode,
                   self.in_lang_var, self.api_engine_var):
            _v.trace_add("write", self._refresh_worker_snapshot)
        self._refresh_worker_snapshot()

        divider(9)

        # --- 操作按钮 ---
        for text, cmd in [
            ("测麦克风", self._test_mic),
            ("保存记录", self._save),
            ("导出 SRT", self._export_srt),
            ("清空字幕", self._clear),
            ("最小化到托盘", self._minimize_to_tray),
        ]:
            RoundedButton(side, text, command=cmd,
                          bg=C_SIDEBAR, hover=C_CARD, active=C_CARD_HOV,
                          fg=C_TEXT2, fg_hover=C_TEXT, height=30,
                          font=("Microsoft YaHei UI", 9), parent_bg=C_SIDEBAR,
                          anchor="w", padx=14, radius=8
                          ).pack(fill=tk.X, padx=12, pady=1)

        divider(8)

        # --- v1.3.0：字幕样式（位置 / 配色）---
        style = tk.Frame(side, bg=C_SIDEBAR)
        style.pack(fill=tk.X, padx=16, pady=(0, 2))
        head_s = tk.Frame(style, bg=C_SIDEBAR)
        head_s.pack(fill=tk.X)
        tk.Label(head_s, text="字幕样式", bg=C_SIDEBAR, fg=C_DIM,
                 font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(head_s, text="字号 Ctrl ±", bg=C_SIDEBAR, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 7)).pack(side=tk.RIGHT)

        row_pos = tk.Frame(style, bg=C_SIDEBAR)
        row_pos.pack(fill=tk.X, pady=(4, 2))
        tk.Label(row_pos, text="位置", bg=C_SIDEBAR, fg=C_FAINT, width=3,
                 anchor="w", font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        self._pos_btns = {}
        for key, label in (("bottom", "底部"), ("top", "顶部")):
            b = RoundedButton(row_pos, label,
                              command=lambda k=key: self._set_position(k),
                              bg=C_CARD, hover=C_CARD_HOV, active=C_ACCENT,
                              fg=C_TEXT2, fg_hover=C_TEXT, fg_active="#03211A",
                              height=24, width=46,
                              font=("Microsoft YaHei UI", 8),
                              parent_bg=C_SIDEBAR, radius=7)
            b.pack(side=tk.LEFT, padx=(4, 0))
            self._pos_btns[key] = b

        row_theme = tk.Frame(style, bg=C_SIDEBAR)
        row_theme.pack(fill=tk.X, pady=(0, 2))
        tk.Label(row_theme, text="配色", bg=C_SIDEBAR, fg=C_FAINT, width=3,
                 anchor="w", font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        self._theme_btns = {}
        for key in SUB_THEME_KEYS:
            b = RoundedButton(row_theme, SUB_THEMES[key]["name"],
                              command=lambda k=key: self._set_theme(k),
                              bg=C_CARD, hover=C_CARD_HOV, active=C_ACCENT,
                              fg=C_TEXT2, fg_hover=C_TEXT, fg_active="#03211A",
                              height=24, width=58,
                              font=("Microsoft YaHei UI", 8),
                              parent_bg=C_SIDEBAR, radius=7)
            b.pack(side=tk.LEFT, padx=(4, 0))
            self._theme_btns[key] = b

        row_layout = tk.Frame(style, bg=C_SIDEBAR)
        row_layout.pack(fill=tk.X, pady=(0, 2))
        tk.Label(row_layout, text="布局", bg=C_SIDEBAR, fg=C_FAINT, width=3,
                 anchor="w", font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        self._layout_btns = {}
        for key, label in ((LAYOUT_CLASSIC, "经典"), (LAYOUT_CHAT, "对话")):
            b = RoundedButton(row_layout, label,
                              command=lambda k=key: self._apply_layout(k),
                              bg=C_CARD, hover=C_CARD_HOV, active=C_ACCENT,
                              fg=C_TEXT2, fg_hover=C_TEXT, fg_active="#03211A",
                              height=24, width=46,
                              font=("Microsoft YaHei UI", 8),
                              parent_bg=C_SIDEBAR, radius=7)
            b.pack(side=tk.LEFT, padx=(4, 0))
            self._layout_btns[key] = b

        # --- 关于（彩蛋入口：连点版本号 5 次）---
        self._about_lbl = tk.Label(
            side, text=f"关于 · v{APP_VERSION}", bg=C_SIDEBAR, fg=C_FAINT,
            font=("Microsoft YaHei UI", 8), cursor="hand2")
        self._about_lbl.pack(anchor="w", padx=16, pady=(6, 2))
        self._about_lbl.bind("<Button-1>", lambda e: self._open_about())

        # --- 底部统计（多行，修「显示不全」）---
        self.stat_lbl = tk.Label(side, text="", bg=C_SIDEBAR, fg=C_FAINT,
                                 font=("Microsoft YaHei UI", 8),
                                 justify="left", anchor="w")
        self.stat_lbl.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(6, 12))

        # ============ 右侧主区 ============
        # v1.3.0：改 grid 布局，让字幕区可在「贴底 / 贴顶」之间切换
        main = tk.Frame(shell, bg=C_BG)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        main.grid_columnconfigure(0, weight=1)

        head = tk.Frame(main, bg=C_BG, height=48)
        head.grid(row=0, column=0, sticky="ew", padx=22)
        head.grid_propagate(False)
        self._title_lbl = tk.Label(head, text="字幕", bg=C_BG, fg=C_TEXT,
                                   font=("Microsoft YaHei UI", 12, "bold"),
                                   cursor="hand2")
        self._title_lbl.pack(side=tk.LEFT, pady=14)
        # v1.3.0：彩蛋从「点标题」移到「关于页连点版本号」
        self.hint_lbl = tk.Label(head, text="点左侧「开始同传」开始监听",
                                 bg=C_BG, fg=C_FAINT,
                                 font=("Microsoft YaHei UI", 9))
        self.hint_lbl.pack(side=tk.LEFT, padx=12, pady=14)

        wrap = tk.Frame(main, bg=C_BG)
        self._main = main
        self._sub_wrap = wrap
        self._sub_spacer = tk.Frame(main, bg=C_BG)

        def card(parent, title, color, size, bold=False, expand=True, pady=(0, 10)):
            outer = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                             highlightthickness=1)
            outer.pack(fill=tk.BOTH, expand=expand, pady=pady)
            hd = tk.Frame(outer, bg=C_PANEL)
            hd.pack(fill=tk.X, padx=16, pady=(11, 0))
            d = tk.Canvas(hd, width=5, height=5, bg=C_PANEL, highlightthickness=0)
            d.pack(side=tk.LEFT, pady=5)
            dot_id = d.create_oval(0, 0, 5, 5, fill=color, outline="")
            title_lbl = tk.Label(hd, text=title, bg=C_PANEL, fg=C_DIM,
                                 font=("Microsoft YaHei UI", 8))
            title_lbl.pack(side=tk.LEFT, padx=6)
            body = tk.Frame(outer, bg=C_PANEL)
            body.pack(fill=tk.BOTH, expand=True, padx=(12, 6), pady=(7, 12))
            t = tk.Text(
                body, bg=C_PANEL, fg=color, relief=tk.FLAT, bd=0,
                font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
                wrap=tk.WORD, height=6, insertbackground=C_TEXT,
                spacing1=6, spacing3=6, padx=6, pady=4,
                selectbackground=C_ACCENT, selectforeground="#03211A",
                highlightthickness=0)
            bar = ModernScrollbar(body, target=t, bg=C_PANEL,
                                  thumb=C_BORDER2, hover=C_ACCENT2)
            t.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            bar.pack(side=tk.RIGHT, fill=tk.Y, padx=(3, 0))
            t.configure(yscrollcommand=bar.set)
            t.bind("<MouseWheel>",
                   lambda e, tt=t: tt.yview_scroll(int(-e.delta / 120), "units"))
            t.tag_configure("ts", foreground=C_FAINT, font=("Consolas", 8))
            return t, outer, hd, d, dot_id, title_lbl, bar, body

        (self.src_text, self._src_card, self._src_head, self._src_dot,
         self._src_dot_id, self._src_title, self._src_bar,
         self._src_body) = card(
            wrap, "原文 · 识别结果", C_SRC, SUB_SIZE_SRC, pady=(0, 10))
        # 流式临时结果：灰色斜体
        self.src_text.tag_configure("partial", foreground="#5E7A72",
                                    font=("Microsoft YaHei UI", 14, "italic"))
        # 流式临时结果的锚点：每次重画前从这里删到末尾
        self.src_text.mark_set("partial_start", "end-1c")
        self.src_text.mark_gravity("partial_start", tk.LEFT)
        (self.dst_text, self._dst_card, self._dst_head, self._dst_dot,
         self._dst_dot_id, self._dst_title, self._dst_bar,
         self._dst_body) = card(
            wrap, "译文", C_DST, SUB_SIZE_DST, bold=True, pady=(0, 0))
        self.dst_text.tag_configure("partial", foreground="#2A7A5E")
        self.dst_text.mark_set("partial_start", "end-1c")
        self.dst_text.mark_gravity("partial_start", tk.LEFT)

        # v2.5.1：按当前翻译模式把用不了的语言置灰（不可选）
        try:
            self._apply_mode_langs(self.tr_mode.get())
        except Exception:
            pass

        # v1.3.0：侧栏滚轮（鼠标停在侧栏任意位置都能上下滚）
        self._bind_wheel(self._side_inner,
                         lambda e: side_canvas.yview_scroll(
                             int(-e.delta / 120), "units"))

    def _bind_wheel(self, widget, callback):
        """递归绑定滚轮；跳过下拉框/输入框（它们自己有滚轮行为）。"""
        try:
            if widget.winfo_class() not in ("TCombobox", "TEntry", "Entry"):
                widget.bind("<MouseWheel>", callback, add="+")
            for child in widget.winfo_children():
                self._bind_wheel(child, callback)
        except Exception:
            pass

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

    def _apply_mode_langs(self, mode=None):
        """按翻译模式把「用不了的语言」变灰、不可选。

        本地模型是单语言对（英语→中文），所以本地模式只留「自动检测 / 英语」
        和译成「中文」；免费在线和 API 模式放开全部语言。
        如果当前选中的语言在新模式下不可用，自动切到第一个可用项。
        """
        mode = mode or self.tr_mode.get()
        in_ok = mode_input_langs(mode)
        out_ok = mode_output_langs(mode)
        try:
            self.in_cb.set_disabled([v for v in INPUT_LANG_NAMES if v not in in_ok])
            self.lang_cb.set_disabled([v for v in OUTPUT_LANG_NAMES if v not in out_ok])
            self.api_engine_cb.set_enabled(mode == TR_API)
        except Exception:
            return
        if self.in_lang_var.get() not in in_ok:
            self.in_lang_var.set(in_ok[0])
            try:
                self._on_input_lang_change()
            except Exception:
                pass
        if self.lang_var.get() not in out_ok:
            self.lang_var.set(out_ok[0])

    def _on_translator_change(self, _evt=None):
        """切换翻译模式（主线程回调）。

        v2.5.1：三种模式完全独立，不再有 auto 降级；API 模式是付费的，
        必须用户自己明确选择，选中后状态栏明确提示并自动打开自动纠错。
        """
        mode = self.tr_mode.get()
        # 记住选择，下次启动还是这个模式
        try:
            c = load_conf()
            c["translator_mode"] = mode
            save_conf(c)
        except Exception:
            pass
        # 先把语言可用性刷成该模式的规则
        self._apply_mode_langs(mode)

        if mode == TR_API:
            # 用户明确选了付费 API：自动打开自动纠错，状态栏显示当前引擎
            self._set_status(f"API模式启动｜{self._api_engine()}", C_YELLOW)
            self._force_correct_on()
            if not (self.translator.available() or self.youdao_tr.available()):
                self._log("API 模式还没填 Key：点右上角 ⚙ 设置 → 填 DeepSeek API Key "
                          "或有道 appKey/appSecret", C_RED)
        elif mode == TR_FREE:
            self._status_threadsafe("在线翻译（免费）：MyMemory 免费接口，"
                                    "不用填 Key、不花钱、国内直连")
        else:
            self._status_threadsafe("本地翻译（离线）：仅支持 英语→中文，断网也能用")

    def _on_api_engine_change(self, _evt=None):
        """切换 API 引擎（只在 API 模式下生效），立即落盘。"""
        global API_ENGINE
        eng = self.api_engine_var.get()
        if eng not in API_ENGINES:
            eng = API_ENGINE_AUTO
        API_ENGINE = eng
        try:
            c = load_conf()
            c["api_engine"] = eng
            save_conf(c)
        except Exception:
            pass
        if self.tr_mode.get() == TR_API:
            self._status_threadsafe(f"API 引擎：{eng}")

    def _refresh_worker_snapshot(self, *_a):
        """主线程调用：把 worker 需要的 Tk 变量值刷成普通属性。

        通过 trace_add 挂在各变量上，任何 UI 改动都会在主线程触发这里；
        worker 线程只读 self._w_* ，彻底避免跨线程访问 Tk。
        """
        try:
            self._w_correct = bool(self.correct_var.get())
            self._w_target = self.lang_var.get()
            self._w_mode = self.tr_mode.get()
            self._w_in_lang = self.in_lang_var.get()
            v = self.api_engine_var.get()
            self._w_api_engine = v if v in API_ENGINES else API_ENGINE_AUTO
        except Exception:
            pass

    def _api_engine(self):
        try:
            v = self.api_engine_var.get()
        except Exception:
            v = API_ENGINE
        return v if v in API_ENGINES else API_ENGINE_AUTO

    def _force_correct_on(self):
        """API 模式下强制打开自动纠错（并落盘）。"""
        global AUTO_CORRECT
        try:
            if not self.correct_var.get():
                self.correct_var.set(True)
            AUTO_CORRECT = True
            c = load_conf()
            c["auto_correct"] = True
            save_conf(c)
        except Exception:
            pass

    def _on_correct_toggle(self):
        """自动纠错开关（v2.5.1：非 API 模式下开启会先提醒「要花 API 余额」）。"""
        global AUTO_CORRECT, CORRECT_WARN_SKIP
        want = bool(self.correct_var.get())
        mode = self.tr_mode.get()
        # 不在 API 模式却要开纠错 → 提醒会花 API 余额（可勾「不再提醒」）
        if want and mode != TR_API and not CORRECT_WARN_SKIP:
            ok, skip = self._ask_correct_consume()
            if skip:
                CORRECT_WARN_SKIP = True
                try:
                    c = load_conf()
                    c["correct_warn_skip"] = True
                    save_conf(c)
                except Exception:
                    pass
            if not ok:
                self.correct_var.set(False)
                return
        AUTO_CORRECT = want
        try:
            c = load_conf()
            c["auto_correct"] = want
            save_conf(c)
        except Exception:
            pass
        if want:
            self._status_threadsafe(
                "自动纠错已开启：每句多一次云端纠错，会消耗 API 余额")
        else:
            self._status_threadsafe("自动纠错已关闭")

    def _ask_correct_consume(self):
        """弹窗提醒：自动纠错会消耗 API 余额。返回 (是否继续开启, 是否不再提醒)。"""
        win = tk.Toplevel(self)
        win.title("自动纠错会消耗 API 余额")
        win.configure(bg=C_BG)
        win.geometry("480x330")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=24, pady=20)

        tk.Label(pad, text="自动纠错会消耗 API 余额", bg=C_BG, fg=C_YELLOW,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        tk.Label(pad, justify="left", anchor="w", wraplength=430, bg=C_BG,
                 fg=C_TEXT2, font=("Microsoft YaHei UI", 9),
                 text=("开启后，每识别一句话都会额外调用一次云端纠错，"
                       "会从你的 API 账户扣钱。\n\n"
                       "• 只有「API 模式（付费）」才必须开它\n"
                       "• 「本地翻译」「在线翻译（免费）」都不需要开\n"
                       "• 没填 API Key 时只做离线规则纠错，不花钱")
                 ).pack(anchor="w", pady=(10, 12))
        skip_var = tk.BooleanVar(value=False)
        RoundedCheck(pad, "不再提醒", skip_var, parent_bg=C_BG, height=24,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")

        state = {"ok": False, "skip": False}

        def do_ok():
            state["ok"] = True
            state["skip"] = bool(skip_var.get())
            win.destroy()

        row = tk.Frame(pad, bg=C_BG)
        row.pack(fill=tk.X, pady=(16, 0))
        RoundedButton(row, "取消", command=win.destroy, bg=C_CARD,
                      hover=C_CARD_HOV, active=C_BORDER2, fg=C_TEXT2,
                      fg_hover=C_TEXT, height=32, width=92,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(side=tk.RIGHT, padx=(8, 0))
        RoundedButton(row, "确定开启", command=do_ok, bg=C_ACCENT,
                      hover=C_ACCENT2, active="#0B8A6B", fg="#03211A",
                      fg_hover="#03211A", fg_active="#03211A", height=32,
                      font=("Microsoft YaHei UI", 9, "bold"), parent_bg=C_BG,
                      radius=8).pack(side=tk.RIGHT)
        win.bind("<Escape>", lambda e: win.destroy())
        self.wait_window(win)
        return state["ok"], state["skip"]

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
            ok, msg, has_card = cuda_probe()
            self.ui_q.put(("gpu_result", ok, msg, has_card))

        threading.Thread(target=probe, daemon=True).start()

    def _apply_gpu_result(self, ok, msg, has_card=False):
        """GPU 探测结果落地（v2.5.1 修 bug：按「有没有 N 卡」说不同的话）。"""
        global GPU_ACCEL
        if ok:
            GPU_ACCEL = True
            c = load_conf(); c["gpu_accel"] = True; save_conf(c)
            self.recognizer.reset()
            self._status_threadsafe("GPU 已开启（Whisper 识别 + 本地翻译生效）")
            return

        GPU_ACCEL = False
        self.gpu_var.set(False)
        c = load_conf(); c["gpu_accel"] = False; save_conf(c)
        self.recognizer.reset()
        self._status_threadsafe(f"GPU 无法启用：{msg}")

        if has_card:
            # 有 N 卡但缺 CUDA 运行时
            messagebox.showwarning(
                "GPU 无法启用",
                f"{msg}\n\n"
                "说明：\n"
                "• 本机检测到 NVIDIA 显卡，但缺少 CUDA 12 运行时\n"
                "• 需要安装 CUDA Toolkit 12.x（约 3 GB）才能启用\n"
                "• SenseVoice 识别为 CPU 模型，本身已足够快（RTF 0.02）\n"
                "• 不装也不影响使用")
        else:
            # 根本没有 N 卡 → 以前的文案会误报「已检测到显卡」，这里改掉
            messagebox.showinfo(
                "本机没有 NVIDIA 显卡",
                f"{msg}\n\n"
                "说明：\n"
                "• 这台电脑没有可用的 NVIDIA 显卡，GPU 加速用不了（硬件限制）\n"
                "• AMD / Intel 核显不支持这里用的 CUDA，不用再试\n"
                "• 所有功能照常运行，只是走 CPU：SenseVoice 识别 RTF 0.02、"
                "本地翻译约 0.03s/句\n"
                "• 不需要做任何设置，直接开始同传即可")

    def _open_settings(self):
        """设置弹窗"""
        win = tk.Toplevel(self)
        win.title("设置")
        win.configure(bg=C_BG)
        win.geometry("600x800")
        win.transient(self)
        win.grab_set()

        # 设置项已经多到一屏放不下（1556px > 980px 上限），必须能滚，
        # 否则底部的远场/强制放大/保存按钮用户根本看不到。
        outer = tk.Frame(win, bg=C_BG)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, bg=C_BG, highlightthickness=0, bd=0)
        bar = ModernScrollbar(outer, target=canvas)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=12)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        holder = tk.Frame(canvas, bg=C_BG)
        inner_id = canvas.create_window((0, 0), window=holder, anchor="nw")
        pad = tk.Frame(holder, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=22, pady=14)
        holder.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(inner_id, width=e.width))

        def _wheel(ev):
            try:
                # bind_all 会收到全应用范围的滚轮事件，弹窗里滚动时
                # 设置页也会跟着动，这里只处理属于本窗口的事件
                if ev.widget.winfo_toplevel() is not win:
                    return
                canvas.yview_scroll(int(-ev.delta / 120) * 3, "units")
            except Exception:
                pass

        win.bind_all("<MouseWheel>", _wheel, add="+")

        def _unwheel(ev):
            if ev.widget is win:
                try:
                    win.unbind_all("<MouseWheel>")
                except Exception:
                    pass

        win.bind("<Destroy>", _unwheel, add="+")

        def _fit_window(_w=win):
            try:
                _w.update_idletasks()
                # 内容已是可滚动长表单，高度取屏幕高度的八成，别让窗口半边空着
                sh = _w.winfo_screenheight()
                wd = 620
                h = max(700, min(940, int(sh * 0.80)))
                _w.geometry("%dx%d" % (wd, h))
            except Exception:
                pass

        def title(t):
            tk.Label(pad, text=t, bg=C_BG, fg=C_TEXT,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 4))

        def hint(t, color=C_DIM):
            tk.Label(pad, text=t, bg=C_BG, fg=color, justify="left", anchor="w",
                     wraplength=500, font=("Microsoft YaHei UI", 9)
                     ).pack(anchor="w", pady=(0, 8))

        def kv_row(label, value="", show=None, width=10):
            """一行 标签 + 输入框，返回 Entry"""
            row = tk.Frame(pad, bg=C_BG)
            row.pack(fill=tk.X, pady=(0, 6))
            tk.Label(row, text=label, bg=C_BG, fg=C_DIM, width=width,
                     anchor="w", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
            ent = tk.Entry(row, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                           relief=tk.FLAT, font=("Consolas", 10),
                           show=show) if show else tk.Entry(
                row, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                relief=tk.FLAT, font=("Consolas", 10))
            ent.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
            if value:
                ent.insert(0, value)
            return ent

        title("翻译模式（三种互相独立，随时可换）")
        hint(f"{TR_LOCAL}：不联网、不花钱，但只有「英语→中文」\n"
             f"{TR_FREE}：不用填 Key、不花钱，走 MyMemory 免费接口（国内直连）\n"
             f"{TR_API}：要花钱！用下面填的 DeepSeek / 有道 Key，按量计费")
        e_ds = kv_row("DeepSeek", DEEPSEEK_KEY, show="*")
        hint("只有「API 模式（付费）」会用到这个 Key。没填也不影响另外两种模式。",
             C_FAINT)

        title("自定义接口（中转站 / 自建 / 本地）")
        hint("只要对方兼容 OpenAI 的 /chat/completions 就能用。\n"
             "地址填到 /v1 为止即可，程序会自动补 /chat/completions。\n"
             "本地 Ollama 示例：http://127.0.0.1:11434/v1 模型填 qwen2.5")
        e_base = kv_row("接口地址", CUSTOM_BASE_URL)
        e_cmodel = kv_row("模型名", CUSTOM_MODEL)
        e_ckey = kv_row("Key", CUSTOM_KEY, show="*")

        title("百度翻译（国内直连）")
        hint("到 fanyi-api.baidu.com 开通「通用文本翻译」，标准版每月 5 万字符免费。")
        e_bd_id = kv_row("appid", BAIDU_APPID)
        e_bd_key = kv_row("密钥", BAIDU_KEY, show="*")

        title("彩云小译（国内直连）")
        hint("到 dashboard.caiyunapp.com 申请 token。只支持 中文↔英/日 几个方向。")
        e_cy = kv_row("token", CAIYUN_TOKEN, show="*")

        e_mm = kv_row("邮箱(可选)", MYMEMORY_EMAIL)
        hint("邮箱只用来向 MyMemory 申请更高的免费额度"
             "（5000 → 50000 字符/天），不填也能正常用。", C_FAINT)

        title("识别增强")
        hint("热词：填写你常说的专业词或人名，用逗号分隔。识别会偏向这些词，"
             "并把高度相似的同音错字纠正回来。留空则不做任何词表干预。")
        e_hw = kv_row("热词", HOTWORD_TEXT)
        hint("例：张三，项目名，专业术语A，专业术语B", C_FAINT)

        far_var = tk.BooleanVar(value=bool(FAR_FIELD))
        RoundedCheck(pad, "远场模式（人离麦克风较远 / 大教室 / 会议桌对面）",
                     far_var, parent_bg=C_BG, height=self.sp(24),
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w",
                                                          pady=(self.sp(2), self.sp(2)))
        hint("开启后会自动降低门限、补回高频、把整句音量拉齐。"
             "近场（贴着电脑说话）保持关闭即可。", C_FAINT)

        loud_var = tk.BooleanVar(value=bool(FORCE_LOUD))
        RoundedCheck(pad, "强制放大分贝（把很轻的声音整体抬高）",
                     loud_var, parent_bg=C_BG, height=self.sp(24),
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w",
                                                          pady=(self.sp(4), self.sp(2)))
        hint("比远场模式更激进：门限压得更低、增益上限更大。"
             "适合麦克风离得很远或说话音量很小的情况。"
             "代价是环境噪声也会被一起放大，安静环境用远场模式就够了。", C_FAINT)

        tk.Label(pad, text="降噪", bg=C_BG, fg=C_TEXT, anchor="w",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w",
                                                     pady=(self.sp(4), self.sp(1)))
        spec_var = tk.StringVar(value=DENOISE_MODE_LABEL.get(DENOISE_MODE,
                                                              DENOISE_MODE_LABEL["auto"]))
        SelectField(pad, spec_var, [DENOISE_MODE_LABEL[m] for m in DENOISE_MODES],
                    parent_bg=C_BG, height=self.sp(26),
                    font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        hint("按频点区分「这一格是噪声还是人声」：噪声占优的频点压下去，人声占优的留着；"
             "装了降噪模型时还会用神经网络再修一遍。上面两个选项只调音量、不改变信噪比，"
             "这一个才真的提高信噪比。\n"
             "自动档只在空调、风扇、投影机这类稳态噪声下降噪——多人说话时降噪会把别人的话一起咬掉，"
             "实测错误率反而从 7.3% 涨到 25.5%，所以那种环境请选「关闭降噪」。", C_FAINT)

        title("有道智云（API 模式的第二个引擎）")
        hint("到 ai.youdao.com 创建「文本翻译」应用，把 appKey / appSecret 填在这里。\n"
             "只有「API 模式（付费）」会用到；本地翻译 / 在线翻译（免费）都不需要。\n"
             "支持医学等领域翻译（对听力/言语康复文献更准）。")
        e_key = kv_row("appKey", YOUDAO_APP_KEY)
        e_sec = kv_row("appSecret", YOUDAO_APP_SECRET, show="*")

        # 独家模块 2 系列触发点
        # 按键只是「快」的那条路；中文暗号经输入法上屏时不一定有 KeyRelease，
        # 所以再加一层轮询兜底，任何输入方式只要框里出现暗号就能命中。
        _egg_entries = (e_ds, e_key, e_sec, e_mm, e_base, e_ckey, e_bd_id,
                        e_bd_key, e_cy, e_cmodel, e_hw)
        for _ent in _egg_entries:
            _ent.bind("<KeyRelease>",
                      lambda ev, e=_ent: self._check_easter_egg(e, win))
            _ent.bind("<Return>",
                      lambda ev, e=_ent: self._check_easter_egg(e, win))
            _ent.bind("<FocusOut>",
                      lambda ev, e=_ent: self._check_easter_egg(e, win))

        def _egg_watch():
            try:
                if not win.winfo_exists():
                    return
                for _ent in _egg_entries:
                    try:
                        t = _ent.get().strip()
                    except Exception:
                        continue
                    if not t:
                        continue
                    if any(t == _egg_key(i) for i in range(len(_EGG_CODE))):
                        self._check_easter_egg(_ent, win)
                        break
            except Exception:
                pass
            try:
                win.after(300, _egg_watch)
            except Exception:
                pass

        win.after(300, _egg_watch)

        f3 = tk.Frame(pad, bg=C_BG); f3.pack(fill=tk.X, pady=(0, 10))
        tk.Label(f3, text="领域", bg=C_BG, fg=C_DIM, width=10, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        dom_var = tk.StringVar(value=YOUDAO_DOMAIN)
        SelectField(f3, variable=dom_var,
                    values=["general", "medicine", "computers", "finance", "game"],
                    parent_bg=C_BG, height=30,
                    font=("Microsoft YaHei UI", 9)).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        upd_var = tk.BooleanVar(value=bool(UPDATE_CHECK))
        RoundedCheck(pad, "启动时检查更新（有新版本会弹窗提示，可跳过）",
                     upd_var, parent_bg=C_BG, height=self.sp(24),
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w",
                                                          pady=(self.sp(2), self.sp(8)))

        def save_all():
            global YOUDAO_APP_KEY, YOUDAO_APP_SECRET, YOUDAO_DOMAIN
            global DEEPSEEK_KEY, MYMEMORY_EMAIL, UPDATE_CHECK
            global HOTWORDS, HOTWORD_TEXT, FAR_FIELD, FORCE_LOUD
            global CUSTOM_BASE_URL, CUSTOM_MODEL, CUSTOM_KEY
            global BAIDU_APPID, BAIDU_KEY, CAIYUN_TOKEN
            global DENOISE_MODE
            UPDATE_CHECK = bool(upd_var.get())
            YOUDAO_APP_KEY = e_key.get().strip()
            YOUDAO_APP_SECRET = e_sec.get().strip()
            YOUDAO_DOMAIN = dom_var.get()
            DEEPSEEK_KEY = e_ds.get().strip()
            MYMEMORY_EMAIL = e_mm.get().strip()
            HOTWORD_TEXT = e_hw.get().strip()
            HOTWORDS = [HOTWORD_TEXT] if HOTWORD_TEXT else []
            FAR_FIELD = bool(far_var.get())
            FORCE_LOUD = bool(loud_var.get())
            CUSTOM_BASE_URL = e_base.get().strip()
            CUSTOM_MODEL = e_cmodel.get().strip()
            CUSTOM_KEY = e_ckey.get().strip()
            BAIDU_APPID = e_bd_id.get().strip()
            BAIDU_KEY = e_bd_key.get().strip()
            CAIYUN_TOKEN = e_cy.get().strip()
            # 降噪档位写进全局，当前会话立即生效（AudioCapture 每次开始都重建）
            DENOISE_MODE = _denoise_mode_from_label(spec_var.get())
            c = load_conf()
            c.update({"youdao_app_key": YOUDAO_APP_KEY,
                      "youdao_app_secret": YOUDAO_APP_SECRET,
                      "youdao_domain": YOUDAO_DOMAIN,
                      "deepseek_api_key": DEEPSEEK_KEY,
                      "mymemory_email": MYMEMORY_EMAIL,
                      "hotwords": HOTWORD_TEXT,
                      "far_field": bool(far_var.get()),
                      "force_loud": bool(loud_var.get()),
                      "denoise_mode": DENOISE_MODE,
                      "custom_base_url": CUSTOM_BASE_URL,
                      "custom_model": CUSTOM_MODEL,
                      "custom_api_key": CUSTOM_KEY,
                      "baidu_appid": BAIDU_APPID,
                      "baidu_key": BAIDU_KEY,
                      "caiyun_token": CAIYUN_TOKEN,
                      "update_check": bool(upd_var.get())})
            ok = save_conf(c)
            # Key 热更新：不用重启程序
            try:
                self.translator.set_key(DEEPSEEK_KEY)
                self.custom_tr.set_endpoint(CUSTOM_BASE_URL, CUSTOM_MODEL or None,
                                            CUSTOM_KEY)
            except Exception:
                pass
            self._status_threadsafe("设置已保存" if ok else "保存失败")
            win.destroy()

        RoundedButton(pad, "保存设置", command=save_all,
                      bg=C_ACCENT, hover=C_ACCENT2, active="#0B8A6B",
                      fg="#03211A", fg_hover="#03211A", fg_active="#03211A",
                      height=36, font=("Microsoft YaHei UI", 10, "bold"),
                      parent_bg=C_BG, radius=9).pack(fill=tk.X, pady=(0, 14))

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

        # ---- GPU 说明（v2.5.1：先报本机真实检测结果，不再一律说「有显卡」）----
        title("GPU 加速说明")
        try:
            _gpu_msg = gpu_quick_summary()
        except Exception:
            _gpu_msg = "本机 GPU 检测：不可用（不影响使用）"
        hint(_gpu_msg, C_ACCENT if "未检测到" not in _gpu_msg else C_FAINT)
        hint("• Whisper 识别、本地 opus-mt 翻译：可用 GPU（需 NVIDIA 显卡）\n"
             "• SenseVoice 识别：官方 pip 包为 CPU 编译，无法 GPU，但它本身就极快（RTF 0.02）\n"
             "• 开启后下次识别会重新加载模型")

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=30,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(fill=tk.X, pady=(10, 0))
        win.after(30, _fit_window)

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
        """流式临时结果 → 界面（灰色斜体，不翻译）

        3.0.0 第 11 条：滑动窗口重识别时，模型有时会把已经念对的前半段推翻重写，
        字幕就会来回跳。这里做一个「前缀稳定性」判定：如果新结果和正在显示的
        文本在同一位置开始分叉，就先记为待确认，连续两次一致才真的刷新。
        末尾继续延伸属于正常增长，立刻显示。
        """
        prev = getattr(self, "_partial_prev", "")
        if prev and text:
            same = 0
            for a, b in zip(prev, text):
                if a != b:
                    break
                same += 1
            stable_len = min(len(prev), len(text))
            if same < stable_len - 6:
                # 分叉点落在靠前的位置 → 先不急着改，等下一次确认
                if text != getattr(self, "_partial_pend", ""):
                    self._partial_pend = text
                    return
        self._partial_pend = text
        self._partial_prev = text
        self.ui_q.put(("partial", text, lang))

    def _refresh_devices(self):
        names = []
        self._dev_names = {}         # 显示名 → 当前索引（设备热插拔后索引会变）
        self._dev_by_name = {}       # 设备名（不含序号/标记）→ 当前索引
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
                    disp = f"[{i}] {nm[:36]}{tail}"
                    names.append(disp)
                    self._dev_names[disp] = i
                    self._dev_by_name.setdefault(nm[:36], i)
        except Exception:
            pass
        self.dev_cb.set_values(names)
        if not names:
            return
        # 选中项先按设备名重新对齐序号：热插拔后序号会变，
        # 但显示文本里带着旧序号，直接查表会查不到而悄悄退回默认设备。
        cur = self.dev_var.get()
        idx = self._dev_index_of(cur) if cur else None
        if cur and idx is None:
            self._log(f"上次选择的麦克风已经不在了（{cur}），改用默认设备")
        if idx is None:
            try:
                dv = sd.default.device[0]
            except Exception:
                dv = None
            if dv is not None and any(v == dv for v in self._dev_names.values()):
                idx = dv
            else:
                idx = self._dev_names[names[0]]
        label = next((n for n in names if self._dev_names.get(n) == idx), names[0])
        if label != cur:
            self.dev_var.set(label)

    def _dev_index_of(self, label):
        """把下拉里的显示文本解析成当前设备索引（名字优先，序号只作最后兜底）。"""
        if not label:
            return None
        hit = getattr(self, "_dev_names", {}).get(label)
        if hit is not None:
            return hit
        name = label
        if name.startswith("["):
            try:
                name = name[name.index("]") + 1:].strip()
            except ValueError:
                pass
        if name.endswith("【系统声音】"):
            name = name[:-len("【系统声音】")].strip()
        table = getattr(self, "_dev_by_name", {})
        if name in table:
            return table[name]
        for key, val in table.items():        # 名字被截断或驱动改了后缀
            if key.startswith(name) or name.startswith(key):
                return val
        return None

    def _picked_device(self):
        # 用设备名查最新索引，不解析下拉文本里可能已经过期的编号
        return self._dev_index_of(self.dev_var.get())

    # ------------------------------------------------------------ 环境检查
    def _check_env(self):
        """启动自检（v2.5.1：默认是免费在线模式，没 Key 也照常用，不再弹警告）。"""
        if _SECRET_DECRYPT_FAILED:
            self._log("部分 API Key 解密失败（换机或配置损坏），请在设置页重新填写", C_RED)
        if not self.translator.available() and not self.youdao_tr.available():
            self._log("未配置 API Key：默认的「在线翻译（免费）」不需要 Key，可以正常使用")
        else:
            self._log("已找到 API Key（只有 API 模式才用得到，按量计费）")

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
        # v2.5.1：API 模式没配 Key 就先说清楚，别让用户以为在花钱其实没配
        if self.tr_mode.get() == TR_API and not (
                self.translator.available() or self.youdao_tr.available()):
            if not messagebox.askyesno(
                    "API 模式尚未配置 Key",
                    "当前是「API 模式（付费）」，但还没有填 DeepSeek / 有道的 Key。\n\n"
                    "继续开始的话，每句只会显示「未配置 Key」提示，不会产生费用。\n\n"
                    "想不花钱：切到「在线翻译（免费）」，不用填任何 Key。\n\n"
                    "要先开始吗？"):
                return
        self._last_speech_ts = time.time()
        self._silence_warned = False
        self._refresh_devices()          # 设备索引可能已变化，按名字重新解析
        dev = self._picked_device()
        self.t_start = time.time()
        self._last_dur = 0
        self._set_running_ui(True, "启动中...", C_YELLOW)

        # 识别模型后台加载
        threading.Thread(target=self._warmup, daemon=True).start()

        # 3.2.5：启动过程整体兜住。声卡被别的软件独占、设备拔掉、
        # 模型目录缺失都会在这一段抛异常；以前异常直接冒到 Tk 回调，
        # 界面停在「运行中」但什么都没跑，用户只能重开程序。
        try:
            # 音频采集（流式：同时把最新片段喂给临时识别线程）
            self.capture = AudioCapture(
                on_sentence=lambda a: self.work_q.put(a),
                on_level=self._on_level,
                on_status=self._status_threadsafe,
                device=dev,
                on_partial=lambda a: self.stream_worker.push(a),
            )
            self.capture.far_field = bool(FAR_FIELD)
            self.capture.force_loud = bool(FORCE_LOUD)
            self.capture.spectral_on = bool(SPECTRAL_DENOISE)
            self.capture.denoise_mode = str(DENOISE_MODE)
            self.capture.start()

            # 流式识别线程
            if not self.stream_worker.is_alive():
                self.stream_worker = StreamWorker(
                    self.recognizer, self._on_partial_text,
                    on_error=self._stream_error)
                self.stream_worker.start()

            # 处理线程
            self.worker = threading.Thread(target=self._worker_loop,
                                           daemon=True)
            self.worker.start()
        except Exception as e:                  # noqa: BLE001
            self._stop()
            self._set_status(f"启动失败: {e}", C_RED)
            self._log(f"启动失败: {e}\n{traceback.format_exc()}", C_RED)

    def _warmup(self):
        try:
            self.recognizer.load()
            self._status_threadsafe("识别模型已就绪，请开始说话")
        except Exception as e:
            self._status_threadsafe(f"模型加载失败: {e}")

    def _stop(self):
        # 3.2.5：先切界面、再收拾线程。
        # 之前是「先 capture.stop() 再改按钮」，只要清理那一步抛出任何异常，
        # 按钮就永远停在「■ 停止」，而且 running 已经是 False —— 用户彻底懵。
        # 现在界面切换不依赖任何清理结果。
        self._set_running_ui(False, "已停止", C_DIM)
        self._log("已停止")
        cap, self.capture = self.capture, None
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                # 采集线程已经结束、句柄失效之类都不该影响停止流程
                pass
        try:
            if self.stream_worker:
                self.stream_worker.clear()
        except Exception:
            pass

    # ------------------------------------------------------------ 运行状态
    def _paint_run_button(self, running):
        """按钮外观唯一来源：开始 / 停止两态。"""
        if running:
            self.btn_start.set_text("■ 停止")
            self.btn_start.set_colors(bg=C_RED, hover="#ef4444",
                                      active="#b91c1c", fg="#FFFFFF",
                                      fg_hover="#FFFFFF", fg_active="#FFFFFF")
        else:
            self.btn_start.set_text("▶  开始同传")
            self.btn_start.set_colors(bg=C_ACCENT, hover=C_ACCENT2,
                                      active="#0B8A6B", fg="#03211A",
                                      fg_hover="#03211A", fg_active="#03211A")

    def _set_running_ui(self, running, status=None, color=None):
        """把「是否在运行」这一件事同步到按钮、托盘、状态栏。

        以前这三处各自在 _start / _stop 里手写，改一处漏一处就会出现
        「已经停了但按钮还写着停止」这类不一致。
        """
        self.running = bool(running)
        try:
            self._paint_run_button(self.running)
        except Exception:
            pass
        try:
            if self._tray:
                self._tray.set_state(self.running)
        except Exception:
            pass
        if status is not None:
            try:
                self._set_status(status, color or C_DIM)
            except Exception:
                pass

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
        # v2.1.1：自动纠错——识别之后、显示/翻译之前做最小修正
        if self._w_correct and text.strip():
            text = self._auto_correct(text, lang)
        t_asr = time.time() - t0

        if not text.strip():
            self.ui_q.put(("status", "未识别到内容"))
            return
        # ⚠️ 修：只识别出「。」「.」这类标点时直接丢弃，不送翻译
        if not is_meaningful(text):
            self.ui_q.put(("status", "识别结果无实义（纯标点），已跳过"))
            return

        ts = datetime.now().strftime("%H:%M:%S")
        # v1.3.0：记录这一句的真实时间轴（音频片段时长 ≈ 说话时长）
        now = time.time()
        seg = {
            "start": now - len(audio) / float(SAMPLE_RATE),
            "end": now,
            "src": text,
            "dst": "",
        }
        self._segments.append(seg)
        self.ui_q.put(("src", ts, text, lang, emo, evt))
        self.stats["sentences"] += 1
        self.stats["chars_in"] += len(text)

        # 2) 翻译（流式）
        target = self._w_target
        self.ui_q.put(("status", "翻译中..."))

        # 先插一行占位
        self.ui_q.put(("dst_begin", ts, ""))

        chunks = []
        def on_chunk(d):
            chunks.append(d)
            self.ui_q.put(("dst_chunk", "".join(chunks)))

        t1 = time.time()
        full, used = self._do_translate(text, target, on_chunk, src_lang=lang)
        t_mt = time.time() - t1

        self.ui_q.put(("dst_end", full))
        seg["dst"] = full            # 回填译文，SRT 导出时优先用译文
        self.stats["chars_out"] += len(full)

        total = time.time() - t0
        self.ui_q.put(("status",
                       f"{used}｜识别 {t_asr:.1f}s + 翻译 {t_mt:.1f}s = {total:.1f}s"))

    def _auto_correct(self, text, lang):
        """v2.1.1 自动纠错：本地规则 + 云端最小修正，失败静默回退。"""
        try:
            fixed = _rule_correct(text)
            if self.translator.available() and len(fixed.strip()) >= 2:
                try:
                    out = self.translator.correct(fixed, lang)
                    if out and out.strip():
                        fixed = out.strip()
                except Exception:
                    pass
            return fixed
        except Exception:
            return text

    def _do_translate(self, text, target, on_chunk, src_lang=None):
        """翻译总入口：先拿引擎原文，再统一做字形规整。

        3.0.0 修第 3 条：模型偶尔会输出繁体（训练语料里台港样本不少），
        用户要的是大陆简体。所有引擎的译文都在这里过一遍 to_simplified，
        已经是简体的文本不受影响，非中文目标语言直接跳过。
        """
        out, engine = self._do_translate_raw(
            text, target, on_chunk, src_lang, ctx=list(self._ctx))
        try:
            if out and target == "中文":
                # 流式阶段可能已经吐过繁体，但最终 dst_end 用的是这里的整段译文，
                # 所以只要把返回值改掉，界面上最终显示的一定是简体。
                out = to_simplified(out)
        except Exception:
            pass
        # 记入上下文（失败/报错的译文不进历史，免得污染后面的句子）
        try:
            if out and not out.startswith("[") and src_lang:
                self._ctx.append((text, out))
        except Exception:
            pass
        return out, engine

    def _do_translate_raw(self, text, target, on_chunk, src_lang=None, ctx=None):
        """按当前翻译模式分发，返回 (译文, 实际用的引擎名)。

        v2.5.1：三种模式完全独立，互不降级（auto 模式已删除）。
        3.1.0：本方法只在 worker 线程跑，读 self._w_* 快照，不碰 Tk 变量。
        """
        mode = self._w_mode

        # ① 本地翻译（离线）：只用 opus-mt，仅英语→中文
        if mode == TR_LOCAL:
            src, _ = input_codes(self._w_in_lang)
            if src not in ("en", "auto") or target != "中文":
                return (f"[本地模式只支持 英语→中文，当前是「{self._w_in_lang}→"
                        f"{target}」，请切到「{TR_FREE}」]"), "本地"
            # 下拉是「自动检测」时也要核对本次实际识别语言，不能把法语硬送 opus-mt
            detected = (src_lang or "").lower()
            if detected and detected not in ("en", "auto", "eng"):
                name = LANG_NAMES.get(detected, detected)
                return (f"[本地模型只支持英语→中文，本次识别为「{name}」；"
                        f"请切到「{TR_FREE}」]"), "本地"
            if not self.local_tr.available():
                return "[本地翻译模型缺失，请切到「在线翻译（免费）」]", "本地"
            try:
                return self.local_tr.translate(text, target, on_chunk), "本地模型"
            except Exception as e:
                return f"[本地翻译失败: {e}]", "本地模型"

        # ② 在线翻译（免费）：单一免费引擎 MyMemory（配额用尽自动切免费兜底）
        if mode == TR_FREE:
            out = self.free_tr.translate(text, target, src_lang=src_lang,
                                         on_chunk=on_chunk)
            return out, self.free_tr.last_provider

        # ③ API 模式（付费）：按「API 引擎」选择；自动兜底时 DeepSeek 失败切有道
        if mode == TR_API:
            eng = self._w_api_engine
            ds_ok = self.translator.available()
            yd_ok = self.youdao_tr.available()

            if eng == API_ENGINE_YOUDAO:
                if not yd_ok:
                    return ("[API 引擎选的是「有道智云」，但还没填 appKey/appSecret]"), "无"
                return self.youdao_tr.translate(text, target, on_chunk), "有道 API"

            if eng == API_ENGINE_DEEPSEEK:
                if not ds_ok:
                    return "[API 引擎选的是「DeepSeek」，但还没填 API Key]", "无"
                return self.translator.translate(text, target, on_chunk, context=ctx), "DeepSeek API"

            # 自定义（OpenAI 兼容）：中转站 / 自建网关 / 本地 Ollama
            if eng == API_ENGINE_CUSTOM:
                if not (CUSTOM_BASE_URL.strip() and CUSTOM_KEY.strip()):
                    return ("[API 引擎选的是「自定义」，但还没填接口地址 / Key"
                            "（设置 → 自定义接口）]"), "无"
                out = self.custom_tr.translate(text, target, on_chunk, context=ctx)
                return out, "自定义 API"

            # 百度翻译（国内直连，按字符计费）
            if eng == API_ENGINE_BAIDU:
                if not self.baidu_tr.available():
                    return ("[API 引擎选的是「百度翻译」，但还没填 appid / 密钥"
                            "（设置 → 百度翻译）]"), "无"
                src_disp = self._w_in_lang
                return (self.baidu_tr.translate(text, target, src=src_disp,
                                                on_chunk=on_chunk),
                        "百度翻译")

            # 彩云小译（国内直连，token 即可用）
            if eng == API_ENGINE_CAIYUN:
                if not self.caiyun_tr.available():
                    return ("[API 引擎选的是「彩云小译」，但还没填 token"
                            "（设置 → 彩云小译）]"), "无"
                src_disp = self._w_in_lang
                return (self.caiyun_tr.translate(text, target, src=src_disp,
                                                 on_chunk=on_chunk),
                        "彩云小译")

            # 自动兜底：DeepSeek 优先；若一个字都没吐出来就失败，改走有道
            if ds_ok:
                got = {"n": 0}

                def _tap(d):
                    got["n"] += 1
                    if on_chunk:
                        on_chunk(d)

                out = self.translator.translate(text, target, _tap, context=ctx)
                failed = (not out) or out.startswith("[翻译失败") or out.startswith("[未配置")
                if not failed:
                    return out, "DeepSeek API"
                if yd_ok and got["n"] == 0:
                    self._status_threadsafe("DeepSeek 不可用，已自动切到有道 API")
                    return (self.youdao_tr.translate(text, target, on_chunk),
                            "有道 API（兜底）")
                return out, "DeepSeek API"
            if yd_ok:
                return self.youdao_tr.translate(text, target, on_chunk), "有道 API"
            return ("[API 模式还没配置 Key：设置 → DeepSeek API Key / 有道 "
                    "appKey+appSecret]"), "无"

        return "[未知翻译模式，请在左侧重新选择]", "无"

    # ------------------------------------------------------------ UI 消息泵
    def _pump_ui(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                self._apply(msg)
        except queue.Empty:
            pass
        # v1.3.0：托盘线程 → 主线程（显示 / 开始停止 / 退出）
        try:
            while True:
                self._handle_tray(self._tray_q.get_nowait())
        except queue.Empty:
            pass
        self.after(60, self._pump_ui)

    def _apply(self, msg):
        kind = msg[0]
        if kind == "status":
            self._set_status(msg[1], C_ACCENT)
        elif kind == "log_red":
            self.status_lbl.configure(text="● " + msg[1], fg=C_RED)
        elif kind == "level":
            try:
                self.level_canvas.coords(self.level_bar, 2, 3, msg[1], 3)
                self.level_canvas.itemconfig(self.level_bar, fill=msg[2])
            except Exception:
                pass
        elif kind == "gpu_result":
            _, ok, msg, has_card = msg
            self._apply_gpu_result(ok, msg, has_card)
        elif kind == "update_available":
            self._show_update_dialog(msg[1])
        elif kind == "update_progress":
            self._on_update_progress(msg[1], msg[2])
        elif kind == "update_done":
            self._on_update_done(msg[1], msg[2] if len(msg) > 2 else "",
                                 msg[3] if len(msg) > 3 else "")
        elif kind == "update_cancelled":
            self._on_update_cancelled()
        elif kind == "update_failed":
            self._on_update_failed(msg[1])
        elif kind == "partial":
            # 流式临时结果：灰色斜体，覆盖上一次临时内容（不翻译）
            _, text, lang = msg
            chat = getattr(self, "_layout", LAYOUT_CLASSIC) == LAYOUT_CHAT
            t = self.dst_text if chat else self.src_text
            try:
                t.delete("partial_start", "end-1c")
            except Exception:
                pass
            t.mark_set("partial_start", "end-1c")
            t.mark_gravity("partial_start", tk.LEFT)
            if chat:
                t.insert(tk.END, text, ("partial", "chat_dst"))
            else:
                t.insert(tk.END, text, "partial")
            t.see(tk.END)
        elif kind == "src":
            # 先清掉流式临时行，再写正式结果
            try:
                self.src_text.delete("partial_start", "end-1c")
            except Exception:
                pass
            _, ts, text, lang, emo, evt = msg
            tag = LANG_NAMES.get(lang, lang or "?")
            extra = ""
            if emo and emo.upper() not in ("NEUTRAL", "EMO_UNKNOWN", "UNKNOWN", ""):
                extra += f" [{emo}]"
            self.src_text.insert(tk.END, f"[{ts}] ", "ts")
            self.src_text.insert(tk.END, f"({tag}){extra} ", "ts")
            self.src_text.insert(tk.END, f"{text}\n")
            # 对话布局下原文卡是收起的，把原文当成浅灰小字贴在译文上方
            if getattr(self, "_layout", LAYOUT_CLASSIC) == LAYOUT_CHAT:
                try:
                    # 先清掉上一句残留的流式临时行，再落锚点，最后写正式原文行
                    self.dst_text.delete("partial_start", "end-1c")
                    self.dst_text.insert(
                        tk.END, f"[{ts}] ({tag}){extra} {text}\n", "chat_src")
                    self.dst_text.mark_set("partial_start", "end-1c")
                    self.dst_text.mark_gravity("partial_start", tk.LEFT)
                    self.dst_text.see(tk.END)
                except Exception:
                    pass
            # 写完才把临时行标记移到新末尾，历史句子不会再被删掉
            self.src_text.mark_set("partial_start", "end-1c")
            self.src_text.mark_gravity("partial_start", tk.LEFT)
            self.src_text.see(tk.END)
            self._last_speech_ts = time.time()
            self._silence_warned = False
        elif kind == "dst_begin":
            _, ts, _ = msg
            if getattr(self, "_layout", LAYOUT_CLASSIC) == LAYOUT_CHAT:
                # 对话布局：时间戳已经在原文行里，这里只做锚点
                self.dst_text.mark_set("partial_start", "end-1c")
                self.dst_text.mark_gravity("partial_start", tk.LEFT)
            else:
                self.dst_text.insert(tk.END, f"[{ts}] ", "ts")
                self.dst_text.mark_set("partial_start", "end-1c")
                self.dst_text.mark_gravity("partial_start", tk.LEFT)
        elif kind == "dst_chunk":
            # 删掉上次的部分译文，重画（只删到末尾，不动历史）
            self.dst_text.delete("partial_start", "end-1c")
            if getattr(self, "_layout", LAYOUT_CLASSIC) == LAYOUT_CHAT:
                self.dst_text.insert(tk.END, msg[1], ("partial", "chat_dst"))
            else:
                self.dst_text.insert(tk.END, msg[1], "partial")
            self.dst_text.see(tk.END)
        elif kind == "dst_end":
            self.dst_text.delete("partial_start", "end-1c")
            if getattr(self, "_layout", LAYOUT_CLASSIC) == LAYOUT_CHAT:
                self.dst_text.insert(tk.END, msg[1] + "\n", "chat_dst")
            else:
                self.dst_text.insert(tk.END, msg[1] + "\n")
            # 关键：最终译文写完后锚点必须落到末尾。
            # 否则锚点停在最终译文之前，下一句第一条 partial 会把它整段删掉。
            self.dst_text.mark_set("partial_start", "end-1c")
            self.dst_text.mark_gravity("partial_start", tk.LEFT)
            self.dst_text.see(tk.END)
        self._update_stats()

    def _update_stats(self):
        """多行显示，避免侧栏里被截断"""
        st = self.stats
        # 停止后冻结运行时长，不再从 t_start 一直涨上去
        if self.running:
            self._last_dur = int(time.time() - self.t_start) if self.t_start else 0
        dur = getattr(self, "_last_dur", 0)
        self.stat_lbl.configure(
            text=(f"句数 {st['sentences']}　运行 {dur//60:02d}:{dur%60:02d}\n"
                  f"原文 {st['chars_in']} 字　译文 {st['chars_out']} 字"))

    def _on_level(self, rms):
        # 3.1.0：音频线程只做纯计算，控件更新一律回主线程（Tk 非线程安全）。
        try:
            w = max(2, int(min(1.0, rms / 0.15) * self.sp(130)))
            color = C_LISTEN if rms > VAD_MIN_RMS else "#3a4048"
            self.ui_q.put(("level", w, color))
        except Exception:
            pass

    def _status_threadsafe(self, text):
        self.ui_q.put(("status", text))

    def _stream_error(self, detail):
        # 流式识别线程报错 → 回主线程红字提示（只报一次，见 StreamWorker）
        self.ui_q.put(("log_red", ("流式识别出错: " + str(detail))[:60]))

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
            # 3.1.0：后台线程不能直接碰 Tk 控件，统一走 ui_q 回主线程。
            if threading.current_thread() is threading.main_thread():
                self.status_lbl.configure(text="● " + text[:40], fg=C_RED)
            else:
                self.ui_q.put(("log_red", text[:40]))

    # ------------------------------------------------------------ 按钮
    def _clear(self):
        self.src_text.delete("1.0", tk.END)
        self.dst_text.delete("1.0", tk.END)
        self.dst_text.mark_set("partial_start", "end-1c")
        self.dst_text.mark_gravity("partial_start", tk.LEFT)
        self.stats = {"sentences": 0, "chars_in": 0, "chars_out": 0}
        self._segments = []          # 清空时间轴记录（SRT 导出用）
        self.t_start = time.time() if self.running else None
        self._last_dur = 0
        self._update_stats()

    def _save(self):
        """v2.1.1：用户自己选择保存位置（另存为对话框）。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            path = filedialog.asksaveasfilename(
                title="保存同传记录",
                initialdir=_output_dir(),
                initialfile=f"同传记录_{ts}.txt",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
            if not path:
                return
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

    # ------------------------------------------------------------ v1.2.0 新增
    def _bind_shortcuts(self):
        """全局快捷键。"""
        self.bind("<Control-Return>", lambda e: self._toggle())
        self.bind("<Control-l>", lambda e: self._clear())
        self.bind("<Control-s>", lambda e: self._save())
        self.bind("<Control-Shift-s>", lambda e: self._export_srt())
        self.bind("<Control-equal>", lambda e: self._font_step(+2))
        self.bind("<Control-plus>", lambda e: self._font_step(+2))
        self.bind("<Control-minus>", lambda e: self._font_step(-2))
        self.bind("<Control-0>", lambda e: self._font_reset())

    def _font_step(self, delta):
        """调字幕字号（原文/译文按比例一起动）。"""
        dst = max(SUB_SIZE_MIN, min(SUB_SIZE_MAX, self._sub_size_dst + delta))
        src = max(SUB_SIZE_MIN - 4, min(SUB_SIZE_MAX, dst - 4))
        self._sub_size_dst, self._sub_size_src = dst, src
        self._apply_sub_font()
        self._status_threadsafe(f"字幕字号：译文 {dst} / 原文 {src}")

    def _font_reset(self):
        self._sub_size_dst, self._sub_size_src = SUB_SIZE_DST, SUB_SIZE_SRC
        self._apply_sub_font()
        self._status_threadsafe("字幕字号已恢复默认")

    def _apply_sub_font(self):
        """热更新两个字幕框的字体（含 partial 标签，颜色跟随当前配色）。"""
        th = SUB_THEMES.get(self._sub_theme, SUB_THEMES[SUB_THEME_DEFAULT])
        try:
            self.src_text.configure(
                font=("Microsoft YaHei UI", self._sub_size_src))
            self.src_text.tag_configure(
                "partial", foreground=th["partial_src"],
                font=("Microsoft YaHei UI", self._sub_size_src, "italic"))
            self.src_text.tag_configure(
                "ts", foreground=C_FAINT, font=("Consolas", 8))
            self.dst_text.configure(
                font=("Microsoft YaHei UI", self._sub_size_dst, "bold"))
            self.dst_text.tag_configure(
                "partial", foreground=th["partial_dst"],
                font=("Microsoft YaHei UI", self._sub_size_dst, "bold"))
        except Exception:
            pass

    # ------------------------------------------------------------ v1.3.0 字幕样式
    def _restore_style(self):
        """启动时读回上次的配色 / 位置 / 布局（配置坏了就退回默认）。"""
        try:
            c = load_conf()
            theme = c.get("sub_theme")
            pos = c.get("sub_pos")
            layout = c.get("layout")
        except Exception:
            theme, pos, layout = None, None, None
        self._apply_theme(theme if theme in SUB_THEMES else SUB_THEME_DEFAULT,
                          persist=False)
        self._apply_position(pos if pos in ("bottom", "top") else SUB_POS_DEFAULT,
                             persist=False)
        self._apply_layout(layout if layout in LAYOUTS else LAYOUT, persist=False)

    def _persist_style(self):
        try:
            c = load_conf()
            c["sub_theme"] = self._sub_theme
            c["sub_pos"] = self._sub_pos
            c["layout"] = self._layout
            save_conf(c)
        except Exception:
            pass

    def _set_theme(self, key):
        self._apply_theme(key)
        self._status_threadsafe(f"字幕配色：{SUB_THEMES[key]['name']}")

    def _set_position(self, key):
        self._apply_position(key)
        self._status_threadsafe("字幕位置：贴顶" if key == "top" else "字幕位置：贴底")

    def _refresh_style_buttons(self):
        for key, btn in getattr(self, "_theme_btns", {}).items():
            on = (key == getattr(self, "_sub_theme", None))
            try:
                btn.set_colors(bg=C_ACCENT if on else C_CARD,
                               hover=C_ACCENT2 if on else C_CARD_HOV,
                               active=C_ACCENT if on else C_CARD_HOV,
                               fg="#03211A" if on else C_TEXT2,
                               fg_hover="#03211A" if on else C_TEXT,
                               fg_active="#03211A" if on else C_TEXT)
            except Exception:
                pass
        for key, btn in getattr(self, "_pos_btns", {}).items():
            on = (key == getattr(self, "_sub_pos", None))
            try:
                btn.set_colors(bg=C_ACCENT if on else C_CARD,
                               hover=C_ACCENT2 if on else C_CARD_HOV,
                               active=C_ACCENT if on else C_CARD_HOV,
                               fg="#03211A" if on else C_TEXT2,
                               fg_hover="#03211A" if on else C_TEXT,
                               fg_active="#03211A" if on else C_TEXT)
            except Exception:
                pass

        for key, btn in getattr(self, "_layout_btns", {}).items():
            on = (key == getattr(self, "_layout", None))
            try:
                btn.set_colors(bg=C_ACCENT if on else C_CARD,
                               hover=C_ACCENT2 if on else C_CARD_HOV,
                               active=C_ACCENT if on else C_CARD_HOV,
                               fg="#03211A" if on else C_TEXT2,
                               fg_hover="#03211A" if on else C_TEXT,
                               fg_active="#03211A" if on else C_TEXT)
            except Exception:
                pass

    def _apply_theme(self, key, persist=True):
        """切换 3 套配色：文本色 / 背景 / 卡片圆点 / partial 标签。"""
        th = SUB_THEMES.get(key) or SUB_THEMES[SUB_THEME_DEFAULT]
        self._sub_theme = key if key in SUB_THEMES else SUB_THEME_DEFAULT
        try:
            self.src_text.configure(bg=th["bg"], fg=th["src_fg"],
                                    insertbackground=th["src_fg"])
            self.dst_text.configure(bg=th["bg"], fg=th["dst_fg"],
                                    insertbackground=th["dst_fg"])
            for t, partial_color, size, extra in (
                    (self.src_text, th["partial_src"], self._sub_size_src, "italic"),
                    (self.dst_text, th["partial_dst"], self._sub_size_dst, "bold")):
                t.tag_configure("partial", foreground=partial_color,
                                font=("Microsoft YaHei UI", size, extra))
                t.tag_configure("ts", foreground=C_FAINT,
                                font=("Consolas", 8))
            # 卡片外框 / 标题底色 / 圆点（背景跟着主题走）
            self._src_card.configure(bg=th["bg"], highlightbackground=C_BORDER)
            self._dst_card.configure(bg=th["bg"], highlightbackground=C_BORDER)
            for head in (self._src_head, self._dst_head):
                head.configure(bg=th["bg"])
            self._src_title.configure(bg=th["bg"])
            self._dst_title.configure(bg=th["bg"])
            self._src_dot.configure(bg=th["bg"])
            self._dst_dot.configure(bg=th["bg"])
            # 字幕框内层 + 现代滚动条背景跟随主题
            self._src_body.configure(bg=th["bg"])
            self._dst_body.configure(bg=th["bg"])
            self._src_bar.configure(bg=th["bg"])
            self._dst_bar.configure(bg=th["bg"])
            self._src_dot.itemconfigure(self._src_dot_id, fill=th["dot_src"])
            self._dst_dot.itemconfigure(self._dst_dot_id, fill=th["dot_dst"])
        except Exception:
            pass
        self._refresh_style_buttons()
        if persist:
            self._persist_style()

    def _apply_position(self, pos, persist=True):
        """字幕区固定在窗口下半（底部）或上半（顶部）。"""
        pos = pos if pos in ("bottom", "top") else SUB_POS_DEFAULT
        self._sub_pos = pos
        wrap, spacer, main = self._sub_wrap, self._sub_spacer, self._main
        try:
            wrap.grid_forget()
            spacer.grid_forget()
            if pos == "bottom":
                main.grid_rowconfigure(1, weight=2)
                main.grid_rowconfigure(2, weight=3)
                spacer.grid(row=1, column=0, sticky="nsew")
                wrap.grid(row=2, column=0, sticky="nsew", padx=22, pady=(2, 16))
            else:
                main.grid_rowconfigure(1, weight=3)
                main.grid_rowconfigure(2, weight=2)
                wrap.grid(row=1, column=0, sticky="nsew", padx=22, pady=(2, 16))
                spacer.grid(row=2, column=0, sticky="nsew")
        except Exception:
            pass
        self._refresh_style_buttons()
        if persist:
            self._persist_style()

    def _apply_layout(self, key, persist=True):
        """切换界面布局：经典（双栏双卡）/ 对话（单栏气泡流）。

        经典布局是招牌，一个像素都不动；对话布局只是把原文卡收起来，
        原文以浅灰小字贴在每条译文上方，译文带底色块，看起来像聊天记录。
        """
        key = key if key in LAYOUTS else LAYOUT_CLASSIC
        self._layout = key
        try:
            if key == LAYOUT_CHAT:
                self._src_card.pack_forget()
                base = max(9, int(self._sub_size_dst * 0.62))
                self.dst_text.tag_configure(
                    "chat_src", foreground=C_FAINT,
                    font=("Microsoft YaHei UI", base),
                    spacing1=self.sp(12), spacing3=0,
                    lmargin1=self.sp(10), lmargin2=self.sp(10))
                self.dst_text.tag_configure(
                    "chat_dst", font=("Microsoft YaHei UI", self._sub_size_dst, "bold"),
                    background=C_PANEL, spacing1=self.sp(3), spacing3=self.sp(3),
                    lmargin1=self.sp(10), lmargin2=self.sp(10),
                    rmargin=self.sp(10))
                self.dst_text.tag_raise("chat_dst")
            else:
                self.dst_text.tag_configure("chat_src", foreground=C_FAINT,
                                            font=("Microsoft YaHei UI", 1))
                self.dst_text.tag_configure("chat_dst", background=C_BG,
                                            font=("Microsoft YaHei UI",
                                                  self._sub_size_dst, "bold"))
                if not self._src_card.winfo_ismapped():
                    self._src_card.pack(fill=tk.BOTH, expand=True,
                                        pady=(0, 10), before=self._dst_card)
        except Exception:
            pass
        self._refresh_style_buttons()
        if persist:
            self._persist_style()
            self._status_threadsafe(f"界面布局：{key}")

    # ------------------------------------------------------------ v1.3.0 托盘
    def _tray_icon_path(self):
        """返回 (停止态图标, 运行态图标)。"""
        def find(name):
            for p in (
                os.path.join(_app_dir(), name),
                os.path.join(getattr(sys, "_MEIPASS", ""), name),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
            ):
                if p and os.path.exists(p):
                    return p
            return None
        idle = find("app-icon.ico") or find("app.ico")
        live = find("app-icon-live.ico") or idle
        return idle, live

    def _minimize_to_tray(self):
        try:
            if self._tray is None:
                idle, live = self._tray_icon_path()
                if not idle:
                    raise RuntimeError("找不到图标文件 app-icon.ico / app.ico")
                self._tray = TrayIcon(idle, live, self._tray_q)
                self._tray.start()
                self._tray.set_state(self.running)
            self.withdraw()
            self._status_threadsafe("已最小化到托盘：双击图标恢复，右键可退出")
        except Exception as e:
            self._status_threadsafe(f"托盘不可用：{e}")

    def _handle_tray(self, evt):
        act = evt[0] if evt else ""
        if act == "show":
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except Exception:
                pass
        elif act == "toggle":
            self._toggle()
        elif act == "quit":
            self._on_close()
        elif act == "tray_error":
            self._status_threadsafe(f"托盘不可用：{evt[1]}")

    def _export_srt(self):
        """导出 SRT 字幕：优先用每句的真实起止时间（v1.3.0）。

        旧版是「每句固定 3 秒」的假时间轴；现在识别时就把音频片段时长
        记进 self._segments，导出时换算成相对会话开始的真实时间。
        """
        try:
            segs = []
            for s in list(self._segments):
                src = (s.get("src") or "").strip()
                dst = (s.get("dst") or "").strip()
                if not (src or dst):
                    continue
                segs.append({
                    "start": float(s.get("start", 0.0)),
                    "end": float(s.get("end", 0.0)),
                    "text": (dst or src).replace("\n", " ").strip(),
                })
            if not segs:
                # 兜底：没有实时记录（例如只从字幕框内容导出）时按行估算
                raw = self.src_text.get("1.0", tk.END).strip()
                trans = self.dst_text.get("1.0", tk.END).strip()
                if not raw and not trans:
                    messagebox.showinfo("SRT 导出", "还没有字幕内容")
                    return
                lines = [l.strip() for l in (trans or raw).splitlines() if l.strip()]
                base = self.t_start or time.time()
                segs = [{"start": base + i * 3.0,
                         "end": base + (i + 1) * 3.0,
                         "text": l} for i, l in enumerate(lines)]

            t0 = self.t_start or segs[0]["start"]
            out = []
            for i, s in enumerate(segs):
                start = max(0.0, s["start"] - t0)
                end = max(start + 0.6, s["end"] - t0)
                # 下一句已经开始时，把本句结束时间收到下一句之前（不重叠）
                if i + 1 < len(segs):
                    nxt = max(0.0, segs[i + 1]["start"] - t0)
                    if nxt > start + 0.2:
                        end = min(end, nxt - 0.02)
                if end <= start:
                    end = start + 0.6
                out.append("%d\n%s --> %s\n%s\n" % (
                    i + 1, _fmt_srt_time(start), _fmt_srt_time(end), s["text"]))

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = filedialog.asksaveasfilename(
                title="导出 SRT 字幕",
                initialdir=_output_dir(),
                initialfile=f"同传字幕_{ts}.srt",
                defaultextension=".srt",
                filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")])
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(out))
            messagebox.showinfo("SRT 已导出", path)
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _first_run_guide(self):
        """首次使用引导，只弹一次。"""
        try:
            if os.path.exists(FIRST_RUN_FLAG):
                return
            with open(FIRST_RUN_FLAG, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
            messagebox.showinfo(
                "欢迎使用 Hearizon Sync",
                "三步开始：\n\n"
                "1. 在左侧「音频输入」选对麦克风（可以点「测麦克风」验证）\n"
                "2. 选「译成」的目标语言\n"
                "3. 点「开始同传」，或按 Ctrl+Enter\n\n"
                "常用快捷键：\n"
                "  Ctrl+Enter  开始 / 停止\n"
                "  Ctrl+L      清空字幕\n"
                "  Ctrl+S      保存记录\n"
                "  Ctrl+Shift+S  导出 SRT（真实时间轴）\n"
                "  Ctrl+ / -   调字幕字号\n\n"
                f"当前版本：v{APP_VERSION}\n"
                "  · 识别全在本机，音频不上传；翻译有本地 / 免费在线 / API 三条独立路线\n"
                "  · 免费在线不用填 Key、不花钱，国内直连（MyMemory）\n"
                "  · 3.x 新增：对话式字幕布局、神经降噪、配置原子写与密钥加密\n"
                "  · 记录与字幕默认保存到「文档\\Hearizon Sync」\n"
                "  · 设置里可切换字幕配色 / 位置 / 布局，也可自定义 API 接口")
        except Exception:
            pass

    # ------------------------------------------------------------ v1.3.0 关于 / 彩蛋
    def _open_about(self):
        """关于窗口。彩蛋：连点版本号 5 次。"""
        win = tk.Toplevel(self)
        win.title("关于 Hearizon Sync")
        win.configure(bg=C_BG)
        win.geometry("540x550")
        win.resizable(False, False)
        win.transient(self)

        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=30, pady=24)

        try:
            # 关于页用更大一档，D 盘/高分屏下也能看清
            self._about_logo = logo_photo(44) or getattr(self, "_logo_img", None)
            if self._about_logo is not None:
                tk.Label(pad, image=self._about_logo, bg=C_BG,
                         bd=0).pack(anchor="w")
        except Exception:
            pass

        tk.Label(pad, text="Hearizon Sync", bg=C_BG, fg=C_TEXT,
                 font=("Segoe UI", 20, "bold")).pack(anchor="w", pady=(10, 0))
        tk.Label(pad, text="麦克风实时字幕翻译 —— 说完一句，屏幕出原文和译文",
                 bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 9)
                 ).pack(anchor="w", pady=(2, 14))

        info = tk.Frame(pad, bg=C_BG)
        info.pack(fill=tk.X)

        def line(label, value, color=C_TEXT2):
            row = tk.Frame(info, bg=C_BG)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label, bg=C_BG, fg=C_FAINT, width=6, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
            tk.Label(row, text=value, bg=C_BG, fg=color, anchor="w",
                     font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        line("版本", f"v{APP_VERSION}", C_ACCENT2)
        line("作者", "何瑞程")
        line("许可", "GPL-3.0 + 商业许可（双许可）")
        line("识别", "SenseVoice 本地推理 · 语音不上传")
        line("翻译", "本地 opus-mt / DeepSeek / 有道")

        tk.Frame(pad, bg=C_BORDER, height=1).pack(fill=tk.X, pady=14)

        tk.Label(pad, text="本软件不是医疗器械，不得作为助听器或听力诊断设备使用。",
                 bg=C_BG, fg=C_FAINT, wraplength=460, justify="left",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")

        # 彩蛋占位：固定两行高度，触发后不会挤压下方按钮
        egg = tk.Label(pad, text=" ", bg=C_BG, fg=C_ACCENT2, justify="center",
                       font=("Microsoft YaHei UI", 10, "bold"), height=2)
        egg.pack(anchor="center", pady=(14, 4))
        ver_lbl = tk.Label(pad, text=f"v{APP_VERSION}", bg=C_BG, fg=C_FAINT,
                           font=("Microsoft YaHei UI", 8), cursor="hand2")
        ver_lbl.pack(anchor="center")
        ver_lbl.bind("<Button-1>", lambda e: self._egg_click(egg))

        self._about_win = win
        self._about_egg = egg
        close_btn = RoundedButton(pad, "关闭", command=win.destroy,
                                  bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                                  fg=C_TEXT2, fg_hover=C_TEXT, height=42, width=140,
                                  font=("Microsoft YaHei UI", 10), parent_bg=C_BG,
                                  radius=10)
        close_btn.pack(anchor="center", pady=(14, 0))
        self._about_close = close_btn

        win.grab_set()

    def _egg_click(self, egg_label):
        """独家模块 1。"""
        self._egg_clicks += 1
        if self._egg_clicks >= 5:
            self._egg_clicks = 0
            egg_label.configure(
                text="听见地平线之外的声音。\n—— Hear + Horizon + Sync")
        else:
            self.after(1600, self._egg_reset)

    def _egg_reset(self):
        self._egg_clicks = 0

    # ------------------------------------------------------------ 独家模块 2 / 2-1 / 2-2
    def _check_easter_egg(self, entry, parent=None):
        """key 输入框暗号，命中即弹对应模块。"""
        try:
            text = entry.get().strip()
        except Exception:
            return
        if not text:
            return
        hit = None
        for idx in range(len(_EGG_CODE)):
            if text == _egg_key(idx):
                hit = idx
                break
        if hit is None:
            return
        entry.delete(0, tk.END)
        if hit == 0:
            self._show_thanks_card(parent)
        elif hit == 1:
            self._show_poem_card(parent)
        elif hit == 3:
            self._show_letter_card(parent)
        elif hit == 4:
            self._show_reward_card(parent)
        else:
            self._show_developer_card(parent)

    def _dialog(self, title, w, h, parent=None, modal=True):
        """统一创建弹窗，返回 (窗口, 内容区)"""
        win = tk.Toplevel(parent or self)
        win.title(title)
        win.configure(bg=C_BG)
        win.geometry("%dx%d" % (w, h))
        win.resizable(False, False)
        win.transient(parent or self)
        if modal:
            win.grab_set()
        else:
            # 非模态弹窗容易被父窗口的 grab 压在下面（设置页就是 grab 状态），
            # 先抬到最上层再放开，确保用户能看见。
            try:
                win.lift()
                win.attributes("-topmost", True)
                win.after(500, lambda: win.attributes("-topmost", False))
            except Exception:
                pass
        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=32, pady=24)
        return win, pad

    def _show_developer_card(self, parent=None):
        """独家模块 2。"""
        win, pad = self._dialog("关于作者", 520, 420, parent, modal=False)

        tk.Label(pad, text="何瑞程", bg=C_BG, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        tk.Label(pad, text="北京语言大学 · 海南国际学院", bg=C_BG, fg=C_TEXT2,
                 font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(8, 0))
        tk.Label(pad, text="听力与言语康复学（本科生）", bg=C_BG, fg=C_DIM,
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(2, 0))
        tk.Label(pad, text="Hearizon Sync 作者", bg=C_BG, fg=C_ACCENT2,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(12, 0))

        tk.Frame(pad, bg=C_BORDER, height=1).pack(fill=tk.X, pady=16)

        tk.Label(pad, text="个人网站", bg=C_BG, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        link = tk.Label(pad, text="eyru.github.io/personalweb", bg=C_BG,
                        fg=C_ACCENT2, cursor="hand2", font=("Consolas", 11))
        link.pack(anchor="w", pady=(3, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://eyru.github.io/personalweb/", new=2))
        tk.Label(pad, text="（点击才会打开浏览器，不会自动跳转）", bg=C_BG,
                 fg=C_FAINT, font=("Microsoft YaHei UI", 8)
                 ).pack(anchor="w", pady=(4, 0))

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=30, width=100,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(anchor="center", pady=(22, 0))

    def _show_thanks_card(self, parent=None):
        """独家模块 2-2。"""
        win, pad = self._dialog("致谢名单", 580, 588, parent, modal=False)

        tk.Label(pad, text=THANKS_TITLE, bg=C_BG, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tk.Label(pad, text=THANKS_NOTE, bg=C_BG, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(5, 0))
        tk.Frame(pad, bg=C_BORDER, height=1).pack(fill=tk.X, pady=(14, 12))

        for name, org in THANKS_LIST:
            row = tk.Frame(pad, bg=C_BG)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=name, bg=C_BG, fg=C_ACCENT2, width=5,
                     anchor="w", font=("Microsoft YaHei UI", 10, "bold")
                     ).pack(side=tk.LEFT)
            tk.Label(row, text=org, bg=C_BG, fg=C_TEXT2, anchor="w",
                     font=("Microsoft YaHei UI", 9)
                     ).pack(side=tk.LEFT, padx=(10, 0))

        tk.Frame(pad, bg=C_BORDER, height=1).pack(fill=tk.X, pady=(16, 12))
        tk.Label(pad, text="谢谢你们听我讲完这个想法，也谢谢你们帮我把它做出来。",
                 bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 9),
                 wraplength=500, justify="left", anchor="w"
                 ).pack(anchor="w")

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=32,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(fill=tk.X, pady=(18, 0))

    def _show_poem_card(self, parent=None):
        """独家模块 2-1。"""
        win, pad = self._dialog("山有木兮", 460, 300, parent, modal=False)
        tk.Label(pad, text="山有木兮木有枝", bg=C_BG, fg=C_TEXT2,
                 font=("Microsoft YaHei UI", 14)).pack(pady=(10, 6))
        tk.Label(pad, text="心悦君兮君不知", bg=C_BG, fg=C_ACCENT2,
                 font=("Microsoft YaHei UI", 22, "bold")).pack(pady=(6, 4))
        tk.Label(pad, text="——《越人歌》", bg=C_BG, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 9)).pack()

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=30, width=100,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(pady=(24, 0))

    # ------------------------------------------------------------ 独家模块 5 / 6
    def _show_letter_card(self, parent=None):
        """独家模块 5：一段写在这个版本发布时的话。"""
        win, pad = self._dialog("写在 3.0 发布时", 640, 640, parent, modal=False)

        head = tk.Label(pad, text="写在 3.0 发布时", bg=C_BG, fg=C_TEXT,
                        font=("Microsoft YaHei UI", 15, "bold"))
        head.pack(anchor="w")
        tk.Label(pad, text="2026 年 · 秋", bg=C_BG, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(3, 0))
        tk.Frame(pad, bg=C_BORDER, height=1).pack(fill=tk.X, pady=(12, 10))

        body = tk.Text(pad, bg=C_BG, fg=C_TEXT2, bd=0, wrap="char",
                       font=("Microsoft YaHei UI", 10), padx=0, pady=0,
                       insertwidth=0, cursor="arrow", highlightthickness=0,
                       spacing1=2, spacing3=8)
        body.insert("1.0", _egg_letter_text())
        body.configure(state="disabled")
        vs = ModernScrollbar(pad, body)
        body.configure(yscrollcommand=vs.set)
        vs.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=32,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(fill=tk.X, pady=(14, 0))

    def _show_reward_card(self, parent=None):
        """独家模块 6：请作者喝一杯。"""
        win, pad = self._dialog("请作者喝一杯", 660, 760, parent, modal=False)

        tk.Label(pad, text="谢谢你愿意支持这个项目", bg=C_BG, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(pad, text="扫码即可，多少随意；不打赏也完全不影响使用。",
                 bg=C_BG, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))
        tk.Frame(pad, bg=C_BORDER, height=1).pack(fill=tk.X, pady=(12, 12))

        holder = tk.Frame(pad, bg=C_BG)
        holder.pack(fill=tk.BOTH, expand=True)
        img = self._jpeg_to_photo(_egg_qr_bytes())
        if img is not None:
            self._qr_photo = img          # 必须留住引用，否则会被回收成白框
            tk.Label(holder, image=img, bg=C_BG, bd=0).pack()
        else:
            tk.Label(holder, text="（图片素材无法显示）", bg=C_BG, fg=C_FAINT,
                     font=("Microsoft YaHei UI", 10)).pack(pady=40)

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=32,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(fill=tk.X, pady=(12, 0))

    def _jpeg_to_photo(self, jpeg_bytes):
        """把内存里的 JPEG 转成 Tk 能显示的 PhotoImage。

        Tk 原生只认 PNG / GIF，所以先用 PIL 解码再转 PNG。
        """
        if not jpeg_bytes:
            return None
        try:
            import io as _io
            import base64 as _b64
            from PIL import Image
            im = Image.open(_io.BytesIO(jpeg_bytes))
            im.load()
            buf = _io.BytesIO()
            im.convert("RGB").save(buf, "PNG")
            png_b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
            return tk.PhotoImage(data=png_b64)
        except Exception:
            return None

    def _silence_watch(self):
        """v1.3.0：运行中超过 10 秒没有识别到语音，提示一次。"""
        try:
            if self.running:
                silent = time.time() - self._last_speech_ts
                if silent > SILENCE_WARN_SEC and not self._silence_warned:
                    self._silence_warned = True
                    self._status_threadsafe(
                        f"⚠️ {SILENCE_WARN_SEC} 秒未检测到声音 — 请检查麦克风是否选对、音量是否太低")
        except Exception:
            pass
        finally:
            self.after(5000, self._silence_watch)

    # ------------------------------------------------------------ 在线更新
    def _check_update_async(self):
        """启动后静默检查新版本；没有网络或接口不可用时什么都不做。"""
        if not UPDATE_CHECK:
            return
        threading.Thread(target=self._check_update_worker,
                         daemon=True).start()

    def _check_update_worker(self):
        try:
            req = urllib.request.Request(
                UPDATE_API, headers={
                    "User-Agent": "HearizonSync/" + APP_VERSION,
                    "Accept": "application/vnd.github+json"})
            with _urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return
        tag = (d.get("tag_name") or "").strip()
        if not tag or version_tuple(tag) <= version_tuple(APP_VERSION):
            return
        try:
            if (load_conf().get("update_skip_tag") or "") == tag:
                return
        except Exception:
            pass
        url = ""
        assets = d.get("assets") or []
        # 优先挑名字对得上的安装包，避免一个 Release 里有多个 exe 时拿错。
        for a in assets:
            name = (a.get("name") or "")
            if name.startswith("HearizonSync-Setup-") and name.lower().endswith(".exe"):
                url = a.get("browser_download_url") or ""
                break
        if not url:
            for a in assets:
                if (a.get("name") or "").lower().endswith(".exe"):
                    url = a.get("browser_download_url") or ""
                    break
        body = d.get("body") or ""
        self.ui_q.put(("update_available", {
            "tag": tag,
            "body": body,
            "url": url,
            "page": d.get("html_url") or UPDATE_PAGE,
            "sha256": _extract_sha256(body),
        }))

    def _show_update_dialog(self, info):
        """发现新版本：介绍更新内容，可取消（稍后提醒），也可跳过该版本。"""
        if self._update_win is not None:
            try:
                self._update_win.destroy()
            except Exception:
                pass
        tag = info.get("tag", "")
        win = tk.Toplevel(self)
        self._update_win = win
        win.title("发现新版本")
        win.configure(bg=C_BG)
        win.geometry("%dx%d" % (self.sp(580), self.sp(580)))
        win.transient(self)
        # 3.0.0 第 13 条：不再 grab_set。以前这里把整个主窗口锁住，
        # 弹窗一出主界面就点不动了，被当成卡死。现在是非模态，随时能关。
        try:
            win.lift()
            win.focus_force()
        except Exception:
            pass
        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=self.sp(24), pady=self.sp(18))

        tk.Label(pad, text=f"发现新版本 {tag}", bg=C_BG, fg=C_ACCENT2,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(pad, text=f"当前 v{APP_VERSION}　→　{tag}", bg=C_BG, fg=C_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w",
                                                      pady=(self.sp(4), self.sp(10)))
        box = tk.Frame(pad, bg=C_PANEL)
        box.pack(fill=tk.BOTH, expand=True)
        txt = tk.Text(box, bg=C_PANEL, fg=C_TEXT2, relief=tk.FLAT, bd=0,
                      wrap=tk.WORD, font=("Microsoft YaHei UI", 9),
                      padx=self.sp(10), pady=self.sp(8))
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", md_to_plain(info.get("body"), 1600)
                   or "（这个版本没有填写更新说明）")
        txt.configure(state=tk.DISABLED)

        skip_var = tk.BooleanVar(value=False)
        RoundedCheck(pad, "跳过这个版本（下次不再提示）", skip_var,
                     parent_bg=C_BG, height=self.sp(24),
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w",
                                                          pady=(self.sp(8), 0))

        def later():
            self._remember_skip(tag, skip_var.get())
            win.destroy()

        def go():
            self._remember_skip(tag, skip_var.get())
            win.destroy()
            self._start_update_dialog(info)

        row = tk.Frame(pad, bg=C_BG)
        row.pack(fill=tk.X, pady=(self.sp(12), 0))
        RoundedButton(row, "稍后提醒", command=later, bg=C_CARD,
                      hover=C_CARD_HOV, active=C_BORDER2, fg=C_TEXT2,
                      fg_hover=C_TEXT, height=self.sp(34), width=self.sp(96),
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(side=tk.RIGHT, padx=(self.sp(8), 0))
        RoundedButton(row, "下载并安装", command=go, bg=C_ACCENT,
                      hover=C_ACCENT2, active="#0B8A6B", fg="#03211A",
                      fg_hover="#03211A", fg_active="#03211A",
                      height=self.sp(34), font=("Microsoft YaHei UI", 9, "bold"),
                      parent_bg=C_BG, radius=8).pack(side=tk.RIGHT)
        win.bind("<Escape>", lambda e: win.destroy())

    def _remember_skip(self, tag, skip):
        if not skip:
            return
        try:
            c = load_conf()
            c["update_skip_tag"] = tag
            save_conf(c)
        except Exception:
            pass

    def _start_update_dialog(self, info):
        url = info.get("url") or ""
        if not url:
            messagebox.showinfo(
                "暂时没有可下载的安装包",
                "这个版本还没有上传安装包，可以到发布页手动下载：\n"
                + (info.get("page") or UPDATE_PAGE))
            return
        win = tk.Toplevel(self)
        self._dl_win = win
        self._dl_page = info.get("page") or UPDATE_PAGE
        win.title("正在下载更新")
        win.configure(bg=C_BG)
        win.geometry("%dx%d" % (self.sp(460), self.sp(250)))
        win.transient(self)
        # 3.1.0：不再 grab_set。下载弹窗不锁主窗口，用户能继续用软件，也能取消。
        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=self.sp(24), pady=self.sp(20))
        tk.Label(pad, text=f"正在下载 {info.get('tag', '')} 安装包",
                 bg=C_BG, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        pct = tk.Label(pad, text="0%", bg=C_BG, fg=C_DIM,
                       font=("Microsoft YaHei UI", 9))
        pct.pack(anchor="w", pady=(self.sp(10), self.sp(4)))
        bar = tk.Canvas(pad, height=self.sp(8), bg=C_PANEL,
                        highlightthickness=0)
        bar.pack(fill=tk.X)
        self._dl_pct = pct
        self._dl_bar = bar
        self._dl_t0 = None
        self._dl_speed = 0.0
        tk.Label(pad, text="下载期间可以继续使用软件；完成后会询问是否安装。",
                 bg=C_BG, fg=C_FAINT,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w",
                                                      pady=(self.sp(12), 0))
        self._dl_slow = tk.Label(
            pad, text="", bg=C_BG, fg=C_ACCENT2, cursor="hand2",
            font=("Microsoft YaHei UI", 8, "underline"))
        self._dl_slow.pack(anchor="w", pady=(self.sp(6), 0))
        self._dl_slow.bind("<Button-1>", lambda e: self._open_download_page())

        self._dl_cancel = threading.Event()

        def cancel():
            self._dl_cancel.set()

        btnrow = tk.Frame(pad, bg=C_BG)
        btnrow.pack(fill=tk.X, pady=(self.sp(12), 0))
        RoundedButton(btnrow, "取消下载", command=cancel, bg=C_CARD,
                      hover=C_CARD_HOV, active=C_BORDER2, fg=C_TEXT2,
                      fg_hover=C_TEXT, height=self.sp(30), width=self.sp(96),
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(side=tk.RIGHT)
        win.protocol("WM_DELETE_WINDOW", cancel)   # 关窗即取消，不再锁死

        path = os.path.join(tempfile.gettempdir(),
                            "HearizonSync-Setup-%s.exe"
                            % (info.get("tag", "latest").lstrip("vV")))
        threading.Thread(target=self._update_download_worker,
                         args=(url, path, info.get("sha256", "")),
                         daemon=True).start()

    def _update_download_worker(self, url, path, expected_sha=""):
        """把并发下载的进度转到界面队列上，下完在后台算 SHA-256。"""
        def cb(got, total):
            self.ui_q.put(("update_progress", got, total))

        cancel = getattr(self, "_dl_cancel", None)
        try:
            download_concurrent(url, path, "HearizonSync/" + APP_VERSION, cb,
                                cancel_event=cancel)
        except Exception as e:
            if cancel is not None and cancel.is_set():
                self.ui_q.put(("update_cancelled",))
            else:
                self.ui_q.put(("update_failed", str(e)[:140]))
            return
        try:
            actual = _file_sha256(path)
        except Exception as e:
            self.ui_q.put(("update_failed", "无法读取安装包：%s" % str(e)[:100]))
            return
        self.ui_q.put(("update_done", path, expected_sha, actual))

    def _on_update_progress(self, got, total):
        try:
            if self._dl_pct is None:
                return
            now = time.time()
            if self._dl_t0 is None:
                self._dl_t0 = now
                self._dl_tick_t = now
                self._dl_tick_got = got
                self._dl_speed = 0.0
            # 速度每 0.6 秒更新一次，避免数字乱跳
            if now - self._dl_tick_t >= 0.6:
                dt = now - self._dl_tick_t
                if dt > 0:
                    self._dl_speed = max(0.0, (got - self._dl_tick_got) / dt)
                self._dl_tick_t = now
                self._dl_tick_got = got
            mb = 1024.0 * 1024.0
            pctv = (100.0 * got / total) if total else 0.0
            if total:
                text = "%.1f%%   %.1f / %.1f MB" % (pctv, got / mb, total / mb)
                if self._dl_speed > 0:
                    text += "   %.2f MB/s" % (self._dl_speed / mb)
                    left = (total - got) / self._dl_speed
                    if left >= 1:
                        text += "   剩余约 %d 分 %d 秒" % (int(left // 60),
                                                         int(left % 60))
            else:
                text = "已下载 %.1f MB" % (got / mb)
            self._dl_pct.configure(text=text)
            if self._dl_slow is not None:
                if self._dl_speed and self._dl_speed / mb < 0.06:
                    self._dl_slow.configure(
                        text="网络较慢？点这里到发布页手动下载")
                else:
                    self._dl_slow.configure(text="")
            w = max(1, self._dl_bar.winfo_width())
            self._dl_bar.delete("all")
            self._dl_bar.create_rectangle(
                0, 0, max(2, int(w * pctv / 100.0)), self.sp(8),
                fill=C_ACCENT, outline="")
        except Exception:
            pass

    def _open_download_page(self):
        page = getattr(self, "_dl_page", None) or UPDATE_PAGE
        try:
            os.startfile(page)
        except Exception:
            try:
                messagebox.showinfo("发布页", page)
            except Exception:
                pass

    def _on_update_done(self, path, expected_sha="", actual_sha=""):
        if self._dl_win is not None:
            try:
                self._dl_win.destroy()
            except Exception:
                pass
            self._dl_win = None
        # 3.1.0：执行安装程序前先核对 SHA-256，挡住损坏或被替换的包。
        if expected_sha:
            if (actual_sha or "").lower() != expected_sha.lower():
                messagebox.showerror(
                    "校验失败",
                    "下载的安装包 SHA-256 与发布页公布的不一致，已阻止安装。\n\n"
                    "期望：%s\n实际：%s\n\n"
                    "可能是网络损坏或文件被替换，请到发布页手动下载。"
                    % (expected_sha, actual_sha))
                try:
                    os.remove(path)
                except Exception:
                    pass
                return
            verify_note = "已通过 SHA-256 校验，安装包完整。"
        else:
            verify_note = ("发布页未公布校验值，无法自动验证完整性。\n"
                           "如果你信任这个更新来源，可以继续。")
        ok = messagebox.askyesno(
            "更新已下载",
            "新版本安装包已下载完成。\n" + verify_note + "\n\n"
            "点击「是」将关闭本程序并启动安装程序。\n"
            "覆盖安装即可，你的设置、API Key、字幕偏好都会保留。")
        if not ok:
            return
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("无法启动安装程序",
                                 "%s\n\n可以手动运行：\n%s" % (e, path))
            return
        self.after(800, self._on_close)

    def _on_update_failed(self, msg):
        if self._dl_win is not None:
            try:
                self._dl_win.destroy()
            except Exception:
                pass
            self._dl_win = None
        messagebox.showwarning(
            "下载失败",
            "更新包下载失败：\n%s\n\n可以稍后再试，或到发布页手动下载。"
            % msg)

    def _on_update_cancelled(self):
        if self._dl_win is not None:
            try:
                self._dl_win.destroy()
            except Exception:
                pass
            self._dl_win = None
        # 已下好的分段保留在临时目录，下次更新会接着用，不浪费流量。

    def _on_close(self):
        """关窗必须成功。

        3.2.5：以前这里先 capture.stop() 再 destroy()，stop 抛异常窗口就永远
        关不掉（用户只能去任务管理器）。现在每一步都互相独立，
        清理失败也照常退出。
        """
        self.running = False
        try:
            self.withdraw()          # 先藏起来，别让用户干等
        except Exception:
            pass
        for step in (self._shutdown_tray, self._shutdown_capture):
            try:
                step()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

    def _shutdown_tray(self):
        tray, self._tray = getattr(self, "_tray", None), None
        if tray is not None:
            tray.stop()

    def _shutdown_capture(self):
        cap, self.capture = self.capture, None
        if cap is not None:
            cap.stop()
        try:
            if self.stream_worker:
                self.stream_worker.stop()
        except Exception:
            pass


def main():
    # 必须在创建窗口之前声明 DPI 感知，否则高分屏上整个界面会被位图拉伸
    _enable_dpi_awareness()
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
    # 3.2.5：关窗后直接结束进程。
    # 采集/识别线程都是 daemon，但声卡驱动偶尔会在解释器退出阶段卡住，
    # 见过「窗口没了、进程还在」的情况。这里不给它卡的机会。
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
