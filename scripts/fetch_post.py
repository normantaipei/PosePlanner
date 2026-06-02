#!/usr/bin/env python3
"""PosePlanner — 從社群貼文擷取圖片（不需 API key）

給一個 Instagram / Twitter-X / Facebook 貼文網址，把貼文裡的圖片抓下來、順手把貼文文字
（caption）也撈出來，產出一份可餵給看圖流程的 JSON。**完全不需要任何 API key**：

  - Twitter / X：走公開的 syndication endpoint（cdn.syndication.twimg.com），
    這支是給「嵌入推文」用的，免登入、免 token 申請。
  - Instagram：走免登入的 /p/<code>/media/?size=l（轉址到實際圖檔）。只拿得到封面、
    抓不到 caption（IG 改成 JS 殼了）；caption 請用 --caption 自己貼。
  - Facebook：FB 對匿名請求把貼文圖鎖死（只會漏出粉專頭像），純免登入幾乎抓不到。
    所以 FB 走 gallery-dl + **你瀏覽器的登入 cookies**（--gallery-dl，搭配
    --cookies-from-browser）——這仍然「不需要 API key / 開發者 token」，只是借用你
    平常的登入態。抓不到就明講，請使用者改手動存圖。

只用 Python 標準庫（urllib），零安裝。若環境裝了 `gallery-dl`，可用 --gallery-dl
走它（覆蓋率更好，IG 多圖 carousel 比較完整，但 IG 通常要 cookies）。

用法
────
  # 抓圖 + caption，印出 JSON（圖存到 --out）
  python3 scripts/fetch_post.py "https://x.com/user/status/123" --out /tmp/poseplanner/

  # 直接吐給入庫流程用的 manifest 骨架（每張圖一筆，已填 source/description=caption）
  python3 scripts/fetch_post.py "https://www.instagram.com/p/XXXX/" \
      --out /tmp/poseplanner/ --manifest-skeleton

輸出 JSON
─────────
  {
    "platform": "twitter" | "instagram",
    "post_url": "...",            # 正規化後的貼文網址
    "author":   "...",            # @handle 或顯示名（抓得到才有）
    "caption":  "...",            # 貼文文字
    "images":   ["/tmp/.../poseplanner_x_123_1.jpg", ...]   # 已下載的本地路徑
  }

擷取完，把 images 交給看圖流程（SKILL.md 第 2 步）：你親自看每張圖產出
description + tags，source 填 post_url，caption 可放進 description 當輔助 context。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from html import unescape

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ── HTTP ────────────────────────────────────────────────────────────────────
def _get(url: str, headers: dict | None = None, binary: bool = False):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", "replace")


def _download(url: str, dest: str) -> str:
    data = _get(url, binary=True)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


# ── platform 偵測 ─────────────────────────────────────────────────────────────
def detect_platform(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u:
        return "instagram"
    if "twitter.com" in u or "x.com" in u or "fxtwitter.com" in u or "vxtwitter.com" in u:
        return "twitter"
    if "facebook.com" in u or "fb.com" in u or "fb.watch" in u or "fb.me" in u:
        return "facebook"
    raise ValueError(f"不支援的網址（只支援 Instagram / Twitter-X / Facebook）：{url}")


# ── Twitter / X：syndication endpoint（免 API key） ───────────────────────────
def _tweet_id(url: str) -> str:
    m = re.search(r"(?:status(?:es)?)/(\d+)", url)
    if not m:
        raise ValueError(f"找不到推文 ID：{url}")
    return m.group(1)


def _syndication_token(tweet_id: str) -> str:
    """複刻 Twitter 前端產 token 的算法（免申請）：
    ((id / 1e15) * PI) 轉 base36，去掉所有 0 與小數點。"""
    n = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    # 整數部分
    i = int(n)
    out = ""
    if i == 0:
        out = "0"
    while i > 0:
        out = digits[i % 36] + out
        i //= 36
    # 小數部分（取夠用的位數即可，反正等下會把 0 全濾掉）
    frac = n - int(n)
    out += "."
    for _ in range(24):
        frac *= 36
        d = int(frac)
        out += digits[d]
        frac -= d
    return re.sub(r"(0+|\.)", "", out)


def fetch_twitter(url: str, out_dir: str) -> dict:
    tid = _tweet_id(url)
    token = _syndication_token(tid)
    api = (
        "https://cdn.syndication.twimg.com/tweet-result"
        f"?id={tid}&token={token}&lang=en"
    )
    raw = _get(api, headers={"Accept": "application/json"})
    data = json.loads(raw)

    caption = unescape(data.get("text", "") or "")
    user = data.get("user", {}) or {}
    author = user.get("screen_name") or user.get("name") or ""
    if author and not author.startswith("@"):
        author = "@" + author

    # 收集圖片網址：mediaDetails / photos（取原圖 name=orig）
    img_urls: list[str] = []
    for md in data.get("mediaDetails", []) or []:
        if md.get("type") == "photo" and md.get("media_url_https"):
            img_urls.append(md["media_url_https"])
    for p in data.get("photos", []) or []:
        if p.get("url"):
            img_urls.append(p["url"])
    # 去重保序
    seen, uniq = set(), []
    for u in img_urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    images = []
    for idx, u in enumerate(uniq, 1):
        full = u + ("&" if "?" in u else "?") + "name=orig"
        ext = os.path.splitext(u.split("?")[0])[1] or ".jpg"
        dest = os.path.join(out_dir, f"poseplanner_x_{tid}_{idx}{ext}")
        try:
            _download(full, dest)
            images.append(dest)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 下載失敗 {u}：{e}", file=sys.stderr)

    return {
        "platform": "twitter",
        "post_url": f"https://x.com/i/status/{tid}",
        "author": author,
        "caption": caption,
        "images": images,
    }


# ── Instagram：embed 頁（免 API key） ────────────────────────────────────────
def _ig_shortcode(url: str) -> str:
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError(f"找不到 Instagram 貼文代碼：{url}")
    return m.group(1)


def fetch_instagram(url: str, out_dir: str, caption: str = "") -> dict:
    """免登入抓 IG 公開貼文。

    現在的 IG 貼文 / embed 頁都是 JS 殼，HTML 裡**不再內嵌圖片網址**，純 HTTP 解析
    已不可行。唯一穩定的免登入管道是 `/p/<code>/media/?size=l`——它會 302 轉址到
    實際圖檔（回 image/jpeg）。限制：**只拿得到封面那張**（多圖 carousel 的其餘張、
    以及貼文文字 caption，免登入都拿不到）。

    所以：
      - 圖：走 /media/?size=l 抓封面。
      - caption（貼文文字）：請使用者自己貼上，用 --caption 傳進來（這就是「貼文輸入」）。
      - 要完整多圖 / 自動 caption → 用有 cookies 的 gallery-dl（--gallery-dl）。
    """
    code = _ig_shortcode(url)
    media = f"https://www.instagram.com/p/{code}/media/?size=l"
    images = []
    try:
        req = urllib.request.Request(media, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("content-type", "")
            data = resp.read()
        if data and ctype.startswith("image/"):
            ext = ".jpg" if "jpeg" in ctype or "jpg" in ctype else (
                ".webp" if "webp" in ctype else ".png" if "png" in ctype else ".jpg"
            )
            dest = os.path.join(out_dir, f"poseplanner_ig_{code}_1{ext}")
            with open(dest, "wb") as f:
                f.write(data)
            images.append(dest)
    except Exception as e:  # noqa: BLE001
        print(f"⚠ /media 抓圖失敗：{e}", file=sys.stderr)

    if not images:
        print(
            "⚠ Instagram 沒抓到圖。可能是私人帳號、已下架、或被平台限流。\n"
            "  可改用有 cookies 的 gallery-dl：--gallery-dl，或手動存圖後走一般入庫。",
            file=sys.stderr,
        )
    else:
        # /media/ 只給封面；提醒多圖貼文其餘張抓不到
        print(
            "ℹ Instagram 免登入只抓得到封面那張；若是多圖貼文，其餘張請用 --gallery-dl "
            "或手動存圖。caption 也請用 --caption 自行貼上。",
            file=sys.stderr,
        )

    return {
        "platform": "instagram",
        "post_url": f"https://www.instagram.com/p/{code}/",
        "author": "",
        "caption": caption,
        "images": images,
    }


# ── Facebook：匿名幾乎抓不到，best-effort + 指引 gallery-dl（免 API key，用 cookies） ─
def _fb_id(url: str) -> str:
    for pat in (r"fbid=(\d+)", r"/posts/(?:pfbid)?(\w+)", r"/videos/(\d+)", r"story_fbid=(\w+)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)[:24]
    return "post"


def fetch_facebook(url: str, out_dir: str, caption: str = "") -> dict:
    """FB 匿名 best-effort：FB 對未登入請求**不吐貼文圖**（payload 只漏粉專頭像），
    所以這裡會盡量撈、但濾掉頭像類（t1.30497-1）。多半會抓不到——抓不到就請改用
    `--gallery-dl --cookies-from-browser <browser>`（借你瀏覽器登入態，免 API key）。"""
    fid = _fb_id(url)
    images = []
    try:
        bot = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        html = _get(url, headers={"User-Agent": bot})
        hh = html.replace("\\/", "/").replace("\\u0026", "&")
        cands = []
        for u in re.findall(
            r"https://scontent[\w.\-]*\.fbcdn\.net/v/[^\"\\ )]+?\.(?:jpg|png|webp)[^\"\\ )]*",
            hh,
        ):
            # t1.30497-1 = 粉專頭像/大頭照，不是貼文圖，濾掉
            if "/t1.30497-1/" in u:
                continue
            if u not in cands:
                cands.append(u)
        # 挑檔案最大的那張（貼文主圖通常最大）
        best, best_sz = None, -1
        for u in cands:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    if not r.headers.get("content-type", "").startswith("image/"):
                        continue
                    sz = int(r.headers.get("content-length") or 0)
                    if sz > best_sz:
                        best, best_sz = u, sz
            except Exception:  # noqa: BLE001
                continue
        if best:
            ext = os.path.splitext(best.split("?")[0])[1] or ".jpg"
            dest = os.path.join(out_dir, f"poseplanner_fb_{fid}_1{ext}")
            _download(best, dest)
            images.append(dest)
    except Exception as e:  # noqa: BLE001
        print(f"⚠ FB 匿名擷取失敗：{e}", file=sys.stderr)

    if not images:
        print(
            "⚠ Facebook 匿名抓不到貼文圖（FB 把它鎖在登入後）。\n"
            "  請改用：--gallery-dl --cookies-from-browser chrome（或 firefox/edge/safari），\n"
            "  借你瀏覽器的登入態擷取——這不需要任何 API key。或手動存圖後走一般入庫。",
            file=sys.stderr,
        )

    return {
        "platform": "facebook",
        "post_url": url,
        "author": "",
        "caption": caption,
        "images": images,
    }


# ── gallery-dl 後援（選配，覆蓋率更好；FB / IG 多圖靠它） ──────────────────────
def fetch_gallery_dl(
    url: str, out_dir: str, platform: str, cookies_from_browser: str = ""
) -> dict:
    if not _which("gallery-dl"):
        raise RuntimeError("環境沒有 gallery-dl，無法用 --gallery-dl（pip install gallery-dl）")
    before = set(os.listdir(out_dir))
    cmd = ["gallery-dl", "-D", out_dir, "--write-metadata", "-o", "output.mode=null"]
    if cookies_from_browser:
        # 借瀏覽器登入態（FB / 私人 IG 需要）；非 API key
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(url)
    subprocess.run(cmd, check=True)
    after = set(os.listdir(out_dir))
    new = [
        os.path.join(out_dir, f)
        for f in sorted(after - before)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]
    return {
        "platform": platform,
        "post_url": url,
        "author": "",
        "caption": "",
        "images": new,
    }


def _which(prog: str) -> str | None:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, prog)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ── manifest 骨架（方便接入庫流程） ──────────────────────────────────────────
def to_manifest_skeleton(result: dict) -> list[dict]:
    src = result["post_url"]
    cap = result.get("caption", "")
    note = f"（貼文文字：{cap}）" if cap else ""
    return [
        {
            "image": img,
            "description": f"TODO：親自看圖補上動作敘述。{note}",
            "tags": [],
            "source": src,
        }
        for img in result["images"]
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="從 IG / Twitter-X 貼文擷取圖片（免 API key）")
    ap.add_argument("url", help="貼文網址")
    ap.add_argument("--out", default="/tmp/poseplanner", help="圖片下載目錄")
    ap.add_argument("--gallery-dl", action="store_true", help="用 gallery-dl 後援（需先裝；FB 多半要這個）")
    ap.add_argument(
        "--cookies-from-browser",
        default="",
        metavar="BROWSER",
        help="搭配 --gallery-dl，借瀏覽器登入態（chrome/firefox/edge/safari）。FB / 私人 IG 需要；非 API key",
    )
    ap.add_argument(
        "--caption",
        default="",
        help="手動貼上貼文文字（IG 免登入抓不到 caption，用這個傳進來；也可覆蓋 Twitter caption）",
    )
    ap.add_argument(
        "--manifest-skeleton",
        action="store_true",
        help="輸出入庫 manifest 骨架（每張圖一筆，待你看圖補 description/tags）",
    )
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    platform = detect_platform(args.url)

    try:
        if args.gallery_dl:
            result = fetch_gallery_dl(
                args.url, args.out, platform, cookies_from_browser=args.cookies_from_browser
            )
        elif platform == "twitter":
            result = fetch_twitter(args.url, args.out)
        elif platform == "facebook":
            result = fetch_facebook(args.url, args.out, caption=args.caption)
        else:
            result = fetch_instagram(args.url, args.out, caption=args.caption)
        # 使用者手動貼的 caption 一律優先（IG 必填、Twitter 選填覆蓋）
        if args.caption:
            result["caption"] = args.caption
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP {e.code}：{e.reason}（貼文可能私人/已刪，或平台限流）", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"✗ 擷取失敗：{e}", file=sys.stderr)
        return 1

    if args.manifest_skeleton:
        print(json.dumps(to_manifest_skeleton(result), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"✅ {platform}：抓到 {len(result['images'])} 張圖 → {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
