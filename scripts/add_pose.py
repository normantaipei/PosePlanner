#!/usr/bin/env python3
"""PosePlanner — 圖片入庫（Phase 1）

把一批圖 + Claude 看圖產出的 {description, tags[]} 寫進 library.db：
  content_hash 去重 → 複製原圖 + 產縮圖 → 算 embedding(選配) → 寫庫 → 更新 usage_count。

兩種用法
────────
1) 批次 manifest（推薦，給 SKILL 流程用）：
     python3 scripts/add_pose.py ingest --manifest today.json
   manifest 是一個 JSON 陣列，每筆：
     {
       "image": "今天的圖/a.jpg",          # 必填，原圖路徑（相對 cwd 或絕對）
       "description": "站姿，持刀回眸…",     # 必填，Claude 產的動作敘述
       "tags": [                           # 必填，至少一個
         {"category": "pose", "name": "站"},
         {"category": "ip",   "name": "鬼滅之刃"}
       ],
       "source": "https://...",            # 選填
       "rating": 5,                        # 選填 1..5
       "favorite": true                    # 選填
     }

2) 單張（給快速測試 / 手動）：
     python3 scripts/add_pose.py add --image a.jpg \
         --description "站姿持刀" --tag pose=站 --tag ip=鬼滅之刃

其他：
     python3 scripts/add_pose.py init          # 只建庫 + seed
     python3 scripts/add_pose.py stats         # 看目前庫狀態

embedding 是選配：裝了 fastembed 才會算並寫入 pose_embeddings，沒裝照樣入庫。
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import vecdb  # sqlite-vec 載入層（同目錄）

# ── 路徑 ──────────────────────────────────────────────────────────
# 本檔在 <root>/scripts/；taxonomy 與 schema 跟著 skill 走（唯讀資源）。
# 但 db 與 data（圖）位置「可被覆寫」——雲端持久化時，Claude 會先把 Drive 上的
# library.db 下載到沙箱（例如 /tmp/poseplanner/），再用 --db / --data 指過來。
# 覆寫優先序：CLI 旗標 > 環境變數 > 預設（skill 內的 data/）。
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "scripts" / "schema.sql"
TAXONOMY_PATH = ROOT / "taxonomy.yaml"

DATA_DIR = Path(os.environ.get("POSEPLANNER_DATA", str(ROOT / "data")))
DB_PATH = Path(os.environ.get("POSEPLANNER_DB", str(DATA_DIR / "library.db")))
IMAGES_DIR = DATA_DIR / "images"
THUMBS_DIR = DATA_DIR / "images" / "thumbs"


def configure(db: str | None = None, data: str | None = None) -> None:
    """依 CLI 旗標重算路徑全域變數（main 在分派前呼叫）。"""
    global DATA_DIR, DB_PATH, IMAGES_DIR, THUMBS_DIR
    if data:
        DATA_DIR = Path(data).expanduser()
    if db:
        DB_PATH = Path(db).expanduser()
    elif data:  # 給了 data 但沒給 db → db 預設落在新的 data 底下
        DB_PATH = DATA_DIR / "library.db"
    IMAGES_DIR = DATA_DIR / "images"
    THUMBS_DIR = DATA_DIR / "images" / "thumbs"

THUMB_MAX = 512  # 縮圖長邊像素
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tiff"}

# 向量：vec0 表的維度是固定的（見 schema.sql 的 FLOAT[384]）。
# 預設「腳本只寫 Claude 給的資料」，不自跑本地模型——只有在
#   (a) manifest 帶了現成 "embedding" 陣列，或
#   (b) 下了 --embed 旗標（需 pip install fastembed）
# 才會寫進 pose_vectors。多語小模型，對中文敘述友善。
EMBED_DIM = 384
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# 創作者沒指定角色時的預設 role；常見值：model / photographer / retoucher…
DEFAULT_CREATOR_ROLE = "creator"

# Tombstone（刪除）fragment 的標記。drive 模式的庫是 Drive 上 append-only 的 fragments，
# 連接器只能新建、不能刪檔，所以「刪一張圖」= 上傳一個只記 content_hash 的小 tombstone zip；
# compact 重建臨時庫時看到 tombstone 就把那些 content_hash 的圖移除（tombstone wins）。
# 正常 fragment 的 manifest 是「陣列」，tombstone 的 manifest 是帶這個 op 的「物件」，不會撞型。
TOMBSTONE_OP = "delete"


# ── taxonomy 載入 ───────────────────────────────────────────────────
def load_taxonomy() -> dict:
    """讀 taxonomy.yaml。優先用 PyYAML；沒裝就用內建最小解析器（只支援本檔的簡單結構）。"""
    text = TAXONOMY_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)["dimensions"]
    except ModuleNotFoundError:
        return _mini_yaml_dimensions(text)


def _mini_yaml_dimensions(text: str) -> dict:
    """極簡解析器，只認得 taxonomy.yaml 的固定格式（dimensions: 下每個維度有 label/open/seed）。"""
    dims: dict = {}
    cur = None
    in_dimensions = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^dimensions:\s*$", line):
            in_dimensions = True
            continue
        if not in_dimensions:
            continue
        m = re.match(r"^  (\w+):\s*$", line)  # 維度名（2 空格縮排）
        if m:
            cur = m.group(1)
            dims[cur] = {"label": cur, "open": True, "seed": []}
            continue
        if cur is None:
            continue
        m = re.match(r"^    label:\s*(.+)$", line)
        if m:
            dims[cur]["label"] = m.group(1).strip()
            continue
        m = re.match(r"^    open:\s*(\w+)$", line)
        if m:
            dims[cur]["open"] = m.group(1).strip().lower() == "true"
            continue
        m = re.match(r"^    seed:\s*\[(.*)\]\s*$", line)
        if m:
            vals = [v.strip() for v in m.group(1).split(",") if v.strip()]
            dims[cur]["seed"] = vals
            continue
    if not dims:
        raise SystemExit("無法解析 taxonomy.yaml；請確認格式或 pip install pyyaml。")
    return dims


# ── DB 初始化 + seed ────────────────────────────────────────────────
def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fresh = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    vecdb.load_vec(conn)  # 載入 sqlite-vec，之後才能建/用 pose_vectors（vec0）
    if fresh:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        seed_tags(conn)
        conn.commit()
        try:
            shown = DB_PATH.relative_to(ROOT)
        except ValueError:
            shown = DB_PATH  # db 在 skill 之外（雲端持久化的沙箱副本）
        print(f"  建立新庫：{shown}")
    else:
        _migrate(conn)  # 既有庫補新欄位（如 thumb_w/h）
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """既有 SQLite 庫的冪等補欄位。SQLite 沒有 ADD COLUMN IF NOT EXISTS，
    靠 PRAGMA table_info 判斷缺哪欄再補。新欄位都可為 NULL，不動既有資料。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(poses)")}
    if not cols:
        return  # 還沒有 poses 表（理論上 fresh 已建好，這裡保險）
    added = False
    for col in ("thumb_w", "thumb_h"):
        if col not in cols:
            conn.execute(f"ALTER TABLE poses ADD COLUMN {col} INTEGER")
            added = True
    if added:
        conn.commit()


