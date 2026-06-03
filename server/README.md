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

| 方法 | 路徑 | 說明 | 需 token |
|---|---|---|---|
| GET | `/health` | 健康檢查 | 否 |
| GET | `/` | 網頁上傳表單 | 否 |
| GET | `/stats` | poses / tags 數 | 否 |
| POST | `/images` | 收**一張原圖 + metadata** 直接入庫（去重、產縮圖、存原圖） | 是 |
| POST | `/fragments` | 收一個 `pack` 出來的 fragment zip 回放併庫（相容雲端格式，只帶縮圖） | 是 |
| GET | `/search?q=&tag=&limit=` | tag + 關鍵字粗篩候選（語意排序交給 Claude） | 否 |
| GET | `/thumbs/{hash}.jpg` | 取縮圖 | 否 |
| GET | `/images/{hash}.ext` | 取原圖 | 否 |

`/images` 的 multipart 欄位：`file`（圖）、`description`、`tags`（JSON 陣列
`[{"category","name"},...]`）、選填 `source` / `rating` / `favorite` / `embedding` / `embedding_model`。

寫入端點用 `Authorization: Bearer <token>`。`.env` 的 `POSEPLANNER_TOKEN` 留空則不檢查
（純內網信任模式）。

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
