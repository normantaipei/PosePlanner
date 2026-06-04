#!/usr/bin/env bash
# 把 PosePlanner 打包成 Claude Desktop 可上傳的 skill .zip
# 用法：bash scripts/build_skill_zip.sh
# 產出：dist/poseplanner-skill.zip（SKILL.md 在 zip 根目錄）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="$ROOT/dist/poseplanner-skill.zip"
mkdir -p "$ROOT/dist"
rm -f "$OUT"

# 從 .env 烤出 data/config.json，讓打包出去的 skill 一上傳就是「私有 DB 已連線」狀態，
# 不必每次在 Claude 開新 session 都重設後端。沒有 .env 就維持原樣（讓 skill 自己問使用者）。
BAKED_CONFIG=""
if [[ -f "$ROOT/.env" ]]; then
  set -a; source "$ROOT/.env"; set +a
  python3 - <<'PY'
import json, os, pathlib
backend = os.environ.get("POSEPLANNER_BACKEND", "selfhost")
cfg = {"backend": backend}
if backend == "selfhost":
    base = os.environ.get("POSEPLANNER_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit("✗ .env 缺 POSEPLANNER_BASE_URL")
    cfg["selfhost"] = {"base_url": base, "token": os.environ.get("POSEPLANNER_TOKEN", "")}
pathlib.Path("data").mkdir(exist_ok=True)
pathlib.Path("data/config.json").write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PY
  BAKED_CONFIG="data/config.json"
  echo "🔐 已從 .env 烤入連線設定（${POSEPLANNER_BACKEND}）→ data/config.json"
else
  echo "ℹ 找不到 .env，略過烤連線；skill 上傳後會在對話裡問你後端。"
fi

# 只放 skill 執行需要的檔；SKILL.md 必須在 zip 根目錄。
# vendor/sqlite-vec/ 是向量引擎的二進位（含 mac/linux），雲端沙箱要靠它載入 sqlite-vec。
# 用 Python 的 zipfile 打包，不依賴外部 zip/unzip 指令（乾淨 VM/Docker 預設常沒裝）。
BAKED_CONFIG="$BAKED_CONFIG" OUT="$OUT" python3 - <<'PY'
import os, zipfile, pathlib, sys

out = os.environ["OUT"]
items = [
    "SKILL.md",
    "taxonomy.yaml",
    "requirements.txt",
    "scripts/backend.py",
    "scripts/add_pose.py",
    "scripts/fetch_post.py",
    "scripts/search.py",
    "scripts/vecdb.py",
    "scripts/schema.sql",
    "scripts/probe_net.py",
    "vendor/sqlite-vec/",
]
baked = os.environ.get("BAKED_CONFIG", "")
if baked:
    items.insert(0, baked)

def files_for(item):
    p = pathlib.Path(item)
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file() and f.name != ".DS_Store":
                yield f
    elif p.is_file():
        if p.name != ".DS_Store":
            yield p
    else:
        sys.exit(f"✗ 找不到要打包的項目：{item}")

names = []
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for item in items:
        for f in files_for(item):
            z.write(f, f.as_posix())
            names.append(f.as_posix())

pathlib.Path(out + ".names").write_text("\n".join(names), encoding="utf-8")
PY

echo "✅ 打包完成：${OUT#$ROOT/}"
echo "   內容："
sed 's/^/     /' "$OUT.names"
rm -f "$OUT.names"
echo
echo "下一步：Claude Desktop → 設定 → Capabilities/能力 → Skills → 上傳這個 zip"
