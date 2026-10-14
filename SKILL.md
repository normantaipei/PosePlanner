---
name: poseplanner
description: >-
  Cosplay 拍攝參考庫（Claude Code 本機執行）。當使用者要「餵圖入庫 / 分析 cos 圖 / 把圖加進 pose 庫 /
  add poses / 整理 cosplay 參考圖 / 找圖 / 刪圖 / 做拍攝計劃書」時使用。看圖產出動作敘述與結構化標籤，
  依 taxonomy 約束寫進使用者的本機資料夾圖庫（或自架私有 DB），逐步累積審美庫。
---

# PosePlanner — cosplay 拍攝參考庫

你是一個 cosplay 拍攝參考庫的管理員。使用者每天會丟「他覺得漂亮」的 cos 圖進來，
你要看圖、產出敘述與標籤、入庫。每一張入庫的圖都是一個正向的審美訊號。

> ⚠️ **這個 skill 只在 Claude Code（本機）執行。** 它要讀寫本機資料夾、或連使用者內網的私有 DB，
> Claude Desktop／手機／claude.ai 的雲端沙箱都做不到（連不到內網，資料也不會留下來）。
> 若你發現自己不在 Claude Code 環境（例如沒有使用者的本機檔案系統），請直接說明不支援，別假裝存好了。

## 何時用這個 skill
使用者說：餵圖、入庫、分析這些 cos 圖、把這資料夾加進庫、add poses、找圖、
**刪圖 / 把這張從庫移除 / delete**、**做一份拍攝計劃書 / shooting plan / 規劃某場地的拍攝**…等。
通常會給你一個本機資料夾或幾張圖的路徑，
**或丟一個 Instagram / Twitter(X) 貼文網址**（見下方「社群貼文入庫」）。

> 想做**拍攝計劃書**（挑場地 + 從庫挑 pose + 出一份 PDF）時，直接看下方
> 「## 拍攝計劃書（make_plan）」整個流程。

## 儲存後端：二選一

庫可以存在兩種地方，記在 skill 的 `data/config.json`：

- **`local`（本機資料夾，預設、零安裝）**：指定一個本機資料夾當圖庫，`library.db`（SQLite + sqlite-vec）、
  `images/`（原圖）、`images/thumbs/`（縮圖）都放在裡面。預設 `~/PosePlanner`。
  入庫 / 搜尋 / 刪圖走 `add_pose.py`、`search.py`。
- **`selfhost`（私有 DB，自架）**：直連使用者網域內自架的 server（`server/docker-compose.yml`，
  PostgreSQL + 圖片接收服務），多台電腦共用、可接找圖前端。入庫 / 搜尋 / 刪圖走 `backend.py`。

**開場必做**：先看目前後端——
```bash
python3 scripts/backend.py status
```
- 印出「尚未設定後端 → 預設用本機資料夾」時，**照預設 `~/PosePlanner` 直接用即可**；
  第一次入庫前順口告訴使用者庫會放在哪，想換位置或改用私有 DB 再幫他設定：
  ```bash
  # 本機資料夾（使用者指定路徑）：
  python3 scripts/backend.py set --backend local --path ~/Pictures/PosePlanner
  # 私有 DB（向使用者要 server 內網位址與 token）：
  python3 scripts/backend.py set --backend selfhost \
      --base-url http://192.168.x.x:8000 --token <使用者的 token>
  ```
  > 私有 DB 有兩組 token：**入庫**要用「讀寫」token（`POSEPLANNER_TOKEN`）；只搜尋不入庫的人用
  > 「唯讀」token（`POSEPLANNER_READ_TOKEN`）。要入庫卻只拿到唯讀 token 時，`/images` 會回 401——
  > 遇到就請使用者改用讀寫 token。token 請勿在回覆裡明文印出。
- 印出「不支援的後端 drive」：Google Drive 模式已移除，請使用者改選 local 或 selfhost；
  舊 Drive 上的資料見下方「從舊版 Google Drive 遷移」。
- 之後的「入庫 / 搜尋 / 刪除」步驟**依後端分流**（見下方各步驟的 🅐/🅑 標記）。
  私有 DB 的架站說明見 [server/README.md](server/README.md)。

> 看圖、產 description + tags 的流程**兩種後端完全一樣**；只有「資料落到哪」不同。

