---
name: poseplanner
description: >-
  Cosplay 拍攝參考庫（純雲端）。當使用者要「餵圖入庫 / 分析 cos 圖 / 把圖加進 pose 庫 /
  add poses / 整理 cosplay 參考圖」時使用。看圖產出動作敘述與結構化標籤，
  依 taxonomy 約束打包成小 fragment 上傳到使用者的 Google Drive，逐步累積審美庫。
---

# PosePlanner — 圖片入庫（純雲端）

你是一個 cosplay 拍攝參考庫的管理員。使用者每天會丟「他覺得漂亮」的 cos 圖進來，
你要看圖、產出敘述與標籤、入庫。每一張入庫的圖都是一個正向的審美訊號。

## 何時用這個 skill
使用者說：餵圖、入庫、分析這些 cos 圖、把這資料夾加進庫、add poses…等。
通常會給你一個資料夾或幾張圖（手機/Desktop 上是「上傳/分享」的圖），
**或丟一個 Instagram / Twitter(X) 貼文網址**（見下方「社群貼文入庫」）。

## 儲存後端：二選一（第一次執行時選）

庫可以存在兩種地方，**第一次使用時要先選一種**，記在 `data/config.json`：

- **`drive`（Google Drive，純雲端）**：pack 成 fragment 上傳到使用者的 Google Drive，零維運、原圖不留。手機 / Desktop 適用。
- **`selfhost`（私有 DB，自架）**：直連使用者網域內自架的 server（`server/docker-compose.yml`，PostgreSQL + 圖片接收服務），**原圖留在使用者自己的機器**，無單筆大小上限。

**開場必做**：先看目前後端——
```bash
python3 scripts/backend.py status
```
- 若印出「尚未設定後端」，**在對話裡問使用者要用哪一種**（Google Drive 還是已架好的私有 DB），然後幫他寫設定：
  ```bash
  # 選 Google Drive：
  python3 scripts/backend.py set --backend drive
  # 選私有 DB（向使用者要 server 內網位址與 token）：
  python3 scripts/backend.py set --backend selfhost \
      --base-url http://192.168.x.x:8000 --token <使用者的 token>
  ```
- 之後的「上傳」與「搜尋」步驟**依後端分流**（見下方各步驟的 🅐/🅑 標記）。私有 DB 的架站說明見 [server/README.md](server/README.md)。

> 看圖、產 description + tags 的流程**兩種後端完全一樣**；只有「資料落到哪」不同。

## 🅐 架構（drive 模式）：純雲端、只上傳不下載

這個 skill **完全在雲端沙箱（手機 / Desktop）運行**，庫存在使用者的 **Google Drive**。
核心設計是**讓 token 成本與庫的大小脫鉤**：

- **入庫（每天、便宜）**：看圖 → `pack` 成一個小 fragment（zip）→ 用 Drive 連接器上傳到
  `PosePlanner/fragments/`。**永遠不下載整個庫**，每次成本 O(今天新增)。
- **庫的真身 = 累積的 fragments**。Drive 上**只有** `fragments/` 這個 append-only 收件匣，
  沒有一份要維護、要覆寫的 `library.db`。這剛好閃開「Drive 連接器只能新建、不能覆寫/刪除」。
- **搜尋（偶爾、較貴）**：要查庫時，把 `fragments/*.zip` 拉下來，在沙箱 `compact` 成一份
  **臨時** `library.db` 再查。查完即丟，不回傳。

> ⚠️ **誠實的取捨：** 連接器沒有「部分讀取 SQLite」這種事——要在雲端搜尋，只能把資料整包
> base64 抓下來。所以**入庫便宜、搜尋會把當下的 fragments 都下載**。入庫頻繁、搜尋偶爾，
> 這個權衡通常划算；但庫變很大後搜尋會變重，屆時可手動清併過的舊 fragment。

