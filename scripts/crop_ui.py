#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crop_ui.py — 把手機 App 截圖（Pinterest / IG / X…）上下的介面外框裁掉，只留主圖。

當使用者丟「App 截圖」要當參考素材時，畫面上方有狀態列／返回鍵、下方有按鈕列、
相關推薦、手機導覽列。這個工具自動偵測主圖的上下邊界（找全幅圖上下的「近黑分隔線」），
把那些 UI 裁掉，輸出乾淨的主圖。

原理：對每一列取樣算平均亮度——
  · 最上方的狀態列通常是純黑（亮度極低）→ 跳過，找到第一列「有內容」的列當上邊界。
  · 全幅主圖的下緣，App 多半接一條近黑分隔線或切到淺色 UI 區 → 取內容開始後
    第一條明顯的近黑分隔列當下邊界。
偵測不準時可用 --top/--bottom/--left/--right 手動覆寫（像素）。

用法：
  # 單檔
  python3 scripts/crop_ui.py shot.jpg
  # 整個資料夾（所有圖），輸出到 ./cropped/
  python3 scripts/crop_ui.py ./screenshots --out-dir ./cropped
  # 手動指定上下邊界
  python3 scripts/crop_ui.py shot.jpg --top 106 --bottom 1608
  # 只預覽偵測到的邊界、先不裁
  python3 scripts/crop_ui.py ./screenshots --dry-run

需要 Pillow。
"""
import argparse
import os
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow：pip install pillow")

EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def row_is_content(px, W, y, sens, step=8):
    """一列是否屬於『照片內容』：用水平亮度變異 + 色彩度判斷。
    純色 UI 帶（白/灰/黑）或細分隔線 → 變異與色彩度都低 → 非內容；
    照片內容 → 至少一項偏高。回傳 True/False。"""
    vals = []
    sat = 0
    for x in range(0, W, step):
        r, g, b = px[x, y][:3]
        vals.append(max(r, g, b))
        sat += max(r, g, b) - min(r, g, b)
    n = max(len(vals), 1)
    mean = sum(vals) / n
    hstd = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
    colour = sat / n
    return (hstd > sens) or (colour > sens)


def detect_bounds(im, sens=14, min_band=24):
    """回傳 (top, bottom)。top=主圖第一列；bottom=主圖最後一列+1（適合 crop）。
    top = 第一列內容；bottom = 內容開始後，第一段連續 >= min_band 列『非內容』的起點
    （即主圖下緣，把狀態列／分隔線／按鈕列／推薦／導覽列一併排除）。"""
    im = im.convert("RGB")
    W, H = im.size
    px = im.load()
    flags = [row_is_content(px, W, y, sens) for y in range(H)]

    top = 0
    while top < H and not flags[top]:
        top += 1
    if top >= H:                       # 整張都判為非內容，放棄上裁
        return 0, H

    bottom = H
    run = 0
    for y in range(top, H):
        if flags[y]:
            run = 0
        else:
            run += 1
            if run >= min_band:        # 連續一段非內容 → 主圖到此為止
                bottom = y - run + 1
                break
    return top, bottom


def crop_one(path, out_dir, top=None, bottom=None, left=None, right=None,
             sens=14, min_band=24, dry=False, prefix=""):
    im = Image.open(path)
    W, H = im.size
    at, ab = detect_bounds(im, sens, min_band)
    t = at if top is None else top
    b = ab if bottom is None else bottom
    l = 0 if left is None else left
    r = W if right is None else right
    name = os.path.basename(path)
    if dry:
        print(f"{name}: {W}x{H} → top={t} bottom={b} left={l} right={r}  (高 {b - t})")
        return None
    if not (0 <= t < b <= H and 0 <= l < r <= W):
        sys.stderr.write(f"⚠ {name}: 邊界異常（top={t} bottom={b} left={l} right={r}），略過\n")
        return None
    c = im.convert("RGB").crop((l, t, r, b))
    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(name)
    out = os.path.join(out_dir, f"{prefix}{stem}.jpg")
    c.save(out, quality=95)
    print(f"✓ {name} → {out}  ({c.size[0]}x{c.size[1]})")
    return out


def main():
    ap = argparse.ArgumentParser(description="裁掉 App 截圖上下的 UI 外框，只留主圖")
    ap.add_argument("paths", nargs="+", help="圖檔或資料夾")
    ap.add_argument("--out-dir", help="輸出資料夾（預設：輸入旁的 cropped/）")
    ap.add_argument("--prefix", default="", help="輸出檔名前綴")
    ap.add_argument("--top", type=int, help="手動上邊界（像素）")
    ap.add_argument("--bottom", type=int, help="手動下邊界（像素）")
    ap.add_argument("--left", type=int, help="手動左邊界（像素）")
    ap.add_argument("--right", type=int, help="手動右邊界（像素）")
    ap.add_argument("--sens", type=int, default=14,
                    help="內容靈敏度門檻（水平變異/色彩度，越低越敏感；預設 14）")
    ap.add_argument("--min-band", type=int, default=24,
                    help="判定主圖下緣所需的連續非內容列數（預設 24）")
    ap.add_argument("--dry-run", action="store_true", help="只印偵測到的邊界，不裁切")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.lower().endswith(EXTS):
                    files.append(os.path.join(p, f))
        elif os.path.isfile(p):
            files.append(p)
        else:
            sys.stderr.write(f"⚠ 找不到：{p}\n")
    if not files:
        sys.exit("沒有可處理的圖檔。")

    base = os.path.dirname(os.path.abspath(files[0]))
    out_dir = args.out_dir or os.path.join(base, "cropped")
    n = 0
    for f in files:
        if crop_one(f, out_dir, args.top, args.bottom, args.left, args.right,
                    args.sens, args.min_band, args.dry_run, args.prefix):
            n += 1
    if not args.dry_run:
        print(f"\n完成：{n}/{len(files)} 張，輸出於 {out_dir}")


if __name__ == "__main__":
    main()