> ⚙️ **向量引擎需求（local）**：`library.db` 含 sqlite-vec 的 vec0 表，是 loadable extension，
> 需要 Python 的 sqlite3 支援 `enable_load_extension`。macOS 內建 `/usr/bin/python3` 不支援，
> 腳本會**自動 re-exec 到支援的 python3**（如 Homebrew 的）；找不到會明確報錯——照錯誤訊息請使用者
> `brew install python` 或設 `POSEPLANNER_PYTHON`。sqlite-vec 二進位已 vendored，**不需 pip 安裝**。
> 縮圖優先用 Pillow，沒裝時 macOS 會改用內建 `sips`。

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
- 然後就跟手動加圖一樣：寫成 `_ingest.json` → 依後端入庫（第 3～5 步）。
- 使用者只貼**文字沒網址**時：沒有可擷取的圖，請他改附圖片走一般入庫；caption 文字可作為敘述參考。

> 後援：環境若裝了 `gallery-dl`，可加 `--gallery-dl`——IG 多圖 carousel / FB 貼文圖 / 自動
> caption 要靠它，搭配 `--cookies-from-browser <瀏覽器>` 借登入態（FB 幾乎必備）。擷取失敗
> （私人、已刪、平台限流、FB 鎖登入）會以非 0 結束並印原因——**據實回報，別假裝抓到了**。

## 入庫流程（一定照這個順序）

### 1. 讀 taxonomy
先讀 `taxonomy.yaml`，記住有哪些**維度（category）**與既有種子值。
標籤的維度是固定的（保證一致性），維度底下的「值」可以成長。