def seed_tags(conn: sqlite3.Connection) -> None:
    dims = load_taxonomy()
    for category, spec in dims.items():
        for name in spec.get("seed", []) or []:
            conn.execute(
                "INSERT OR IGNORE INTO tags(name, category, status, usage_count) "
                "VALUES (?,?, 'active', 0)",
                (name, category),
            )
    print(f"  seed taxonomy：{len(dims)} 個維度")


# ── tag 解析：既有維度→active，新維度→proposed ──────────────────────
def resolve_tag(conn: sqlite3.Connection, category: str, name: str, dims: dict) -> tuple[int, bool]:
    """回傳 (tag_id, is_new_tag)。先查 alias，再查 tags，找不到就建立。"""
    category = category.strip()
    name = name.strip()
    if not category or not name:
        raise ValueError(f"tag 不可有空欄位：category={category!r} name={name!r}")

    # 同義詞收斂
    row = conn.execute(
        "SELECT canonical_tag_id FROM tag_aliases WHERE category=? AND alias=?",
        (category, name),
    ).fetchone()
    if row:
        return row[0], False

    row = conn.execute(
        "SELECT id FROM tags WHERE category=? AND name=?", (category, name)
    ).fetchone()
    if row:
        return row[0], False

    # 新 tag：既有維度→active；全新維度→proposed
    status = "active" if category in dims else "proposed"
    cur = conn.execute(
        "INSERT INTO tags(name, category, status, usage_count) VALUES (?,?,?,0)",
        (name, category, status),
    )
    return cur.lastrowid, True


