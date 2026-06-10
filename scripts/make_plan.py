#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_plan.py — 把「拍攝計劃書」JSON 渲染成 acosta 風格的 HTML / PDF。

設計同 skill 哲學：**Claude 產資料、腳本只寫檔**。
你（Claude）負責看圖、挑 pose、研究場地、寫出 plan.json；本腳本只負責把它排版成
一份乾淨的 A4 計劃書（封面 + 場地/時間表 + 每個 Look 一頁），風格對齊使用者既有的
acosta_taipei_shooting_plan_v2.pdf（白底、灰標籤格、細框線表格、粗體標題加底線、
參考圖虛線框）。

用法：
  # 1) 先吐一份骨架，照著填
  python3 scripts/make_plan.py --skeleton > plan.json

  # 2) 渲染成 HTML（圖用絕對路徑；可直接用瀏覽器開）
  python3 scripts/make_plan.py --plan plan.json --out-html plan.html

  # 3) 直接出 PDF（自動找 Chrome/Chromium 無頭列印）
  python3 scripts/make_plan.py --plan plan.json --pdf 拍攝計劃.pdf

選項：
  --base-dir DIR   圖片相對路徑的根目錄（預設：plan.json 所在資料夾）
  --embed          把圖片以 base64 內嵌進 HTML（產物可攜、不依賴本機檔案）
  --max-img-width  內嵌前先把圖縮到此寬度（需 Pillow；預設 1000，0=不縮）

