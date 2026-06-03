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
  PUT  /poses/{id}/tags  (json)     改一張 pose 的 tags（整批替換 / 只新增 / 只移除）
  PUT  /poses/{id}/creators (json)  改一張 pose 的創作者（模特兒 / 攝影師…；整批替換 / 增 / 移除）
  GET  /search?q=&tag=&limit=       tag + 關鍵字粗篩候選（語意排序交給 Claude）
  GET  /thumbs/{name}               取縮圖（讀取 token 即可，公開找圖頁用這個）
  GET  /images/{name}               取『原圖』——需讀寫 token（防整庫高清被搬走）

讀取端點（/stats /search /thumbs）需『讀取』token（讀寫或唯讀皆可）。
原圖 /images 與所有寫入端點需『讀寫』token（環境變數 POSEPLANNER_TOKEN；
未設定則不檢查，僅建議在純內網時這樣）。/search、/stats 另有速率限制。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import db

DATA_DIR = Path(os.environ.get("POSEPLANNER_DATA", "/data"))
IMAGES_DIR = DATA_DIR / "images"
THUMBS_DIR = DATA_DIR / "thumbs"
THUMB_MAX = 1280   # 縮圖長邊上限；放大檢視夠清楚，又不外洩原圖（原圖端點已鎖讀寫 token）
# 兩種權限的 token：
#   POSEPLANNER_TOKEN       讀寫（入庫 + 查詢 + 取圖）
#   POSEPLANNER_READ_TOKEN  唯讀（只能查詢 + 取圖，不能入庫）
RW_TOKEN = os.environ.get("POSEPLANNER_TOKEN", "").strip()
RO_TOKEN = os.environ.get("POSEPLANNER_READ_TOKEN", "").strip()
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tiff"}

app = FastAPI(title="PosePlanner 私有 DB", version="1.0")

# ── CORS ────────────────────────────────────────────────────────────
# 前端搜尋頁（web/）多半跟 server 不同源，瀏覽器的 fetch(/search)、fetch(/stats)
# 需要 server 回 CORS 標頭才讀得到。預設放行所有來源（讀取仍受 token 把關）；
# 要收斂時設 POSEPLANNER_CORS_ORIGINS=「逗號分隔的來源清單」，例如
#   POSEPLANNER_CORS_ORIGINS=https://pose.example.com,http://192.168.1.50:8080
_cors_env = os.environ.get("POSEPLANNER_CORS_ORIGINS", "*").strip()
_cors_origins = ["*"] if _cors_env in ("", "*") else [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET"],          # 前端只讀（/search /stats /thumbs /images）
    allow_headers=["*"],
)

# ── 速率限制（防有人寫腳本快速列舉整庫）─────────────────────────────
# 只掛在「列舉成本高」的 /search、/stats 上；/thumbs、/images 不限速以免拖慢正常瀏覽。
# 反向代理後面要拿到真實 IP：優先讀 X-Forwarded-For 第一段（單一可信代理時夠用；
# 若代理不可信，這個 header 可被偽造繞過——那種情境請改在 nginx 層限速）。
# 預設 60/分鐘；設環境變數 POSEPLANNER_SEARCH_RATE 調整，設 "10000/minute" 之類即等同關閉。
SEARCH_RATE = os.environ.get("POSEPLANNER_SEARCH_RATE", "60/minute").strip() or "60/minute"


def _client_key(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else get_remote_address(request)


limiter = Limiter(key_func=_client_key)
app.state.limiter = limiter


def _ratelimit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "請求太頻繁，請稍後再試"})


app.add_exception_handler(RateLimitExceeded, _ratelimit_handler)


@app.on_event("startup")
def _startup() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    db.wait_and_init()