# ── 創作者：一張圖可多人，各帶 role（模特兒 / 攝影師…）────────────────
def resolve_creator(conn: sqlite3.Connection, name: str, handle: str | None = None,
                    url: str | None = None, note: str | None = None) -> int:
    """以 name 去重取得 creator id，沒有就建立。後補的 handle/url/note 只填空欄、不覆寫。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("creator name 不可為空")
    handle = (handle or "").strip() or None
    url = (url or "").strip() or None
    note = (note or "").strip() or None

    row = conn.execute("SELECT id FROM creators WHERE name=?", (name,)).fetchone()
    if row:
        cid = row[0]
        if handle or url or note:
            conn.execute(
                "UPDATE creators SET handle=COALESCE(handle,?), url=COALESCE(url,?), "
                "note=COALESCE(note,?) WHERE id=?",
                (handle, url, note, cid),
            )
        return cid
    cur = conn.execute(
        "INSERT INTO creators(name, handle, url, note) VALUES (?,?,?,?)",
        (name, handle, url, note),
    )
    return cur.lastrowid


def link_creators(conn: sqlite3.Connection, pose_id: int, creators: list | None,
                  st: "Stats | None" = None) -> int:
    """把一份創作者清單 [{name, role, handle?, url?}, ...] 掛到 pose 上（(creator, role) 去重）。"""
    n = 0
    for c in creators or []:
        if not isinstance(c, dict):
            print(f"    ⚠ 略過壞 creator：{c!r}"); continue
        name = (c.get("name") or "").strip()
        if not name:
            print(f"    ⚠ 略過缺 name 的 creator：{c!r}"); continue
        role = (c.get("role") or "").strip() or DEFAULT_CREATOR_ROLE
        cid = resolve_creator(conn, name, c.get("handle"), c.get("url"), c.get("note"))
        cur = conn.execute(
            "INSERT OR IGNORE INTO pose_creators(pose_id, creator_id, role) VALUES (?,?,?)",
            (pose_id, cid, role),
        )
        if cur.rowcount and st is not None:
            st.creators.add(f"{role}:{name}")
        n += 1
    return n


# ── 圖片：hash / 複製 / 縮圖 ────────────────────────────────────────
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def store_image(src: Path, content_hash: str) -> tuple[str, str | None, int | None, int | None]:
    """把原圖複製進 data/images/<hash>.<ext>，產縮圖。
    回傳 (image_rel, thumb_rel|None, thumb_w|None, thumb_h|None)。"""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)

    ext = src.suffix.lower() or ".jpg"
    dst = IMAGES_DIR / f"{content_hash}{ext}"
    if not dst.exists():
        shutil.copy2(src, dst)

    thumb = THUMBS_DIR / f"{content_hash}.jpg"
    thumb_rel: str | None = None
    tw = th = None
    if make_thumbnail(dst, thumb):
        thumb_rel = str(thumb.relative_to(DATA_DIR))
        tw, th = read_dims(thumb)

    return str(dst.relative_to(DATA_DIR)), thumb_rel, tw, th


def read_dims(path: Path) -> tuple[int | None, int | None]:
    """讀圖檔長寬（給前端預留長寬比）；沒有 Pillow 或讀不到就回 (None, None)，不影響入庫。"""
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def make_thumbnail(src: Path, dst: Path) -> bool:
    """產縮圖：優先 Pillow，退而用 macOS sips。都沒有就放棄（回 False，不影響入庫）。"""
    try:
        from PIL import Image  # type: ignore

        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            im.save(dst, "JPEG", quality=85)
        return True
    except ModuleNotFoundError:
        pass
    except Exception as e:  # 壞檔等
        print(f"    ⚠ Pillow 產縮圖失敗（{e}），改試 sips")

    if shutil.which("sips"):
        try:
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-Z", str(THUMB_MAX),
                 str(src), "--out", str(dst)],
                check=True, capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"    ⚠ sips 產縮圖失敗：{e.stderr.decode(errors='ignore')[:120]}")
    return False


# ── 向量 ────────────────────────────────────────────────────────────
# 預設不自算向量（腳本只寫 Claude 給的資料）。兩種來源：
#   1) manifest 帶現成 "embedding": [float, ...] → vec_from_list()
#   2) --embed 旗標 → fastembed 本地算（選配，較重）
_EMBEDDER = None
_EMBED_DISABLED = False


def vec_from_list(values: list, *, model: str = "external") -> tuple[str, bytes] | None:
    """把 manifest 提供的向量陣列轉成 (model, float32_bytes)。維度不符就拒絕。"""
    if not values:
        return None
    if len(values) != EMBED_DIM:
        print(f"    ⚠ 略過向量：維度 {len(values)} ≠ 預期 {EMBED_DIM}")
        return None
    try:
        arr = array.array("f", [float(x) for x in values])
    except (TypeError, ValueError):
        print("    ⚠ 略過向量：embedding 內含非數值")
        return None
    return model, arr.tobytes()


def embed_text(text: str) -> tuple[str, bytes] | None:
    """用 fastembed 本地算向量（僅 --embed 時呼叫）。回傳 (model, float32_bytes) 或 None。"""
    global _EMBEDDER, _EMBED_DISABLED
    if _EMBED_DISABLED:
        return None
    if _EMBEDDER is None:
        try:
            from fastembed import TextEmbedding  # type: ignore

            _EMBEDDER = (TextEmbedding(model_name=EMBED_MODEL), EMBED_MODEL)
        except ModuleNotFoundError:
            print("  ⚠ --embed 需要 fastembed：pip install fastembed（這次先不算向量）")
            _EMBED_DISABLED = True
            return None
        except Exception as e:
            print(f"  ⚠ embedding 初始化失敗，略過：{e}")
            _EMBED_DISABLED = True
            return None
    model, model_name = _EMBEDDER
    vec = next(iter(model.embed([text])))
    arr = array.array("f", [float(x) for x in vec])
    if len(arr) != EMBED_DIM:
        print(f"  ⚠ 模型維度 {len(arr)} ≠ vec0 的 {EMBED_DIM}，略過向量")
        return None
    return model_name, arr.tobytes()


def store_vector(conn: sqlite3.Connection, pose_id: int, model: str, blob: bytes) -> None:
    """把向量寫進 vec0 表（先刪舊列再插入，等同 upsert）。"""
    conn.execute("DELETE FROM pose_vectors WHERE pose_id=?", (pose_id,))
    conn.execute(
        "INSERT INTO pose_vectors(pose_id, embedding, model) VALUES (?,?,?)",
        (pose_id, blob, model),
    )


# ── 入庫單筆 ────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.added = 0
        self.dup = 0
        self.removed = 0      # compact 時因 tombstone 而被略過/移除的張數
        self.new_tags = 0
        self.errors = 0
        self.added_chars: set[str] = set()
        self.new_tag_labels: list[str] = []
        self.creators: set[str] = set()        # 本批新掛上的「role:name」


def ingest_one(conn: sqlite3.Connection, entry: dict, dims: dict, base: Path, st: Stats,
               embed: bool = False) -> None:
    img_field = entry.get("image")
    if not img_field:
        print("  ✗ 略過：缺 image 欄位"); st.errors += 1; return
    src = Path(img_field).expanduser()
    if not src.is_absolute():
        src = base / src           # 相對路徑以 manifest 所在資料夾為基準
    if not src.exists():
        print(f"  ✗ 找不到圖：{img_field}"); st.errors += 1; return

    description = (entry.get("description") or "").strip()
    tags = entry.get("tags") or []
    if not description or not tags:
        print(f"  ✗ 略過 {src.name}：description / tags 不可空"); st.errors += 1; return

    content_hash = sha256_file(src)
    exists = conn.execute(
        "SELECT id FROM poses WHERE content_hash=?", (content_hash,)
    ).fetchone()
    if exists:
        print(f"  ↻ 重複略過：{src.name}"); st.dup += 1; return

    image_rel, thumb_rel, thumb_w, thumb_h = store_image(src, content_hash)

    cur = conn.execute(
        "INSERT INTO poses(image_path, thumbnail_path, thumb_w, thumb_h, description, "
        "content_hash, favorite, rating, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (image_rel, thumb_rel, thumb_w, thumb_h, description, content_hash,
         1 if entry.get("favorite") else 0, entry.get("rating"),
         entry.get("source") or src.name),
    )
    pose_id = cur.lastrowid

    # tags
    for t in tags:
        category = t.get("category"); name = t.get("name")
        if not category or not name:
            print(f"    ⚠ 略過壞 tag：{t!r}"); continue
        tag_id, is_new = resolve_tag(conn, category, name, dims)
        conn.execute(
            "INSERT OR IGNORE INTO pose_tags(pose_id, tag_id) VALUES (?,?)",
            (pose_id, tag_id),
        )
        conn.execute("UPDATE tags SET usage_count = usage_count + 1 WHERE id=?", (tag_id,))
        if is_new:
            st.new_tags += 1
            status = conn.execute("SELECT status FROM tags WHERE id=?", (tag_id,)).fetchone()[0]
            flag = "（proposed 新維度）" if status == "proposed" else ""
            st.new_tag_labels.append(f"{category}:{name}{flag}")
            if category == "character":
                st.added_chars.add(name)

    # 創作者（選配）：模特兒 / 攝影師…一張圖可多人
    link_creators(conn, pose_id, entry.get("creators"), st)

    # 向量（選配）：優先用 manifest 帶來的，其次才是 --embed 本地算
    vec = vec_from_list(entry.get("embedding") or [], model=entry.get("embedding_model") or "external")
    if not vec and embed:
        vec = embed_text(description)
    if vec:
        model_name, blob = vec
        store_vector(conn, pose_id, model_name, blob)

    st.added += 1
    print(f"  ＋ {src.name} → #{pose_id}（{len(tags)} tags）")


# ── 子指令 ──────────────────────────────────────────────────────────
def cmd_ingest(args) -> None:
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        raise SystemExit(f"找不到 manifest：{manifest_path}")
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("poses") or entries.get("items") or [entries]
    if not isinstance(entries, list):
        raise SystemExit("manifest 應是 JSON 陣列。")
    base = manifest_path.parent
    _run_entries(entries, base, embed=getattr(args, "embed", False))


def _parse_creator_flags(flags: list | None) -> list[dict]:
    """把 --creator 旗標解析成 [{"name","role"}, ...]。格式：name=role 或只給 name（role 用預設）。"""
    creators = []
    for kv in flags or []:
        if "=" in kv:
            name, role = kv.split("=", 1)
            creators.append({"name": name.strip(), "role": role.strip() or DEFAULT_CREATOR_ROLE})
        else:
            creators.append({"name": kv.strip(), "role": DEFAULT_CREATOR_ROLE})
    return creators


def cmd_add(args) -> None:
    tags = []
    for kv in args.tag or []:
        if "=" not in kv:
            raise SystemExit(f"--tag 格式要 category=name，收到：{kv}")
        cat, name = kv.split("=", 1)
        tags.append({"category": cat, "name": name})
    entry = {
        "image": args.image,
        "description": args.description,
        "tags": tags,
        "creators": _parse_creator_flags(args.creator),
        "source": args.source,
        "rating": args.rating,
        "favorite": args.favorite,
    }
    _run_entries([entry], Path.cwd(), embed=getattr(args, "embed", False))


def _run_entries(entries: list, base: Path, embed: bool = False) -> None:
    dims = load_taxonomy()
    conn = connect()
    st = Stats()
    try:
        for entry in entries:
            try:
                ingest_one(conn, entry, dims, base, st, embed=embed)
            except Exception as e:
                print(f"  ✗ 入庫失敗：{e}"); st.errors += 1
        summary = build_summary(conn, st)
        conn.execute(
            "INSERT INTO ingest_log(n_added, n_dup, n_new_tags, summary) VALUES (?,?,?,?)",
            (st.added, st.dup, st.new_tags, summary),
        )
        conn.commit()
    finally:
        conn.close()
    print("\n" + summary)


def build_summary(conn: sqlite3.Connection, st: Stats) -> str:
    parts = [f"今天 +{st.added} 張"]
    if st.dup:
        parts.append(f"重複 {st.dup} 張")
    if st.removed:
        parts.append(f"依 tombstone 移除 {st.removed} 張")
    if st.errors:
        parts.append(f"失敗 {st.errors} 張")
    if st.added_chars:
        parts.append("新增角色：" + "、".join(sorted(st.added_chars)))
    if st.creators:
        parts.append("創作者：" + "、".join(sorted(st.creators)))
    if st.new_tag_labels:
        shown = "、".join(st.new_tag_labels[:8])
        more = f" 等 {len(st.new_tag_labels)} 個" if len(st.new_tag_labels) > 8 else ""
        parts.append(f"新學到 tag：{shown}{more}")
    return "；".join(parts) + "。"


# ── Fragment：「只上傳不下載」的單位 ───────────────────────────────
# 雲端（手機 / Desktop）流程：pack 把「一份正常 manifest（含原圖路徑）」就地轉成
# 一個**自包含、不含原圖 bytes** 的小 fragment（zip）：
#     frag-<ts>.zip
#       ├ manifest.json   每筆帶 content_hash + thumbnail 檔名（給之後 compact 回放）
#       └ thumbs/<hash>.jpg
# pack **完全不碰 library.db**——pack 完只要把這一個 zip 上傳到 Drive 的
# fragments/ 即可（連接器只傳這一小包，永不下載整個庫）。
#
# 搜尋時：把 Drive 上累積的 fragment zip 全下載回沙箱，再用 compact 一次回放併成一份
# **臨時** library.db（content_hash 去重）來查；查完即丟，不回傳 Drive。
def cmd_pack(args) -> None:
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        raise SystemExit(f"找不到 manifest：{manifest_path}")
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("poses") or entries.get("items") or [entries]
    if not isinstance(entries, list):
        raise SystemExit("manifest 應是 JSON 陣列。")
    base = manifest_path.parent

    out_arg = Path(args.out).expanduser()
    if out_arg.suffix.lower() == ".zip":
        out_zip = out_arg
    else:  # 給的是資料夾 → 自動命名 frag-<時間戳>.zip
        out_arg.mkdir(parents=True, exist_ok=True)
        out_zip = out_arg / f"frag-{datetime.now():%Y%m%dT%H%M%S}.zip"
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    resolved: list[dict] = []
    n_thumb = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        thumb_files: list[tuple[str, Path]] = []
        for entry in entries:
            img_field = entry.get("image")
            if not img_field:
                print("  ✗ 略過：缺 image 欄位"); continue
            src = Path(img_field).expanduser()
            if not src.is_absolute():
                src = base / src
            if not src.exists():
                print(f"  ✗ 找不到圖：{img_field}"); continue
            description = (entry.get("description") or "").strip()
            tags = entry.get("tags") or []
            if not description or not tags:
                print(f"  ✗ 略過 {src.name}：description / tags 不可空"); continue

            content_hash = sha256_file(src)
            ext = src.suffix.lower() or ".jpg"
            thumb_name = None
            thumb_w = thumb_h = None
            thumb_dst = tmp / f"{content_hash}.jpg"
            if make_thumbnail(src, thumb_dst):
                thumb_name = f"{content_hash}.jpg"
                thumb_files.append((thumb_name, thumb_dst))
                thumb_w, thumb_h = read_dims(thumb_dst)
                n_thumb += 1
            else:
                print(f"    ⚠ {src.name} 沒產出縮圖（缺 Pillow？雲端請先 pip install pillow）")

            resolved.append({
                "content_hash": content_hash,
                "description": description,
                "tags": tags,
                "creators": entry.get("creators") or [],
                "source": entry.get("source") or src.name,
                "rating": entry.get("rating"),
                "favorite": bool(entry.get("favorite")),
                "image_ext": ext,
                "thumbnail": thumb_name,
                "thumb_w": thumb_w,
                "thumb_h": thumb_h,
                "embedding": entry.get("embedding"),
                "embedding_model": entry.get("embedding_model"),
            })

        if not resolved:
            raise SystemExit("沒有任何有效筆可打包（檢查圖路徑 / description / tags）。")

        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(resolved, ensure_ascii=False, indent=2))
            for name, path in thumb_files:
                z.write(path, f"thumbs/{name}")

    size_kb = out_zip.stat().st_size / 1024
    print(f"\n打包完成：{len(resolved)} 筆 → {out_zip}（縮圖 {n_thumb}，{size_kb:.0f} KB）")
    print("  ↳ 雲端：把這「一個 zip」上傳到 Drive 的 PosePlanner/fragments/ 即可（不下載整個庫）。")


def _store_thumb_from(content_hash: str, src_thumb: Path) -> str | None:
    """compact 用：把 fragment 內的縮圖複製進 data/images/thumbs/，回傳相對路徑。"""
    if not src_thumb.exists():
        return None
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    dst = THUMBS_DIR / f"{content_hash}.jpg"
    if not dst.exists():
        shutil.copy2(src_thumb, dst)
    return str(dst.relative_to(DATA_DIR))


def compact_one(conn: sqlite3.Connection, entry: dict, dims: dict, frag_dir: Path,
                st: Stats) -> None:
    """回放一筆 fragment 進庫。原圖不在本機（手機端未上傳），只記 content_hash + 縮圖。"""
    content_hash = (entry.get("content_hash") or "").strip()
    if not content_hash:
        print("  ✗ 略過：fragment 筆缺 content_hash"); st.errors += 1; return
    description = (entry.get("description") or "").strip()
    tags = entry.get("tags") or []
    if not description or not tags:
        print(f"  ✗ 略過 {content_hash[:8]}：description / tags 不可空"); st.errors += 1; return

    if conn.execute("SELECT id FROM poses WHERE content_hash=?", (content_hash,)).fetchone():
        st.dup += 1; return

    thumb_rel = None
    thumb_w, thumb_h = entry.get("thumb_w"), entry.get("thumb_h")
    if entry.get("thumbnail"):
        thumb_rel = _store_thumb_from(content_hash, frag_dir / "thumbs" / entry["thumbnail"])
        # 舊 fragment 的 manifest 沒帶尺寸時，現場讀縮圖補上。
        if thumb_rel and not (thumb_w and thumb_h):
            thumb_w, thumb_h = read_dims(DATA_DIR / thumb_rel)
    # 原圖留在加圖當下的裝置，本機沒有檔；image_path 只記邏輯路徑供日後對照。
    image_rel = f"images/{content_hash}{entry.get('image_ext') or '.jpg'}"

    cur = conn.execute(
        "INSERT INTO poses(image_path, thumbnail_path, thumb_w, thumb_h, description, "
        "content_hash, favorite, rating, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (image_rel, thumb_rel, thumb_w, thumb_h, description, content_hash,
         1 if entry.get("favorite") else 0, entry.get("rating"), entry.get("source")),
    )
    pose_id = cur.lastrowid

    for t in tags:
        category = t.get("category"); name = t.get("name")
        if not category or not name:
            print(f"    ⚠ 略過壞 tag：{t!r}"); continue
        tag_id, is_new = resolve_tag(conn, category, name, dims)
        conn.execute("INSERT OR IGNORE INTO pose_tags(pose_id, tag_id) VALUES (?,?)",
                     (pose_id, tag_id))
        conn.execute("UPDATE tags SET usage_count = usage_count + 1 WHERE id=?", (tag_id,))
        if is_new:
            st.new_tags += 1
            status = conn.execute("SELECT status FROM tags WHERE id=?", (tag_id,)).fetchone()[0]
            flag = "（proposed 新維度）" if status == "proposed" else ""
            st.new_tag_labels.append(f"{category}:{name}{flag}")
            if category == "character":
                st.added_chars.add(name)

    link_creators(conn, pose_id, entry.get("creators"), st)

    vec = vec_from_list(entry.get("embedding") or [],
                        model=entry.get("embedding_model") or "external")
    if vec:
        store_vector(conn, pose_id, vec[0], vec[1])

    st.added += 1
    print(f"  ＋ {content_hash[:8]} → #{pose_id}（{len(tags)} tags）")


def cmd_compact(args) -> None:
    """把一批 fragment（zip 或解開的資料夾）回放併進 library.db。content_hash 去重、可重跑。"""
    frag_root = Path(args.fragments).expanduser()
    if not frag_root.exists():
        raise SystemExit(f"找不到 fragments：{frag_root}")

    # 收集來源：*.zip 與「解開的 manifest.json 資料夾」都吃。
    sources: list[Path] = []
    if frag_root.is_file():
        sources = [frag_root]
    else:
        sources = sorted(frag_root.rglob("*.zip")) + sorted(frag_root.rglob("manifest.json"))
    if not sources:
        raise SystemExit(f"{frag_root} 底下找不到 fragment（*.zip 或 manifest.json）。")

    dims = load_taxonomy()
    conn = connect()
    st = Stats()
    try:
        with tempfile.TemporaryDirectory() as td:
            # ── Pass 1：解開所有來源、讀 manifest，把正常 fragment 與 tombstone 分流。
            #    tombstone 的 content_hash 先全部收進 dead，正常筆稍後一律跳過/移除這些 hash。
            normal: list[tuple[str, Path, list]] = []   # (label, frag_dir, entries)
            dead: set[str] = set()
            for src in sources:
                if src.suffix.lower() == ".zip":
                    frag_dir = Path(td) / src.stem
                    frag_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(src) as z:
                        z.extractall(frag_dir)
                    mf = frag_dir / "manifest.json"
                    label = src.name
                else:  # manifest.json
                    frag_dir = src.parent
                    mf = src
                    label = str(src.relative_to(frag_root)) if frag_root.is_dir() else src.name
                if not mf.exists():
                    print(f"  ⚠ 略過（zip 內無 manifest.json）：{src.name}"); continue
                manifest = json.loads(mf.read_text(encoding="utf-8"))
                if isinstance(manifest, dict) and manifest.get("poseplanner_op") == TOMBSTONE_OP:
                    hashes = [h.strip() for h in (manifest.get("content_hashes") or []) if str(h).strip()]
                    dead.update(hashes)
                    note = manifest.get("note")
                    print(f"⊘ tombstone {label}：標記刪除 {len(hashes)} 筆"
                          + (f"（{note}）" if note else ""))
                    continue
                if not isinstance(manifest, list):
                    print(f"  ⚠ 略過非陣列 fragment：{label}"); continue
                normal.append((label, frag_dir, manifest))

            # ── Pass 2：回放正常筆，content_hash 落在 dead 的一律不入。
            for label, frag_dir, entries in normal:
                print(f"▸ {label}（{len(entries)} 筆）")
                for entry in entries:
                    if (entry.get("content_hash") or "").strip() in dead:
                        st.removed += 1; continue
                    try:
                        compact_one(conn, entry, dims, frag_dir, st)
                    except Exception as e:
                        print(f"  ✗ 失敗：{e}"); st.errors += 1

            # 保險：若 db 是重用的（非用完即焚），把已存在卻被 tombstone 的列也刪掉。
            # CASCADE + trg_poses_del_vec 會連帶清掉 pose_tags / pose_creators / 向量。
            for h in dead:
                conn.execute("DELETE FROM poses WHERE content_hash=?", (h,))

        summary = build_summary(conn, st)
        conn.execute(
            "INSERT INTO ingest_log(n_added, n_dup, n_new_tags, summary) VALUES (?,?,?,?)",
            (st.added, st.dup, st.new_tags, "compact｜" + summary),
        )
        conn.commit()
    finally:
        conn.close()
    print("\n" + summary)


# ── 刪除（drive 模式）：產一個 tombstone fragment ───────────────────
# 兩段式安全閥：預設只「列出將刪什麼」(dry-run)，不寫任何檔；要真的產 tombstone
# 必須再加 --confirm。SKILL 流程要求 Claude 先把這些圖渲染給使用者看、明確同意後才 --confirm。
def _resolve_targets(conn, ids: list[int], hashes: list[str]) -> tuple[list[dict], list[str]]:
    """把使用者點的 id / hash 對應回臨時庫裡的 pose（含 content_hash + 敘述）。
    回傳 (targets, missing)；targets 每筆 {id?, content_hash, description, tags}。"""
    targets: list[dict] = []
    seen: set[str] = set()
    missing: list[str] = []

    def add_row(row, label):
        if not row:
            missing.append(label); return
        pid, ch, desc = row
        if ch in seen:
            return
        seen.add(ch)
        targets.append({
            "id": pid, "content_hash": ch,
            "description": desc or "", "tags": _tags_of(conn, pid),
        })

    for pid in ids:
        add_row(
            conn.execute(
                "SELECT id, content_hash, description FROM poses WHERE id=?", (pid,)
            ).fetchone(),
            f"#{pid}",
        )
    for h in hashes:
        h = h.strip()
        if not h:
            continue
        if h in seen:
            continue
        # 允許給「前綴」（搜尋表常只顯示 hash 前 8 碼）
        add_row(
            conn.execute(
                "SELECT id, content_hash, description FROM poses WHERE content_hash=? "
                "OR content_hash LIKE ? LIMIT 1", (h, h + "%"),
            ).fetchone(),
            h,
        )
    return targets, missing


def _tags_of(conn, pose_id: int) -> list[str]:
    return [
        f"{cat}:{name}"
        for cat, name in conn.execute(
            "SELECT t.category, t.name FROM pose_tags pt "
            "JOIN tags t ON t.id=pt.tag_id WHERE pt.pose_id=? ORDER BY t.category",
            (pose_id,),
        )
    ]


def cmd_forget(args) -> None:
    """drive 模式刪除：把選中的圖打包成一個 tombstone fragment（上傳到 Drive fragments/ 後，
    日後 compact 重建臨時庫就不會再出現它們）。預設 dry-run，--confirm 才真的產出 zip。"""
    if not DB_PATH.exists():
        raise SystemExit(
            "找不到臨時庫，無法對照要刪哪幾張。請先照搜尋流程 compact 出 library.db，"
            "用同一個 --db 指過來。"
        )
    ids: list[int] = []
    for chunk in args.ids or []:
        for tok in chunk.replace("，", ",").split(","):
            tok = tok.strip().lstrip("#")
            if tok:
                if not tok.isdigit():
                    raise SystemExit(f"--ids 只能是數字 id（逗號分隔），收到：{tok}")
                ids.append(int(tok))
    hashes: list[str] = []
    for chunk in args.hash or []:
        hashes += [h for h in chunk.replace("，", ",").split(",") if h.strip()]
    if not ids and not hashes:
        raise SystemExit("請用 --ids（搜尋表上的 #）或 --hash 指定要刪哪幾張。")

    conn = connect()
    try:
        targets, missing = _resolve_targets(conn, ids, hashes)
    finally:
        conn.close()

    if missing:
        print("⚠ 這些在臨時庫裡找不到（id 可能來自別次 compact，或 hash 打錯）：" + "、".join(missing))
    if not targets:
        raise SystemExit("沒有可刪的目標——請確認 id/hash 來自『這次搜尋』compact 出的同一個庫。")

    print(f"\n將刪除 {len(targets)} 張（drive：產 tombstone，日後搜尋就不再出現）：")
    for t in targets:
        print(f"  • #{t['id']}  {t['content_hash'][:12]}  {t['description'][:40]}")
        if t["tags"]:
            print(f"      tags: {'、'.join(tg.split(':', 1)[-1] for tg in t['tags'])}")

    if not args.confirm:
        print("\n— 這是預覽（dry-run），還沒有刪任何東西，也還沒產生 tombstone。")
        print("  請先把上面這些圖渲染給使用者確認；得到明確同意後，再加 --confirm 重跑這條指令。")
        return

    if not args.out:
        raise SystemExit("--confirm 需要 --out 指定 tombstone 輸出路徑（.zip 或資料夾）。")
    out_arg = Path(args.out).expanduser()
    if out_arg.suffix.lower() == ".zip":
        out_zip = out_arg
    else:
        out_arg.mkdir(parents=True, exist_ok=True)
        out_zip = out_arg / f"tomb-{datetime.now():%Y%m%dT%H%M%S}.zip"
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "poseplanner_op": TOMBSTONE_OP,
        "content_hashes": [t["content_hash"] for t in targets],
        "note": args.note or f"刪除 {len(targets)} 張（經使用者同意）",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    size_kb = out_zip.stat().st_size / 1024
    print(f"\n✓ 已產生 tombstone：{out_zip}（{len(targets)} 筆，{size_kb:.1f} KB）")
    print("  ↳ 把這個 zip 跟一般 fragment 一樣上傳到 Drive 的 PosePlanner/fragments/")
    print("    （create_file，contentMimeType=application/zip，disableConversionToGoogleType=true）。")
    print("  上傳成功後，這幾張圖在日後的搜尋（compact）就不會再出現。")


def cmd_init(args) -> None:
    conn = connect()
    conn.commit(); conn.close()
    print("初始化完成。")


def cmd_stats(args) -> None:
    if not DB_PATH.exists():
        print("尚未建庫，先跑：python3 scripts/add_pose.py init"); return
    conn = connect()
    n_poses = conn.execute("SELECT COUNT(*) FROM poses").fetchone()[0]
    n_emb = conn.execute("SELECT COUNT(*) FROM pose_vectors").fetchone()[0]
    n_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    n_prop = conn.execute("SELECT COUNT(*) FROM tags WHERE status='proposed'").fetchone()[0]
    n_creators = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
    print(f"poses：{n_poses}（有向量 {n_emb}）")
    print(f"tags ：{n_tags}（proposed {n_prop}）")
    print(f"創作者：{n_creators}")
    print("\n熱門 tag：")
    for cat, name, uc in conn.execute(
        "SELECT category, name, usage_count FROM tags "
        "WHERE usage_count>0 ORDER BY usage_count DESC LIMIT 12"
    ):
        print(f"  {uc:>3}  {cat}:{name}")
    conn.close()


def main() -> None:
    vecdb.ensure_capable_interpreter()  # 必要時 re-exec 到支援 load_extension 的 python3
    p = argparse.ArgumentParser(description="PosePlanner 圖片入庫")
    # 路徑覆寫可放在子指令「前或後」都行（雲端持久化時把 db/data 指到沙箱副本）。
    p.add_argument("--db", default=None,
                   help="library.db 路徑（預設 data/library.db；亦可用環境變數 POSEPLANNER_DB）")
    p.add_argument("--data", default=None,
                   help="資料目錄，圖存這裡（預設 ./data；亦可用 POSEPLANNER_DATA）")

    # 同樣兩個旗標也掛到各子指令上；未提供時用 SUPPRESS 以免蓋掉前置值
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--db", default=argparse.SUPPRESS)
    parent.add_argument("--data", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", parents=[parent], help="批次匯入 manifest JSON")
    pi.add_argument("--manifest", required=True)
    pi.add_argument("--embed", action="store_true",
                    help="入庫時用本地 fastembed 算敘述向量寫進 vec0（選配，需 pip install fastembed）")
    pi.set_defaults(func=cmd_ingest)

    pa = sub.add_parser("add", parents=[parent], help="單張匯入")
    pa.add_argument("--image", required=True)
    pa.add_argument("--description", required=True)
    pa.add_argument("--tag", action="append", help="category=name，可重複")
    pa.add_argument("--creator", action="append",
                    help="創作者，格式 name=role（role 如 model/photographer），可重複；只給 name 則用預設角色")
    pa.add_argument("--source")
    pa.add_argument("--rating", type=int)
    pa.add_argument("--favorite", action="store_true")
    pa.add_argument("--embed", action="store_true",
                    help="用本地 fastembed 算向量寫進 vec0（選配）")
    pa.set_defaults(func=cmd_add)

    pp = sub.add_parser("pack", parents=[parent],
                        help="把 manifest+圖打包成可上傳的 fragment zip（不寫庫；雲端/手機用）")
    pp.add_argument("--manifest", required=True)
    pp.add_argument("--out", required=True,
                    help="輸出 .zip 路徑，或一個資料夾（會自動命名 frag-<時間戳>.zip）")
    pp.set_defaults(func=cmd_pack)

    pcc = sub.add_parser("compact", parents=[parent],
                         help="把一批 fragment（zip/資料夾）回放併進 library.db（去重；本機用）")
    pcc.add_argument("--fragments", required=True,
                     help="含若干 fragment 的根目錄或單一 zip（遞迴找 *.zip / manifest.json）")
    pcc.set_defaults(func=cmd_compact)

    pf = sub.add_parser("forget", parents=[parent],
                        help="drive 模式刪除：把選中的圖打包成 tombstone（預設 dry-run，--confirm 才產出）")
    pf.add_argument("--ids", action="append",
                    help="要刪的 pose id（搜尋表上的 #，逗號分隔，可重複）")
    pf.add_argument("--hash", action="append",
                    help="直接指定 content_hash（可給前綴；逗號分隔，可重複）")
    pf.add_argument("--out", default=None,
                    help="tombstone .zip 路徑或輸出資料夾（--confirm 時必填；自動命名 tomb-<時間戳>.zip）")
    pf.add_argument("--note", help="這次刪除的備註，會寫進 tombstone")
    pf.add_argument("--confirm", action="store_true",
                    help="確認刪除：實際產出 tombstone zip（沒給就只做 dry-run 預覽）")
    pf.set_defaults(func=cmd_forget)

    sub.add_parser("init", parents=[parent], help="建庫 + seed").set_defaults(func=cmd_init)
    sub.add_parser("stats", parents=[parent], help="看庫狀態").set_defaults(func=cmd_stats)

    args = p.parse_args()
    configure(getattr(args, "db", None), getattr(args, "data", None))
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
