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

需要 Node runtime（SSR / 代理）：

```bash
npm run build
node .output/server/index.mjs     # 或：npm start
# 同樣用環境變數提供連線資訊：
#   NUXT_POSEPLANNER_BASE_URL=http://192.168.x.x:8000 NUXT_POSEPLANNER_TOKEN=... node .output/server/index.mjs
```

> 純靜態（`npm run generate`）會失去 server 代理 → token 就藏不住、圖片也代理不了。
> 這個前端刻意走 SSR；要靜態託管請改回「token 放前端」的設計。
