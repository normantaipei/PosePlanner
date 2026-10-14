#!/usr/bin/env bash
# PosePlanner — 安裝 skill 到 Claude Code（非互動，給 AI 代跑或手動跑都行）
#
# 用法（不需要先 clone，腳本會自己 clone 到 ~/.claude/skills/poseplanner）：
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/scripts/install.sh \
#     | bash -s -- --backend local [--path ~/PosePlanner]
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/scripts/install.sh \
#     | bash -s -- --backend selfhost --base-url http://192.168.x.x:8000 --token <讀寫 token>
#
# 已經裝過再跑一次 = git pull 更新 + 重設後端（圖庫資料不動）。
# 結束碼：0 成功；2 參數錯誤；3 已裝好但本機 Python 不支援 sqlite-vec（local 需要，見訊息）；
#         4 已裝好但連不到 selfhost server（見訊息）。
set -euo pipefail

REPO_URL="${POSEPLANNER_REPO_URL:-https://github.com/normantaipei/PosePlanner.git}"
SKILL_DIR="${POSEPLANNER_SKILL_DIR:-$HOME/.claude/skills/poseplanner}"

BACKEND="" LIB_PATH="" BASE_URL="" TOKEN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)  BACKEND="${2:-}"; shift 2 ;;
    --path)     LIB_PATH="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --token)    TOKEN="${2:-}"; shift 2 ;;
    -h|--help)  sed -n '2,14p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "✗ 不認得的參數：$1" >&2; exit 2 ;;
  esac
done
case "$BACKEND" in
  local) ;;
  selfhost) [[ -n "$BASE_URL" ]] || { echo "✗ selfhost 需要 --base-url" >&2; exit 2; } ;;
  *) echo "✗ 需要 --backend local 或 --backend selfhost" >&2; exit 2 ;;
esac

log() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }

command -v git >/dev/null || { echo "✗ 需要 git" >&2; exit 2; }
command -v python3 >/dev/null || { echo "✗ 需要 python3" >&2; exit 2; }

# ── 1. 取得 skill ────────────────────────────────────────────────────
if [[ -L "$SKILL_DIR" ]]; then
  log "已是軟連結（開發者安裝）：$SKILL_DIR → $(readlink "$SKILL_DIR")，不動它"
elif [[ -d "$SKILL_DIR/.git" ]]; then
  log "已安裝，更新：$SKILL_DIR"
  git -C "$SKILL_DIR" pull --ff-only --quiet
elif [[ -e "$SKILL_DIR" ]]; then
  echo "✗ $SKILL_DIR 已存在但不是 git clone，請先移走它再重跑。" >&2; exit 2
else
  log "下載 skill 到 $SKILL_DIR"
  mkdir -p "$(dirname "$SKILL_DIR")"
  git clone --depth 1 --quiet "$REPO_URL" "$SKILL_DIR"
fi
cd "$SKILL_DIR"

# ── 2. 設定後端 ──────────────────────────────────────────────────────
log "設定後端：$BACKEND"
if [[ "$BACKEND" == "local" ]]; then
  python3 scripts/backend.py set --backend local ${LIB_PATH:+--path "$LIB_PATH"}
else
  python3 scripts/backend.py set --backend selfhost --base-url "$BASE_URL" --token "$TOKEN"
fi

# ── 3. local：建庫，順便驗證 Python 能載入 sqlite-vec ──────────────────
if [[ "$BACKEND" == "local" ]]; then
  log "建立圖庫並檢查 sqlite-vec"
  if ! python3 scripts/add_pose.py init; then
    cat >&2 <<'EOF'

⚠ skill 已裝好，但這台的 Python 不支援載入 sqlite-vec，本機圖庫還不能用。
  macOS 內建的 /usr/bin/python3 不支援，裝 Homebrew 的 Python 即可（腳本會自動改用它）：
      brew install python
  裝完重跑這支安裝腳本（同樣的參數）即可。
EOF
    exit 3
  fi
fi

STATUS="$(python3 scripts/backend.py status)"
echo "$STATUS"
if [[ "$BACKEND" == "selfhost" ]] && ! grep -q "連線    ：OK" <<<"$STATUS"; then
  cat >&2 <<'EOF'

⚠ skill 已裝好、設定已存，但連不到私有 DB server。請確認：
  • 這台電腦跟 server 在同一個區網，server 有在跑（curl <base-url>/health）
  • base-url 的 IP / 埠正確，token 是「讀寫」token
  修正後重跑這支安裝腳本（或 python3 scripts/backend.py set ...）即可。
EOF
  exit 4
fi
cat <<EOF

✅ PosePlanner 安裝完成：$SKILL_DIR
   重開 Claude Code（或開新對話），輸入 /poseplanner，或直接說「幫我把這些 cos 圖入庫」。
EOF
