"""PosePlanner 自架 server — Postgres 連線層 + 入庫核心。

對應 scripts/add_pose.py 的 resolve_tag / compact_one 行為，移植到 Postgres。
連線用 psycopg3 連線池；啟動時冪等套 schema、seed taxonomy。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = Path(os.environ.get("SCHEMA_PATH", HERE / "schema.sql"))
TAXONOMY_PATH = Path(os.environ.get("TAXONOMY_PATH", "/app/taxonomy.yaml"))

EMBED_DIM = 384

_pool: ConnectionPool | None = None


def dsn() -> str:
    """從環境變數組 Postgres DSN。docker-compose 會帶這些進來。"""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "db")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("POSTGRES_USER", "poseplanner")
    pwd = os.environ.get("POSTGRES_PASSWORD", "poseplanner")
    name = os.environ.get("POSTGRES_DB", "poseplanner")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(dsn(), min_size=1, max_size=10, kwargs={"autocommit": True})
    return _pool


def wait_and_init(retries: int = 30, delay: float = 2.0) -> None:
    """等 Postgres 起來 → 套 schema → seed taxonomy。啟動時呼叫，冪等可重跑。"""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            with pool().connection() as conn:
                conn.execute("SELECT 1")
            break
        except Exception as e:  # DB 還沒準備好
            last_err = e
            time.sleep(delay)
    else:
        raise RuntimeError(f"連不上 Postgres：{last_err}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with pool().connection() as conn:
        conn.execute(schema_sql)
    seed_taxonomy()


# ── taxonomy ────────────────────────────────────────────────────────
def load_taxonomy() -> dict:
    """讀 taxonomy.yaml。優先 PyYAML，沒裝就用極簡解析器。"""
    if not TAXONOMY_PATH.exists():
        return {}
    text = TAXONOMY_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text).get("dimensions", {}) or {}
    except ModuleNotFoundError:
        return _mini_yaml_dimensions(text)


def _mini_yaml_dimensions(text: str) -> dict:
    dims: dict = {}
    cur = None
    in_dims = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^dimensions:\s*$", line):
            in_dims = True
            continue
        if not in_dims:
            continue
        m = re.match(r"^  (\w+):\s*$", line)
        if m:
            cur = m.group(1)
            dims[cur] = {"seed": []}
            continue
        if cur is None:
            continue
        m = re.match(r"^    seed:\s*\[(.*)\]\s*$", line)
        if m:
            dims[cur]["seed"] = [v.strip() for v in m.group(1).split(",") if v.strip()]
    return dims


def known_categories() -> set[str]:
    return set(load_taxonomy().keys())


def seed_taxonomy() -> None:
    dims = load_taxonomy()
    if not dims:
        return
    with pool().connection() as conn:
        for category, spec in dims.items():
            for name in spec.get("seed", []) or []:
                conn.execute(
                    "INSERT INTO tags(name, category, status, usage_count) "
                    "VALUES (%s,%s,'active',0) ON CONFLICT (category, name) DO NOTHING",
                    (name, category),
                )


# ── tag 解析：既有維度→active，新維度→proposed ──────────────────────
def resolve_tag(conn: psycopg.Connection, category: str, name: str, dims: set[str]) -> tuple[int, bool]:
    category = (category or "").strip()
    name = (name or "").strip()
    if not category or not name:
        raise ValueError(f"tag 不可有空欄位：category={category!r} name={name!r}")

    row = conn.execute(
        "SELECT canonical_tag_id FROM tag_aliases WHERE category=%s AND alias=%s",
        (category, name),
    ).fetchone()
    if row:
        return row[0], False

    row = conn.execute(
        "SELECT id FROM tags WHERE category=%s AND name=%s", (category, name)
    ).fetchone()
    if row:
        return row[0], False

    status = "active" if category in dims else "proposed"
    row = conn.execute(
        "INSERT INTO tags(name, category, status, usage_count) VALUES (%s,%s,%s,0) RETURNING id",
        (name, category, status),
    ).fetchone()
    return row[0], True


def _embedding_literal(values) -> str | None:
    """把向量陣列轉成 pgvector 的字面量 '[a,b,...]'；維度不符回 None。"""
    if not values:
        return None
    try:
        nums = [float(x) for x in values]
    except (TypeError, ValueError):
        return None
    if len(nums) != EMBED_DIM:
        return None
    return "[" + ",".join(repr(x) for x in nums) + "]"


def upsert_pose(conn: psycopg.Connection, entry: dict, dims: set[str]) -> tuple[int | None, str]:
    """寫入一筆 pose（content_hash 去重）。回傳 (pose_id|None, 狀態)。
    狀態：'added' / 'dup' / 'error'。entry 欄位對齊 fragment manifest。"""
    content_hash = (entry.get("content_hash") or "").strip()
    description = (entry.get("description") or "").strip()
    tags = entry.get("tags") or []
    if not content_hash or not description or not tags:
        return None, "error"

    row = conn.execute("SELECT id FROM poses WHERE content_hash=%s", (content_hash,)).fetchone()
    if row:
        return row[0], "dup"

    emb = _embedding_literal(entry.get("embedding"))
    row = conn.execute(
        "INSERT INTO poses(image_path, thumbnail_path, description, content_hash, "
        "favorite, rating, source, embedding, embedding_model) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            entry.get("image_path"),
            entry.get("thumbnail_path"),
            description,
            content_hash,
            bool(entry.get("favorite")),
            entry.get("rating"),
            entry.get("source"),
            emb,
            entry.get("embedding_model") if emb else None,
        ),
    ).fetchone()
    pose_id = row[0]

    for t in tags:
        category = t.get("category")
        name = t.get("name")
        if not category or not name:
            continue
        tag_id, _is_new = resolve_tag(conn, category, name, dims)
        conn.execute(
            "INSERT INTO pose_tags(pose_id, tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (pose_id, tag_id),
        )
        conn.execute("UPDATE tags SET usage_count = usage_count + 1 WHERE id=%s", (tag_id,))

    return pose_id, "added"


def _link_tag(conn: psycopg.Connection, pose_id: int, tag_id: int) -> bool:
    """掛一個 tag 到 pose；真的新增了才 +usage_count。回傳是否變動。"""
    cur = conn.execute(
        "INSERT INTO pose_tags(pose_id, tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (pose_id, tag_id),
    )
    if cur.rowcount:
        conn.execute("UPDATE tags SET usage_count = usage_count + 1 WHERE id=%s", (tag_id,))
        return True
    return False


def _unlink_tag(conn: psycopg.Connection, pose_id: int, tag_id: int) -> bool:
    """卸一個 tag；真的移除了才 -usage_count（不低於 0）。回傳是否變動。"""
    cur = conn.execute(
        "DELETE FROM pose_tags WHERE pose_id=%s AND tag_id=%s", (pose_id, tag_id)
    )
    if cur.rowcount:
        conn.execute(
            "UPDATE tags SET usage_count = GREATEST(usage_count - 1, 0) WHERE id=%s", (tag_id,)
        )
        return True
    return False


def _resolve_pairs(conn: psycopg.Connection, tags: list, dims: set[str]) -> set[int]:
    """把 [{category,name}, ...] 解析成 canonical tag_id 集合（略過空欄位）。"""
    ids: set[int] = set()
    for t in tags or []:
        category = (t.get("category") or "").strip()
        name = (t.get("name") or "").strip()
        if not category or not name:
            continue
        tag_id, _is_new = resolve_tag(conn, category, name, dims)
        ids.add(tag_id)
    return ids


def update_pose_tags(
    conn: psycopg.Connection,
    pose_id: int,
    dims: set[str],
    *,
    replace: list | None = None,
    add: list | None = None,
    remove: list | None = None,
) -> dict | None:
    """改一張 pose 的 tags。pose 不存在回 None。

    replace 有給 → 整批替換成 replace（add/remove 視為附加微調）；
    否則只套用 add / remove。回傳 {added, removed, tags}（tags 為更新後完整清單）。
    """
    if conn.execute("SELECT 1 FROM poses WHERE id=%s", (pose_id,)).fetchone() is None:
        return None

    current = {
        r[0]
        for r in conn.execute(
            "SELECT tag_id FROM pose_tags WHERE pose_id=%s", (pose_id,)
        ).fetchall()
    }
    add_ids = _resolve_pairs(conn, add, dims)
    remove_ids = _resolve_pairs(conn, remove, dims)

    if replace is not None:
        target = _resolve_pairs(conn, replace, dims) | add_ids
        target -= remove_ids
        to_add = target - current
        to_remove = current - target
    else:
        to_add = add_ids - remove_ids
        to_remove = remove_ids - add_ids

    added = sum(_link_tag(conn, pose_id, tid) for tid in to_add)
    removed = sum(_unlink_tag(conn, pose_id, tid) for tid in to_remove)

    tags = [
        {"category": cat, "name": name}
        for cat, name in conn.execute(
            "SELECT t.category, t.name FROM pose_tags pt JOIN tags t ON t.id=pt.tag_id "
            "WHERE pt.pose_id=%s ORDER BY t.category, t.name",
            (pose_id,),
        ).fetchall()
    ]
    return {"added": added, "removed": removed, "tags": tags}
