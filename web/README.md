# PosePlanner 找圖前端

一個**只做搜尋**的純靜態頁面，社群媒體（FB / IG）風格：進頁先給你一輪「推薦」圖片
可以瀏覽，搜尋框打字就即時找圖。資料全部來自你自架的私有 DB server（`../server/`）。

- 框架：**Vue 3**（全域版，含模板編譯器；由 `build.py` vendored 進 `vendor/`，離線可跑，
  抓不到會自動 fallback 到 CDN）。
- 沒有 npm / Vite / 打包步驟。建構腳本 `build.py` 只用 Python 標準庫。

## 🚀 一鍵架設（全新空 VM）

在一台**乾淨的 Ubuntu / Debian VM** 上，把後端 domain 與『讀取』token 當「後綴」帶上，貼這一行——
Docker、git、python3、config.js、nginx 容器全自動搞定：

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/PosePlanner/main/web/bootstrap.sh \
  | bash -s -- http://192.168.1.50:8000 <read_token>
```

它會：裝 Docker / git / python3 → clone repo → 跑 `build.py`（產 `config.js`、vendored Vue、測連線）
→ 用 **nginx 容器**把純靜態的 `web/` 服務出去（`--restart unless-stopped`，開機自動起）→ 印出前端網址。

> 🔌 **埠自動避讓**：預設從 8080 起，被占用會自動往上換（8081、8082…）。
> 雲端 VM 記得到防火牆 / 安全群組放行那個 TCP 埠，否則外部連不進來。
>
> 後端沒設 token（純內網信任）時，token 留空：`… | bash -s -- http://192.168.1.50:8000`。
> 也可改用環境變數：`… | BASE_URL=http://192.168.1.50:8000 TOKEN=<read_token> bash`。
> 換後端 / 換 token：重跑同一行帶新參數即可（覆蓋 `config.js`、重啟容器）。
> 可用環境變數客製：`POSEPLANNER_DIR`（安裝目錄）、`WEB_PORT`（起始埠，預設 8080）。

## 一鍵建構（本機 / 已有環境）

```bash
# 互動式：會問你 server domain 與『讀取』token
python3 web/build.py

# 或直接給參數
python3 web/build.py --base-url http://192.168.1.50:8000 --token <read_token>

# 建構完順手起一個本機伺服器（建議用這個開，別走 file://）
python3 web/build.py --serve --port 8080
#   → 瀏覽器開 http://localhost:8080/
```

`build.py` 會：
1. 測一下連線（打 server 的 `/health`、`/stats`）。
2. 產生 `config.js`（內含 domain + token；已被 `.gitignore` 排除，不進版控）。
3. 把 Vue 抓進 `vendor/`（離線用；`--no-vendor` 可跳過改用 CDN）。
4. `--serve` 時起一個本機靜態伺服器。

## token 怎麼填

- 填 server 的**讀取** token（環境變數 `POSEPLANNER_READ_TOKEN`）。這頁只查詢、不入庫，
  用唯讀 token 最安全。
- server 兩個 token 都沒設（純內網信任）時，直接 Enter 留空即可。

## 跨源（CORS）

前端通常跟 server 不同源，所以 server 端要放行跨源讀取。`../server/api/app.py` 已內建
CORS middleware，預設放行所有來源（讀取仍受 token 把關）。要收斂時在 server 設環境變數：

```bash
POSEPLANNER_CORS_ORIGINS=http://localhost:8080,https://pose.example.com
```

## 功能

- **推薦 feed**：沒輸入關鍵字時，依「收藏 → 評分 → 時間」排序給一輪圖（server `/search` 無 q）。
- **搜尋**：打字 350ms 後即時查（對 description 做關鍵字粗篩）。
- **點標籤**：每張圖的 tag chip 可點，改成用該標籤篩選。
- **無限捲動**：往下滑自動載下一頁（server `/search` 的 `offset` 分頁）。
- **點圖放大**：lightbox 看原圖。

## 安全提醒

`config.js` 以明文存放 token，這頁是給你信任的人 / 內網查詢用的。別把含 `config.js` 的
`web/` 公開放到不特定人能存取的地方。
