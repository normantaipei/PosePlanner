#!/usr/bin/env bash
# 印出 PosePlanner 私有 DB 的連線資訊：base-url + 兩組 token + 給 Claude 的初始化 prompt。
# 架好後想再查一次就跑：（在 server/ 底下）
#   bash show-info.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "找不到 server/.env，請先架設後端（bootstrap.sh 或 docker compose up -d --build）。"
  exit 1
fi

# || true：欄位不存在（如純內網信任沒設 token）時，grep 回非 0 也不讓 set -e 中斷
get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r' || true; }
PORT="$(get API_PORT)"; PORT="${PORT:-8000}"
TOKEN="$(get POSEPLANNER_TOKEN)"
READ_TOKEN="$(get POSEPLANNER_READ_TOKEN)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"; IP="${IP:-<這台機器的IP>}"

echo "=================================================================="
if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
  echo "✅ PosePlanner 私有 DB 運作中（對外埠 ${PORT}）。"
else
  echo "⚠ /health 沒回應——容器可能還沒起來或沒在跑。看 log：docker compose logs -f api"
fi
echo "  健康檢查 ：http://localhost:${PORT}/health"
echo "  網頁上傳 ：http://${IP}:${PORT}/"
echo
echo "  base-url ：http://${IP}:${PORT}"
echo "  讀寫 token（入庫用）        ：${TOKEN:-（未設＝純內網信任，不檢查）}"
echo "  唯讀 token（只給人搜尋、不能改）：${READ_TOKEN:-（未設）}"
echo "=================================================================="
echo
echo "📋 把下面這段貼給 Claude（每個新對話開頭貼一次，它就記得怎麼連你的私有 DB）："
echo "------------------------------------------------------------------"
cat <<PROMPT
我的 PosePlanner 用「私有 DB」後端。請在開始任何入庫 / 搜尋前，先設定好連線：

    python3 scripts/backend.py set --backend selfhost \\
        --base-url http://${IP}:${PORT} --token ${TOKEN:-<讀寫 token>}
    python3 scripts/backend.py status

入庫：看完圖、寫好 manifest（_ingest.json）後，用
    python3 scripts/backend.py ingest --manifest <manifest 路徑>
（原圖會直接送進我的私有 DB，不走 Google Drive。）
搜尋：python3 scripts/backend.py search "<描述>" --tag <維度=值> --table
請勿在回覆裡明文印出 token。
PROMPT
echo "------------------------------------------------------------------"
echo
echo "（只想開放搜尋給別人 → 把上面 --token 換成『唯讀 token』給對方即可。）"
