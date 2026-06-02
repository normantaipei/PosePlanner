#!/usr/bin/env python3
"""沙箱對外網路探針 v2 — 找出「白名單放行、可拿來上傳」的目標。

背景：Claude 沙箱的對外網路是「白名單 egress proxy」。非白名單網域，proxy 會回
HTTP 403（不是主機本身回的）。所以「收到 403」其實是「被擋」，不能算連通。

本版判定改嚴：
  - 真的打到主機（2xx/3xx，或 400/401 這種「主機有回但要 auth/參數」）→ REACHED ✅
  - 403 / 407 → 多半是 egress proxy 擋的 → BLOCKED(proxy) ⛔
  - 連線層失敗（timeout / DNS / TLS）→ BLOCKED ❌

每個目標都用「真的連通時會回 2xx 的 GET endpoint」來測，避免再假陽性。
只做唯讀小請求，不上傳任何東西。

用法（在實際要跑 skill 的沙箱執行）：
    python3 scripts/probe_net.py
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
import urllib.error
import urllib.request

# (label, url, 期望真的連通時的狀態碼)；選的 endpoint 在「真的連到」時會回 200/可辨識碼
TARGETS = [
    # —— 最有戲的上傳目標 ——
    ("github-api",   "https://api.github.com/zen",                                   {200}),
    ("github-raw",   "https://raw.githubusercontent.com/github/gitignore/main/README.md", {200}),
    ("github-web",   "https://github.com/robots.txt",                                {200}),
    ("github-codeload", "https://codeload.github.com",                               {200, 400, 404}),
    # —— 已知在白名單、當對照組 ——
    ("anthropic",    "https://api.anthropic.com/v1/models",                          {200, 401, 400}),
    ("pypi",         "https://pypi.org/pypi/pip/json",                               {200}),
    ("adobe-io",     "https://ims-na1.adobelogin.com/ims/check/v6/token",            {200, 400, 401, 405}),
    # —— 常見雲端物件儲存（多半不在白名單，用來確認 proxy 行為）——
    ("aws-s3",       "https://s3.amazonaws.com",                                     {200, 307, 400, 403}),
    ("cloudflare-r2","https://r2.cloudflarestorage.com",                             {200, 400, 401, 403}),
    ("gcs",          "https://storage.googleapis.com",                              {200, 400}),
]


def probe(url: str, timeout: float = 7.0) -> tuple[str, str]:
    """回傳 (verdict, detail)。verdict ∈ REACHED / PROXY / BLOCKED。"""
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "pp-probe/2"})
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return "REACHED", f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (403, 407):
            return "PROXY", f"HTTP {e.code}（多半是 egress proxy 擋下，非主機回應）"
        if e.code in (400, 401, 404, 405, 429):
            return "REACHED", f"HTTP {e.code}（主機有回，代表連得到）"
        return "REACHED", f"HTTP {e.code}"
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
        return "BLOCKED", f"{type(e).__name__}: {e}"


def main() -> int:
    print("探測沙箱白名單（唯讀，不上傳）…\n")
    reached, proxied, blocked = [], [], []
    results = []
    for label, url, _ok in TARGETS:
        verdict, detail = probe(url)
        mark = {"REACHED": "✅", "PROXY": "⛔", "BLOCKED": "❌"}[verdict]
        print(f"  {mark} {label:14s} {detail}\n       {url}")
        results.append({"label": label, "url": url, "verdict": verdict, "detail": detail})
        (reached if verdict == "REACHED" else proxied if verdict == "PROXY" else blocked).append(label)

    print("\n── 結論 ──")
    print(f"  可上傳目標（REACHED）：{', '.join(reached) or '（無）'}")
    print(f"  被 proxy 擋（PROXY）  ：{', '.join(proxied) or '（無）'}")
    print(f"  連不到（BLOCKED）     ：{', '.join(blocked) or '（無）'}")
    github_ok = any(l.startswith("github") for l in reached)
    if github_ok:
        print("\n➡ GitHub 連得到 → 可走『Python 直接 push 圖+庫到私有 repo』，任何大小、不經模型。")
    elif reached:
        print(f"\n➡ 有白名單目標可用：{reached[0]}。看哪個適合當圖庫。")
    else:
        print("\n➡ 沒有可上傳目標 → 大檔只能走同步資料夾。")
    print("\nJSON:", json.dumps({"reached": reached, "proxy": proxied, "blocked": blocked,
                                 "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