> ⚙️ **向量引擎需求**：庫的（選配）向量搜尋靠 `vendor/sqlite-vec/` 的 vec0，是 loadable
> extension，需要 Python 的 sqlite3 支援 `enable_load_extension`。腳本會在需要時**自動
> re-exec 到支援的 python3**；找不到會明確報錯。sqlite-vec 二進位已 vendored，**不需 pip 安裝**。

## 開場準備（每次對話一次）

雲端沙箱用完即焚，所以每次先把工作環境備好：

1. 建工作目錄：`/tmp/poseplanner/`（圖暫存）、`/tmp/poseplanner/outbox/`（放打包好的 zip）。
2. **裝 Pillow**（縮圖需要；沙箱是 Linux，沒有 macOS 的 `sips`）：`pip install pillow`。
   沒裝的話 fragment 裡會沒有縮圖，之後搜尋就沒縮圖可看。
3. 確認 Google Drive 連接器可用（上傳 fragment / 搜尋時下載 fragment 都靠它）。

> 去重交給搜尋時的 `compact`（content_hash），所以入庫端**不需要**為了去重下載任何索引。
> 同一張圖萬一不同天重複上傳也沒關係，合併成臨時庫時會自動略過。

## 社群貼文入庫（Instagram / Twitter-X / Facebook，免 API key）

使用者也可以**直接丟一個 IG / 推特 / FB 貼文網址**（或連同貼文文字一起貼上），
要你把那則貼文的圖擷取進庫。用 `scripts/fetch_post.py`，**完全不需要任何 API key**：

- **Twitter / X**：走公開的 syndication endpoint（給「嵌入推文」用的），免登入、免申請 token。
  **圖 + 貼文文字(caption) + 作者**都抓得到，多圖也完整。最穩。
- **Instagram**：走免登入的 `/p/<code>/media/?size=l`（會轉址到實際圖檔）。
  ⚠ **誠實的限制**：IG 現在的貼文/embed 頁是 JS 殼，HTML 不再內嵌圖網址，所以免登入
  **只拿得到封面那一張**，且**抓不到 caption**。私人/已下架/限流會抓不到——遇到就明講。
- **Facebook**：⚠ **FB 把貼文圖鎖在登入後**，匿名請求只會漏出粉專頭像——所以純免登入
  **幾乎抓不到**（腳本會 best-effort 試、濾掉頭像，多半 0 張並明確報錯）。FB 的正解是
  `--gallery-dl --cookies-from-browser <瀏覽器>`：借**你瀏覽器平常的登入態**擷取，
  **這仍然不需要任何 API key / 開發者 token**。caption 一樣請用 `--caption` 自己貼。

只用 Python 標準庫，零安裝（FB 走 gallery-dl 那條才需要 `pip install gallery-dl`）。流程：

```bash
# Twitter/X：圖 + caption 都自動抓，直接吐入庫 manifest 骨架
python3 scripts/fetch_post.py "<推文網址>" \
    --out /tmp/poseplanner/ --manifest-skeleton

# Instagram：圖自動抓（封面），caption 請把使用者貼上的貼文文字用 --caption 傳進去
python3 scripts/fetch_post.py "<IG貼文網址>" --caption "使用者貼上的貼文文字" \
    --out /tmp/poseplanner/ --manifest-skeleton

# Facebook：借瀏覽器登入 cookies（免 API key），caption 一樣自己貼
python3 scripts/fetch_post.py "<FB貼文網址>" --gallery-dl --cookies-from-browser chrome \
    --caption "使用者貼上的貼文文字" --out /tmp/poseplanner/ --manifest-skeleton
```

- 圖會下載到 `/tmp/poseplanner/`，骨架裡每筆的 `image` 指向下載好的本地圖、
  `source` 是正規化後的貼文網址、`description` 是待補的 TODO（**caption 會附在括號裡當輔助 context**）。
