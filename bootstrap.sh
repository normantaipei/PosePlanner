#!/usr/bin/env bash
# PosePlanner 一鍵：後端 → 前端 → skill zip，一次到位（自動把 domain / token 串到位）。
#
# 在一台乾淨機器上直接跑（會自己 clone 專案）：
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/bootstrap.sh | bash
#
# 或在已 clone 好的 repo 根目錄：
#   bash bootstrap.sh
#
# 它會（缺什麼裝什麼，可重複執行）：
#   1. 裝 git → clone/pull 專案到 ~/PosePlanner
#   2. 起【後端】（PostgreSQL + 圖片服務）；產出 server/.env（含 domain 與兩組 token）
#   3. 從 server/.env 讀出 domain + token，把『讀取 token』交給【前端】並起前端容器
#   4. 把 domain + 『讀寫 token』烤進【skill】並打包成 dist/poseplanner-skill.zip
#   5. 印出：前端網址、skill 安裝步驟
#
# 三個服務的 token 權限是分開的（使用者不用手動複製貼上）：
#   • 前端找圖頁 → 唯讀 token（只看得到縮圖，搬不走原圖）
#   • skill（入庫端） → 讀寫 token（看圖入庫 + 搜尋）
#
# 可用環境變數覆寫：
#   POSEPLANNER_DIR   安裝目錄（預設 $HOME/PosePlanner）
#   POSEPLANNER_REPO  repo 網址（預設官方 GitHub）
#   BASE_URL          後端對外 domain（預設自動偵測本機區網 IP，如 http://192.168.1.50:8000）
#   API_PORT          後端對外埠（預設 8000，被占用自動避讓）
#   WEB_PORT          前端對外埠（預設 8080，被占用自動避讓）
set -euo pipefail

REPO="${POSEPLANNER_REPO:-https://github.com/normantaipei/PosePlanner.git}"
DIR="${POSEPLANNER_DIR:-$HOME/PosePlanner}"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

log()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# docker 要不要 sudo：root 不用；非 root 又連不到 docker socket 才加 sudo（Mac/Desktop 不用）
if [ "$(id -u)" -eq 0 ]; then DSUDO=""
elif docker info >/dev/null 2>&1; then DSUDO=""
else DSUDO="sudo"; fi

# 區網 IP：前端容器與 skill 都要靠這個位址連回後端（容器內的 localhost 是容器自己，連不到 host）
detect_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then           # macOS
    for ifc in en0 en1 en2 en3; do
      ip="$(ipconfig getifaddr "$ifc" 2>/dev/null || true)"
      [ -n "$ip" ] && { echo "$ip"; return; }
    done
  fi
  if command -v hostname >/dev/null 2>&1; then            # Linux（GNU hostname -I）
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  fi
  return 1
}

# .env upsert：把 FILE 裡的 KEY 設成 VALUE（沒有就新增）。不用 sed -i 以兼顧 GNU/BSD。
upsert_env() {
  local key="$1" val="$2" file="$3"
  if [ -f "$file" ] && grep -qE "^${key}=" "$file"; then
    grep -vE "^${key}=" "$file" > "$file.tmp" && echo "${key}=${val}" >> "$file.tmp" && mv "$file.tmp" "$file"
  else
    echo "${key}=${val}" >> "$file"
  fi
}

# ── 0. git + 取得程式碼（curl|bash 也能用：自己 clone）──────────────
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
cd "$DIR"

# ── 1. 後端 ──────────────────────────────────────────────────────────
log "[1/3] 啟動後端（server/bootstrap.sh）…"
POSEPLANNER_DIR="$DIR" bash "$DIR/server/bootstrap.sh"

[ -f "$DIR/server/.env" ] || die "後端起來了卻找不到 server/.env，無法取得 domain/token。"

