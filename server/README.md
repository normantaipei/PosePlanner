# PosePlanner 私有 DB（自架）

Google Drive 模式之外的**第二種儲存後端**：把庫存在**你網域內**的一台機器上——
PostgreSQL 存 metadata、本機硬碟存原圖與縮圖，並提供一個 HTTP **圖片接收服務**。

和純雲端（Drive）模式的差別：

| | Google Drive | 私有 DB（這個） |
|---|---|---|
| 資料落點 | 你的 Google Drive | 你自己的機器 / 內網 |
| 原圖 | 不存（只上傳縮圖 fragment） | **存原圖**（無單筆大小上限） |
| 連線 | Claude 的 Drive 連接器 | 直連 `http://<內網IP>:8000` |
| 適合 | 手機 / 隨身、零維運 | 家用 NAS / 工作室主機、要留原圖 |

> 二選一，由 `backend.py` 記在 `data/config.json`。第一次執行時 skill 會問你。

---

## 啟動

需要 Docker + Docker Compose。

```bash
cd server
cp .env.example .env          # ⚠ 改掉 POSTGRES_PASSWORD 與 POSEPLANNER_TOKEN
docker compose up -d --build
```

起來後：

```bash
curl http://localhost:8000/health           # {"ok": true, ...}
open  http://localhost:8000/                 # 極簡網頁上傳表單（手動丟圖測試）
```

容器：

- **db** — `pgvector/pgvector:pg16`，資料存在 named volume `pgdata`。預設**不對外開 5432**，只在 compose 內網給 api 用。
- **api** — FastAPI 接收服務（埠 `8000`），原圖 / 縮圖存在 named volume `media`（容器內 `/data`）。啟動時自動套 `db/schema.sql` 並用 `taxonomy.yaml` seed 標籤。

---

## 讓 Claude 端連上

在裝了 skill 的機器（Claude Code / Desktop 沙箱）上：

```bash
python3 scripts/backend.py set --backend selfhost \
    --base-url http://192.168.x.x:8000 \
    --token <你在 .env 設的 POSEPLANNER_TOKEN>
python3 scripts/backend.py status            # 確認連得到
```

之後入庫 / 搜尋就會自動走這台 server（見專案根 `SKILL.md`）。

---

## API

| 方法 | 路徑 | 說明 | 權限 |
|---|---|---|---|
| GET | `/health` | 健康檢查 | 公開 |
| GET | `/` | 網頁上傳表單 | 公開 |
| GET | `/stats` | poses / tags 數 | 讀取 |
| POST | `/images` | 收**一張原圖 + metadata** 直接入庫（去重、產縮圖、存原圖） | 讀寫 |
| POST | `/fragments` | 收一個 `pack` 出來的 fragment zip 回放併庫（相容雲端格式，只帶縮圖） | 讀寫 |
| PUT | `/poses/{id}/tags` | 改一張 pose 的 tags（整批替換 / 只新增 / 只移除） | 讀寫 |
| GET | `/search?q=&tag=&limit=` | tag + 關鍵字粗篩候選（語意排序交給 Claude） | 讀取 |
| GET | `/thumbs/{hash}.jpg` | 取縮圖 | 讀取 |
| GET | `/images/{hash}.ext` | 取原圖 | 讀取 |
| GET | `/skill` | 下載打包好的 skill zip（連線設定即時烤入） | **僅限區網** |

### `/skill` — 區網內直接下載 skill

不必手動 `scp` skill 檔。在**區網內**的另一台機器 / 手機開：

```
http://<這台機器內網IP>:8000/skill            # 讀寫版（自己入庫用，預設）
http://<這台機器內網IP>:8000/skill?token=ro    # 唯讀版（只給人搜尋）
http://<這台機器內網IP>:8000/skill?token=none  # 不烤 token（上傳後自己再設）
```

會即時把 `SKILL.md` / `scripts/` / `vendor/sqlite-vec/` 打包成 `poseplanner-skill.zip`，
並把 `data/config.json` 烤成「指向這台 server」——下載解開即為**私有 DB 已連線**狀態，
直接傳到 Claude Desktop → 設定 → Capabilities → Skills → 上傳即可。

- **安全**：只放行**私有網段 / loopback / link-local** 的來源 IP（用真實 TCP 對端，
  **不信任**可偽造的 `X-Forwarded-For`），公網來源一律 `403`。要放行 VPN 子網等額外網段，
  設 `POSEPLANNER_SKILL_ALLOW_CIDRS=100.64.0.0/10,...`（compose 已預留環境變數）。
- ⚠ **若你把 api 擺在反向代理（nginx 等）後面**：server 看到的對端會是代理的 IP（多半是
  loopback/私有），等於對所有經代理的人開放——這種情境請改在**代理層**限制 `/skill` 的來源。
- repo 由 compose 以 `../:/repo:ro` 唯讀掛進容器供即時打包；改了 `POSEPLANNER_REPO` 要對應調整。

`/images` 的 multipart 欄位：`file`（圖）、`description`、`tags`（JSON 陣列
`[{"category","name"},...]`）、選填 `source` / `rating` / `favorite` / `embedding` / `embedding_model`。

`/poses/{id}/tags` 的 JSON body（三個欄位皆為 `[{"category","name"},...]`，可混用）：

```jsonc
{"tags":   [...]}   // 整批替換成這份清單
{"add":    [...]}   // 只新增（已有的略過）
{"remove": [...]}   // 只移除
```

新標籤沿用入庫規則（既有維度→`active`、未知維度→`proposed`），並維護 `tags.usage_count`。
回傳 `{"id", "added", "removed", "tags"}`，`tags` 為更新後完整清單；pose 不存在回 `404`。

```bash
# 整批替換
curl -X PUT http://localhost:8000/poses/42/tags \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"tags":[{"category":"people_count","name":"單人"},{"category":"framing","name":"半身"}]}'

# 只加一個、移掉一個
curl -X PUT http://localhost:8000/poses/42/tags \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"add":[{"category":"emotion","name":"開朗"}],"remove":[{"category":"framing","name":"全身"}]}'
```

### 權限：兩組 token

- **讀寫** token（`.env` 的 `POSEPLANNER_TOKEN`）：可入庫 + 查詢 + 取圖。
- **唯讀** token（`POSEPLANNER_READ_TOKEN`）：只能查詢 + 取圖，**不能入庫**。
- 兩個都留空 = 純內網信任模式（完全不檢查）。

帶 token 兩種方式：`Authorization: Bearer <token>` 標頭，或 `?t=<token>` query 參數
（後者讓對話裡的 `<img>` 縮圖網址也能帶讀取權限渲染——`backend.py search --table`
會自動把設定的 token 接在縮圖網址後面）。

---

## 備份 / 還原

```bash
# 備份 Postgres
docker compose exec db pg_dump -U poseplanner poseplanner > backup.sql
# 備份原圖 / 縮圖
docker run --rm -v poseplanner_media:/data -v "$PWD":/out alpine \
    tar czf /out/media.tgz -C /data .
```

> volume 名稱前綴是 compose 專案名（資料夾名），實際名稱用 `docker volume ls` 確認。