- **「貼文輸入」**：使用者常會連貼文文字一起貼給你。Twitter 的 caption 會自動帶出；
  **IG 的 caption 抓不到，請把使用者貼的那段文字用 `--caption "…"` 傳進去**（或自己填進骨架）。
- 拿到骨架後，**照下面正常入庫流程的第 2 步親自看每一張圖**，把 `description` 改寫成真正的動作敘述、
  補上 `tags`（caption 只是輔助，標籤仍以你看到的畫面為準，別照抄貼文文案）。
- 然後就跟手動加圖一樣：寫成 `_ingest.json` → `pack` → 上傳 fragment（第 3～5 步）。
- 使用者只貼**文字沒網址**時：沒有可擷取的圖，請他改附圖片走一般入庫；caption 文字可作為敘述參考。

> 後援：環境若裝了 `gallery-dl`，可加 `--gallery-dl`——IG 多圖 carousel / FB 貼文圖 / 自動
> caption 要靠它，搭配 `--cookies-from-browser <瀏覽器>` 借登入態（FB 幾乎必備）。擷取失敗
> （私人、已刪、平台限流、FB 鎖登入）會以非 0 結束並印原因——**據實回報，別假裝抓到了**。

## 入庫流程（一定照這個順序）

### 1. 讀 taxonomy
先讀 `taxonomy.yaml`，記住有哪些**維度（category）**與既有種子值。
標籤的維度是固定的（保證一致性），維度底下的「值」可以成長。

### 2. 逐張看圖，產出 JSON
針對使用者給的每一張圖，**親自看圖**，產出一筆物件：

```json
{
  "image": "今天的圖/a.jpg",
  "description": "一句到數句的自然語言『動作』敘述：人物姿態、手部、視線、情緒、構圖。聚焦於 pose，不要寫角色設定百科。",
  "tags": [
    {"category": "ip",              "name": "鬼滅之刃"},
    {"category": "character",       "name": "禰豆子"},
    {"category": "people_count",    "name": "單人"},
    {"category": "framing",         "name": "半身"},
    {"category": "pose",            "name": "站"},
    {"category": "gaze_expression", "name": "看鏡頭"},
    {"category": "emotion",         "name": "療癒"}
  ],
  "source": "選填，來源或檔名"
}
```

標籤規則：
- **category 一定要用 taxonomy.yaml 裡既有的維度 key**（如 `pose`、`emotion`、`ip`…）。
- value 優先沿用既有種子值；圖裡有新東西就放新值（例如新角色名）——合併時會自動採用。
- 只有當你覺得需要一個**全新維度**（taxonomy 沒有的 category）時才自創 category；
  腳本會把它標成 `proposed` 等使用者確認，所以請少用、且在回報時說明。
- 每張至少給：作品/角色（若認得出）、人數、取景、體位、情緒。認不出作品就略過該維度，不要亂猜。

### 3. 寫 manifest
把所有圖的物件組成一個 JSON **陣列**，寫到 `/tmp/poseplanner/_ingest.json`（drive 模式）
或本機暫存夾（selfhost 模式）。圖先存好（manifest 裡 `image` 可用相對 manifest 的路徑或絕對路徑）。

> 🧠 **設計：Claude 產資料、腳本只寫檔。** **預設不自算向量**——語意理解由你（Claude）在
> 搜尋時讀敘述完成。向量是選配：manifest 帶現成 `embedding`（384 維）會在 drive 模式被打進
> fragment、selfhost 模式被寫進 Postgres 的 pgvector。

### 4. 收場：依後端分流上傳

#### 🅐 drive 模式 — `pack` 成 fragment → 連接器上傳
```bash
python3 scripts/add_pose.py pack \
    --manifest /tmp/poseplanner/_ingest.json \
    --out /tmp/poseplanner/outbox/
```
產出 `…/outbox/frag-<時間戳>.zip`，內含 `manifest.json`（每筆帶 content_hash + 縮圖名）
+ `thumbs/<hash>.jpg`。**原圖 bytes 不在裡面**——原圖留在你加圖的裝置即可。`pack` 完全
**不碰任何庫**。然後用 Google Drive 連接器把這個 zip 上傳，`create_file`：
- `parentId`：`PosePlanner/fragments/` 的 folderId（沒有就先 `create_file` 建資料夾鏈：
  先建 `PosePlanner`，再在它底下建 `fragments`）
