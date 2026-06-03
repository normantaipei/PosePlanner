-- PosePlanner 私有 DB（自架）結構 — PostgreSQL 版
-- 對應 scripts/schema.sql（SQLite + sqlite-vec）的內容，移植到 Postgres。
-- 由 api 服務在啟動時冪等套用（CREATE ... IF NOT EXISTS），不需手動灌。
--
-- 向量：用 pgvector 擴充（pgvector/pgvector 映像內建）。向量是「選配」——
-- 沒有向量的 pose 一樣能用 tag / 描述檢索 + Claude 語意挑選。

CREATE EXTENSION IF NOT EXISTS vector;

-- ── 核心：每一張入庫的 pose ──────────────────────────────
CREATE TABLE IF NOT EXISTS poses (
  id              SERIAL PRIMARY KEY,
  image_path      TEXT,                              -- 伺服器上原圖的相對路徑（images/<hash>.<ext>）
  thumbnail_path  TEXT,                              -- 縮圖相對路徑（thumbs/<hash>.jpg）
  description     TEXT,                              -- Claude 看圖產的自然語言動作敘述
  content_hash    TEXT NOT NULL UNIQUE,              -- sha256，去重用（同張圖不重複入庫）
  favorite        BOOLEAN NOT NULL DEFAULT FALSE,
  rating          INTEGER,                           -- 可選顯式回饋 1..5
  source          TEXT,                              -- 來源（檔名 / 網址 / 備註）
  embedding       vector(384),                       -- 選配敘述向量（pgvector）
  embedding_model TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 標籤：動態、可延展 ───────────────────────────────────
CREATE TABLE IF NOT EXISTS tags (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,                          -- 標籤值，如「站」
  category    TEXT NOT NULL,                          -- 維度，如 pose / ip / emotion
  usage_count INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'active',         -- active / proposed
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (category, name)
);

-- 同義詞收斂：把別名指向 canonical tag
CREATE TABLE IF NOT EXISTS tag_aliases (
  alias            TEXT NOT NULL,
  category         TEXT NOT NULL,
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
  id       SERIAL PRIMARY KEY,
  label    TEXT,
  centroid BYTEA,
  summary  TEXT,
  size     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS taste_profile (
  id         SERIAL PRIMARY KEY,
  summary    TEXT,
  top_tags   JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_log (
  id         SERIAL PRIMARY KEY,
  batch_date TIMESTAMPTZ NOT NULL DEFAULT now(),
  n_added    INTEGER NOT NULL DEFAULT 0,
  n_dup      INTEGER NOT NULL DEFAULT 0,
  n_new_tags INTEGER NOT NULL DEFAULT 0,
  summary    TEXT
);

-- ── 計劃書（Phase 4，先建表）─────────────────────────────
CREATE TABLE IF NOT EXISTS plans (
  id         SERIAL PRIMARY KEY,
  title      TEXT NOT NULL,
  brief      TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plan_items (
  id       SERIAL PRIMARY KEY,
  plan_id  INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  pose_id  INTEGER NOT NULL REFERENCES poses(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pose_tags_tag ON pose_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category);
CREATE INDEX IF NOT EXISTS idx_tags_status   ON tags(status);
