#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用「豆包原图」重新生成 app.ico / icon.png —— 只做技术修复，不改设计
技术点：多级 LANCZOS 缩放 / alpha 清理 / AND 掩码用 alpha<128 / 透明区 RGB 填主体色
"""
import io, sys, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from PIL import Image, ImageFilter
import numpy as np

SRC = '_中间件/icon-backup.png'      # 1280×1280 豆包原图
SIZES = [16, 24, 32, 48, 64, 128, 256]

src = Image.open(SRC).convert('RGBA')
print('源（豆包原图）:', src.size)


def chain(im, target):
    cur = im
    while cur.width > target * 2:
        cur = cur.resize((max(target, cur.width // 2),) * 2, Image.LANCZOS)
    return cur.resize((target, target), Image.LANCZOS)


def clean(im, lo=24, hi=232):
    """低 alpha 彻底透明、高 alpha 拉满，中间保留作抗锯齿；透明区 RGB 填主体色"""
    a = np.asarray(im).copy()
    al = a[..., 3]
    al = np.where(al < lo, 0, np.where(al > hi, 255, al)).astype(np.uint8)
    a[..., 3] = al
    body = a[..., :3][al > 200]
    fill = body.mean(axis=0).round().astype(np.uint8) if len(body) else np.array([1, 110, 89], np.uint8)
    t = al == 0
    a[t, 0], a[t, 1], a[t, 2] = fill[0], fill[1], fill[2]
    return Image.fromarray(a, 'RGBA'), tuple(int(x) for x in fill)


first, fill = clean(chain(src, 512))
print(f'   主体色 = {fill}')
first.save('icon.png')
print('✅ icon.png 512×512（豆包原图）')

entries = []
print(f"\n{'尺寸':>5} {'类型':>4} {'四角a':>7} {'掩码透明':>9}")
for s in SIZES:
    img = clean(chain(src, s))[0]
    if s <= 48:      # 小尺寸轻锐化，提升清晰度（不改图案）
        r, g, b, al = img.split()
        rgb = Image.merge('RGB', (r, g, b)).filter(
            ImageFilter.UnsharpMask(radius=0.8, percent=80, threshold=2))
        img = Image.merge('RGBA', (*rgb.split(), al))
        img = clean(img)[0]
    px = img.load()
    arr = np.asarray(img)
    corner = int(arr[0, 0, 3])

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
                if px[x, y][3] < 128:        # ⚠️ 用 <128，不是 ==0
                    bits[x // 8] |= (0x80 >> (x % 8)); ones += 1
            mask += bytes(bits)
        mr = f'{ones/(s*s)*100:.1f}%'
        bih = struct.pack('<IiiHHIIiiII', 40, s, s * 2, 1, 32, 0,
                          len(pd) + len(mask), 0, 0, 0, 0)
        data = bih + pd + bytes(mask); kind = 'BMP'
    entries.append((s, data, kind))
    print(f'{s:>5} {kind:>4} {corner:>7} {mr:>9}')

out = bytearray(); out += struct.pack('<HHH', 0, 1, len(entries))
off = 6 + 16 * len(entries)
for s, data, kind in entries:
    d = 0 if s >= 256 else s
    out += struct.pack('<BBBBHHII', d, d, 0, 0, 1, 32, len(data), off); off += len(data)
for s, data, kind in entries:
    out += data
open('app.ico', 'wb').write(bytes(out))
print(f'\n✅ app.ico ({len(out):,} B) —— 豆包原图 + 修好的掩码')

print('\n=== 白底/黑底 角落检验 ===')
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
