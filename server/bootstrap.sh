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
#   4. 收掉自己的舊容器後挑一個沒被占用的對外埠（從 API_PORT 起自動往上避讓），寫回 .env
#   5. docker compose up -d --build 把 PostgreSQL + 圖片接收服務拉起來
#   6. 印出健康檢查結果，以及 Claude 端要用的 base-url 與 token
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
die() { printf '\n\033[1;31m✗ %b\033[0m\n' "$*" >&2; exit 1; }

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

set_env_port() {  # 把 .env 的 API_PORT 設成 $1（沒有就新增）。不用 sed -i 以兼顧 GNU/BSD
  local p="$1"
  if [ -f .env ] && grep -qE '^API_PORT=' .env; then
    grep -vE '^API_PORT=' .env > .env.tmp && echo "API_PORT=${p}" >> .env.tmp && mv .env.tmp .env
  else
    echo "API_PORT=${p}" >> .env
  fi
}

ensure_token() {  # .env 完全沒有這個 token 欄位就補一組亂數（讓舊版 .env 升級也有兩組 token）
  local key="$1"
  if ! grep -qE "^${key}=" .env; then
    echo "${key}=$(rand_hex)$(rand_hex)" >> .env
    log "偵測到 .env 缺 ${key}，已補上一組亂數 token。"
  fi
}

# ── 1. Docker ───────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "安裝 Docker（官方 get.docker.com）…"
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker 2>/dev/null || true
else
  log "Docker 已安裝，略過。"
fi

# 把 Docker 官方 apt 來源加進去（docker-compose-plugin 只在官方來源才有）。
# 用在「Docker 是用 Debian 的 docker.io 裝的、官方來源沒被 get.docker.com 加過」的機器。
add_docker_apt_repo() {
  command -v curl >/dev/null 2>&1 || $SUDO apt-get install -y curl ca-certificates || return 1
  local codename arch
  codename="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")"
  [ -n "$codename" ] || return 1
  arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
  $SUDO install -m 0755 -d /etc/apt/keyrings || return 1
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg || return 1
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg || true
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${codename} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null || return 1
  $SUDO apt-get update -y || return 1
}

# compose：優先 docker compose（外掛），退而 docker-compose
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  # 逐步退讓地安裝，任何一步成功就停：
  #  1) 直接裝官方外掛（機器若已有 Docker 官方來源，這步就成）
  #  2) 加 Docker 官方 apt 來源後再裝外掛（docker.io 裝出來的 Docker 多半缺這來源）
  #  3) 退到 Debian 內建的 docker-compose（v1，舊但夠用）
  log "安裝 docker compose…"
  $SUDO apt-get update -y || true
  if $SUDO apt-get install -y docker-compose-plugin 2>/dev/null && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif add_docker_apt_repo && $SUDO apt-get install -y docker-compose-plugin 2>/dev/null && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif $SUDO apt-get install -y docker-compose 2>/dev/null && command -v docker-compose >/dev/null 2>&1; then
    log "改用 Debian 內建的 docker-compose（v1）。"
    COMPOSE="docker-compose"
  else
    die "裝不起來 docker compose。請手動擇一安裝後重跑：\n  sudo apt-get install -y docker-compose-plugin   （需 Docker 官方 apt 來源）\n  sudo apt-get install -y docker-compose           （Debian 內建 v1）"
  fi
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

# ── 3. .env（亂數密鑰；埠稍後決定）──────────────────────────────────
if [ ! -f .env ]; then
  log "產生 server/.env（亂數密碼與兩組 token：讀寫 / 唯讀）…"
  PW="$(rand_hex)"
  TOKEN="$(rand_hex)$(rand_hex)"
  READ_TOKEN="$(rand_hex)$(rand_hex)"
  cat > .env <<EOF
POSTGRES_USER=poseplanner
POSTGRES_PASSWORD=${PW}
POSTGRES_DB=poseplanner
POSEPLANNER_TOKEN=${TOKEN}
POSEPLANNER_READ_TOKEN=${READ_TOKEN}
API_PORT=${API_PORT}
EOF
fi
# 既有（舊版）.env 可能只有一組 token → 補齊缺少的那組
ensure_token POSEPLANNER_TOKEN
ensure_token POSEPLANNER_READ_TOKEN

# ── 4. 先收掉自己的舊容器，再決定對外埠（自動避讓）──────────────────
# 為什麼先 down：否則我們自己上一輪還跑著的 api 容器也占著那個埠，會被誤判成「衝突」。
# down 只移除容器/網路，**volume（Postgres 資料、圖片）會保留**，資料不丟。
log "停掉本專案舊容器以釋放埠（volume 資料保留）…"
$DOCKER_SUDO $COMPOSE down --remove-orphans >/dev/null 2>&1 || true

# 想要的埠：.env 既有的 API_PORT 優先，否則用起始值。占用就自動往上換並寫回 .env。
DESIRED="$API_PORT"
if grep -qE '^API_PORT=' .env; then
  DESIRED="$(grep -E '^API_PORT=' .env | cut -d= -f2- | tr -d ' \r')"
fi
PORT="$(find_free_port "$DESIRED")"
if [ "$PORT" != "$DESIRED" ]; then
  log "埠 ${DESIRED} 已被其他服務占用，自動改用 ${PORT}（已寫回 .env）。"
fi
set_env_port "$PORT"

# ── 5. 啟動 ─────────────────────────────────────────────────────────
log "建置並啟動容器（首次會拉映像、裝相依，請稍候）…"
$DOCKER_SUDO $COMPOSE up -d --build

# ── 6. 健康檢查 + 連線資訊（含兩組 token + 給 Claude 的 prompt）────────
log "等服務起來…"
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

echo
# 連線資訊與兩組 token 統一由 show-info.sh 印出（之後想再查一次也跑這支）。
bash ./show-info.sh