# ── 認證 ────────────────────────────────────────────────────────────
# token 可走 Authorization: Bearer <token> 標頭，或 ?t=<token> query 參數
# （後者讓對話裡的 <img> 縮圖網址也能帶讀取權限渲染）。
def _provided_token(authorization: str | None, t: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return (t or "").strip()


def require_write(authorization: str | None = Header(default=None), t: str | None = None) -> None:
    """寫入端點：未設讀寫 token → 純內網信任不檢查；否則必須帶讀寫 token。"""
    if not RW_TOKEN:
        return
    if _provided_token(authorization, t) != RW_TOKEN:
        raise HTTPException(status_code=401, detail="需要『讀寫』token")


def require_read(authorization: str | None = Header(default=None), t: str | None = None) -> None:
    """讀取端點：兩個 token 都沒設 → 開放；否則讀寫或唯讀 token 皆可。"""
    if not RW_TOKEN and not RO_TOKEN:
        return
    tok = _provided_token(authorization, t)
    if tok and tok in {x for x in (RW_TOKEN, RO_TOKEN) if x}:
        return
    raise HTTPException(status_code=401, detail="需要『讀取』token（讀寫或唯讀皆可）")


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
@limiter.limit(SEARCH_RATE)
def stats(request: Request, _: None = Depends(require_read)) -> dict:
    with db.pool().connection() as conn:
        n_poses = conn.execute("SELECT COUNT(*) FROM poses").fetchone()[0]
        n_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        n_prop = conn.execute("SELECT COUNT(*) FROM tags WHERE status='proposed'").fetchone()[0]
        n_creators = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
    return {"poses": n_poses, "tags": n_tags, "proposed_tags": n_prop, "creators": n_creators}


@app.post("/images")
async def upload_image(
    file: UploadFile = File(...),
    description: str = Form(...),
    tags: str = Form(...),                       # JSON 陣列：[{"category","name"}, ...]
    creators: str | None = Form(default=None),   # 選配 JSON 陣列：[{"name","role","handle?","url?"}, ...]
    source: str | None = Form(default=None),
    rating: int | None = Form(default=None),
    favorite: bool = Form(default=False),
    embedding: str | None = Form(default=None),  # 選配 JSON 陣列（384 維）
    embedding_model: str | None = Form(default=None),
    _: None = Depends(require_write),
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

    creator_list: list = []
    if creators:
        try:
            creator_list = json.loads(creators)
            assert isinstance(creator_list, list)
        except Exception:
            raise HTTPException(status_code=400, detail="creators 必須是 JSON 陣列")

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
        "creators": creator_list,
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
    _: None = Depends(require_write),
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
                "creators": e.get("creators") or [],
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


@app.put("/poses/{pose_id}/tags")
async def update_pose_tags(
    pose_id: int,
    body: dict = Body(...),
    _: None = Depends(require_write),
) -> JSONResponse:
    """改一張 pose 的 tags。body 為 JSON 物件，三種欄位（皆為 [{category,name}, ...]）：

      {"tags":  [...]}             整批替換成這份清單
      {"add":   [...]}             只新增（已有的略過）
      {"remove":[...]}             只移除
      {"tags": [...], "add":[...], "remove":[...]}   可混用：先替換再微調

    新標籤沿用入庫規則：既有維度→active，未知維度→proposed。
    回傳 {added, removed, tags}（tags 為更新後完整清單）。
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必須是 JSON 物件")
    replace = body.get("tags")
    add = body.get("add")
    remove = body.get("remove")
    for key, val in (("tags", replace), ("add", add), ("remove", remove)):
        if val is not None and not isinstance(val, list):
            raise HTTPException(status_code=400, detail=f"{key} 必須是 JSON 陣列")
    if replace is None and add is None and remove is None:
        raise HTTPException(status_code=400, detail="需至少給 tags / add / remove 其一")

    dims = db.known_categories()
    with db.pool().connection() as conn:
        result = db.update_pose_tags(
            conn, pose_id, dims, replace=replace, add=add, remove=remove
        )
    if result is None:
        raise HTTPException(status_code=404, detail="找不到這張 pose")
    return JSONResponse({"id": pose_id, **result})


@app.put("/poses/{pose_id}/creators")
async def update_pose_creators(
    pose_id: int,
    body: dict = Body(...),
    _: None = Depends(require_write),
) -> JSONResponse:
    """改一張 pose 的創作者。body 為 JSON 物件，三種欄位（皆為
    [{name, role, handle?, url?}, ...]，role 省略時預設 'creator'）：

      {"creators": [...]}          整批替換成這份清單
      {"add":      [...]}          只新增（已有的 (creator, role) 略過）
      {"remove":   [...]}          只移除
      {"creators": [...], "add":[...], "remove":[...]}   可混用：先替換再微調

    回傳 {added, removed, creators}（creators 為更新後完整清單）。
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必須是 JSON 物件")
    replace = body.get("creators")
    add = body.get("add")
    remove = body.get("remove")
    for key, val in (("creators", replace), ("add", add), ("remove", remove)):
        if val is not None and not isinstance(val, list):
            raise HTTPException(status_code=400, detail=f"{key} 必須是 JSON 陣列")
    if replace is None and add is None and remove is None:
        raise HTTPException(status_code=400, detail="需至少給 creators / add / remove 其一")

    with db.pool().connection() as conn:
        result = db.update_pose_creators(conn, pose_id, replace=replace, add=add, remove=remove)
    if result is None:
        raise HTTPException(status_code=404, detail="找不到這張 pose")
    return JSONResponse({"id": pose_id, **result})


@app.get("/search")
@limiter.limit(SEARCH_RATE)
def search(request: Request, q: str = "", tag: list[str] | None = None, limit: int = 20, offset: int = 0,
          _: None = Depends(require_read)) -> JSONResponse:
    """tag（category=name，可多個 AND）+ 關鍵字（對 description 做 AND LIKE）粗篩候選。
    語意排序由 Claude 讀 description 完成——和 SQLite 版 search.py 同設計。
    offset 供前端 feed 無限捲動分頁用（從第 offset 筆之後再取 limit 筆）。"""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
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
    sql += (" ORDER BY p.favorite DESC, p.rating IS NULL, p.rating DESC, p.created_at DESC "
            "LIMIT %s OFFSET %s")
    params.append(limit)
    params.append(offset)

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
                "creators": db.pose_creators(conn, pid),
            })
    return JSONResponse(out)


@app.get("/thumbs/{name}")
def get_thumb(name: str, _: None = Depends(require_read)):
    path = (THUMBS_DIR / name).resolve()
    if THUMBS_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="找不到縮圖")
    return FileResponse(path)


@app.get("/images/{name}")
def get_image(name: str, _: None = Depends(require_write)):
    # 原圖只給『讀寫』token（後台/入庫工具）。公開找圖頁只吃 /thumbs，
    # 拿不到高清原圖——這是「別人能瀏覽、搬不走原圖」的主要防線。
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
  <label>創作者 creators（JSON 陣列，選填）</label>
  <textarea name="creators" rows="2">[{"name":"模特兒名","role":"model"},{"name":"攝影師名","role":"photographer"}]</textarea>
  <label>來源 source（選填）</label><input name="source">
  <label>Bearer token（若 server 有設）</label><input name="_token_hint" disabled
    placeholder="網頁表單不帶 token；有設 token 時請用 backend.py 上傳">
  <button type="submit">上傳入庫</button>
</form></body></html>"""
