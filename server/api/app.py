"""PosePlanner 自架 server — 圖片 / fragment 接收服務（FastAPI）。

這是「私有 DB」模式的後端：跟 Google Drive 模式二選一。和純雲端不同，
私有 server **可以收原圖**並存在你網域內的硬碟 + PostgreSQL，不受 Drive
「只能新建、單筆大小上限」的限制。

端點
────
  GET  /health                      健康檢查（免 token）
  GET  /                            極簡網頁上傳表單（瀏覽器手動丟圖用）
  GET  /stats                       庫狀態（poses / tags 數）
  POST /images     (multipart)      收「一張原圖 + metadata」直接入庫（去重、產縮圖）
  POST /fragments  (multipart)      收一個 pack 出來的 fragment zip，回放併庫（相容雲端格式）
  GET  /search?q=&tag=&limit=       tag + 關鍵字粗篩候選（語意排序交給 Claude）
  GET  /thumbs/{name}               取縮圖
  GET  /images/{name}               取原圖

除了 /health、/、/thumbs、/images（讀圖）外，寫入端點需要 Bearer token
（環境變數 POSEPLANNER_TOKEN；未設定則不檢查，僅建議在純內網時這樣）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import db

DATA_DIR = Path(os.environ.get("POSEPLANNER_DATA", "/data"))
IMAGES_DIR = DATA_DIR / "images"
THUMBS_DIR = DATA_DIR / "thumbs"
THUMB_MAX = 512
TOKEN = os.environ.get("POSEPLANNER_TOKEN", "").strip()
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tiff"}

app = FastAPI(title="PosePlanner 私有 DB", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    db.wait_and_init()


# ── 認證 ────────────────────────────────────────────────────────────
def require_token(authorization: str | None = Header(default=None)) -> None:
    if not TOKEN:  # 未設 token → 純內網信任模式，不檢查
        return
    expected = f"Bearer {TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="缺少或錯誤的 Bearer token")


# ── 縮圖 ────────────────────────────────────────────────────────────
def make_thumbnail(img_bytes: bytes, dst: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(img_bytes)) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            im.save(dst, "JPEG", quality=85)
        return True
    except Exception:
        return False


# ── 端點 ────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "poseplanner-private-db"}


@app.get("/stats")
def stats() -> dict:
    with db.pool().connection() as conn:
        n_poses = conn.execute("SELECT COUNT(*) FROM poses").fetchone()[0]
        n_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        n_prop = conn.execute("SELECT COUNT(*) FROM tags WHERE status='proposed'").fetchone()[0]
    return {"poses": n_poses, "tags": n_tags, "proposed_tags": n_prop}


@app.post("/images")
async def upload_image(
    file: UploadFile = File(...),
    description: str = Form(...),
    tags: str = Form(...),                       # JSON 陣列：[{"category","name"}, ...]
    source: str | None = Form(default=None),
    rating: int | None = Form(default=None),
    favorite: bool = Form(default=False),
    embedding: str | None = Form(default=None),  # 選配 JSON 陣列（384 維）
    embedding_model: str | None = Form(default=None),
    _: None = Depends(require_token),
) -> JSONResponse:
    """收一張原圖 + metadata，直接入庫。content_hash 去重、產縮圖、存原圖。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空檔案")
    try:
        tag_list = json.loads(tags)
        assert isinstance(tag_list, list)
    except Exception:
        raise HTTPException(status_code=400, detail="tags 必須是 JSON 陣列")
    if not description.strip() or not tag_list:
        raise HTTPException(status_code=400, detail="description / tags 不可空")

    content_hash = hashlib.sha256(raw).hexdigest()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".jpg"

    # 存原圖（已存在就不重寫）
    img_dst = IMAGES_DIR / f"{content_hash}{ext}"
    if not img_dst.exists():
        img_dst.write_bytes(raw)

    # 縮圖
    thumb_dst = THUMBS_DIR / f"{content_hash}.jpg"
    thumb_rel = None
    if thumb_dst.exists() or make_thumbnail(raw, thumb_dst):
        thumb_rel = f"thumbs/{content_hash}.jpg"

    emb = None
    if embedding:
        try:
            emb = json.loads(embedding)
        except Exception:
            emb = None

    entry = {
        "content_hash": content_hash,
        "description": description,
        "tags": tag_list,
        "source": source or (file.filename or content_hash[:8]),
        "rating": rating,
        "favorite": favorite,
        "image_path": f"images/{content_hash}{ext}",
        "thumbnail_path": thumb_rel,
        "embedding": emb,
        "embedding_model": embedding_model,
    }

    dims = db.known_categories()
    with db.pool().connection() as conn:
        pose_id, state = db.upsert_pose(conn, entry, dims)
    return JSONResponse({"id": pose_id, "status": state, "content_hash": content_hash})