get() { grep -E "^$1=" "$DIR/server/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r' || true; }
API_PORT_FINAL="$(get API_PORT)"; API_PORT_FINAL="${API_PORT_FINAL:-8000}"
RW_TOKEN="$(get POSEPLANNER_TOKEN)"        # 讀寫：給 skill
READ_TOKEN="$(get POSEPLANNER_READ_TOKEN)" # 唯讀：給前端

# domain：使用者可用 BASE_URL 覆寫；否則用偵測到的區網 IP；都沒有才退 localhost（前端容器可能連不到）
if [ -z "${BASE_URL:-}" ]; then
  if HOST_IP="$(detect_ip)"; then
    BASE_URL="http://${HOST_IP}:${API_PORT_FINAL}"
  else
    BASE_URL="http://localhost:${API_PORT_FINAL}"
    log "⚠ 偵測不到區網 IP，domain 暫用 ${BASE_URL}；前端容器/skill 可能連不到後端，建議改用 BASE_URL=http://<本機IP>:${API_PORT_FINAL} 重跑。"
  fi
fi
log "後端 domain：${BASE_URL}（讀寫 token 給 skill、唯讀 token 給前端）"

# ── 2. 前端（自動拿 domain + 唯讀 token，免手動複製）──────────────────
log "[2/3] 啟動前端（web/bootstrap.sh，自動帶入 domain + 唯讀 token）…"
POSEPLANNER_DIR="$DIR" bash "$DIR/web/bootstrap.sh" "$BASE_URL" "$READ_TOKEN"

# 抓前端容器實際對外埠（web bootstrap 會自動避讓，這裡回查最終值）
WEB_PORT_FINAL="$($DSUDO docker port poseplanner-web 3000 2>/dev/null | head -1 | sed -E 's/.*:([0-9]+)$/\1/' || true)"
WEB_PORT_FINAL="${WEB_PORT_FINAL:-${WEB_PORT:-8080}}"

# ── 3. skill（自動拿 domain + 讀寫 token，烤進 zip）───────────────────
log "[3/3] 打包 skill（自動把 domain + 讀寫 token 烤進連線設定）…"
ROOT_ENV="$DIR/.env"
upsert_env POSEPLANNER_BACKEND  selfhost     "$ROOT_ENV"
upsert_env POSEPLANNER_BASE_URL "$BASE_URL"  "$ROOT_ENV"
upsert_env POSEPLANNER_TOKEN    "$RW_TOKEN"  "$ROOT_ENV"
bash "$DIR/scripts/build_skill_zip.sh"

SKILL_ZIP="$DIR/dist/poseplanner-skill.zip"

# ── 完成：教學 ───────────────────────────────────────────────────────
printf '\n\033[1;32m✅ 三個服務都就緒了！\033[0m\n'
cat <<EOF

────────────────────────────────────────────────────────────
① 打開前端找圖頁（瀏覽器）
    本機：     http://localhost:${WEB_PORT_FINAL}/
    區網/外網： ${BASE_URL%:*}:${WEB_PORT_FINAL}/   （雲端 VM 記得防火牆放行 TCP ${WEB_PORT_FINAL}）
  前端已自動帶入『唯讀』token：只看得到縮圖，搬不走原圖，可安心分享這個網址。

② 安裝 skill（入庫端，已自動烤入『讀寫』連線，上傳即用、免再設定）
    zip 路徑：${SKILL_ZIP}
    Claude Desktop → 設定 → Capabilities/能力 → Skills → Upload skill → 選上面這個 zip → 啟用
  之後在對話裡丟 cos 圖說「加進 pose 庫」，圖會直接入到你這台後端。

③ 連線資訊
    後端 domain：${BASE_URL}
    兩組 token（讀寫/唯讀）任何時候再查：bash ${DIR}/server/show-info.sh
────────────────────────────────────────────────────────────

重跑這支腳本即可重啟全部並自動重新串好 domain/token（資料保留）。停止：
  ${DSUDO:+sudo }docker rm -f poseplanner-web                    # 前端
  cd ${DIR}/server && ${DSUDO:+sudo }docker compose down         # 後端（volume 資料保留）
EOF
