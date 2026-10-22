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
    双击「HearizonSync.bat」，或 .venv\\Scripts\\python.exe HearizonSync.py
"""

import os
import sys
import json
import math
import queue
import threading
import time
import hashlib
import uuid
import urllib.parse
import urllib.request
import traceback
import ctypes
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
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAbpElEQVR42o2bd7xdZZnvv8+71i6n"
    "lySkJ4RASCAYAkkMSuiiAoKoNEX0g723jx2FEUbvzNULzOhwR/TC6KCgcikfQVERI4mJJNSQQkkj"
    "IT3n5NTd1nqf+8d6V9vnZOaeT/bnnOy99lpvecrv+T2/V2Z+9SMc7eeMaceeu3Xg8KUHRkfOrtaC"
    "xSONKnXbILQWVMEIAtHfACrRb1FUFZHocwuICBpfBxgxgKBqAc081SCiyTviviMKagTUYFWj54pN"
    "Hi1qMB6UPI9SoUBrqfXZztaWlce2dTy84cC+J442RxlvAc45bsEF6/bu/vTew/svG9GQMLRgLYJg"
    "vOiJVqNhi3upCuIWQCWalKoiCCpuAdBkriKCiKRzVyW6mviO0T+x7hnRfaIFcM+NF8rdwxhQBKsh"
    "agy+59Eihim9vQ8tPWbWD5/c+cqfmufqdZ15eu6Nk6bMuOHJXa/etWvwyPxqvYYJAwyCGEXiDcbt"
    "pnupxuNVN0lxc7DpSku0iyjuPgZUUBFU3UK4C5KvSToudWuCSvbt7F5ijI/neQiKWIuGAXVr6avX"
    "5+8ZGbhuwcRpYd/o8F+PugCnTZt5x6rd27/SN9iPF1o8I2AMKvFcNXk1P5ymYaU7GZm6iS1ETGS6"
    "seWrImjkNphoMZ0FGVWMW2yVeOLRgos0Pc/9P3Y9RBBj8BU8axmuVjgcVM9bPPXYKYeGBx8ZswBL"
    "px337b/s2vblgcEBCp4HIljVjIVmJq3pfOP3m/clGsQ4y5N5P1kmcVeIjT6WdPniv41KbF8uTuSH"
    "kl+QzDOMARE8hEq1xuFGbcmZs0/QPQN9K50dwgeXn3X++kO7/uHI0DAlv5gEHXLhSUA9UBP5e/TV"
    "3OpHE7DRrokiomCi34JFsC4+gJF4p+O7x4Etvp9Jli6OH9GYhNxcYxfM/FibsTC3SSpQMgWODAzy"
    "zL7X/uHjy887P7GAI9XKv2w+fHCeb0NUYr/WbGDGSDoYcSucmJqku6CJs2f2WOKdl6yJpG8bQcVz"
    "H6vz8/h6QXHPF0HEIHH2EQWs+7+kmUdIF84FFJHInY0Ig406gYa9/SPDvzTXLznrnFf3H7wkGK0h"
    "YlBr00hNNDjRKP0kfoY6XzZuUE3mLoKReNGSbU2v1UzqjP1WbTTYpt3UxL1c/CDOHJK61DgBMc7O"
    "kfEbF5wVTwxBvcHLB/Zf8tkzLzzHazv7jZ/ffHDfcrFhFLyb7ClKYXHO1ox/u9XnaAEp9W/JmHM0"
    "HWFsKNdMJjBJmhTyrpi6W5QxJIlAkhlHcrUbu0s/mDhnUxWlIP6A2TvYf3Y9rOF5BmvDMf5kklig"
    "ZEI32NRN8pkh2qGxWSN9qTRFl6xl5TZRM66jme/o2DwpmWdI1rqswyPR39ZG2a1er/P6kb6z/b6h"
    "wVM1sOB7ObAisXm5tBSl9mjw0Vqqy+Ga2a0oeKnG8TpOSTbN45kFM8akcaTJGuJFi61HIzwZpU2R"
    "zEZper1k3IV8VhAkijWAMQKhpa8yfKoZqFYREUJr0x0FrCrWQd5kA0062DgvJ5NIYoN15thk4Dbv"
    "8/m/00VkXM+wmYCpmdQqufuoHYtR4vtaZ5XR/QTPGAarFfxKUI9SUi76aAxGsVECclCVJCKPBUNk"
    "QexYE82AFGNMYgVxUMyZf+zPMSTMpb/4M819p3nxVLOWlK9F1CqeEYaCOr7FOvNyF4U2mUQchNQN"
    "wDrnVpExxU0OOwhpKnM7mAc++cVJFiKeiAqe8fBMMcoOCKEqSoBai9VgTG2RcyMXOo6yR1GqVrAC"
    "flTUGYfBbbK7WbAWB2fJGJ2MswiaAjoQ57duIRQHjBDUythVS9KnRyO0hJUKBI3UbgEKBUyxiOd5"
    "ickbUpCkMr7lZS1EXIUZVZbga+x/GbNPrM7tdlrCShIfcnBXs/4sbvUVMc27kM3PaSo0InjGp1av"
    "Qm2UcmsrJ8+cw/GTp3JMdy8Gy4Ej/by0fy+b9u+hOjwE5TLFQhEbWjde4wrvo/+kKdRggFBDfIn9"
    "T135qdn0BqomKUKas4+1dkxg0+yOqQEJmzF1ap8CRqLsUxseYtqESVz7lrO4csmbWDhjDqVCITeB"
    "ar3Oxt07+MVTT3Lv31ey58hhim0dWKuECgbjzM+6gJzLK66IMg41unhS+ORValFMBoXF5oWAipcL"
    "jJGpp3Yi7mtq4nihCF6C79FMTCHyO1ytYIxBrEdQGeH9Z5zLNy+7ihOnTE8GfXhokCPVUQyGntZW"
    "utvak8827d7JN+//Tx5cv4pSRyc2tkzRscUbaRAVVRQPYzwCFPE+dbVG2dtCaBMTl2i5sJJirSSY"
    "aVp6mjjiGFetWQX1MEYRl3tEBDWSASQODIlgKzVuvPRqvn35ezEiNMKQB9at4rcvrGfLntcYqlTB"
    "GHrb2pk/bRbvXLyMS05dhohQbzS46f6f871Hfx0tglosNAGyTOlK6FzPxxjBGoPIp65Qz5EMolEV"
    "pWPCqKBiMikldZt4oaJcqlgL4kJTAgkdABGriTka3ycYGuYb77iKW664DkHYsGsHX733p/x50/PU"
    "VKHgQ60WBUPfB69Eh+fxloWn8v33foQ5x0xBVfnyL3/KDx77v5Q7OwltiKqganKoUdU66xAED2Oi"
    "OXmy9KSbRDUhGsZUI87487HMJiFfnK9lslFSj2sWrBgwCAaDXyjQGBnhracs5c7rP4NnDKtf3sTl"
    "/3ozz7y2Hb+tg1JLKwi85ZTTuf7st1IJG7zedxBbLrFx11Yef34dZ81fxOSublaceDLP7HiVLbt3"
    "UmgpO9ClqeNJ6sLiahsRwQKeeePCm0TSNCIqYxGbkKxeUqFpjAo1qiIl729KtqRNb+N5htAq7X6B"
    "uz7yeWb2TmTbgf2851+/x46BQ7R3dmPV0ggDisbw+Bdu5KJFS7hm6Vl0FIqs2bIBr7WNPQN9rN78"
    "Apedfga97R28YeYcfrN+NcONepTVTJYcidFmtBOeeNEGGcFjyYKbcnk9U843V2TikFUaYTWB3c2M"
    "jDP63CIKUPCL1AYHufqMc/joOW8jtJav33c3f9r4NG29vQRhiAKeb7BhiAmV02YfR2dbG2fOX8iJ"
    "U2fw8PpVSLnMnkMHODA4wLuWvInJXT0cGBpk9aYX8MutEXMdo8cEaLl4ZhwXicGTJQtuikvJOPjJ"
    "eAgteekY8CIxL5CzAI0JcVfKC74YbGjxPI+b33Ut86dOZ8PuHXzh3ruQcgkEarUqokpYrRIEAau3"
    "vsTvnl7LrIlTmDdlGgumz6K7tY2Hn1pNS2cXm17bxmmz53L8lOnMnTSFe55axUijhnHZS0QRMz5/"
    "GAEvMRm210XmHFudTtZKmsaaSYME3SVkXVx92ASgqBFClLJfoFyIqLdfrVnJ6OgQhVKRanWUC+ef"
    "yuOfu5GffuDTLJ9zAoiyof8AV/zouzz07FoAPnbexbz79DdRGRmhqpY7//IYQRgwd/JULl64GB0d"
    "xfdNQrmpo9yTl6aB0fOWLbyJDCOfM33JYHeT2HqCGj1HOGLjCduEeIiMQlLWSOMH+lTrVTbt3sHz"
    "u3bwkzVPEHguL49W+NyF7+DKZSs4dfZcrlm2gsODAzz92quEfoEnN77AOxe/kd72DqZ29fDA+r9R"
    "NXDgSD8XnLSI6T0TaIQBv1n3N0yp5DYgYocji/SS8cdh0TNLTrpJM8VPbPZKBgaLRMFL0ypMM2Uz"
    "GArGJGhRvEzjwyE9g0STDEOM77O7/yDrXtlIzffBc2nY83n+te3UahVOnDKDnvYO3nbKEjbseJWX"
    "9u5msDpKIwy5eNFSZvRO5IktL7Lt8EEqlQrzp87kjBPmUyoW+eVTqxipVfFMMclskqGyjeMNLAZj"
    "Rd0bJCWvtRZCxbjKEBHq1Rq+DSgJFCH9bYQiAfVG1dFPJBYRNU+ChNBsVKuURSio0lZuobOnl1bP"
    "pwVDUK8TqrJ7cIBv3HcX7779ZnYcOoDveXzz0quZWG5FfI/fvbienQf343ke5y84BU8tYpT1215C"
    "VTmms4v5k6ei9TrGjxlpyVWiGiNSUXwjhqTXpGlJEcNXYwyNapWLTlnKDRe/h/ZSiUYYJqWoAgXP"
    "4x8f+RW/XruSlu4J2FAJNQBVfN9HrFKvVrjlXe/jslOXUanXksrSGI9yocCLr7/GZ++7iwPDg7RO"
    "msSqLc9x8wP3cOeHPs9ps4/nzAVv4IFn/saBgSP8besWZk+azOLZx1MsFKioZfuh/QxURulqbWNm"
    "7yTQjdEuu7nF2QAxGf4RfJoqNgW8uDw2jhBthHzgzPM444QFaFO8iH9+9P5Pcqi/nydeeBrp7MIY"
    "H0SojVRgdJgbLn8v37zkyqNWagumz+Jna57gt8/+HS0WMN0TuP+FddxwcD9zJk9h2ey5PPD0WkbD"
    "kB2HDgAws3ci5VIrlSCkf3SYSr1Gd2sbXS2tEKb1cVT8SAJnNJMFfKsGEZtAWhFBbcThRX5rQC1r"
    "t7zIe05/E9ZaHl67imde30lraytdpTIfPOsCJnV08fNPfIV//u2vePiZdewePoJvDIumTueD57yN"
    "T5z3dgB2HNzHz1c9TqFYYrQyzLuXnMnCWcex70gfm1/fhRQLhFh8v8BwtcZL+3YzZ/IUpnT1UPA8"
    "GmHAkcooAJ1tbZQLPlhLLQxd7odioZCk6hxnke1NqCBq8McjD5S06WFtCOUiDzyzli9d/B6m9Uyg"
    "IfCPv/kZdLQijZCNr+/k1vd9hOm9E7n9uk/yxYsPcmiwH2M8ZvROZFJHFwA7Dx3g6n/7n/x922Zo"
    "1Dlu6gw+ct4leMbw0HPr2LZ/L8X21ihGu8nYLNvs0GjcbwhtiNUozRb8oiN2oBGEuYJNHKscxyLN"
    "tNyNly1dMkSHaFS8aBhSLrWy48A+/uPJPyHAVW86m29f/QEIGrT19nDHX3/Ppbd9hzWvbGG0Xmf2"
    "hEmcPmcei2fPZVJHF30jwzy4fjUXfv9bPLVnN+WOTtq6e7j7Y19hZu9E+oaH+D8rHwPPICbq/4c2"
    "oFwoMHPiMQD0jQzSsBYRQ29bR1Iuj9aqoEpPSxstxRKqMDAyCKKZ9p6m3Sl1bTqxWFF8q0EW0brI"
    "ncf1YWgxrW384PcPcNGiZZw6ew43XHYNBwcHueOPD1Hs7eGPr25i5Q9u4IJ5J3PanBOY0XsMgQ3Z"
    "fmAfa1/ZwqptmyiU2/AIKTWUH3/oc6yYdxIAt//hYZ7e9jKl7l5CG1AoFhgdGuT044/nxCnTUVU2"
    "vP4ahEpbS5m5k6ZEFnX4APV6HULLrN4JdLa0MFSrsLu/D4yfpj81+YZp2tmNSFExcfs6i+BSV7BY"
    "/GKBvuooH/rJrTz4+W8zc8JEbn//x5nVO5Fbfvsr6kGDRrnMI5ue4ZEXn3aVlQEbgl/AlFpojAyy"
    "cNosbr3241xw8qkA3LdmJT945NcUOzqxNsAYIQxDUOULF15K0ffZcXA/q17aiBjD1J4JLJ87D4D1"
    "O7fTqEeM06LZc/GM4eDQAFv278EUi1irCdEngLUx/e5FvYq4O2zI9/eae25iItVFqbWVZ3Zv55p/"
    "+yd2HtxPwfP42qVX8cTXvsf1bzyHCaUWin4p8kX3fb9QoOT5zO3u5ebLr+Wxr343mfz961bz4bt/"
    "RMX3wUTm32gE1I4McNM73svFi5YB8Iu1K9l26CCqyoUL3sC0ngnUGnVWvvwiVpTOtnZWnHgyCjy3"
    "YysHBo5QKBYI1OZ0AyksDhwdJ/jNwS+JmM3NS4l2ptzezuqtm7n4f93Ibe/9KBecchpLj5vH0o99"
    "ia37X2fN1pd4ae/rDIwMI8YwqaubxTPnsHzuAiZ0RL47WBnlzid+x40P30uj4FEyRcIgJCTkmNYO"
    "/ul9H+f9bz4PMcLqlzfz/ccegoLPxHIbnzr/IgAe37yBF3ZsA4Glx85jybHHI8CjL6wHDRHjoWEj"
    "D+eTQiidm5/ULa7fN16jQWMqGWiEDQrt7Ww8uI+Lbr+Fj624gOtWXMDS4+Yxd/J05k6eftRcP1qr"
    "8ZctG/iXxx7ksc0vQLEIoxWkVKRUKFAfGOZr77yW61acD8BT217mgz+5lYGwga1W+M4VH2DB1BlU"
    "GnX+/YlHGaxVKRjDNcvPoq1UYk//IR7d8DSUWwjCMFf1ab5rnzRjfDEGUUGsEmaZ1IwliII1qSbA"
    "2pBiuUxoLT/800Pc99RfOWvBIlbMW8jCGbOY3t1La7EEKIPVKq/1HeLZnVt5fONzPPnKZgIbUmhp"
    "YVpnD8tnzeX3m59jOGggxSKPbFjP6XNOYO3WLfzzo/dzqFZDK6N85q2X8eFzIyxx75q/8PvnnwbP"
    "sPTY47ly2ZsB+NnqP7Ov7yDFjh5CG46T3B1DF5frIoj3mas0TnnZL4lm6gOrWM8k8pVYeGDE4Hke"
    "tXoNrVRBhK5yC23lVoqFAopSq9cZrtQYrlXAQLmljWKxzODwAPd//Ku8Y/FSvvPgvdzy0D209PRQ"
    "qVbpLZboq9VBDEXgS+dfwrcuv4aWQpH121/hotu+w5FGA4KAhz79dd7+htN5vf8QZ3/3a2zrP0yp"
    "WCLQfLNNJVWYGIlYaSsGX23cdbUp85VrWRPl54wwIaoyjZOiWIpeAdNRRNUyEloGRoddaRwBj4Lx"
    "aOloT3qC1UYVPMPU7h4Kns/C6ccCHhIqraUyg9ZSKregQYMfv++jfODNkUu8sGsn19zxffrqdcLK"
    "CF98+7t42ymnYdVy+x8eZuu+PZQ7e2jY4CiSiQylJx4iiolbYtqcIhMaQ5JmgmSYE81cbK0S2JBQ"
    "FTGGou9RLBQpFosUCz7iGUK1NMKQwIYRj1BvsHX/XlSVqT3ddJRbCMIARfFECMIAD2X5sScA8Oiz"
    "67j01pvZPjxA2Khx7kmL+NalVyEirNy8kZ/88Xf4LW0OGaYmLBnKvrkBjIIx+BjHkooxSeNQE8mK"
    "Tbo42RQZaftStYc6ltWihECIJq+4/Zq0YY0HVlm/41VEhNNmz2XJnLnURyv44qEKHoZKo84V//59"
    "rr3zNq788Q/YUxnGBjUWTZ3Ff3z4C3S3tHFgcIAv3PNj+sMGXqkYtfldHzJ6ScoFSao8ifqJijEa"
    "FTxiZExZKJrKeZo1gqkfZRSyGRYp6i7Z8Vh2CAOkVOTh59ZxaGiQ9nILX7/kKrpKRYYqQ3ie4Am0"
    "lFrZsH8P96xdSc0YGrVRTp82m/s//XVm9k6kETT41F0/5PnXtlFsbyNoBEmZbq1BrWQUY16e5QLU"
    "GDyWnXyT5sw6ny6kqYDIAIbMhXlBRHahJKcFTHmGol/gYP8hCsbn/JMXcdwxUzhh6gzWvLKZvr7D"
    "BEGDIAgSs+0UuHrZ2fzkQ59l1sRjqDUafO4/7+Bnq/9MubsnulaOolfMyq2ybiwSdYaSoWmkziTU"
    "RO0aE0pW8wDCGOM+C8e0ysdaiuTFVCIYBbWWQhhw1/Vf5MrlKwDYtn8v96xZyd+3v0zf8CDFUpGT"
    "p8/i8kXLuGDhaQD0jwzx5Xvu5KdP/oFCdw/Y5qaujm3tmLhZm0p1rQHxPnVFJO2zkgQQyQqgIq0c"
    "TZ2PfJcoU3fnlGSpzKyJcI1AV8EYGkGDNvG45d3X8ekL3pGuulWOVEYoFYq0FIvJ9//+6ha+/qu7"
    "eWLT8xS6uqIoo8aJSTQP4FTHpcJFbLQAnkH8T16lkQ7AJu1149hUq9aJolzAG08Z8t8ugHHVZqz3"
    "FTA2UZ8ZYwjDkLBW5+0LF3P9igt587yTOaarK6n7h6oVNuzawb1r/sIvnlrJ4ZERiq0dhDaI9EOS"
    "0vIp1JWj6oVwC4BnEP8TV2l0k0g7IzpWzBTz6UcTKKcZ0Y41P6cvkETqFGnvjGblvJFIul6pYIww"
    "pXsi8yZOprPcSq1RY2d/H9sO76Ner0C5lZJfjDpI2bHGlV2sYdQ8/M2p2Vy32qKOEZImZYlGvmuN"
    "OXokbxY/jdtWzYqdNCeyzqq+Y6FFqb0NC+wZ7mNP3/60NvF9vGKBcnsnodqE+sopxN05heh5ZhyP"
    "lby+0FpUJCJFRdMonvRAHUdoXH5P4quRcZQkmksf0YBM2iKLR6NhGhNipVdmF8MgQAV8v4ApliLW"
    "WCOaPpHtJSVtRkQbu6WmTVmJkWhOI2Jd8U/i2r7xon5dMiDHwxkbi+SaTy7EO5fig0xecTuTtRrN"
    "cIx+4iYxtkqdLKaronUMNapLPE1rWc1OvknNnpPciRNuqE0ULhH4EWd5FlWDJ4Lp8AtpFewWQHRc"
    "e046YNli0cS+x1jskBtgKj/Np8qMNmnMEYxmhXmiY7CpOINx8Ie1448sycKK1YAuv4DpLLcmitBE"
    "6hLjaKtNgtPsJBUTQ824IyTWHWRylYRTjUYgyzVJRXMt94SsJGrKWte1USdojKF0bheaqpUozYSO"
    "5rKpKrmJDZKEG4zen9jVjelp73hOPElBnaoDL5FFW5VxBIcayVBI9fkGG9UfY14Wo/FnmryPMbmX"
    "GI/4odba9KVNSjTJaBZj6k0EwXcqVMmkwqwwQpOzCBoqYjyG6tXnzJze3pWlQpkwDHOHorJwEWOa"
    "9L3RYGxidnYMhyCJiWpO/J1zG0l7iJpFo6FTo8e+ruQWQhLmWhIXSgteM/4RgtzmWYzvsXekf6VZ"
    "Pn3ugxMKRULCnJFnz/EkK2nDaGKSlpNWoyN1cXAKYxWJbSYkwGIiQaNKskASa3oJIytyTI0nBh+D"
    "h+eAVCamOHZK3T2MU6//f/8YD8IAz5MHvS2zuncc1zNhye6RoXlO4pWvCsWdCcyRiwbjxeL4PJss"
    "xqRH44xkZafpwQtpIqoSRCTuTINkDpzJGPW7SiqYzp0ckfH7lk2ydSiVULzfTm9p/54BOPv4+bdN"
    "75pAEISIO8uXxQUm9nXjg/GTs0Gp6ipFWFGNLQk1nqR915GH0N3UaQdN8wk0SUkMV3Jr7IoirmHr"
    "YpTjK9KzSua/nrxV8KKmra0FtyWHpp7bs2v78mnH6t7R4XNHKxU8z+SPo4nj1GIzTCBmOkCcCCJb"
    "biYB1cSEi7smsYKoSSEOnOTrJxmjVMmWuyaO7CY+uzTe4b0m/bwBSiXCRnjjzK6Ou3PnBncP9q9c"
    "PvPYKftGR5ZU6jU8THJSLCEV47ya6zKNPR2QWwQ3OGn+XoZlHo+IyAKb8WaVOX2EJbW+cTu9uMm3"
    "lKAe/O8ZHR1fGffk6N6hwUcWT5kWDgSN80atJbQOVIjBSFYCl3aMZBwSIg54kukQxcdVbHoaLqfc"
    "UIctxgiv1URFWnyWMAsg3D1jON+MWKNdN1AugucRKt+a0dbx1f/y7PDB0ZG/vmXOiatrNuyq1xvz"
    "g4KHJW5DJ2pKJ5iWBOGpZmlHyWhLx6sucQcwU4Qo2eMzmkpZ1En0U4g69sBVrP6OS+O4gKJQiDJM"
    "wTwkAZ+Z0dp29397eBpgx0DftkYY3Lt0xpy/Bhr2i0ipiExFrRNeaAKUEs1wZsq5yrCpvE7xRCpV"
    "i4NcfMLL5HqTWUI7cxxUxtYnEcqMYoIKz6qR+yQ035je2vY/OorFbeOFhv8H6GzC4DMTCAUAAAAA"
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
# ---- v1.2.0 新增 ----
APP_VERSION = "2.0.1"
SUB_SIZE_DST = 18          # 译文默认字号
SUB_SIZE_SRC = 14          # 原文默认字号
SUB_SIZE_MIN, SUB_SIZE_MAX = 14, 32
FIRST_RUN_FLAG = os.path.join(os.path.expanduser("~"), ".hearizon_sync_seen")

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


def _fmt_srt_time(seconds):
    """秒 → SRT 时间戳 HH:MM:SS,mmm（v1.3.0：真实时间轴用）"""
    ms = max(0, int(round(float(seconds) * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


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
        super().__init__(master, width=self.WIDTH, bg=bg or C_BG,
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

    def __init__(self, anchor, values, current, on_choose):
        super().__init__(anchor)
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
            item = tk.Label(outer, text=v, bg=C_PANEL,
                            fg=(C_ACCENT2 if is_cur else C_TEXT2),
                            anchor="w", font=("Microsoft YaHei UI", 9),
                            padx=14, pady=7)
            item.pack(fill=tk.X)
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
        super().__init__(master, bg=parent_bg or master.cget("bg"),
                         highlightthickness=0, bd=0, height=height, width=80)
        self._var = variable
        self._values = list(values)
        self._cmd = command
        self._font = font
        self._radius = radius
        self._placeholder = placeholder
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
        self.create_text(13, h / 2, text=val, fill=C_TEXT, anchor="w",
                         font=self._font)
        ax = w - 15
        self.create_line(ax - 4, h / 2 - 2, ax, h / 2 + 2, fill=C_DIM,
                         width=2, capstyle=tk.ROUND)
        self.create_line(ax, h / 2 + 2, ax + 4, h / 2 - 2, fill=C_DIM,
                         width=2, capstyle=tk.ROUND)

    def set_values(self, values):
        self._values = list(values)

    def _open(self, _e=None):
        if not self._values:
            return
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
        self._popup = ChoicePopup(self, self._values, self._var.get(), self._choose)

    def _choose(self, value):
        self._popup = None
        self._var.set(value)
        if self._cmd:
            try:
                self._cmd()
            except Exception:
                pass


class RoundedCheck(tk.Canvas):
    """自绘圆角复选框（GPU 加速开关）。"""

    def __init__(self, master, text, variable, command=None,
                 parent_bg=None, height=26, font=("Microsoft YaHei UI", 9)):
        super().__init__(master, bg=parent_bg or master.cget("bg"),
                         highlightthickness=0, bd=0, height=height, width=90)
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

    NIM_ADD, NIM_DELETE = 0, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
    MF_STRING, MF_SEPARATOR = 0x0, 0x800
    TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10

    def __init__(self, icon_path, event_q, tip="Hearizon Sync"):
        self._icon_path = icon_path
        self._q = event_q
        self._tip = tip[:127]
        self._hwnd = None
        self._nid = None
        self._thread = None
        self._ready = threading.Event()
        self._user32 = None
        self._shell32 = None
        self._kernel32 = None
        self._wndproc_cb = None

    # ---------- 对外 ----------
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

    def _load_icon(self):
        hicon = self._user32.LoadImageW(
            None, self._icon_path, self.IMAGE_ICON, 16, 16, self.LR_LOADFROMFILE)
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
        nid.hIcon = self._load_icon()
        nid.szTip = self._tip
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
        u.AppendMenuW(menu, self.MF_STRING, 2, "开始 / 停止同传")
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
        self.title(f"Hearizon Sync v{APP_VERSION}")
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
        self._segments = []           # SRT 用：每句的真实起止时间
        self._tray = None             # 托盘对象（第一次用到才创建）
        self._tray_q = queue.Queue()  # 托盘线程 → 主线程消息
        self._restore_style()         # 读回上次的配色 / 位置

        self.after(900, self._first_run_guide)
        self.after(5000, self._silence_watch)

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

        # ============ 左侧栏（v1.3.0：内容超出时上下滚动）============
        side_outer = tk.Frame(shell, bg=C_SIDEBAR, width=252)
        side_outer.pack(side=tk.LEFT, fill=tk.Y)
        side_outer.pack_propagate(False)

        side_canvas = tk.Canvas(side_outer, bg=C_SIDEBAR, highlightthickness=0,
                                bd=0, width=244)
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

        # ④ 翻译引擎
        self.tr_mode = tk.StringVar(value=TRANSLATOR)
        self.tr_cb = field("翻译引擎", self.tr_mode, TRANSLATOR_CHOICES,
                           self._on_translator_change)

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
        SelectField(f3, variable=dom_var,
                    values=["general", "medicine", "computers", "finance", "game"],
                    parent_bg=C_BG, height=30,
                    font=("Microsoft YaHei UI", 9)).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

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

        RoundedButton(pad, "保存有道设置", command=save_youdao,
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

        # ---- GPU 说明 ----
        title("GPU 加速说明")
        hint("• Whisper 识别、本地 opus-mt 翻译：可用 GPU（需 NVIDIA 显卡）\n"
             "• SenseVoice 识别：官方 pip 包为 CPU 编译，无法 GPU，但它本身就极快（RTF 0.02）\n"
             "• 开启后下次识别会重新加载模型")

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=30,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(fill=tk.X, pady=(10, 0))

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
        self.dev_cb.set_values(names)
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
        self._last_speech_ts = time.time()
        self._silence_warned = False
        dev = self._picked_device()
        self.running = True
        self.t_start = time.time()
        self.btn_start.set_text("■ 停止")
        self.btn_start.set_colors(bg=C_RED, hover="#ef4444", active="#b91c1c",
                                  fg="#FFFFFF", fg_hover="#FFFFFF",
                                  fg_active="#FFFFFF")
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
        self.btn_start.set_text("▶  开始同传")
        self.btn_start.set_colors(bg=C_ACCENT, hover=C_ACCENT2, active="#0B8A6B",
                                  fg="#03211A", fg_hover="#03211A",
                                  fg_active="#03211A")
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
        seg["dst"] = full            # 回填译文，SRT 导出时优先用译文
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
            self._last_speech_ts = time.time()
            self._silence_warned = False
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
        # 从音频线程调用，只改一条线，开销极小
        try:
            w = max(2, int(min(1.0, rms / 0.15) * 130))
            self.level_canvas.coords(self.level_bar, 2, 3, w, 3)
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
        self._segments = []          # 清空时间轴记录（SRT 导出用）
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
        """启动时读回上次的配色 / 位置（配置坏了就退回默认）。"""
        try:
            c = load_conf()
            theme = c.get("sub_theme")
            pos = c.get("sub_pos")
        except Exception:
            theme, pos = None, None
        self._apply_theme(theme if theme in SUB_THEMES else SUB_THEME_DEFAULT,
                          persist=False)
        self._apply_position(pos if pos in ("bottom", "top") else SUB_POS_DEFAULT,
                             persist=False)

    def _persist_style(self):
        try:
            c = load_conf()
            c["sub_theme"] = self._sub_theme
            c["sub_pos"] = self._sub_pos
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

    # ------------------------------------------------------------ v1.3.0 托盘
    def _tray_icon_path(self):
        for p in (
            os.path.join(_app_dir(), "app-icon.ico"),
            os.path.join(_app_dir(), "app.ico"),
            os.path.join(getattr(sys, "_MEIPASS", ""), "app.ico"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"),
        ):
            if p and os.path.exists(p):
                return p
        return None

    def _minimize_to_tray(self):
        try:
            if self._tray is None:
                ico = self._tray_icon_path()
                if not ico:
                    raise RuntimeError("找不到图标文件 app-icon.ico / app.ico")
                self._tray = TrayIcon(ico, self._tray_q,
                                      tip=f"Hearizon Sync v{APP_VERSION}")
                self._tray.start()
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
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                f"同传字幕_{ts}.srt")
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
                "v2.0.1 新增：\n"
                "  · 全新界面：圆角按钮 / 自绘下拉 / 现代滚动条\n"
                "  · 字幕样式：3 套配色 + 贴底 / 贴顶\n"
                "  · 系统托盘：点「最小化到托盘」后台运行\n"
                "  · 关于页：左下角「关于」，连点版本号有惊喜")
        except Exception:
            pass

    # ------------------------------------------------------------ v1.3.0 关于 / 彩蛋
    def _open_about(self):
        """关于窗口。彩蛋：连点版本号 5 次。"""
        win = tk.Toplevel(self)
        win.title("关于 Hearizon Sync")
        win.configure(bg=C_BG)
        win.geometry("540x470")
        win.resizable(False, False)
        win.transient(self)

        pad = tk.Frame(win, bg=C_BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=30, pady=24)

        try:
            if getattr(self, "_logo_img", None) is not None:
                tk.Label(pad, image=self._logo_img, bg=C_BG, bd=0).pack(anchor="w")
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

        # 彩蛋占位（默认空，连点版本号 5 次出现）
        egg = tk.Label(pad, text="", bg=C_BG, fg=C_ACCENT2, justify="center",
                       font=("Microsoft YaHei UI", 10, "bold"))
        egg.pack(anchor="center", pady=(18, 4))
        ver_lbl = tk.Label(pad, text=f"v{APP_VERSION}", bg=C_BG, fg=C_FAINT,
                           font=("Microsoft YaHei UI", 8), cursor="hand2")
        ver_lbl.pack(anchor="center")
        ver_lbl.bind("<Button-1>", lambda e: self._egg_click(egg))

        RoundedButton(pad, "关闭", command=win.destroy,
                      bg=C_CARD, hover=C_CARD_HOV, active=C_BORDER2,
                      fg=C_TEXT2, fg_hover=C_TEXT, height=30, width=110,
                      font=("Microsoft YaHei UI", 9), parent_bg=C_BG,
                      radius=8).pack(anchor="center", pady=(16, 0))

        win.grab_set()

    def _egg_click(self, egg_label):
        """彩蛋：连点版本号 5 次。"""
        self._egg_clicks += 1
        if self._egg_clicks >= 5:
            self._egg_clicks = 0
            egg_label.configure(
                text="听见地平线之外的声音。\n—— Hear + Horizon + Sync")
        else:
            self.after(1600, self._egg_reset)

    def _egg_reset(self):
        self._egg_clicks = 0

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

    def _on_close(self):
        try:
            if getattr(self, "_tray", None) is not None:
                self._tray.stop()
                self._tray = None
        except Exception:
            pass
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
