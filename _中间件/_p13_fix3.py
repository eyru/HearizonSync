#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.1.0 修复：① 日韩改走 Whisper（SenseVoice 的 language 参数实测无效）
                ② 状态栏超长文字滚动（修 local 提示显示不全）"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

p = 'HearizonSync.py'
s = open(p, encoding='utf-8').read()
n = 0


def rep(a, b):
    global s, n
    if a in s:
        s = s.replace(a, b, 1); n += 1
    else:
        raise SystemExit("未命中: " + a[:80])


# ---------------------------------------------------------------- 1) 日韩归到 Whisper
rep('''# 这些语言 SenseVoice 不支持，选了必须切 whisper
SV_UNSUPPORTED = {"德语", "法语", "意大利语", "西班牙语"}''',
'''# 这些语言必须走 Whisper。原因分两类：
#   ① 德/法/意/西：SenseVoice 模型本身不支持
#   ② 日/韩：实测 sherpa-onnx 1.13.8 的 from_sense_voice(language=...) 参数【无效】
#      （传入 ko 仍输出 en 标签），模型永远走自动检测，韩语会被误判成日语。
#      Whisper 的 language 参数可靠，所以日韩也归到这边。
SV_UNSUPPORTED = {"日语", "韩语", "德语", "法语", "意大利语", "西班牙语"}''')

# ---------------------------------------------------------------- 2) 注释里同步说明
rep('''# 每项：(界面显示名, SenseVoice 代码, Whisper 代码)
#   SenseVoice 原生支持：中 / 英 / 日 / 韩 / 粤
#   德 / 法 / 意 / 西 必须走 Whisper 通道''',
'''# 每项：(界面显示名, SenseVoice 代码, Whisper 代码)
#   SenseVoice 模型原生支持：中 / 英 / 日 / 韩 / 粤
#   ⚠️ 但本版 sherpa-onnx 的 language 参数无效，日/韩锁定不住（会混淆），
#      所以实际走向：中/英/粤 → SenseVoice；日/韩/德/法/意/西 → Whisper
#   德 / 法 / 意 / 西 SenseVoice 模型本身不支持''')

# ---------------------------------------------------------------- 3) 状态栏滚动（跑马灯）
rep('''        self.status_lbl = tk.Label(st, text="未开始", bg=C_SIDEBAR, fg=C_DIM,
                                   font=("Microsoft YaHei UI", 9))
        self.status_lbl.pack(side=tk.LEFT, padx=6)''',
'''        self.status_lbl = tk.Label(st, text="未开始", bg=C_SIDEBAR, fg=C_DIM,
                                   font=("Microsoft YaHei UI", 9), anchor="w")
        self.status_lbl.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        # 跑马灯状态（超长文字滚动显示）
        self._marquee_job = None
        self._marquee_src = ""
        self._marquee_pos = 0''')

# ---------------------------------------------------------------- 4) 状态写入改走 marquee
rep('''    def _status_threadsafe(self, text):
        self.ui_q.put(("status", text))''',
'''    def _status_threadsafe(self, text):
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
        self._marquee_job = self.after(150, self._marquee_tick)''')

# ---------------------------------------------------------------- 5) UI 泵里的 status 改用 _set_status
rep('''        if kind == "status":
            self.status_lbl.configure(text="● " + msg[1], fg=C_ACCENT)''',
'''        if kind == "status":
            self._set_status(msg[1], C_ACCENT)''')

# ---------------------------------------------------------------- 6) 启停/异常状态也用 _set_status
rep('''        self.status_lbl.configure(text="● 启动中...", fg=C_YELLOW)''',
    '''        self._set_status("启动中...", C_YELLOW)''')
rep('''        self.status_lbl.configure(text="● 已停止", fg=C_DIM)''',
    '''        self._set_status("已停止", C_DIM)''')

open(p, 'w', encoding='utf-8').write(s)
print(f"修复 {n} 处")