@app.post("/fragments")
async def upload_fragment(
    file: UploadFile = File(...),
    _: None = Depends(require_token),
) -> JSONResponse:
    """收一個 pack 出來的 fragment zip（manifest.json + thumbs/）回放併庫。
    原圖不在 fragment 裡（雲端格式只帶縮圖），所以這條只記縮圖 + metadata。"""
    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是有效的 zip")

    names = set(zf.namelist())
    if "manifest.json" not in names:
        raise HTTPException(status_code=400, detail="zip 內缺 manifest.json")
    entries = json.loads(zf.read("manifest.json").decode("utf-8"))
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="manifest.json 應是陣列")

    dims = db.known_categories()
    added = dup = errors = 0
    with db.pool().connection() as conn:
        for e in entries:
            ch = (e.get("content_hash") or "").strip()
            # 落地縮圖
            thumb_rel = None
            if e.get("thumbnail") and f"thumbs/{e['thumbnail']}" in names:
                thumb_dst = THUMBS_DIR / f"{ch}.jpg"
                if not thumb_dst.exists():
                    thumb_dst.write_bytes(zf.read(f"thumbs/{e['thumbnail']}"))
                thumb_rel = f"thumbs/{ch}.jpg"
            entry = {
                "content_hash": ch,
                "description": e.get("description"),
                "tags": e.get("tags") or [],
                "source": e.get("source"),
                "rating": e.get("rating"),
                "favorite": e.get("favorite"),
                "image_path": f"images/{ch}{e.get('image_ext') or '.jpg'}",
                "thumbnail_path": thumb_rel,
                "embedding": e.get("embedding"),
                "embedding_model": e.get("embedding_model"),
            }
            try:
                _pid, state = db.upsert_pose(conn, entry, dims)
            except Exception:
                errors += 1
                continue
            if state == "added":
                added += 1
            elif state == "dup":
                dup += 1
            else:
                errors += 1
        conn.execute(
            "INSERT INTO ingest_log(n_added, n_dup, n_new_tags, summary) VALUES (%s,%s,%s,%s)",
            (added, dup, 0, f"fragment｜+{added} 重複{dup} 失敗{errors}"),
        )
    return JSONResponse({"added": added, "dup": dup, "errors": errors})


@app.get("/search")
def search(q: str = "", tag: list[str] | None = None, limit: int = 20) -> JSONResponse:
    """tag（category=name，可多個 AND）+ 關鍵字（對 description 做 AND LIKE）粗篩候選。
    語意排序由 Claude 讀 description 完成——和 SQLite 版 search.py 同設計。"""
    tag = tag or []
    tag_pairs = []
    for kv in tag:
        if "=" in kv:
            c, n = kv.split("=", 1)
            tag_pairs.append((c.strip(), n.strip()))

    clauses = []
    params: list = []
    if tag_pairs:
        ors = " OR ".join(["(t.category=%s AND t.name=%s)"] * len(tag_pairs))
        sub_params = [x for pair in tag_pairs for x in pair]
        clauses.append(
            f"p.id IN (SELECT p2.id FROM poses p2 "
            f"JOIN pose_tags pt ON pt.pose_id=p2.id JOIN tags t ON t.id=pt.tag_id "
            f"WHERE {ors} GROUP BY p2.id "
            f"HAVING COUNT(DISTINCT t.category||':'||t.name) = %s)"
        )
        params += sub_params + [len(tag_pairs)]
    for kw in q.split():
        clauses.append("p.description LIKE %s")
        params.append(f"%{kw}%")

    sql = "SELECT p.id, p.image_path, p.thumbnail_path, p.description, p.favorite, p.rating FROM poses p"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY p.favorite DESC, p.rating IS NULL, p.rating DESC, p.created_at DESC LIMIT %s"
    params.append(limit)

    out = []
    with db.pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        for pid, image_path, thumb, desc, fav, rating in rows:
            tags = [
                f"{cat}:{name}"
                for cat, name in conn.execute(
                    "SELECT t.category, t.name FROM pose_tags pt JOIN tags t ON t.id=pt.tag_id "
                    "WHERE pt.pose_id=%s ORDER BY t.category",
                    (pid,),
                ).fetchall()
            ]
            out.append({
                "id": pid,
                "image_path": image_path,
                "thumbnail_path": thumb,
                "description": desc,
                "favorite": bool(fav),
                "rating": rating,
                "tags": tags,
            })
    return JSONResponse(out)


@app.get("/thumbs/{name}")
def get_thumb(name: str):
    path = (THUMBS_DIR / name).resolve()
    if THUMBS_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="找不到縮圖")
    return FileResponse(path)


@app.get("/images/{name}")
def get_image(name: str):
    path = (IMAGES_DIR / name).resolve()
    if IMAGES_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="找不到原圖")
    return FileResponse(path)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """極簡上傳頁：瀏覽器手動丟一張圖測試 /images（標籤用 JSON 陣列）。"""
    return """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>PosePlanner 私有 DB</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px}
label{display:block;margin:12px 0 4px}input,textarea{width:100%;padding:6px;box-sizing:border-box}
button{margin-top:16px;padding:8px 20px}</style></head><body>
<h1>PosePlanner 私有 DB — 圖片入庫</h1>
<p>手動上傳一張 cos 圖測試接收服務。正式入庫請走 Claude skill（backend.py）。</p>
<form action="/images" method="post" enctype="multipart/form-data">
  <label>圖片</label><input type="file" name="file" accept="image/*" required>
  <label>動作敘述 description</label><textarea name="description" rows="3" required></textarea>
  <label>標籤 tags（JSON 陣列）</label>
  <textarea name="tags" rows="3" required>[{"category":"people_count","name":"單人"},{"category":"framing","name":"全身"}]</textarea>
  <label>來源 source（選填）</label><input name="source">
  <label>Bearer token（若 server 有設）</label><input name="_token_hint" disabled
    placeholder="網頁表單不帶 token；有設 token 時請用 backend.py 上傳">
  <button type="submit">上傳入庫</button>
</form></body></html>"""