### 2. 逐張看圖，產出 JSON
入庫需要**本機檔案路徑**（腳本要讀檔算 hash、複製原圖）。使用者只把圖貼在對話裡、你拿不到檔案路徑時，
請他給圖所在的資料夾或檔案位置。針對使用者給的每一張圖，**親自看圖**，產出一筆物件：

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
  "creators": [
    {"name": "模特兒名", "role": "model"},
    {"name": "攝影師名", "role": "photographer", "handle": "@ig_handle", "url": "https://..."}
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

創作者規則（`creators`，**選填**）：
- 一張圖可記**多個**創作者，每筆 `name` 必填、`role` 標角色（常見：`model` 模特兒、`photographer` 攝影師、`retoucher` 修圖、`makeup` 妝髮）。
- `role` 省略時存成 `creator`（未分類）。`handle`（社群帳號）、`url`（作品集/連結）、`note` 皆選填。
- 沒有可靠的創作者資訊就整段省略，不要亂填或臆測來源。`creators` 不在 taxonomy 約束內，是獨立的人物紀錄。

### 3. 寫 manifest
把所有圖的物件組成一個 JSON **陣列**，寫到 `/tmp/poseplanner/_ingest.json`。
manifest 裡 `image` 可用相對 manifest 的路徑或絕對路徑（通常直接用使用者給的原圖絕對路徑）。

> 🧠 **設計：Claude 產資料、腳本只寫檔。** **預設不自算向量**——語意理解由你（Claude）在
> 搜尋時讀敘述完成。向量是選配：manifest 帶現成 `embedding`（384 維）會被寫進
> local 的 sqlite-vec，或 selfhost 的 pgvector。

### 4. 依後端入庫

#### 🅐 local — 寫進本機資料夾圖庫
```bash
python3 scripts/add_pose.py ingest --manifest /tmp/poseplanner/_ingest.json
```
content_hash 去重 → 原圖複製進 `<圖庫>/images/` → 產縮圖 → 寫 `library.db`。第一次會自動建庫。
原圖已經複製進圖庫，使用者原本的檔案可以留著也可以刪。

#### 🅑 selfhost — 原圖直接送進私有 DB
```bash
python3 scripts/backend.py ingest --manifest /tmp/poseplanner/_ingest.json
```
它會逐張把**原圖 + description + tags** POST 到 server 的 `/images`，伺服器端
content_hash 去重、產縮圖、寫 PostgreSQL。回報 `+N / 重複 / 失敗`。

> 失敗（缺圖、連不到 server、token 錯）就**據實回報**，別假裝存好了。

### 5. 回報
把腳本印出的摘要轉述給使用者，並補充你的觀察，例如：
> 今天 +12 張，新增角色：雷電將軍；新學到 tag：持薙刀、戰鬥姿。
> 我注意到你最近偏好「逆光 × 慵懶」這個調性。

如果有 `proposed` 新維度，主動問使用者是否要保留。

## 檢索（搜尋庫）

使用者問「我有沒有 X 的圖 / 找幾張像 Y 的」時，依後端分流。

> 🔴 **搜尋的鐵則：回覆一定要看得到圖。**
> 使用者搜尋是為了「看圖挑」，不是讀標籤。所以**每一次搜尋的最終回覆都必須把挑出的圖以縮圖
> 渲染在對話裡**（Markdown `![](縮圖)` 的表格）——**絕不可以只回 tag / 文字描述**。
> - 流程固定：**先粗篩 → 你讀 description 做語意挑選 → 用 `--ids` 把選中的那幾張連縮圖一起印出來**。
> - 縮圖欄若出現 `—`（缺縮圖），照實說明那張入庫時沒縮圖，別默默吞掉。

### 🅐 local — 直接查本機圖庫
1. **粗篩候選**（這步給你看 description / tags，不是最終回覆）。
   用 `--json` 拿機器可讀候選，方便你讀敘述做語意挑選：
   ```bash
   python3 scripts/search.py "想要回眸的帥氣站姿" --json
   python3 scripts/search.py --tag ip=鬼滅之刃 --tag framing=半身 --limit 30 --json
   ```
   `QUERY` 以空白拆成多個關鍵字，對 description／tag 名稱／創作者名做 AND 模糊比對
   （只是粗篩，真正的語意排序由你判斷）。全身照之類的「取景」查詢用 `--tag framing=全身`；
   某角色的範例圖用 `--tag character=<角色>`（配合自然語言 QUERY 收斂）。

2. **語意挑選 → 用 `--ids` 把選中的圖連縮圖印成表**（**這才是回給使用者的最終輸出**）：
   你從候選裡讀 description、剔掉不貼近的、按貼近度排好，把選中的 `id` 用 `--ids` 傳回去，
   search.py 會**保留你給的順序**吐一張含縮圖的 Markdown 表（縮圖 / 描述 / 標籤 / 訊號）：
   ```bash
   python3 scripts/search.py --ids 7,3,12,5 --table
   ```
   縮圖欄是圖庫裡的**本機絕對路徑**，整段貼進對話就會渲染。
   **回覆務必含這張圖表**，再用一兩句說明你為什麼挑這些、淘汰了哪些。
   > 想直接呈現粗篩結果不另做挑選時，也可以一步到位 `--table`（仍含縮圖）：
   > `python3 scripts/search.py --tag framing=全身 --limit 12 --table`
   > 但只要使用者是在「找特定感覺的圖」，請走上面的 `--json` → `--ids` 兩段式，回覆品質更好。

> 進階：若庫裡有向量（入庫時 manifest 給過 `embedding`，或 `ingest --embed`），可加 `--knn` 走
> sqlite-vec 向量最近鄰。一般情況用預設模式即可，語意交給你來挑。

### 🅑 selfhost — 直接打 server
私有 DB 一直在線，縮圖是 server URL（內網對話能直接渲染）。流程：
```bash
# 1) 粗篩候選（讀敘述用）
python3 scripts/backend.py search "回眸的帥氣站姿" --tag framing=半身 --json
# 2) 讀 description 語意挑選後，把選中的排成含縮圖的表回給使用者
python3 scripts/backend.py search "回眸的帥氣站姿" --tag framing=半身 --table
```
server 回的是 **tag + 關鍵字粗篩候選**；**你仍要讀 description 做語意排序、剔掉不貼近的**。
**最終回覆一樣要含縮圖**（見上面的「搜尋鐵則」）：`--table` 的縮圖欄是帶 token 的 server URL，
整段貼進對話就會渲染。別只回 tag/文字——挑完後把保留的那幾列連縮圖一起貼，並說明取捨。

## 刪除（從庫移除圖）

使用者說「刪掉這張 / 這張不要了 / 移除剛剛那幾張 / delete #3」時用這個流程。

> 🔴 **刪除的鐵則：一定要先經使用者明確同意才能真的刪。**
> 1. **先定位**：刪除一律以「搜尋表上的 `#` 或 content_hash」指定。請先照上面的檢索流程把候選撈出來、
>    把要刪的那幾張**連縮圖渲染在對話裡**給使用者看。
> 2. **先 dry-run**：跑下面的指令（**不要**帶 `--confirm`）。它只會列出「將刪什麼」，不動任何資料。
> 3. **明確徵求同意**：把 dry-run 列出的清單 + 縮圖一起呈現，**等使用者清楚說「對，刪」**才往下。
>    使用者沒明確答應、或語氣含糊（「好像不太喜歡」）就**停在這裡問清楚**，不要自己決定刪。
> 4. **才執行**：得到同意後，重跑同一條指令並加 `--confirm`。

### 🅐 local — 直接從本機圖庫刪
id 是穩定的，直接刪即可（連帶清 tags/creators/向量 + 圖庫裡的原圖與縮圖檔）：
```bash
# 1) dry-run 預覽
python3 scripts/add_pose.py delete --ids 3,7
# 2) 取得使用者明確同意後才加 --confirm
python3 scripts/add_pose.py delete --ids 3,7 --confirm
```
- `--ids` 用搜尋表上的 `#`；也可用 `--hash <content_hash 或前綴>` 直接指定。
- 找不到的 id/hash 會明確列出來，別硬刪。刪除無法復原（除非使用者自己有備份圖庫資料夾）。

### 🅑 selfhost — 直接打 server 刪
私有 DB 一直在線、id 是穩定的，直接刪即可（連帶清 tags/creators + 磁碟原圖縮圖）：
```bash
# 1) dry-run 預覽
python3 scripts/backend.py delete --ids 3,7
# 2) 取得使用者明確同意後才加 --confirm
python3 scripts/backend.py delete --ids 3,7 --confirm
```
失敗（連不到 server、token 非讀寫）就**據實回報**，別假裝刪好了。

## 拍攝計劃書（make_plan）

使用者說「幫我規劃在 ○○ 的拍攝 / 做一份拍攝計劃書 / shooting plan」時，把「**研究場地 →
從庫挑 pose → 排版成 PDF**」串成一份計劃書。產物風格對齊使用者既有的
`acosta_taipei_shooting_plan_v2.pdf`（白底、灰標籤格、細框線表格、粗體標題加底線、參考圖虛線框）：
**封面 + 場地資訊 + 時間表 + 每個 Look 一頁**。

> 🧠 一樣是「**Claude 產資料、腳本只排版**」：你負責看圖、挑 pose、研究場地、寫出 `plan.json`，
> `scripts/make_plan.py` 只把它渲染成 HTML/PDF。

### 1. 整理參考素材
- 使用者丟的若是**手機 App 截圖**（Pinterest / IG / X，上方有狀態列、下方有按鈕列與推薦），
  先用 `crop_ui.py` 把上下 UI 外框裁掉、只留主圖：
  ```bash
  python3 scripts/crop_ui.py <資料夾或圖> --out-dir reference/ --dry-run   # 先看偵測邊界
  python3 scripts/crop_ui.py <資料夾或圖> --out-dir reference/             # 實際裁
  ```
  偵測不準時用 `--top/--bottom/--left/--right` 手動覆寫（像素）。
- 把使用者的**實拍範例 / 角色插畫**也放進 `reference/`，這些是計劃書的「預想圖」。

### 2. 研究場地（web search）
搜該場地的設施 / 地標 / 票務 / 開放時間 / 交通 / 適合取景點，整理成一份
`location/<場地>.md`（地標表 + 拍攝建議）。這份是計劃書「場地資訊」與「時間表」的素材來源。

### 3. 從 pose 庫挑相符動作
照「## 檢索（搜尋庫）」流程挑出**貼合主題與調性**的 pose（雙人 cos 就過濾 `people_count=雙人`；
依場地調性挑情緒，例如親子樂園挑甜美/活潑、剔掉暗黑/殺氣/性感棚拍）。挑完把選中的圖**下載到本地**
`reference/poselib/` 以便嵌進 PDF：
- **selfhost**：原圖在 server，用帶**唯讀 token** 的 URL 抓
  `<base_url>/images/<hash>.jpg?t=<read_token>`（縮圖用 `/thumbs/<hash>.jpg?t=…`）。
  hash 取自 `backend.py search --json` 的 `image_path`。
- **local**：原圖就在圖庫裡，`search.py --json` 的 `image_path` 是相對圖庫資料夾的路徑
  （`backend.py status` 看圖庫位置），直接複製。

> ⚠ **已知坑（selfhost）**：server 端 `--tag` 過濾目前可能未生效、且回傳有筆數上限（~100）。
> 要「依 tag 完整撈某類」時，改成 **抓回來在本地用 Python 過濾 `tags`**（必要時用關鍵字搜尋補抓較舊的），
> 別只信 server 端過濾。

### 4. 寫 plan.json（你產資料）
先吐骨架照著填：
```bash
python3 scripts/make_plan.py --skeleton > plan.json
```
結構：`title` / `subtitle` / `pages[]`（封面與前置頁，block 型別 `info`/`heading`/`para`/`refs`/`table`）
/ `looks[]`（每個 look 自動各佔一頁，含 `fields`/`refs`/`notes`）/ `footer`。
完整 worked example 見 [`docs/sample_plan.json`](docs/sample_plan.json)（兒童新樂園 miComet 9 頁計劃書）。
文字支援 `**粗體**`。圖片路徑相對 `--base-dir`（預設 plan.json 所在資料夾）。
**原則**：一個 Look = 一個場景／地標，把選定的 pose 對應到具體地標，notes 寫 場景/構圖/動作/光線/表情。

### 5. 渲染成 PDF
```bash
python3 scripts/make_plan.py --plan plan.json --pdf 拍攝計劃.pdf --embed --max-img-width 900
```
- `--embed` 把圖 base64 內嵌（產物可攜、不依賴本機檔案）；`--max-img-width` 內嵌前縮圖控制體積。
- PDF 靠無頭 Chrome/Chromium 列印（腳本自動找）；找不到就**明講**並保留 HTML，請使用者用瀏覽器「列印成 PDF」。
- 渲染後可用 `pdftoppm -png` 抽幾頁自我檢查版面有沒有溢出（封面內容太多會把圖擠到頁底裁掉——
  就把區塊拆到下一頁）。

## 從舊版 Google Drive 遷移

舊版的 Google Drive 模式已移除。使用者若在 Drive 的 `PosePlanner/fragments/` 還有資料：

1. 請使用者從 Drive 網頁把 `PosePlanner/fragments/` 整個資料夾下載下來（`frag-*.zip`、`tomb-*.zip`），
   例如放在 `~/Downloads/fragments/`。
2. 依後端匯入（content_hash 去重、可重跑；`tomb-*.zip` 裡記的刪除會一併套用）：
   ```bash
   # local：
   python3 scripts/add_pose.py compact --fragments ~/Downloads/fragments
   # selfhost：逐個 zip 送進 server
   for f in ~/Downloads/fragments/*.zip; do python3 scripts/backend.py push --fragment "$f"; done
   ```
   > selfhost 要先送 `frag-*.zip` 再送 `tomb-*.zip`，刪除才會蓋過新增（上面的 glob 剛好依檔名排序）。
3. ⚠ fragment 只帶縮圖、**不含原圖**，所以遷移過來的圖只有縮圖可看；需要原圖時請使用者重新入庫那幾張。

## 注意
- 不要重複入庫：以 content_hash 去重，但你也別把同一張圖在 manifest 裡列兩次。
- 看不清楚或不是 cosplay 的圖，先問使用者，不要硬塞標籤。
- 本機圖庫就是一個資料夾：備份 / 搬家 / 分享都是整個資料夾複製即可（`library.db` 是單一檔，無 WAL 旁檔）。

## 相關腳本
- `scripts/backend.py`：**儲存後端選擇與私有 DB 客戶端**（純標準庫）。`status` / `setup` /
  `set`（local｜selfhost）/ `ingest`（selfhost：原圖逐張送 /images）/ `push`（遷移：送舊 fragment zip）/
  `search`（打 server /search）/ `delete`（selfhost：刪 pose，預設 dry-run、`--confirm` 才刪）/
  `image`（單張）。**已實作**。
- `scripts/add_pose.py`：本機圖庫核心。`ingest`（manifest 入庫）/ `add`（單張）/ `delete`（刪 pose，
  預設 dry-run、`--confirm` 才刪）/ `compact`（遷移：匯入舊 Drive fragments）/ `stats` / `init`。**已實作**。
- `scripts/fetch_post.py`：從 IG / Twitter-X / Facebook 貼文擷取圖片 + caption（免 API key；
  Twitter/IG 純標準庫，FB 走 gallery-dl + 瀏覽器 cookies）。**已實作**。
- `scripts/search.py`：tag 篩選 + Claude 語意挑選（選配向量 KNN）；`--json` 拿候選、
  `--ids 7,3,12 --table` 把選中的圖（保留順序、含縮圖）印成 Markdown 表直接貼進對話。**已實作**。
- `scripts/vecdb.py`：sqlite-vec 載入層（挑平台二進位、必要時 re-exec 到可用 python）。
- `scripts/make_plan.py`：把 `plan.json` 渲染成 acosta 風格的拍攝計劃書 HTML/PDF
  （`--skeleton` 產骨架 / `--plan … --pdf …` 出 PDF / `--embed` 內嵌圖）。**已實作**。
- `scripts/crop_ui.py`：把手機 App 截圖上下的 UI 外框裁掉只留主圖（自動偵測，可手動覆寫邊界）。**已實作**。
- `scripts/update_profile.py`：重算審美畫像（Phase 2，未實作）
