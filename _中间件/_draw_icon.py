#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自绘 Hearizon Sync 图标：深绿圆角底 + 纤细白色声波（白面积 ~7%，小尺寸不糊）
输出：icon.png(512) / app.ico(7尺寸) / 打印 logo base64
"""
import io, sys, struct, base64
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

S = 1024            # 母版边长
SS = 4              # 超采样倍数
GRID = S * SS

# ---------------- 配色（与软件 UI 同源：#016E59 系）
TOP    = (16, 122, 100)      # 上：亮一点的墨绿
BOTTOM = (4,  72,  58)       # 下：深墨绿
WHITE  = (247, 252, 250)     # 象牙白
RADIUS = int(S * 0.235)      # 圆角半径


def lerp(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


# ---------------- 1) 背景渐变 + 圆角遮罩（超采样）
canvas = Image.new('RGBA', (GRID, GRID), (0, 0, 0, 0))
cd = ImageDraw.Draw(canvas)

for y in range(GRID):
    k = y / (GRID - 1)
    # 轻微非线性，让上半部更亮
    kk = k ** 1.15
    cd.line([(0, y), (GRID, y)], fill=lerp(TOP, BOTTOM, kk) + (255,))

mask = Image.new('L', (GRID, GRID), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [0, 0, GRID - 1, GRID - 1], radius=RADIUS * SS, fill=255)

# ---------------- 2) 声波：3 根圆头竖条
bars = [
    (-1, 0.255),
    (0,  0.470),
    (1,  0.255),
]
BAR_W = 0.082 * S
GAP   = 0.105 * S          # 条中心间距
cx = cy = S / 2

wave = Image.new('L', (GRID, GRID), 0)
wd = ImageDraw.Draw(wave)
for idx, hfrac in bars:
    x = cx + idx * GAP
    h = hfrac * S
    w2 = BAR_W / 2
    box = [(x - w2) * SS, (cy - h / 2) * SS, (x + w2) * SS, (cy + h / 2) * SS]
    wd.rounded_rectangle(box, radius=w2 * SS, fill=255)

# ---------------- 3) 合成：底色 + 白色声波（声波只在圆角内）
body = canvas
body.putalpha(mask)

white_layer = Image.new('RGBA', (GRID, GRID), WHITE + (255,))
white_layer.putalpha(Image.composite(wave, Image.new('L', (GRID, GRID), 0), mask))
body = Image.alpha_composite(body, white_layer)

# ---------------- 4) 缩到母版尺寸
icon = body.resize((S, S), Image.LANCZOS)
icon = icon.resize((512, 512), Image.LANCZOS)     # 交付用 512
icon.save('icon.png')
print(f'✅ icon.png 512×512')

# ---------------- 5) 统计（白面积、边缘）
a = np.asarray(icon.convert('RGBA'))
rgb = a[..., :3].astype(int); al = a[..., 3]
op = al > 128
white = (rgb[..., 0] > 230) & (rgb[..., 1] > 230) & (rgb[..., 2] > 230) & op
print(f'   白色占比: {white.sum()/op.sum()*100:.1f}%  （旧图标 9.7%，中心 35%）')
h = w = 512
yy, xx = np.mgrid[0:h, 0:w]
r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / (w / 2)
for lo, hi, name in [(0, 0.3, '中心'), (0.3, 0.6, '中环'), (0.6, 0.85, '外环'), (0.85, 1.2, '最外')]:
    m = op & (r >= lo) & (r < hi)
    if m.sum():
        print(f'   {name}: 白 {(white & m).sum()/m.sum()*100:5.1f}%')
edge = op & (r >= 0.97)
print(f'   边缘环: 白 {(white & edge).sum()/edge.sum()*100:.2f}%  平均RGB={rgb[edge].mean(axis=0).round(0)}')


# ---------------- 6) 生成 ICO（多级缩放 + alpha 清理 + 正确掩码）
def resize_chain(im, target):
    cur = im
    while cur.width > target * 2:
        cur = cur.resize((max(target, cur.width // 2),) * 2, Image.LANCZOS)
    return cur.resize((target, target), Image.LANCZOS)


def clean_alpha(im, lo=24, hi=232):
    a = np.asarray(im).copy()
    al = a[..., 3]
    al = np.where(al < lo, 0, np.where(al > hi, 255, al)).astype(np.uint8)
    a[..., 3] = al
    body = a[..., :3][al > 200]
    fill = body.mean(axis=0).round().astype(np.uint8) if len(body) else np.array(BOTTOM, np.uint8)
    t = al == 0
    a[t, 0], a[t, 1], a[t, 2] = fill[0], fill[1], fill[2]
    return Image.fromarray(a, 'RGBA')


SIZES = [16, 24, 32, 48, 64, 128, 256]
big = icon.convert('RGBA')
entries = []
print(f"\n{'尺寸':>5} {'类型':>4} {'四角alpha':>12} {'白占比':>7} {'掩码透明':>8}")
for s in SIZES:
    img = clean_alpha(resize_chain(big, s))
    px = img.load()
    arr = np.asarray(img)
    corners = [int(arr[0, 0, 3]), int(arr[0, -1, 3]), int(arr[-1, 0, 3]), int(arr[-1, -1, 3])]
    o = arr[..., 3] > 128
    wh = (arr[..., 0] > 230) & (arr[..., 1] > 230) & (arr[..., 2] > 230) & o
    wratio = f'{wh.sum()/max(1,o.sum())*100:.1f}%'

    if s >= 256:
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        data = buf.getvalue(); kind = 'PNG'; mratio = '-'
    else:
        rows = []
        for y in range(s - 1, -1, -1):
            row = bytearray()
            for x in range(s):
                r_, g_, b_, a_ = px[x, y]
                row += bytes([b_, g_, r_, a_])
            rows.append(bytes(row))
        pixel_data = b''.join(rows)
        rowb = ((s + 31) // 32) * 4
        mask = bytearray(); ones = 0
        for y in range(s - 1, -1, -1):
            bits = bytearray(rowb)
            for x in range(s):
                if px[x, y][3] < 128:
                    bits[x // 8] |= (0x80 >> (x % 8)); ones += 1
            mask += bytes(bits)
        mratio = f'{ones/(s*s)*100:.1f}%'
        bih = struct.pack('<IiiHHIIiiII', 40, s, s * 2, 1, 32, 0,
                          len(pixel_data) + len(mask), 0, 0, 0, 0)
        data = bih + pixel_data + bytes(mask); kind = 'BMP'
    entries.append((s, data, kind))
    print(f'{s:>5} {kind:>4} {str(corners):>12} {wratio:>7} {mratio:>8}')

out = bytearray()
out += struct.pack('<HHH', 0, 1, len(entries))
off = 6 + 16 * len(entries)
for s, data, kind in entries:
    d = 0 if s >= 256 else s
    out += struct.pack('<BBBBHHII', d, d, 0, 0, 1, 32, len(data), off)
    off += len(data)
for s, data, kind in entries:
    out += data
open('app.ico', 'wb').write(bytes(out))
print(f'\n✅ app.ico ({len(out):,} B)')

# ---------------- 7) UI logo base64（64×64）
logo64 = clean_alpha(resize_chain(big, 64))
buf = io.BytesIO(); logo64.save(buf, format='PNG', optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode('ascii')
open('_中间件/_logo_b64.txt', 'w').write(b64)
print(f'✅ UI logo base64 已生成（{len(b64)} 字符）→ _中间件/_logo_b64.txt')

# ---------------- 8) 白底/黑底检验
print('\n=== 白底/黑底 角落检验 ===')
for s in SIZES:
    tmp = Image.open('app.ico'); fr = []
    try:
        while True:
            fr.append(tmp.copy()); tmp.seek(tmp.tell() + 1)
    except EOFError:
        pass
    im = next((f for f in fr if f.size[0] == s), None)
    if im is None:
        continue
    im = im.convert('RGBA')
    wb = np.asarray(Image.alpha_composite(Image.new('RGBA', im.size, (255,)*4), im).convert('RGB'))
    bb = np.asarray(Image.alpha_composite(Image.new('RGBA', im.size, (0, 0, 0, 255)), im).convert('RGB'))
    wc = wb[0:2, 0:2].reshape(-1, 3).mean(axis=0)
    bc = bb[0:2, 0:2].reshape(-1, 3).mean(axis=0)
    ok = wc.min() >= 250 and bc.max() <= 5
    print(f'  {s:>3}: 白底={tuple(int(v) for v in wc)} 黑底={tuple(int(v) for v in bc)}  {"OK" if ok else "FAIL"}')
