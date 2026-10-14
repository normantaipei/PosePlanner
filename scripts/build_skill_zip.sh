#!/usr/bin/env bash
# 把 PosePlanner 打包成 skill .zip，給「不想 git clone」的 Claude Code 機器安裝
# 用法：bash scripts/build_skill_zip.sh
# 產出：dist/poseplanner-skill.zip（SKILL.md 在 zip 根目錄）
# 安裝：unzip dist/poseplanner-skill.zip -d ~/.claude/skills/poseplanner
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="$ROOT/dist/poseplanner-skill.zip"
mkdir -p "$ROOT/dist"
rm -f "$OUT"

# 從 .env 烤出 data/config.json，讓打包出去的 skill 一解壓就是設定好的狀態
# （私有 DB 已連線，或指定好本機圖庫資料夾）。沒有 .env 就維持原樣（預設本機資料夾 ~/PosePlanner）。
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
elif backend == "local":
    path = os.environ.get("POSEPLANNER_LIBRARY", "").strip()
    if path:
        cfg["local"] = {"path": path}
else:
    raise SystemExit(f"✗ .env 的 POSEPLANNER_BACKEND 只能是 local 或 selfhost（收到 {backend}）")
pathlib.Path("data").mkdir(exist_ok=True)
pathlib.Path("data/config.json").write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PY
  BAKED_CONFIG="data/config.json"
  echo "🔐 已從 .env 烤入連線設定（${POSEPLANNER_BACKEND}）→ data/config.json"
else
  echo "ℹ 找不到 .env，略過烤設定；skill 預設用本機資料夾 ~/PosePlanner 當圖庫。"
fi

# 只放 skill 執行需要的檔；SKILL.md 必須在 zip 根目錄。
# vendor/sqlite-vec/ 是向量引擎的二進位（含 mac/linux），本機圖庫要靠它載入 sqlite-vec。
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
    "scripts/make_plan.py",
    "scripts/crop_ui.py",
    "scripts/vecdb.py",
    "scripts/schema.sql",
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
echo "下一步：在跑 Claude Code 的機器上解壓到 skills 資料夾，重開 Claude Code 即可："
echo "    unzip -o ${OUT#$ROOT/} -d ~/.claude/skills/poseplanner"
