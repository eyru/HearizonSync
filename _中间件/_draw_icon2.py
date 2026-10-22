#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自绘图标 v2：5 根细声波条（白面积 ~5%），小尺寸单独用 3 根粗条保证清晰"""
import io, sys, struct, base64
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from PIL import Image, ImageDraw
import numpy as np

S, SS = 1024, 4
GRID = S * SS
TOP, BOTTOM = (17, 126, 103), (4, 68, 55)
WHITE = (248, 252, 251)
RADIUS = int(S * 0.235)


def lerp(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def build(bars, bar_w, gap):
    """bars: [(idx, hfrac)]；返回 S×S RGBA"""
    canvas = Image.new('RGBA', (GRID, GRID), (0, 0, 0, 0))
    cd = ImageDraw.Draw(canvas)
    for y in range(GRID):
        cd.line([(0, y), (GRID, y)],
                fill=lerp(TOP, BOTTOM, (y / (GRID - 1)) ** 1.15) + (255,))

    mask = Image.new('L', (GRID, GRID), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, GRID - 1, GRID - 1],
                                           radius=RADIUS * SS, fill=255)
    canvas.putalpha(mask)

    wave = Image.new('L', (GRID, GRID), 0)
    wd = ImageDraw.Draw(wave)
    cx = cy = S / 2
    for idx, hf in bars:
        x = cx + idx * gap
        h = hf * S
        w2 = bar_w * S / 2
        wd.rounded_rectangle([(x - w2) * SS, (cy - h / 2) * SS,
                              (x + w2) * SS, (cy + h / 2) * SS],
                             radius=w2 * SS, fill=255)

    white = Image.new('RGBA', (GRID, GRID), WHITE + (255,))
    white.putalpha(Image.composite(wave, Image.new('L', (GRID, GRID), 0), mask))
    return Image.alpha_composite(canvas, white).resize((S, S), Image.LANCZOS)


# 三套分级：小尺寸用粗条保证可辨，大尺寸用细条求精致
SMALL = build([(-1, 0.290), (0, 0.540), (1, 0.290)],
              bar_w=0.135, gap=0.250)      # 16 / 24
MID   = build([(-1, 0.270), (0, 0.500), (1, 0.270)],
              bar_w=0.115, gap=0.225)      # 32 / 48
BIG   = build([(-2, 0.165), (-1, 0.255), (0, 0.370), (1, 0.255), (2, 0.165)],
              bar_w=0.072, gap=0.108)      # 64+

icon = BIG.resize((512, 512), Image.LANCZOS)
icon.save('icon.png')
print('✅ icon.png 512×512')

a = np.asarray(icon.convert('RGBA')); rgb = a[..., :3].astype(int); al = a[..., 3]
op = al > 128
wh = (rgb[..., 0] > 195) & (rgb[..., 1] > 205) & (rgb[..., 2] > 200) & op
print(f'   白色占比: {wh.sum()/op.sum()*100:.1f}%')
h = w = 512
yy, xx = np.mgrid[0:h, 0:w]
r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / (w / 2)
for lo, hi, name in [(0, 0.3, '中心'), (0.3, 0.6, '中环'), (0.6, 0.85, '外环'), (0.85, 1.2, '最外')]:
    m = op & (r >= lo) & (r < hi)
    if m.sum():
        print(f'   {name}: 白 {(wh & m).sum()/m.sum()*100:5.1f}%')
e = op & (r >= 0.97)
print(f'   边缘环: 白 {(wh & e).sum()/e.sum()*100:.2f}%')


def chain(im, target):
    cur = im
    while cur.width > target * 2:
        cur = cur.resize((max(target, cur.width // 2),) * 2, Image.LANCZOS)
    return cur.resize((target, target), Image.LANCZOS)


def clean(im, lo=24, hi=232):
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
entries = []
print(f"\n{'尺寸':>5} {'源':>4} {'四角a':>7} {'白%':>6} {'掩码':>7}")
for s in SIZES:
    base = SMALL if s <= 24 else BIG
    ssrc = '小' if s <= 24 else '大'
    img = clean(chain(base, s))
    px = img.load(); arr = np.asarray(img)
    corner = int(arr[0, 0, 3])
    o = arr[..., 3] > 128
    wh2 = (arr[..., 0] > 195) & (arr[..., 1] > 205) & (arr[..., 2] > 200) & o
    wr = f'{wh2.sum()/max(1,o.sum())*100:.1f}%'

    if s >= 256:
        buf = io.BytesIO(); img.save(buf, format='PNG', optimize=True)
        data = buf.getvalue(); kind = 'PNG'; mr = '-'
    else:
        rows = []
        for y in range(s - 1, -1, -1):
            row = bytearray()
            for x in range(s):
                r_, g_, b_, a_ = px[x, y]
                row += bytes([b_, g_, r_, a_])
            rows.append(bytes(row))
        pd = b''.join(rows)
        rowb = ((s + 31) // 32) * 4
        mask = bytearray(); ones = 0
        for y in range(s - 1, -1, -1):
            bits = bytearray(rowb)
            for x in range(s):
                if px[x, y][3] < 128:
                    bits[x // 8] |= (0x80 >> (x % 8)); ones += 1
            mask += bytes(bits)
        mr = f'{ones/(s*s)*100:.1f}%'
        bih = struct.pack('<IiiHHIIiiII', 40, s, s * 2, 1, 32, 0,
                          len(pd) + len(mask), 0, 0, 0, 0)
        data = bih + pd + bytes(mask); kind = 'BMP'
    entries.append((s, data, kind))
    print(f'{s:>5} {ssrc:>4} {corner:>7} {wr:>6} {mr:>7}')

out = bytearray(); out += struct.pack('<HHH', 0, 1, 7)
off = 6 + 16 * 7
for s, data, kind in entries:
    d = 0 if s >= 256 else s
    out += struct.pack('<BBBBHHII', d, d, 0, 0, 1, 32, len(data), off); off += len(data)
for s, data, kind in entries:
    out += data
open('app.ico', 'wb').write(bytes(out))
print(f'\n✅ app.ico ({len(out):,} B)')

logo = clean(chain(BIG, 64))
buf = io.BytesIO(); logo.save(buf, format='PNG', optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode('ascii')
open('_中间件/_logo_b64.txt', 'w').write(b64)
print(f'✅ UI logo base64 ({len(b64)} 字符)')

print('\n=== 白底/黑底角落检验 ===')
for s in SIZES:
    t = Image.open('app.ico'); fr = []
    try:
        while True:
            fr.append(t.copy()); t.seek(t.tell() + 1)
    except EOFError:
        pass
    im = next((f for f in fr if f.size[0] == s), None)
    if im is None:
        continue
    im = im.convert('RGBA')
    wb = np.asarray(Image.alpha_composite(Image.new('RGBA', im.size, (255,) * 4), im).convert('RGB'))
    bb = np.asarray(Image.alpha_composite(Image.new('RGBA', im.size, (0, 0, 0, 255)), im).convert('RGB'))
    wc = wb[0:2, 0:2].reshape(-1, 3).mean(axis=0); bc = bb[0:2, 0:2].reshape(-1, 3).mean(axis=0)
    print(f'  {s:>3}: 白底={tuple(int(v) for v in wc)} 黑底={tuple(int(v) for v in bc)}  '
          f'{"OK" if wc.min() >= 250 and bc.max() <= 5 else "FAIL"}')