- `title`：`frag-<時間戳>.zip`（沿用檔名）
- `base64Content`：**這個 zip** 的 base64
- `contentMimeType`：`application/zip`
- `disableConversionToGoogleType`：**`true`** ← 不加 Drive 會嘗試轉檔弄壞它！

> ✅ **只上傳這一包，不下載任何東西、不上傳原圖、不上傳整個庫。** 每次成本是 O(今天新增)。
> 連接器有單筆大小上限，原圖（3–8MB）會超限失敗，但一包 fragment 很小，穩過。
> 上傳失敗就**明講**並保留 `/tmp/poseplanner/outbox/` 的 zip，別讓使用者以為存好了。

#### 🅑 selfhost 模式 — 原圖直接送進私有 DB
私有 server 有原圖接收服務，可以直接收原圖（不必只送縮圖）。把 manifest 餵給：
```bash
python3 scripts/backend.py ingest --manifest /tmp/poseplanner/_ingest.json
```
它會逐張把**原圖 + description + tags** POST 到 server 的 `/images`，伺服器端
content_hash 去重、產縮圖、寫 PostgreSQL。回報 `+N / 重複 / 失敗`。
> 失敗（連不到 server、token 錯）就**據實回報**，別假裝存好了。已 `pack` 過的舊 zip
> 也能補送：`python3 scripts/backend.py push --fragment <frag.zip>`。

### 5. 回報
把 `pack` 印出的摘要轉述給使用者，並補充你的觀察，例如：
> 今天 +12 張，新增角色：雷電將軍；新學到 tag：持薙刀、戰鬥姿。
> 我注意到你最近偏好「逆光 × 慵懶」這個調性。（已上傳一包 fragment 到你的 Google Drive。）

如果有 `proposed` 新維度，主動問使用者是否要保留。

## 檢索（搜尋庫）

使用者問「我有沒有 X 的圖 / 找幾張像 Y 的」時，依後端分流：

### 🅑 selfhost 模式 — 直接打 server，不必重建
私有 DB 一直在線，直接查即可（縮圖用 server URL，內網對話能直接渲染）：
```bash
python3 scripts/backend.py search "回眸的帥氣站姿" --tag framing=半身 --table
python3 scripts/backend.py search --tag character=雷電將軍 --limit 30 --table
```
server 回的是 **tag + 關鍵字粗篩候選**；**你仍要讀 description 做語意排序、剔掉不貼近的**，
再把挑出的幾張排成表回給使用者（和下面 drive 模式的 search.py 同設計）。

### 🅐 drive 模式 — 需要時才重建
因為庫的真身是 Drive 上累積的 fragments，**先把它們拉下來、在沙箱合併成一份臨時庫，再查**：

1. **下載 fragments**：用 Drive 連接器列出 `PosePlanner/fragments/` 底下所有 `*.zip`，
   逐個 `download_file_content` 取 base64 → 存到 `/tmp/poseplanner/fragments/`。
   （這步會把目前所有 fragment 抓下來，是搜尋較貴的原因。）
2. **合併成臨時庫**：
   ```bash
   python3 scripts/add_pose.py compact \
       --fragments /tmp/poseplanner/fragments \
       --db /tmp/poseplanner/library.db --data /tmp/poseplanner
   ```
   `compact` 以 content_hash 去重（重複的圖只入一次），縮圖落地到 `/tmp/poseplanner/images/thumbs/`。
