-- PosePlanner 資料庫結構
-- 用法：由 add_pose.py 套用（DB 不存在時自動建）。
-- ⚠ 本檔含 sqlite-vec 的 vec0 虛擬表，**必須在已載入 sqlite-vec extension 的連線上**
--   執行，所以不要再用 `sqlite3 data/library.db < scripts/schema.sql` 直接灌——
--   那條連線沒載入 extension 會在 pose_vectors 那行報錯。改跑：
--     python3 scripts/add_pose.py init
--   （腳本會先載入 vendored 的 vec0 再 executescript。）

-- 用 rollback journal（預設）而非 WAL：乾淨關閉後不留 -wal/-shm 旁檔，
-- 整個庫就是「單一 library.db」一個檔，方便上傳/下載 Drive、打包分享。
PRAGMA foreign_keys = ON;

-- ── 核心：每一張入庫的 pose ──────────────────────────────
CREATE TABLE IF NOT EXISTS poses (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  image_path     TEXT NOT NULL,            -- 相對於 data/ 的原圖路徑
  thumbnail_path TEXT,                     -- 相對於 data/ 的縮圖路徑
  description    TEXT,                     -- Claude 看圖產的自然語言動作敘述
  content_hash   TEXT NOT NULL UNIQUE,     -- sha256，去重用（同張圖不重複入庫）
  favorite       INTEGER NOT NULL DEFAULT 0,
  rating         INTEGER,                  -- 可選顯式回饋 1..5
  source         TEXT,                     -- 來源（檔名 / 網址 / 備註）
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 敘述向量（語意搜尋用）。改用 sqlite-vec 的 vec0 虛擬表，支援真正的 KNN：
--   SELECT pose_id, distance FROM pose_vectors WHERE embedding MATCH ? ORDER BY distance LIMIT k
-- 向量為「選配」：沒有向量的 pose 就不在此表裡，照樣能用 tag / 描述檢索 + Claude 語意挑選。
-- 維度固定 384（見 add_pose.py 的 EMBED_DIM）；+model 為 auxiliary 欄，記錄向量來源。
-- 注意：vec0 是虛擬表，不吃 FOREIGN KEY / CASCADE，刪 pose 時靠下面的 trigger 清向量。
CREATE VIRTUAL TABLE IF NOT EXISTS pose_vectors USING vec0(
  pose_id   INTEGER PRIMARY KEY,
  embedding FLOAT[384],
  +model    TEXT
);

-- ── 標籤：動態、可延展 ───────────────────────────────────
CREATE TABLE IF NOT EXISTS tags (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,               -- 標籤值，如「站姿」
  category    TEXT NOT NULL,               -- 維度，如 pose / ip / emotion
  usage_count INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'active',  -- active / proposed
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (category, name)
);

-- 同義詞收斂：把別名指向 canonical tag，避免越長越亂
CREATE TABLE IF NOT EXISTS tag_aliases (
  alias           TEXT NOT NULL,
  category        TEXT NOT NULL,
  canonical_tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (category, alias)
);

CREATE TABLE IF NOT EXISTS pose_tags (
  pose_id INTEGER NOT NULL REFERENCES poses(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  PRIMARY KEY (pose_id, tag_id)
);

-- ── 學習層（Phase 2，先建表）─────────────────────────────
CREATE TABLE IF NOT EXISTS taste_clusters (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  label    TEXT,
  centroid BLOB,
  summary  TEXT,
  size     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS taste_profile (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  summary    TEXT,
  top_tags   TEXT,                          -- JSON 陣列
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 每日餵圖回報紀錄
CREATE TABLE IF NOT EXISTS ingest_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_date TEXT NOT NULL DEFAULT (datetime('now')),
  n_added    INTEGER NOT NULL DEFAULT 0,
  n_dup      INTEGER NOT NULL DEFAULT 0,
  n_new_tags INTEGER NOT NULL DEFAULT 0,
  summary    TEXT
);

-- ── 計劃書（Phase 4，先建表）─────────────────────────────
CREATE TABLE IF NOT EXISTS plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  brief      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plan_items (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id  INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  pose_id  INTEGER NOT NULL REFERENCES poses(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pose_tags_tag  ON pose_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_tags_category  ON tags(category);
CREATE INDEX IF NOT EXISTS idx_tags_status    ON tags(status);

-- 刪 pose 時連帶清掉它在 vec0 表的向量（vec0 不支援 FK CASCADE，用 trigger 補）。
CREATE TRIGGER IF NOT EXISTS trg_poses_del_vec
AFTER DELETE ON poses
BEGIN
  DELETE FROM pose_vectors WHERE pose_id = OLD.id;
END;
