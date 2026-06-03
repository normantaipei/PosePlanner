#!/usr/bin/env python3
"""PosePlanner 找圖前端 — 一鍵建構腳本。

這支腳本把「純靜態 Vue 搜尋頁」接上你的私有 DB server：
  1) 問你 server 的 domain（base-url）與『讀取』token（POSEPLANNER_READ_TOKEN）。
  2) 測一下連線（GET /health、/stats）。
  3) 產生 web/config.js（前端載入它取得 domain + token）。
  4) 把 Vue 抓進 web/vendor/（離線也能跑；抓不到會 fallback 到 CDN）。
  5) 可選：--serve 直接起一個本機靜態伺服器，瀏覽器打開就用。

只用 Python 標準庫（urllib / http.server），不需要 npm / Vite / pip。

用法
────
  python3 web/build.py                       # 互動式問 domain + 讀取 token
  python3 web/build.py --base-url http://192.168.1.50:8000 --token <read_token>
  python3 web/build.py --serve --port 8080   # 建構完順手起本機伺服器
  python3 web/build.py --no-vendor           # 不抓 Vue，前端直接用 CDN

注意：config.js 會以明文存放『讀取』token——這頁是給你信任的人/內網用的查詢介面，
別把產生好的 web/ 連同 config.js 公開放到不特定人能存取的地方。
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
CONFIG_PATH = WEB_DIR / "config.js"
VENDOR_DIR = WEB_DIR / "vendor"
VUE_DST = VENDOR_DIR / "vue.global.prod.js"
VUE_URLS = [
    "https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js",
    "https://unpkg.com/vue@3/dist/vue.global.prod.js",
    "https://cdnjs.cloudflare.com/ajax/libs/vue/3.5.13/vue.global.prod.min.js",
]


# ── HTTP（標準庫）────────────────────────────────────────────────────
def _http_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "PosePlanner-build"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(str(e))


def test_connection(base_url: str, token: str) -> None:
    """打 /health 與 /stats 確認 domain / token 可用；失敗只警告、不擋建構。"""
    base = base_url.rstrip("/")
    tq = ("?t=" + urllib.parse.quote(token)) if token else ""
    try:
        st, body = _http_get(f"{base}/health")
        if st != 200:
            print(f"  ⚠ /health 回 {st}（server 在嗎？）")
        else:
            print(f"  ✓ /health OK：{body.decode(errors='ignore')[:80]}")
    except ConnectionError as e:
        print(f"  ⚠ 連不到 {base}/health：{e}")
        return
    try:
        st, body = _http_get(f"{base}/stats{tq}")
        if st == 401:
            print("  ⚠ /stats 回 401：讀取 token 不對，或該填卻沒填。")
        elif st == 200:
            data = json.loads(body.decode("utf-8"))
            print(f"  ✓ /stats OK：poses={data.get('poses')} creators={data.get('creators')}")
        else:
            print(f"  ⚠ /stats 回 {st}：{body.decode(errors='ignore')[:120]}")
    except (ConnectionError, json.JSONDecodeError) as e:
        print(f"  ⚠ /stats 測試失敗：{e}")


# ── 產出 ────────────────────────────────────────────────────────────
def write_config(base_url: str, token: str) -> None:
    base = base_url.rstrip("/")
    content = (
        "// 由 web/build.py 產生，請勿手改；重跑 build.py 會覆蓋。\n"
        "// 內含 server domain 與『讀取』token——別把此檔公開外流。\n"
        "window.POSEPLANNER_CONFIG = {\n"
        f"  baseUrl: {json.dumps(base, ensure_ascii=False)},\n"
        f"  token: {json.dumps(token, ensure_ascii=False)}\n"
        "};\n"
    )
    CONFIG_PATH.write_text(content, encoding="utf-8")
    print(f"  ✓ 已寫入 {CONFIG_PATH.relative_to(WEB_DIR.parent)}")


def vendor_vue(retries: int = 3) -> None:
    """把 Vue 抓進 web/vendor/。抓不到不致命——index.html 會 fallback 到 CDN。"""
    if VUE_DST.exists() and VUE_DST.stat().st_size > 10000:
        print(f"  ✓ Vue 已 vendored（{VUE_DST.stat().st_size // 1024} KB），略過下載")
        return
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for url in VUE_URLS:
        for attempt in range(retries):
            try:
                st, body = _http_get(url, timeout=30)
                if st == 200 and len(body) > 10000:
                    VUE_DST.write_bytes(body)
                    print(f"  ✓ Vue 下載完成 → {VUE_DST.relative_to(WEB_DIR.parent)}（{len(body)//1024} KB，來源 {url}）")
                    return
                break  # 非 200 → 換下一個來源
            except ConnectionError as e:
                wait = 2 ** attempt
                print(f"  … 下載 Vue 失敗（{e}），{wait}s 後重試…")
                time.sleep(wait)
    print("  ⚠ 抓不到 Vue（無網路？）。沒關係——頁面會自動 fallback 到 CDN；"
          "但若你要離線使用，請在有網路時重跑或手動放 web/vendor/vue.global.prod.js。")


# ── 本機伺服器 ──────────────────────────────────────────────────────
def serve(port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))

    class Reusable(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with Reusable(("0.0.0.0", port), handler) as httpd:
        print(f"\n▸ 前端已在本機啟動：http://localhost:{port}/")
        print("  （Ctrl+C 結束）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


# ── 互動 ────────────────────────────────────────────────────────────
def prompt(msg: str, default: str = "") -> str:
    suffix = f"（預設 {default}）" if default else ""
    val = input(f"{msg}{suffix}：").strip()
    return val or default


def main() -> None:
    p = argparse.ArgumentParser(description="PosePlanner 找圖前端一鍵建構")
    p.add_argument("--base-url", help="server domain，如 http://192.168.1.50:8000")
    p.add_argument("--token", help="『讀取』token（server 的 POSEPLANNER_READ_TOKEN；沒設 token 就留空）")
    p.add_argument("--serve", action="store_true", help="建構完起一個本機靜態伺服器")
    p.add_argument("--port", type=int, default=8080, help="--serve 的埠（預設 8080）")
    p.add_argument("--no-vendor", action="store_true", help="不下載 Vue（前端改用 CDN）")
    args = p.parse_args()

    print("PosePlanner 找圖前端 — 建構\n")
    base_url = args.base_url
    token = args.token
    if not base_url:
        base_url = prompt("server domain（如 http://192.168.1.50:8000）")
        if not base_url:
            raise SystemExit("沒給 domain，無法建構。")
        if token is None:
            token = prompt("『讀取』token（server 沒設 token 就直接 Enter 跳過）")
    if token is None:
        token = ""
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url

    print("\n[1/3] 測試連線…")
    test_connection(base_url, token)

    print("\n[2/3] 寫入設定…")
    write_config(base_url, token)

    print("\n[3/3] 準備 Vue…")
    if args.no_vendor:
        print("  － 跳過下載（--no-vendor）；頁面會用 CDN 載 Vue。")
    else:
        vendor_vue()

    print("\n✅ 建構完成。")
    if args.serve:
        serve(args.port)
    else:
        print("開啟方式（擇一）：")
        print(f"  • 起本機伺服器：python3 {Path(__file__).name} --serve   然後開 http://localhost:8080/")
        print(f"  • 或用任何靜態伺服器把 {WEB_DIR} 這個資料夾服務出去")
        print("  （建議用伺服器開，別直接雙擊 index.html 走 file://——部分瀏覽器會擋跨源讀取）")


if __name__ == "__main__":
    sys.exit(main())