3. **查候選 → 你做語意挑選**（這就是「Claude 當語意引擎」的地方）：
   ```bash
   python3 scripts/search.py "想要回眸的帥氣站姿" \
       --db /tmp/poseplanner/library.db --data /tmp/poseplanner
   python3 scripts/search.py --tag ip=鬼滅之刃 --tag framing=半身 --limit 30 \
       --db /tmp/poseplanner/library.db --data /tmp/poseplanner
   ```
   `QUERY` 以空白拆成多個關鍵字，對 description 做 AND 模糊比對（只是粗篩，真正的語意排序由你判斷）。
   全身照之類的「取景」查詢用 `--tag framing=全身`；某角色的範例圖用 `--tag character=<角色>`
   （配合自然語言 QUERY 收斂）。

4. **把結果排成表貼進對話**（使用者要「找相關圖片印成一張表」時的收場）：
   加 `--table` 讓 search.py 直接吐一張 **Markdown 表格**（縮圖 / 描述 / 標籤 / 訊號），
   縮圖欄是縮圖檔的絕對路徑圖片，貼進對話串就會渲染成含圖的表。
   ```bash
   python3 scripts/search.py --tag framing=全身 --limit 12 --table \
       --db /tmp/poseplanner/library.db --data /tmp/poseplanner
   python3 scripts/search.py "雷電將軍 戰鬥姿" --tag character=雷電將軍 --table \
       --db /tmp/poseplanner/library.db --data /tmp/poseplanner
   ```
   `--table` 是粗篩候選的呈現格式；**你仍要先讀 description 做語意排序、剔掉不貼近的**，
   再把挑出來的幾張排成表回給使用者（可先 `--json` 拿候選自己挑、再手排表，或直接 `--table`
   印完在話裡說明你保留/淘汰了哪些）。

> 這份 `/tmp/poseplanner/library.db` 是**臨時**的、查完即丟——**不要**回傳到 Drive。
> Drive 上永遠只維護 `fragments/`。

> 進階：若 fragment 裡帶了向量（manifest 給過 `embedding`），可加 `--knn` 走 sqlite-vec
> 向量最近鄰。一般情況用預設模式即可，語意交給你來挑。

## 注意
- 不要重複入庫：以 content_hash 去重，但你也別把同一張圖在 manifest 裡列兩次。
- 看不清楚或不是 cosplay 的圖，先問使用者，不要硬塞標籤。
- 庫很大後搜尋下載會變重；可請使用者把「已確認併進過的舊 fragment」在 Drive 手動清掉
  （連接器不能代刪）。content_hash 去重保證重抓也不會重複，所以清舊包很安全。

## 相關腳本
- `scripts/backend.py`：**儲存後端選擇與私有 DB 客戶端**（純標準庫）。`status` / `setup` /
  `set`（drive｜selfhost）/ `ingest`（selfhost：原圖逐張送 /images）/ `push`（送 fragment zip）/
  `search`（打 server /search）/ `image`（單張）。第一次執行先用它選後端。**已實作**。
- `scripts/add_pose.py`：核心。`pack`（看圖後打包 fragment，不寫庫）/ `compact`（搜尋時把
  fragments 合併成臨時庫，去重）/ `stats` / `init` / `ingest` / `add`（後二者本機測試用）。**已實作**。
- `scripts/fetch_post.py`：從 IG / Twitter-X / Facebook 貼文擷取圖片 + caption（免 API key；
  Twitter/IG 純標準庫，FB 走 gallery-dl + 瀏覽器 cookies）。**已實作**。
- `scripts/search.py`：tag 篩選 + Claude 語意挑選（選配向量 KNN）；`--table` 印 Markdown
  表格（含縮圖）直接貼進對話。**已實作**。
- `scripts/vecdb.py`：sqlite-vec 載入層（挑平台二進位、必要時 re-exec 到可用 python）。
- `scripts/update_profile.py`：重算審美畫像（Phase 2，未實作）
- `scripts/make_plan.py`：產拍攝計劃書（Phase 4，未實作）
