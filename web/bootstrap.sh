#!/usr/bin/env bash
# PosePlanner 找圖前端 — 一鍵架設（給全新的空 Linux VM）
#
# 後端的 domain 與『讀取』token 用「後綴」帶上（兩種寫法擇一）：
#
#   # 位置參數（建議；像後綴一樣接在後面）
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/web/bootstrap.sh \
#     | bash -s -- http://192.168.1.50:8000 <read_token>
#
#   # 或用環境變數
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/web/bootstrap.sh \
#     | BASE_URL=http://192.168.1.50:8000 TOKEN=<read_token> bash
#
# 這支腳本會（缺什麼裝什麼，可重複執行）：
#   1. 沒有 Docker 就用官方 get.docker.com 裝好 Docker
#   2. 沒有 git / python3 就裝起來，然後把 repo clone 到 ~/PosePlanner（已存在就 git pull）
#   3. 用 web/build.py 產生 web/config.js（寫入後端 domain + 讀取 token）、把 Vue 抓進 vendor/、順手測連線
#   4. 收掉自己的舊前端容器後挑一個沒被占用的對外埠（從 WEB_PORT 起自動往上避讓）
#   5. 用 nginx 容器把純靜態的 web/ 服務出去（--restart unless-stopped，開機自動起）
#   6. 印出前端網址
#
# 帶後端連線資訊的方式（domain 必填；token 視後端有沒有設）：
#   $1 / BASE_URL   後端 domain，如 http://192.168.1.50:8000
#   $2 / TOKEN      後端的『讀取』token（POSEPLANNER_READ_TOKEN；後端沒設就留空 ""）
#
# 可用環境變數覆寫：
#   POSEPLANNER_DIR   安裝目錄（預設 $HOME/PosePlanner）
#   POSEPLANNER_REPO  repo 網址（預設官方 GitHub）
#   WEB_PORT          前端對外埠（預設 8080，被占用會自動往上避讓）
set -euo pipefail

REPO="${POSEPLANNER_REPO:-https://github.com/normantaipei/PosePlanner.git}"
DIR="${POSEPLANNER_DIR:-$HOME/PosePlanner}"
WEB_PORT="${WEB_PORT:-8080}"
CONTAINER="poseplanner-web"

# 後端連線資訊：位置參數優先，否則退回環境變數
BASE_URL="${1:-${BASE_URL:-}}"
TOKEN="${2:-${TOKEN:-}}"

# root 直接跑；非 root 就在需要時加 sudo
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

log() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

port_in_use() {  # 這個 host 上有沒有人在 listen 這個埠（含其他 docker 容器發布的）
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1  # 偵測不了就當沒占用，交給 docker 自己報錯
  fi
}

find_free_port() {  # 從 $1 起往上找第一個沒被占用的埠
  local start="$1" cand i
  for i in $(seq 0 50); do
    cand=$((start + i))
    if ! port_in_use "$cand"; then echo "$cand"; return 0; fi
  done
  echo "$start"  # 找不到就回原值，讓 docker 自己報錯
}

# ── 0. 檢查必填的後端 domain ─────────────────────────────────────────
if [ -z "$BASE_URL" ]; then
  die "沒帶後端 domain。用法：curl -fsSL …/web/bootstrap.sh | bash -s -- http://<後端IP>:<埠> <read_token>"
fi

# ── 1. Docker ───────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "安裝 Docker（官方 get.docker.com）…"
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker 2>/dev/null || true
else
  log "Docker 已安裝，略過。"
fi
# 非 root 又還沒進 docker 群組時，這支腳本內仍用 sudo 跑 docker
DOCKER_SUDO=""
if [ -n "$SUDO" ] && ! docker info >/dev/null 2>&1; then DOCKER_SUDO="$SUDO"; fi

# ── 2. git / python3 + 取得程式碼 ───────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  log "安裝 git…"
  $SUDO apt-get update -y && $SUDO apt-get install -y git
fi
if ! command -v python3 >/dev/null 2>&1; then
  log "安裝 python3（build.py 需要）…"
  $SUDO apt-get update -y && $SUDO apt-get install -y python3
fi

if [ -d "$DIR/.git" ]; then
  log "repo 已在 $DIR，git pull 更新…"
  git -C "$DIR" pull --ff-only || true
else
  log "clone $REPO → $DIR …"
  git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR/web"

# ── 3. 建構：產 config.js（domain + token）、vendored Vue、測連線 ─────
log "用 build.py 接上後端 ${BASE_URL} …"
python3 build.py --base-url "$BASE_URL" --token "$TOKEN"

# ── 4. 決定對外埠（自動避讓）─────────────────────────────────────────
log "收掉本專案舊前端容器以釋放埠…"
$DOCKER_SUDO docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

PORT="$(find_free_port "$WEB_PORT")"
if [ "$PORT" != "$WEB_PORT" ]; then
  log "埠 ${WEB_PORT} 已被其他服務占用，自動改用 ${PORT}。"
fi

# ── 5. 用 nginx 容器服務純靜態的 web/ ───────────────────────────────
log "啟動 nginx 容器服務前端（首次會拉映像，請稍候）…"
$DOCKER_SUDO docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "${PORT}:80" \
  -v "$DIR/web":/usr/share/nginx/html:ro \
  nginx:alpine >/dev/null

# ── 6. 連線資訊 ─────────────────────────────────────────────────────
# 對外 IP：抓第一個非 loopback 的 IPv4 當提示（抓不到就用 localhost）
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$HOST_IP" ] && HOST_IP="localhost"

TOKEN_NOTE="$([ -n "$TOKEN" ] && echo '(已寫入 config.js)' || echo '(空 — 後端純內網信任模式)')"
printf '\n\033[1;32m✅ 前端已上線\033[0m\n'
cat <<EOF

  本機：     http://localhost:${PORT}/
  區網/外網： http://${HOST_IP}:${PORT}/   （雲端 VM 記得到防火牆/安全群組放行 TCP ${PORT}）

  後端 domain： ${BASE_URL}
  讀取 token：  ${TOKEN_NOTE}

換後端 / 換 token：重跑這支腳本並帶上新的參數即可（會覆蓋 config.js、重啟容器）。
停止前端：  ${DOCKER_SUDO:+sudo }docker rm -f ${CONTAINER}
EOF
