# PosePlanner

**Cosplay 拍攝計劃書生成小幫手** —— 一個會**自動學習、持續延展**的個人 cos 攝影參考庫。

每天餵入你覺得漂亮的 cosplay 圖，它會自動辨識角色／作品、轉成動作敘述與結構化標籤入庫，逐漸學會你的審美與出鏡偏好，最後幫你挑 pose、生成拍攝計劃書。

---

## 願景

- **餵圖即學習**：每天把喜歡的圖丟進來，自動分析、入庫、累積。
- **越用越懂你**：每張圖都是「我覺得漂亮」的正向訊號，系統從中萃取你的審美畫像。
- **可分享**：整個庫是一個可攜帶的檔案，打包成 Claude Code Skill，任何人裝上就能用、也能繼承你的審美庫。

## 形式與技術定位

- **形式**：Claude Code Skill（無後端伺服器、純檔案、可攜帶）。
- **視覺分析**：直接由 Claude 看圖產出敘述與標籤——不需另接 vision API。
- **語意搜尋**：用本地 embedding（如 `fastembed` / 多語模型 `bge-m3`，支援中文），向量存進 SQLite，免 API key。
- **儲存**：單一 SQLite 檔（`library.db`）+ `images/` 資料夾，整包即可分享。

## 專案結構（規劃）

```
poseplanner/
├── SKILL.md              # 指示 Claude：看圖 → 依 taxonomy 產出 JSON
├── taxonomy.yaml         # 標籤維度的初始種子（非封閉，可成長）
├── scripts/
│   ├── add_pose.py       # 收 JSON+圖片 → 去重 → embedding → 寫 DB
│   ├── search.py         # tag 篩選 + 語意搜尋 + 相似推薦
│   ├── update_profile.py # 重算審美畫像、分群
│   ├── consolidate.py    # 同義詞合併、proposed→active、清冷門 tag
│   └── make_plan.py      # 選 pose → shot list → 計劃書
└── data/
    ├── library.db        # SQLite（可攜帶、可分享）
    └── images/           # 原圖 + 縮圖
```

## 資料庫設計

```sql
-- 核心
poses (
  id, image_path, thumbnail_path,
  description,                 -- Claude 產的自然語言動作敘述
  content_hash,               -- 去重（同張圖不重複入庫）
  favorite, rating,           -- 可選的顯式回饋
  source, created_at
)
pose_embeddings (pose_id, vector)      -- 敘述向量（語意搜尋）

-- 標籤（動態、可延展）
tags (id, name, category, usage_count, status, created_at)  -- status: active / proposed
tag_aliases (alias, canonical_tag_id)  -- 同義詞收斂，避免越長越亂
pose_tags (pose_id, tag_id)

-- 學習
taste_clusters (id, label, centroid, summary, size)  -- 自動發現的「調性」
taste_profile (id, summary, top_tags, updated_at)    -- 整體審美畫像（快取）
ingest_log (id, batch_date, n_added, n_dup, n_new_tags, summary)

-- 計劃書
plans (id, title, brief, created_at)
plan_items (id, plan_id, pose_id, position, note)
```

## 標籤維度（taxonomy 初始種子）

固定維度是「一致性」的關鍵；值可隨餵圖成長。

| 維度 | 範例值 |
|---|---|
| 作品 / IP | 鬼滅之刃 / 原神 / 火影 / 東方 / VTuber（開放，可成長）|
| 角色 | 角色名，如 禰豆子 / 雷電將軍 / 初音（開放，可成長）|
| 角色屬性 | 萌系 / 帥氣 / 御姐 / 病嬌 / 反派 / 中二 |
| 人數 | 單人 / 雙人 / 團 cos |
| 取景 | 全身 / 半身 / 特寫 |
| 體位 | 站 / 坐 / 跪 / 倚靠 / 戰鬥 / 飛踢 |
| 手部 / 道具 | 持刀 / 持槍 / 法杖 / 比手勢 / 撥髮 |
| 視線 / 表情 | 看鏡頭 / 側望 / 閉眼 / 帥氣 / 嬌羞 |
| 情緒 | 自然 / 慵懶 / 性感 / 殺氣 / 療癒 |
| 鏡位 | 平視 / 俯拍 / 仰拍 / 過肩 |
| 場景 | 棚拍 / 漫展 / 外景 / 綠幕去背 / 動漫實景 |
| 風格 | 還原劇照 / 唯美 / 暗黑 / 日系 / 二創 |

## 自動學習機制（三層）

1. **動態 taxonomy**：既有維度內的新值自動採用；全新維度標 `proposed`，達門檻或經確認才升級。每個 tag 記 `usage_count`。
   *（預設策略：**半自動**——既有維度自動、新維度才問你。）*
2. **同義詞收斂**：`tag_aliases` 把同義詞指向 canonical tag，定期由 Claude 提合併建議，讓庫長到上千張也不亂。
3. **審美畫像**：對敘述向量做分群（k-means），自動發現你的幾種「調性」，並產出人類可讀摘要（例：「近期偏好 逆光 × 街頭 × 慵懶」）。

## 每日餵圖流程

```
poseplanner add ./今天的圖/
  └─ 逐張：content_hash 去重 → Claude 看圖產 {description, tags}
            → tag 對映（既有 or 新增/proposed）→ 算 embedding → 寫入
  └─ 批次後：更新 usage_count、重算 taste_profile
            → 若 proposed tag 達門檻 / 同義詞過多 → 觸發整理
  └─ 回報：「今天 +12 張，新增角色：雷電將軍；新學到 tag：持薙刀、戰鬥姿；『暗黑御姐』調性又長了 8 張」
```

## 搜尋流程

tag 維度硬篩（站姿 + 室內）縮小範圍 → 敘述向量語意排序（「想要慵懶的感覺」）→ Claude 對 top-N 做最後挑選。tag 管精確、向量管模糊，互補。

---

## 開發路線（逐項完成）

### Phase 1 — 圖片入庫
- [ ] 定義 `taxonomy.yaml` 初始種子
- [ ] 建表 SQL + seed tags
- [ ] SKILL.md：看圖 → 產 `{description, tags[]}` JSON（受 taxonomy 約束、可提 proposed 新 tag）
- [ ] `add_pose.py`：批次資料夾匯入、content_hash 去重、存圖+縮圖、算 embedding、寫庫、更新 usage_count

### Phase 2 — 學習迴圈
- [ ] `update_profile.py`：重算向量重心、k-means 分群、產審美摘要
- [ ] `consolidate.py`：同義詞合併建議、proposed→active 升級、冷門 tag 清理
- [ ] 每日 ingest 回報（`ingest_log`）

### Phase 3 — 檢索
- [ ] `search.py`：tag 組合篩選
- [ ] 語意搜尋（向量）+ 相似姿勢推薦

### Phase 4 — 拍攝計劃書
- [ ] `make_plan.py`：選 pose → shot list → Markdown / PDF
- [ ] 依審美畫像推薦 pose
- [ ] `plans` / `plan_items` 管理

### Phase 5 — 分享
- [ ] 匯出／匯入 pose pack（一個 `.db` + `images/` 即可分享）
- [ ] 打包成可安裝的 Claude Code Skill
