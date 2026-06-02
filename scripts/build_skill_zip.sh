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

# 只放 skill 執行需要的檔；SKILL.md 必須在 zip 根目錄。
# vendor/sqlite-vec/ 是向量引擎的二進位（含 mac/linux），雲端沙箱要靠它載入 sqlite-vec。
zip -r "$OUT" \
  SKILL.md \
  taxonomy.yaml \
  requirements.txt \
  scripts/add_pose.py \
  scripts/search.py \
  scripts/vecdb.py \
  scripts/schema.sql \
  scripts/probe_net.py \
  vendor/sqlite-vec/ \
  -x '*.DS_Store' >/dev/null

echo "✅ 打包完成：${OUT#$ROOT/}"
echo "   內容："
unzip -l "$OUT" | awk 'NR>3 && $4!="" {print "     "$4}'
echo
echo "下一步：Claude Desktop → 設定 → Capabilities/能力 → Skills → 上傳這個 zip"
