# PosePlanner 找圖前端（Nuxt 3）

社群媒體風格（FB/IG 感）的搜尋 / 推薦 feed。**Nuxt 3 + SSR**，透過 server route 代理你的
私有 DB server——『讀取』token 只存在 server 端環境變數，**永遠不會出現在瀏覽器**，瀏覽器
也不直接連你的 DB server（圖片同樣經由 `/api/media` 串流代理）。

## 架構

```
瀏覽器 ──► Nuxt server (/api/search, /api/stats, /api/media/*) ──► 你的 Python DB server
                 ▲ token 只在這層補上（NUXT_POSEPLANNER_TOKEN）
```

- `server/api/*` — 後端代理（搜尋、庫狀態、圖片串流）。
- `composables/useFeed.ts` — feed 狀態：搜尋、標籤篩選、無限捲動分頁。
- `utils/pose.ts` — 純函式：把後端資料整理成卡片視圖物件。
- `components/` — `TopBar`、`PostCard`、`AppDrawer`、`DevModal`、`LightBox`。
- `pages/index.vue` — 主頁，串接以上元件 + 無限捲動 + 鍵盤/彈層。

## 設定

```bash
cp .env.example .env     # 填入 NUXT_POSEPLANNER_BASE_URL 與（可選）NUXT_POSEPLANNER_TOKEN
npm install
```

## 開發

```bash
npm run dev              # http://localhost:3000
```

## 上線

### 🚀 一鍵部署（空白 Linux VM）

在全新的 Linux 機器上,一行接上後端就上線(缺什麼裝什麼,可重複執行):

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/web/bootstrap.sh \
  | bash -s -- http://192.168.1.50:8000 <read_token>
```

[bootstrap.sh](bootstrap.sh) 會:裝 Docker/git → clone repo → `docker build` 前端映像 →
起容器(後端 domain + token 以**環境變數**帶入,不烤進映像、不外洩給瀏覽器;
`--restart unless-stopped` 開機自動起;埠被占用自動往上避讓)→ 印出網址。

> 換後端 / 換 token:重跑這支腳本帶新參數即可(會重建映像、重啟容器)。

### 手動(Docker)

```bash
docker build -t poseplanner-web .
docker run -d --name poseplanner-web --restart unless-stopped -p 8080:3000 \
  -e NUXT_POSEPLANNER_BASE_URL=http://192.168.x.x:8000 \
  -e NUXT_POSEPLANNER_TOKEN=<read_token> \
  poseplanner-web
```

### 手動(裸 Node)

```bash
npm run build
NUXT_POSEPLANNER_BASE_URL=http://192.168.x.x:8000 NUXT_POSEPLANNER_TOKEN=... \
  node .output/server/index.mjs        # 或：npm start
```

> 純靜態(`npm run generate`)會失去 server 代理 → token 就藏不住、圖片也代理不了。
> 這個前端刻意走 SSR;要靜態託管請改回「token 放前端」的設計。
