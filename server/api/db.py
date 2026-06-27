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
# 創作者沒指定角色時的預設 role；常見值：model / photographer / retoucher…
DEFAULT_CREATOR_ROLE = "creator"

_pool: ConnectionPool | None = None


def dsn(dbname: str | None = None) -> str:
    """從環境變數組 Postgres DSN。docker-compose 會帶這些進來。
    dbname 可覆寫目標資料庫（補建 DB 時要連到一定存在的維護 DB postgres）。"""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "db")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("POSTGRES_USER", "poseplanner")
    pwd = os.environ.get("POSTGRES_PASSWORD", "poseplanner")
    name = dbname or os.environ.get("POSTGRES_DB", "poseplanner")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(dsn(), min_size=1, max_size=10, kwargs={"autocommit": True})
    return _pool


def _ensure_database(connect_timeout: int = 3) -> None:
    """目標資料庫不存在就補建，讓系統自我修復。

    POSTGRES_DB 只在 volume「第一次初始化（空目錄）」時才會被建立；若 volume 是舊的
    或半初始化的（cluster + 使用者都在，但這個 DB 沒被建到），api 會一直噴
    `database "..." does not exist`。這裡用一定存在的維護 DB postgres 連進去，沒有就
    CREATE DATABASE 補上。POSTGRES_USER 在官方映像是超級使用者，足以建庫。
    DATABASE_URL 模式（多半是受管 PG）不處理，交給該服務自己管理。
    """
    if os.environ.get("DATABASE_URL"):
        return
    name = os.environ.get("POSTGRES_DB", "poseplanner")
    with psycopg.connect(dsn("postgres"), connect_timeout=connect_timeout, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            # 識別字不能用參數化綁定，name 取自我們自己的環境變數，非外部輸入。
            conn.execute(f'CREATE DATABASE "{name}"')


def _probe(connect_timeout: int = 3) -> None:
    """用短逾時的直接連線確認 Postgres 真的可連、可認證（含密碼）。

    為什麼不用連線池：ConnectionPool 的 .connection() 預設要等 30 秒才逾時，DB
    還沒好或密碼不符時，每次重試都會卡滿 30 秒，整個 startup 看起來像「死當」、
    body 都不回。直接連線帶 connect_timeout，失敗就快速彈出、進下一輪重試。
    注意：db 容器的 healthcheck（pg_isready）只確認「能連」，不驗密碼；密碼對不對
    要靠這裡真的連一次才知道。
    """
    with psycopg.connect(dsn(), connect_timeout=connect_timeout, autocommit=True) as conn:
        conn.execute("SELECT 1")


def wait_and_init(retries: int = 60, delay: float = 2.0) -> None:
    """等 Postgres 起來 → 套 schema → seed taxonomy。啟動時呼叫，冪等可重跑。

    整段（連線確認 + schema + seed）都包在重試裡：Postgres「首次初始化」會先起一個
    暫時 server 再重啟，期間連線可能成功一下又斷掉；只重試最初的 SELECT 1 會在
    schema/seed 階段踩到斷線而失敗（這就是「乾淨環境第一次跑必失敗、第二次才正常」
    的根因——第二次 volume 已初始化好，不再有這段重啟）。
    """
    last_err: Exception | None = None
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    for _ in range(retries):
        try:
            _ensure_database()  # 半初始化的舊 volume 可能缺這個 DB → 自己補建
            _probe()
            with pool().connection() as conn:
                conn.execute(schema_sql)
            seed_taxonomy()
            return
        except Exception as e:  # DB 還沒好 / 首次初始化重啟中 / 短暫斷線 / 密碼還沒套上
            last_err = e
            time.sleep(delay)
    raise RuntimeError(f"連不上或初始化 Postgres 失敗（重試 {retries} 次）：{last_err}")


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


# ── 創作者：一張圖可多人，各帶 role（模特兒 / 攝影師…）────────────────
def resolve_creator(
    conn: psycopg.Connection,
    name: str,
    handle: str | None = None,
    url: str | None = None,
    note: str | None = None,
) -> int:
    """以 name 去重取得 creator id，沒有就建立。後來才補的 handle/url/note 只填空欄、不覆寫。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("creator name 不可為空")
    handle = (handle or "").strip() or None
    url = (url or "").strip() or None
    note = (note or "").strip() or None

    row = conn.execute("SELECT id FROM creators WHERE name=%s", (name,)).fetchone()
    if row:
        cid = row[0]
        if handle or url or note:
            conn.execute(
                "UPDATE creators SET handle=COALESCE(handle,%s), url=COALESCE(url,%s), "
                "note=COALESCE(note,%s) WHERE id=%s",
                (handle, url, note, cid),
            )
        return cid
    row = conn.execute(
        "INSERT INTO creators(name, handle, url, note) VALUES (%s,%s,%s,%s) RETURNING id",
        (name, handle, url, note),
    ).fetchone()
    return row[0]


def _iter_creators(creators: list | None):
    """把 [{"name","role","handle","url","note"}, ...] 正規化成
    (name, role, handle, url, note)，略過沒有 name 的項。"""
    for c in creators or []:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        role = (c.get("role") or "").strip() or DEFAULT_CREATOR_ROLE
        yield name, role, c.get("handle"), c.get("url"), c.get("note")


def _link_creators(conn: psycopg.Connection, pose_id: int, creators: list | None) -> int:
    """把一份創作者清單掛到 pose 上（以 (creator, role) 去重）。回傳處理筆數。"""
    n = 0
    for name, role, handle, url, note in _iter_creators(creators):
        cid = resolve_creator(conn, name, handle, url, note)
        conn.execute(
            "INSERT INTO pose_creators(pose_id, creator_id, role) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (pose_id, cid, role),
        )
        n += 1
    return n


def pose_creators(conn: psycopg.Connection, pose_id: int) -> list[dict]:
    """讀一張 pose 的創作者清單：[{name, role, handle, url}, ...]（依 role、name 排序）。"""
    return [
        {"name": name, "role": role, "handle": handle, "url": url}
        for name, role, handle, url in conn.execute(
            "SELECT c.name, pc.role, c.handle, c.url FROM pose_creators pc "
            "JOIN creators c ON c.id=pc.creator_id WHERE pc.pose_id=%s "
            "ORDER BY pc.role, c.name",
            (pose_id,),
        ).fetchall()
    ]


def _resolve_creator_pairs(conn: psycopg.Connection, creators: list | None) -> set[tuple[int, str]]:
    """把創作者清單解析成 (creator_id, role) 集合（給整批替換的差集計算用）。"""
    out: set[tuple[int, str]] = set()
    for name, role, handle, url, note in _iter_creators(creators):
        cid = resolve_creator(conn, name, handle, url, note)
        out.add((cid, role))
    return out


def update_pose_creators(
    conn: psycopg.Connection,
    pose_id: int,
    *,
    replace: list | None = None,
    add: list | None = None,
    remove: list | None = None,
) -> dict | None:
    """改一張 pose 的創作者。語意與 update_pose_tags 相同：
    replace 有給 → 整批替換（add/remove 視為附加微調）；否則只套用 add / remove。
    pose 不存在回 None；否則回 {added, removed, creators}（creators 為更新後完整清單）。"""
    if conn.execute("SELECT 1 FROM poses WHERE id=%s", (pose_id,)).fetchone() is None:
        return None

    current = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT creator_id, role FROM pose_creators WHERE pose_id=%s", (pose_id,)
        ).fetchall()
    }
    add_pairs = _resolve_creator_pairs(conn, add)
    remove_pairs = _resolve_creator_pairs(conn, remove)

    if replace is not None:
        target = _resolve_creator_pairs(conn, replace) | add_pairs
        target -= remove_pairs
        to_add = target - current
        to_remove = current - target
    else:
        to_add = add_pairs - remove_pairs
        to_remove = remove_pairs - add_pairs

    for cid, role in to_add:
        conn.execute(
            "INSERT INTO pose_creators(pose_id, creator_id, role) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (pose_id, cid, role),
        )
    for cid, role in to_remove:
        conn.execute(
            "DELETE FROM pose_creators WHERE pose_id=%s AND creator_id=%s AND role=%s",
            (pose_id, cid, role),
        )
    return {
        "added": len(to_add),
        "removed": len(to_remove),
        "creators": pose_creators(conn, pose_id),
    }


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
        "INSERT INTO poses(image_path, thumbnail_path, thumb_w, thumb_h, description, "
        "content_hash, favorite, rating, source, embedding, embedding_model) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            entry.get("image_path"),
            entry.get("thumbnail_path"),
            entry.get("thumb_w"),
            entry.get("thumb_h"),
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

    _link_creators(conn, pose_id, entry.get("creators"))

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


def get_pose(conn: psycopg.Connection, pose_id: int) -> dict | None:
    """取一張 pose 的摘要（給刪除前的 dry-run 確認用）。不存在回 None。"""
    row = conn.execute(
        "SELECT id, content_hash, image_path, thumbnail_path, thumb_w, thumb_h, "
        "description, favorite, rating FROM poses WHERE id=%s", (pose_id,)
    ).fetchone()
    if not row:
        return None
    pid, ch, image_path, thumb, thumb_w, thumb_h, desc, fav, rating = row
    tags = [
        f"{cat}:{name}"
        for cat, name in conn.execute(
            "SELECT t.category, t.name FROM pose_tags pt JOIN tags t ON t.id=pt.tag_id "
            "WHERE pt.pose_id=%s ORDER BY t.category", (pose_id,)
        ).fetchall()
    ]
    return {
        "id": pid, "content_hash": ch, "image_path": image_path,
        "thumbnail_path": thumb, "thumb_w": thumb_w, "thumb_h": thumb_h,
        "description": desc, "favorite": bool(fav),
        "rating": rating, "tags": tags, "creators": pose_creators(conn, pose_id),
    }


def delete_pose(conn: psycopg.Connection, pose_id: int) -> dict | None:
    """刪一張 pose。pose_tags / pose_creators 靠 FK ON DELETE CASCADE 連帶清掉；
    tags.usage_count 在刪 pose_tags 前先補扣。回傳被刪 pose 的 {id, content_hash,
    image_path, thumbnail_path}（給呼叫端清磁碟檔），不存在回 None。"""
    row = conn.execute(
        "SELECT id, content_hash, image_path, thumbnail_path FROM poses WHERE id=%s",
        (pose_id,),
    ).fetchone()
    if not row:
        return None
    pid, ch, image_path, thumb = row
    # CASCADE 不會幫忙維護 usage_count，先把這張用到的 tag 計數扣回來。
    conn.execute(
        "UPDATE tags SET usage_count = GREATEST(usage_count - 1, 0) WHERE id IN "
        "(SELECT tag_id FROM pose_tags WHERE pose_id=%s)", (pose_id,)
    )
    conn.execute("DELETE FROM poses WHERE id=%s", (pose_id,))
    return {"id": pid, "content_hash": ch, "image_path": image_path, "thumbnail_path": thumb}


def delete_pose_by_hash(conn: psycopg.Connection, content_hash: str) -> dict | None:
    """依 content_hash 刪一張 pose（給 tombstone fragment 回放用）。不存在回 None。"""
    row = conn.execute(
        "SELECT id FROM poses WHERE content_hash=%s", ((content_hash or "").strip(),)
    ).fetchone()
    if not row:
        return None
    return delete_pose(conn, row[0])
