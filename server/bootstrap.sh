#!/usr/bin/env bash
# PosePlanner 私有 DB — 一鍵架設（給全新的空 Linux VM）
#
# 在一台乾淨的 Ubuntu / Debian VM 上直接跑：
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/server/bootstrap.sh | bash
#
# 這支腳本會（缺什麼裝什麼，可重複執行）：
#   1. 沒有 Docker 就用官方 get.docker.com 裝好 Docker + compose 外掛
#   2. 沒有 git 就裝 git，然後把 repo clone 到 ~/PosePlanner（已存在就 git pull）
#   3. server/.env 不存在就產一份，密碼與 token 用亂數自動產生
#   4. docker compose up -d --build 把 PostgreSQL + 圖片接收服務拉起來
#   5. 印出健康檢查結果，以及 Claude 端要用的 base-url 與 token
#
# 可用環境變數覆寫：
#   POSEPLANNER_DIR   安裝目錄（預設 $HOME/PosePlanner）
#   POSEPLANNER_REPO  repo 網址（預設官方 GitHub）
#   API_PORT          api 對外埠（預設 8000）
set -euo pipefail

REPO="${POSEPLANNER_REPO:-https://github.com/normantaipei/PosePlanner.git}"
DIR="${POSEPLANNER_DIR:-$HOME/PosePlanner}"
API_PORT="${API_PORT:-8000}"

# root 直接跑；非 root 就在需要時加 sudo
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

log() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }

rand_hex() {  # 32 hex 字元，優先 openssl，退而用 /dev/urandom
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 16
  else head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

port_in_use() {  # 這個 host 上有沒有人在 listen 這個埠（含其他 docker 容器發布的）
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1  # 偵測不了就當沒占用，交給 compose 自己報錯
  fi
}

find_free_port() {  # 從 $1 起往上找第一個沒被占用的埠
  local start="$1" cand i
  for i in $(seq 0 50); do
    cand=$((start + i))
    if ! port_in_use "$cand"; then echo "$cand"; return 0; fi
  done
  echo "$start"  # 找不到就回原值，讓 compose 自己報錯
}

# ── 1. Docker ───────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "安裝 Docker（官方 get.docker.com）…"
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker 2>/dev/null || true
else
  log "Docker 已安裝，略過。"
fi

# compose：優先 docker compose（外掛），退而 docker-compose
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  log "安裝 docker compose 外掛…"
  $SUDO apt-get update -y && $SUDO apt-get install -y docker-compose-plugin
  COMPOSE="docker compose"
fi
# 非 root 又還沒進 docker 群組時，這支腳本內仍用 sudo 跑 docker
DOCKER_SUDO=""
if [ -n "$SUDO" ] && ! docker info >/dev/null 2>&1; then DOCKER_SUDO="$SUDO"; fi

# ── 2. 取得程式碼 ───────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  log "安裝 git…"
  $SUDO apt-get update -y && $SUDO apt-get install -y git
fi

if [ -d "$DIR/.git" ]; then
  log "repo 已在 $DIR，git pull 更新…"
  git -C "$DIR" pull --ff-only || true
else
  log "clone $REPO → $DIR …"
  git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR/server"

# ── 3. 決定對外埠 + .env（亂數密鑰）─────────────────────────────────
# 埠來源優先序：既有 .env > 自動避讓（從 API_PORT 起找沒被占用的）。
# 這樣使用者的伺服器若已站了 8000（或你指定的埠），會自動往上換一個，不會撞車。
if [ -f .env ] && grep -qE '^API_PORT=' .env; then
  PORT="$(grep -E '^API_PORT=' .env | cut -d= -f2- | tr -d ' \r')"
  log "server/.env 已存在，沿用其中的對外埠 ${PORT}。"
else
  PORT="$(find_free_port "$API_PORT")"
  if [ "$PORT" != "$API_PORT" ]; then
    log "埠 ${API_PORT} 已被占用，自動改用 ${PORT}。（要指定別的：API_PORT=<埠> 再跑一次）"
  fi
fi

if [ ! -f .env ]; then
  log "產生 server/.env（亂數密碼與 token，對外埠 ${PORT}）…"
  PW="$(rand_hex)"
  TOKEN="$(rand_hex)$(rand_hex)"
  cat > .env <<EOF
POSTGRES_USER=poseplanner
POSTGRES_PASSWORD=${PW}
POSTGRES_DB=poseplanner
POSEPLANNER_TOKEN=${TOKEN}
API_PORT=${PORT}
EOF
fi
TOKEN="$(grep -E '^POSEPLANNER_TOKEN=' .env | cut -d= -f2-)"

# ── 4. 啟動 ─────────────────────────────────────────────────────────
log "建置並啟動容器（首次會拉映像、裝相依，請稍候）…"
$DOCKER_SUDO $COMPOSE up -d --build

# ── 5. 健康檢查 + 連線資訊 ─────────────────────────────────────────
log "等服務起來…"
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP="${IP:-<這台VM的IP>}"
echo
echo "=================================================================="
if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  echo "✅ PosePlanner 私有 DB 已啟動（對外埠 ${PORT}）。"
else
  echo "⚠ 容器已啟動，但 /health 暫時沒回應。看 log：$COMPOSE logs -f api"
fi
echo "  健康檢查 ：http://localhost:${PORT}/health"
echo "  網頁上傳 ：http://${IP}:${PORT}/"
echo
echo "在裝了 skill 的機器（Claude Code / Desktop）上設定後端："
echo "  python3 scripts/backend.py set --backend selfhost \\"
echo "      --base-url http://${IP}:${PORT} \\"
echo "      --token ${TOKEN}"
echo "=================================================================="
