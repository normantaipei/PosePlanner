#!/usr/bin/env python3
"""PosePlanner — 儲存後端選擇與私有 DB 客戶端。

PosePlanner 有兩種儲存後端，第一次執行時二選一（記在 data/config.json）：

  1) drive     — 純雲端：pack 成 fragment 上傳到你的 Google Drive（由 Claude 用連接器做）。
  2) selfhost  — 私有 DB：直連你網域內自架的 server（server/docker-compose.yml），
                 原圖 + metadata 存在你自己的機器（PostgreSQL + 硬碟）。

這支腳本只用 Python 標準庫（urllib），不需要任何 pip 安裝。

設定
────
  python3 scripts/backend.py status                         # 看目前後端
  python3 scripts/backend.py setup                          # 互動式選擇（終端機用）
  python3 scripts/backend.py set --backend drive            # 用 Google Drive
  python3 scripts/backend.py set --backend selfhost \       # 用私有 DB
      --base-url http://192.168.1.50:8000 --token <token>

私有 DB（selfhost）模式下的入庫 / 搜尋
──────────────────────────────────────
  python3 scripts/backend.py ingest  --manifest /tmp/poseplanner/_ingest.json
      # 把 manifest 裡每張「原圖 + description + tags」POST 到 server /images（去重）。
  python3 scripts/backend.py push --fragment frag-xxx.zip
      # 把一個已 pack 的 fragment zip POST 到 server /fragments（相容雲端格式）。
  python3 scripts/backend.py search "回眸帥氣站姿" --tag framing=半身 --table
      # 對 server /search 粗篩，印 Markdown 表（縮圖用 server URL，內網可直接渲染）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("POSEPLANNER_DATA", str(ROOT / "data")))
CONFIG_PATH = Path(os.environ.get("POSEPLANNER_CONFIG", str(DATA_DIR / "config.json")))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tiff"}


# ── 設定檔 ──────────────────────────────────────────────────────────
def load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入設定：{CONFIG_PATH}")


def require_selfhost(cfg: dict | None) -> dict:
    if not cfg or cfg.get("backend") != "selfhost":
        raise SystemExit(
            "目前不是 selfhost（私有 DB）模式。\n"
            "先設定：python3 scripts/backend.py set --backend selfhost "
            "--base-url http://<內網IP>:8000 --token <token>"
        )
    sh = cfg.get("selfhost") or {}
    if not sh.get("base_url"):
        raise SystemExit("設定缺 selfhost.base_url，請重新 set。")
    return sh


# ── HTTP（標準庫；含 multipart 編碼）────────────────────────────────
def _auth_headers(sh: dict) -> dict:
    token = (sh.get("token") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _multipart_body(fields: dict[str, str], files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    """組 multipart/form-data。files 是 (field_name, filename, content) 串列。"""
    boundary = f"----PosePlanner{uuid.uuid4().hex}"
    crlf = b"\r\n"
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        out += b"--" + boundary.encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        out += str(value).encode("utf-8") + crlf
    for name, filename, content in files:
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out += b"--" + boundary.encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode() + crlf
        out += f"Content-Type: {ctype}".encode() + crlf + crlf
        out += content + crlf
    out += b"--" + boundary.encode() + b"--" + crlf
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _request(method: str, url: str, *, headers: dict, data: bytes | None = None,
             content_type: str | None = None, timeout: int = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise SystemExit(f"連不到 server（{url}）：{e.reason}")


def _get_json(sh: dict, path: str, params: list[tuple[str, str]] | None = None) -> object:
    url = sh["base_url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    status, body = _request("GET", url, headers=_auth_headers(sh))
    if status >= 400:
        raise SystemExit(f"GET {path} 失敗（{status}）：{body.decode(errors='ignore')[:300]}")
    return json.loads(body.decode("utf-8"))


def _post_multipart(sh: dict, path: str, fields: dict, files: list, timeout: int = 120) -> tuple[int, object]:
    url = sh["base_url"].rstrip("/") + path
    body, ctype = _multipart_body(fields, files)
    status, raw = _request("POST", url, headers=_auth_headers(sh), data=body,
                           content_type=ctype, timeout=timeout)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = raw.decode(errors="ignore")
    return status, parsed


# ── 子指令 ──────────────────────────────────────────────────────────
def cmd_status(args) -> None:
    cfg = load_config()
    if not cfg:
        print("尚未設定後端。第一次使用請選一種：")
        print("  • Google Drive：python3 scripts/backend.py set --backend drive")
        print("  • 私有 DB     ：python3 scripts/backend.py set --backend selfhost "
              "--base-url http://<內網IP>:8000 --token <token>")
        print("  • 互動式      ：python3 scripts/backend.py setup")
        return
    backend = cfg.get("backend")
    print(f"後端：{backend}")
    if backend == "selfhost":
        sh = cfg.get("selfhost") or {}
        print(f"  base_url：{sh.get('base_url')}")
        print(f"  token   ：{'已設' if sh.get('token') else '（無，純內網信任）'}")
        try:
            health = _get_json(sh, "/health")
            stats = _get_json(sh, "/stats")
            print(f"  連線    ：OK（{health}）")
            print(f"  庫狀態  ：poses={stats.get('poses')} tags={stats.get('tags')} "
                  f"proposed={stats.get('proposed_tags')}")
        except SystemExit as e:
            print(f"  連線    ：✗ {e}")


def cmd_set(args) -> None:
    if args.backend == "drive":
        save_config({"backend": "drive"})
        print("已設為 Google Drive 模式：pack 後由 Claude 用連接器上傳到你的 Drive。")
        return
    if args.backend == "selfhost":
        if not args.base_url:
            raise SystemExit("selfhost 需要 --base-url，例如 http://192.168.1.50:8000")
        cfg = {"backend": "selfhost",
               "selfhost": {"base_url": args.base_url.rstrip("/"), "token": args.token or ""}}
        save_config(cfg)
        # 立刻測一下連線（失敗只警告，不擋設定）
        try:
            health = _get_json(cfg["selfhost"], "/health")
            print(f"連線測試 OK：{health}")
        except SystemExit as e:
            print(f"⚠ 設定已存，但連線測試失敗：{e}")
        return
    raise SystemExit("--backend 只能是 drive 或 selfhost")


def cmd_setup(args) -> None:
    """互動式（終端機）。Claude 端通常改用 set（先在對話裡問使用者再 set）。"""
    print("PosePlanner 第一次設定 — 選擇庫要存在哪：\n")
    print("  1) Google Drive（純雲端，零維運，原圖不留）")
    print("  2) 私有 DB（自架 server，原圖留在你網域內的機器）\n")
    choice = input("輸入 1 或 2：").strip()
    if choice == "1":
        save_config({"backend": "drive"})
        print("✓ 已選 Google Drive。")
    elif choice == "2":
        base_url = input("server 位址（如 http://192.168.1.50:8000）：").strip()
        token = input("Bearer token（server .env 的 POSEPLANNER_TOKEN，沒設就留空）：").strip()
        ns = argparse.Namespace(backend="selfhost", base_url=base_url, token=token)
        cmd_set(ns)
    else:
        raise SystemExit("沒有有效選擇，未變更設定。")


def _read_manifest(path: Path) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("poses") or entries.get("items") or [entries]
    if not isinstance(entries, list):
        raise SystemExit("manifest 應是 JSON 陣列。")
    return entries


def cmd_ingest(args) -> None:
    """selfhost：把 manifest 裡每張原圖 + metadata POST 到 server /images。"""
    sh = require_selfhost(load_config())
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        raise SystemExit(f"找不到 manifest：{manifest_path}")
    base = manifest_path.parent
    entries = _read_manifest(manifest_path)

    added = dup = errors = 0
    for entry in entries:
        img_field = entry.get("image")
        if not img_field:
            print("  ✗ 略過：缺 image 欄位"); errors += 1; continue
        src = Path(img_field).expanduser()
        if not src.is_absolute():
            src = base / src
        if not src.exists():
            print(f"  ✗ 找不到圖：{img_field}"); errors += 1; continue
        description = (entry.get("description") or "").strip()
        tags = entry.get("tags") or []
        if not description or not tags:
            print(f"  ✗ 略過 {src.name}：description / tags 不可空"); errors += 1; continue

        fields = {
            "description": description,
            "tags": json.dumps(tags, ensure_ascii=False),
            "source": entry.get("source") or src.name,
            "favorite": str(bool(entry.get("favorite"))).lower(),
        }
        if entry.get("rating") is not None:
            fields["rating"] = str(entry["rating"])
        if entry.get("embedding"):
            fields["embedding"] = json.dumps(entry["embedding"])
            if entry.get("embedding_model"):
                fields["embedding_model"] = entry["embedding_model"]

        content = src.read_bytes()
        status, parsed = _post_multipart(sh, "/images", fields, [("file", src.name, content)])
        if status >= 400:
            print(f"  ✗ {src.name} 上傳失敗（{status}）：{parsed}"); errors += 1; continue
        state = parsed.get("status") if isinstance(parsed, dict) else None
        if state == "added":
            added += 1; print(f"  ＋ {src.name} → #{parsed.get('id')}")
        elif state == "dup":
            dup += 1; print(f"  ↻ 重複略過：{src.name}")
        else:
            errors += 1; print(f"  ✗ {src.name}：{parsed}")

    print(f"\n入庫完成（私有 DB）：+{added} 張，重複 {dup}，失敗 {errors}。")
    print(f"  → server：{sh['base_url']}")


def cmd_image(args) -> None:
    """selfhost：單張原圖 + metadata 直接 POST（快速測試 / 手動）。"""
    sh = require_selfhost(load_config())
    src = Path(args.image).expanduser()
    if not src.exists():
        raise SystemExit(f"找不到圖：{src}")
    tags = []
    for kv in args.tag or []:
        if "=" not in kv:
            raise SystemExit(f"--tag 格式要 category=name，收到：{kv}")
        c, n = kv.split("=", 1)
        tags.append({"category": c, "name": n})
    if not args.description or not tags:
        raise SystemExit("需要 --description 與至少一個 --tag。")
    fields = {
        "description": args.description,
        "tags": json.dumps(tags, ensure_ascii=False),
        "source": args.source or src.name,
        "favorite": str(bool(args.favorite)).lower(),
    }
    if args.rating is not None:
        fields["rating"] = str(args.rating)
    status, parsed = _post_multipart(sh, "/images", fields, [("file", src.name, src.read_bytes())])
    if status >= 400:
        raise SystemExit(f"上傳失敗（{status}）：{parsed}")
    print(f"OK：{parsed}")


def cmd_push(args) -> None:
    """selfhost：把一個已 pack 的 fragment zip POST 到 /fragments。"""
    sh = require_selfhost(load_config())
    zip_path = Path(args.fragment).expanduser()
    if not zip_path.exists():
        raise SystemExit(f"找不到 fragment：{zip_path}")
    status, parsed = _post_multipart(sh, "/fragments", {}, [("file", zip_path.name, zip_path.read_bytes())])
    if status >= 400:
        raise SystemExit(f"上傳 fragment 失敗（{status}）：{parsed}")
    print(f"OK（私有 DB 回放）：{parsed}")


def _md_cell(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def cmd_search(args) -> None:
    """selfhost：對 server /search 粗篩候選；--table 印 Markdown 表（縮圖用 server URL）。"""
    sh = require_selfhost(load_config())
    params: list[tuple[str, str]] = []
    if args.query:
        params.append(("q", args.query))
    for kv in args.tag or []:
        params.append(("tag", kv))
    params.append(("limit", str(args.limit)))
    results = _get_json(sh, "/search", params)
    if not isinstance(results, list):
        raise SystemExit(f"server 回傳非預期：{results}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    base = sh["base_url"].rstrip("/")
    token = (sh.get("token") or "").strip()

    def media_url(rel: str) -> str:
        # 讀取端點有 token 時，縮圖網址要帶 ?t=<token>，對話裡的 <img> 才載得到
        url = f"{base}/{rel}"
        if token:
            url += "?t=" + urllib.parse.quote(token)
        return url

    if not results:
        print("（沒有符合的 pose）")
        return
    if args.table:
        print(f"找到 **{len(results)}** 張相關圖片（私有 DB）：\n")
        print("| 縮圖 | # | 描述 | 標籤 | 訊號 |")
        print("|---|---|---|---|---|")
        for r in results:
            thumb = r.get("thumbnail_path")
            cell = f"![#{r['id']}]({media_url(thumb)})" if thumb else "—"
            signals = []
            if r.get("favorite"):
                signals.append("★")
            if r.get("rating"):
                signals.append(f"{r['rating']}/5")
            tag_vals = [t.split(":", 1)[-1] for t in r.get("tags", [])]
            print(f"| {cell} | {r['id']} | {_md_cell(r['description'])} "
                  f"| {_md_cell('、'.join(tag_vals)) or '—'} | {_md_cell(' '.join(signals)) or '—'} |")
        print("\n> 以上是粗篩候選；請依使用者描述讀敘述做語意挑選，剔掉不貼近的。")
    else:
        print(f"找到 {len(results)} 筆候選（私有 DB）：\n")
        for r in results:
            head = f"#{r['id']}"
            if r.get("favorite"):
                head += "  ★"
            if r.get("rating"):
                head += f"  {r['rating']}/5"
            print(head)
            print(f"  {r['description']}")
            print(f"  tags: {'、'.join(r.get('tags', [])) or '（無）'}")
            if r.get("thumbnail_path"):
                print(f"  thumb: {media_url(r['thumbnail_path'])}")
            print()
        print("→ 以上是粗篩候選；請依使用者的描述語意挑出最貼近的幾張。")


def main() -> None:
    p = argparse.ArgumentParser(description="PosePlanner 後端選擇與私有 DB 客戶端")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="看目前後端與連線狀態").set_defaults(func=cmd_status)
    sub.add_parser("setup", help="互動式選擇後端（終端機用）").set_defaults(func=cmd_setup)

    ps = sub.add_parser("set", help="設定後端（drive / selfhost）")
    ps.add_argument("--backend", required=True, choices=["drive", "selfhost"])
    ps.add_argument("--base-url", help="selfhost server 位址，如 http://192.168.1.50:8000")
    ps.add_argument("--token", help="selfhost token：入庫端用『讀寫』token "
                    "（POSEPLANNER_TOKEN）；只搜尋的人用『唯讀』token（POSEPLANNER_READ_TOKEN）")
    ps.set_defaults(func=cmd_set)

    pin = sub.add_parser("ingest", help="selfhost：manifest 裡的原圖逐張 POST /images")
    pin.add_argument("--manifest", required=True)
    pin.set_defaults(func=cmd_ingest)

    pim = sub.add_parser("image", help="selfhost：單張原圖直接 POST /images")
    pim.add_argument("--image", required=True)
    pim.add_argument("--description", required=True)
    pim.add_argument("--tag", action="append", help="category=name，可重複")
    pim.add_argument("--source")
    pim.add_argument("--rating", type=int)
    pim.add_argument("--favorite", action="store_true")
    pim.set_defaults(func=cmd_image)

    pp = sub.add_parser("push", help="selfhost：POST 一個 fragment zip 到 /fragments")
    pp.add_argument("--fragment", required=True)
    pp.set_defaults(func=cmd_push)

    psr = sub.add_parser("search", help="selfhost：對 server /search 粗篩候選")
    psr.add_argument("query", nargs="?", default="")
    psr.add_argument("--tag", action="append", help="category=name，可重複（AND）")
    psr.add_argument("--limit", type=int, default=20)
    psr.add_argument("--json", action="store_true")
    psr.add_argument("--table", action="store_true", help="印 Markdown 表（含縮圖 URL）")
    psr.set_defaults(func=cmd_search)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