plan.json 結構（最小可省略大多數欄位）：
{
  "title": "兒童新樂園",
  "subtitle": "拍攝計劃 Shooting Plan · miComet",
  "pages": [                       // 封面與前置頁；每個 page 物件 = 一頁
    { "blocks": [
        {"type":"info", "rows": [["主題 Theme","..."], ["拍攝地點","..."]]},
        {"type":"heading", "text":"企劃概念 Concept"},
        {"type":"para", "text":"支援 **粗體** 的段落文字"},
        {"type":"refs", "items": [{"cap":"圖說","img":"a.jpg","h":185}]},
        {"type":"table", "cols":["地標","特色","適合"],
         "rows":[["摩天輪","高 40m","地標主視覺"]], "note":"※ 備註"}
    ]}
  ],
  "looks": [                       // 每個 look 自動各佔一頁
    {
      "title": "Look 01",
      "fields": [["場景","摩天輪"], ["角色 Character","Suisei × Miko"]],
      "refs":  [{"cap":"參考圖","img":"ref.jpg","h":165}],
      "notes": [["場景","..."], ["構圖","支援 **粗體**"]],
      "footer": "（選填）此頁底部小字"
    }
  ],
  "footer": "（選填）最後一頁底部素材索引"
}
"""
import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# ------------------------------------------------------------------ CSS（acosta 風格）
CSS = """
  @page { size: A4; margin: 18mm 16mm; }
  * { box-sizing: border-box; }
  body {
    font-family: "PingFang TC","Hiragino Sans GB","Noto Sans CJK TC","Microsoft JhengHei",sans-serif;
    color: #1a1a1a; font-size: 10.5pt; line-height: 1.6; margin: 0;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  .page { page-break-after: always; }
  .page:last-child { page-break-after: auto; }
  h1.cover { font-size: 30pt; font-weight: 800; margin: 4px 0 2px; letter-spacing: 1px; }
  .subtitle { font-size: 15pt; font-weight: 700; color: #8a8a8a; margin: 0 0 22px; }
  h2 { font-size: 14pt; font-weight: 700; margin: 26px 0 10px;
       padding-bottom: 5px; border-bottom: 2px solid #2b2b2b; }
  .look-title { font-size: 19pt; font-weight: 800; margin: 0 0 14px; }
  h3 { font-size: 11.5pt; font-weight: 700; margin: 18px 0 8px; }
  table { border-collapse: collapse; width: 100%; }
  .info td { border: 1px solid #dcdcdc; padding: 7px 12px; font-size: 10.5pt; vertical-align: middle; }
  .info td.label { background: #f0f0f0; font-weight: 700; width: 24%; white-space: nowrap; }
  table.grid { border: 1px solid #dcdcdc; }
  table.grid th { background: #efefef; font-weight: 700; padding: 8px 10px;
                  border: 1px solid #dcdcdc; font-size: 10pt; text-align: left; }
  table.grid td { border: 1px solid #dcdcdc; padding: 7px 10px; font-size: 9.8pt; vertical-align: top; }
  .muted { color: #888; font-size: 9pt; }
  .star { color: #c0392b; font-weight: 700; }
  .refs { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 6px; }
  .refbox { border: 1.5px dashed #bdbdbd; padding: 6px; border-radius: 3px; background: #fafafa; }
  .refbox .cap { font-size: 8.5pt; color: #888; margin: 0 0 5px; }
  .refbox img { display: block; border-radius: 2px; }
  .notes p { margin: 4px 0; }
  .notes b.lbl { display: inline-block; min-width: 3.4em; }
  .foot { color: #aaa; font-size: 8.5pt; margin-top: 30px; border-top: 1px solid #eee; padding-top: 6px; }
"""

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
]


def esc(s):
    return html.escape(str(s if s is not None else ""))


def rich(s):
    """很輕量的行內格式：**粗體** -> <b>，其餘 escape。"""
    out, last = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*", str(s if s is not None else "")):
        out.append(esc(s[last:m.start()]))
        out.append("<b>" + esc(m.group(1)) + "</b>")
        last = m.end()
    out.append(esc(s[last:]))
    return "".join(out)


# ------------------------------------------------------------------ 圖片解析（路徑/base64）
def resolve_img(img, base_dir, embed, max_w):
    path = img if os.path.isabs(img) else os.path.join(base_dir, img)
    path = os.path.abspath(path)
    if not os.path.exists(path):
        sys.stderr.write(f"⚠ 找不到圖片：{path}\n")
        return "", False
    if not embed:
        return "file://" + path, True
    data = None
    if max_w and max_w > 0:
        try:
            from PIL import Image
            import io
            im = Image.open(path).convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, int(im.height * max_w / im.width)))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
            mime = "image/jpeg"
        except Exception as e:
            sys.stderr.write(f"⚠ 縮圖失敗（改用原圖內嵌）：{e}\n")
    if data is None:
        data = open(path, "rb").read()
        ext = os.path.splitext(path)[1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "image/jpeg")
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode()), True


def render_refs(items, base_dir, embed, max_w):
    if not items:
        return ""
    cells = []
    for it in items:
        src, ok = resolve_img(it.get("img", ""), base_dir, embed, max_w)
        if not ok:
            continue
        h = it.get("h", 165)
        cap = it.get("cap", "")
        cells.append(
            '<div class="refbox"><p class="cap">%s</p>'
            '<img src="%s" style="height:%spx"></div>' % (esc(cap), src, esc(h))
        )
    if not cells:
        return ""
    return '<div class="refs">' + "".join(cells) + "</div>"


def render_block(b, base_dir, embed, max_w):
    t = b.get("type")
    if t == "info":
        rows = "".join(
            '<tr><td class="label">%s</td><td>%s</td></tr>' % (esc(l), rich(v))
            for l, v in b.get("rows", [])
        )
        return '<table class="info">%s</table>' % rows
    if t == "heading":
        lvl = b.get("level", 2)
        tag = "h3" if lvl >= 3 else "h2"
        return "<%s>%s</%s>" % (tag, esc(b.get("text", "")), tag)
    if t == "para":
        return "<p>%s</p>" % rich(b.get("text", ""))
    if t == "refs":
        return render_refs(b.get("items", []), base_dir, embed, max_w)
    if t == "table":
        cols = b.get("cols", [])
        thead = "<tr>%s</tr>" % "".join("<th>%s</th>" % esc(c) for c in cols)
        body = "".join(
            "<tr>%s</tr>" % "".join("<td>%s</td>" % rich(c) for c in row)
            for row in b.get("rows", [])
        )
        note = '<p class="muted">%s</p>' % rich(b["note"]) if b.get("note") else ""
        return '<table class="grid">%s%s</table>%s' % (thead, body, note)
    if t == "spacer":
        return "<div style='height:%spx'></div>" % esc(b.get("h", 12))
    sys.stderr.write(f"⚠ 未知 block type: {t}\n")
    return ""


def render_look(lk, base_dir, embed, max_w):
    fields = "".join(
        '<tr><td class="label">%s</td><td>%s</td></tr>' % (esc(l), rich(v))
        for l, v in lk.get("fields", [])
    )
    parts = ['<div class="look-title">%s</div>' % esc(lk.get("title", "Look"))]
    if fields:
        parts.append('<table class="info">%s</table>' % fields)
    refs = render_refs(lk.get("refs", []), base_dir, embed, max_w)
    if refs:
        parts.append("<h3>預想圖 / 參考圖 Reference</h3>")
        parts.append(refs)
    notes = lk.get("notes", [])
    if notes:
        parts.append("<h3>構圖 / 動作筆記 Notes</h3>")
        ns = "".join('<p><b class="lbl">%s：</b>%s</p>' % (esc(l), rich(v)) for l, v in notes)
        parts.append('<div class="notes">%s</div>' % ns)
    if lk.get("footer"):
        parts.append('<div class="foot">%s</div>' % rich(lk["footer"]))
    return '<div class="page">%s</div>' % "".join(parts)


def render_html(plan, base_dir, embed, max_w):
    out = ['<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><style>%s</style></head><body>' % CSS]
    pages = plan.get("pages", [])
    for i, pg in enumerate(pages):
        blocks = pg.get("blocks", [])
        inner = []
        # 第一頁第一塊若是 info 之前，補上封面標題
        if i == 0:
            inner.append('<h1 class="cover">%s</h1>' % esc(plan.get("title", "")))
            if plan.get("subtitle"):
                inner.append('<div class="subtitle">%s</div>' % esc(plan["subtitle"]))
        for b in blocks:
            inner.append(render_block(b, base_dir, embed, max_w))
        out.append('<div class="page">%s</div>' % "".join(inner))
    for lk in plan.get("looks", []):
        out.append(render_look(lk, base_dir, embed, max_w))
    if plan.get("footer"):
        # 附在最後一頁底部：若沒有 looks/pages 就自建一頁
        if out[-1].endswith("</div>"):
            out[-1] = out[-1][:-6] + ('<div class="foot">%s</div></div>' % rich(plan["footer"]))
        else:
            out.append('<div class="page"><div class="foot">%s</div></div>' % rich(plan["footer"]))
    out.append("</body></html>")
    return "".join(out)


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


def to_pdf(html_path, pdf_path):
    chrome = find_chrome()
    if not chrome:
        sys.stderr.write(
            "✗ 找不到 Chrome/Chromium，無法輸出 PDF。\n"
            "  請改開 HTML 後用瀏覽器「列印成 PDF」，或安裝 Chrome。\n"
        )
        return False
    cmd = [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
           "--print-to-pdf=" + os.path.abspath(pdf_path),
           "file://" + os.path.abspath(html_path)]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not os.path.exists(pdf_path):
        sys.stderr.write("✗ PDF 輸出失敗：\n" + r.stderr.decode("utf-8", "ignore")[-800:] + "\n")
        return False
    return True


SKELETON = {
    "title": "場地名稱",
    "subtitle": "拍攝計劃 Shooting Plan · 主題",
    "pages": [
        {"blocks": [
            {"type": "info", "rows": [
                ["主題 Theme", "角色 A × 角色 B 雙人 Cos"],
                ["拍攝地點", "完整地址"],
                ["建議日期", "假日整日"],
                ["票務", "門票 / 一日券資訊"],
                ["調性", "明亮、繽紛、活潑甜美"],
            ]},
            {"type": "heading", "text": "企劃概念 Concept"},
            {"type": "para", "text": "一段概念說明，可用 **粗體** 強調重點。"},
            {"type": "refs", "items": [
                {"cap": "實拍範例｜場景一", "img": "reference/ex1.jpg", "h": 185},
                {"cap": "實拍範例｜場景二", "img": "reference/ex2.jpg", "h": 185},
            ]},
        ]},
        {"blocks": [
            {"type": "heading", "text": "場地資訊 Location"},
            {"type": "para", "text": "動線說明。"},
            {"type": "table",
             "cols": ["地標", "特色", "適合 cut"],
             "rows": [["摩天輪", "園區地標", "主視覺、夜景"],
                      ["旋轉木馬", "繽紛背景", "坐騎互動"]],
             "note": "※ 安全第一，設施運轉中勿取景。"},
            {"type": "heading", "text": "時間表 Time Schedule"},
            {"type": "table",
             "cols": ["時段", "地標", "Look", "備註"],
             "rows": [["09:30", "摩天輪", "Look 1", "上午自然光"],
                      ["17:00", "摩天輪", "Look 1 重拍", "<span class='star'>★</span> 夜間點燈"]]},
        ]},
    ],
    "looks": [
        {
            "title": "Look 01",
            "fields": [
                ["場景", "摩天輪"],
                ["角色 Character", "角色 A × 角色 B"],
                ["作品 Series", "作品名"],
                ["時段 Time", "上午 / 傍晚"],
                ["服裝重點", "便服 AU"],
                ["道具 Props", "—"],
            ],
            "refs": [{"cap": "參考圖｜pose #66", "img": "reference/poselib/pose_066.jpg", "h": 165}],
            "notes": [
                ["場景", "場景描述。"],
                ["構圖", "構圖重點，可用 **粗體**。"],
                ["動作", "動作指引。"],
                ["光線", "光線建議。"],
                ["表情", "情緒方向。"],
            ],
        }
    ],
    "footer": "素材：場地資料 location/…｜實拍範例 N 張｜動作參考 N 張（pose 庫 #…）",
}


def main():
    ap = argparse.ArgumentParser(description="把拍攝計劃 JSON 渲染成 acosta 風格 HTML/PDF")
    ap.add_argument("--plan", help="plan.json 路徑")
    ap.add_argument("--out-html", help="輸出 HTML 路徑（預設：與 PDF 同名 .html 或 plan.html）")
    ap.add_argument("--pdf", help="輸出 PDF 路徑（需要 Chrome/Chromium）")
    ap.add_argument("--base-dir", help="圖片相對路徑根目錄（預設：plan.json 所在資料夾）")
    ap.add_argument("--embed", action="store_true", help="圖片以 base64 內嵌（產物可攜）")
    ap.add_argument("--max-img-width", type=int, default=1000, help="內嵌前縮圖寬度（0=不縮）")
    ap.add_argument("--skeleton", action="store_true", help="印出骨架 JSON 後結束")
    args = ap.parse_args()

    if args.skeleton:
        print(json.dumps(SKELETON, ensure_ascii=False, indent=2))
        return

    if not args.plan:
        ap.error("需要 --plan（或用 --skeleton 先產骨架）")

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    base_dir = args.base_dir or os.path.dirname(os.path.abspath(args.plan))
    html_doc = render_html(plan, base_dir, args.embed, args.max_img_width)

    out_html = args.out_html
    if not out_html:
        if args.pdf:
            out_html = os.path.splitext(args.pdf)[0] + ".html"
        else:
            out_html = os.path.join(base_dir, "plan.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print("✓ HTML：" + os.path.abspath(out_html))

    if args.pdf:
        if to_pdf(out_html, args.pdf):
            sz = os.path.getsize(args.pdf) // 1024
            print("✓ PDF ：%s（%d KB）" % (os.path.abspath(args.pdf), sz))
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
