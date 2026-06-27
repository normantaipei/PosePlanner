"""PosePlanner 自架 server — 圖片 / fragment 接收服務（FastAPI）。

這是「私有 DB」模式的後端：跟 Google Drive 模式二選一。和純雲端不同，
私有 server **可以收原圖**並存在你網域內的硬碟 + PostgreSQL，不受 Drive
「只能新建、單筆大小上限」的限制。

端點
────
  GET  /health                      健康檢查（免 token）
  GET  /stats                       庫狀態（poses / tags 數）
  POST /images     (multipart)      收「一張原圖 + metadata」直接入庫（去重、產縮圖）
  POST /fragments  (multipart)      收一個 pack 出來的 fragment zip，回放併庫（相容雲端格式）
  PUT  /poses/{id}/tags  (json)     改一張 pose 的 tags（整批替換 / 只新增 / 只移除）
  PUT  /poses/{id}/creators (json)  改一張 pose 的創作者（模特兒 / 攝影師…；整批替換 / 增 / 移除）
  GET  /poses/{id}                  取一張 pose 摘要（刪除前 dry-run 確認用）
  DELETE /poses/{id}                刪一張 pose（連帶清 tags/creators + 磁碟原圖縮圖；需讀寫 token）
  GET  /search?q=&tag=&limit=       tag + 關鍵字粗篩候選（語意排序交給 Claude）
  GET  /thumbs/{name}               取縮圖（讀取 token 即可，公開找圖頁用這個）
  GET  /images/{name}               取『原圖』——需讀寫 token（防整庫高清被搬走）
  GET  /skill                       下載打包好的 skill zip（**僅限區網**，免 token；連線設定即時烤入）

讀取端點（/stats /search /thumbs）需『讀取』token（讀寫或唯讀皆可），可對外網開放。
原圖 /images/{name} 需『讀寫』token。所有「寫入 DB」的端點（POST /images、
POST /fragments、PUT /poses/{id}/tags、PUT /poses/{id}/creators、DELETE /poses/{id}）
除了讀寫 token，還**僅限區網**（require_write_lan，看真實 TCP 對端 IP）——對外網只開放
web 搜尋。token 未設定則不檢查，僅建議在純內網時這樣。/search、/stats 另有速率限制。
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import zipfile
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

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

# repo 根（含 SKILL.md / scripts / vendor），給 /skill 即時打包用。
# 容器內由 compose 以 ../:/repo:ro 掛進來（POSEPLANNER_REPO=/repo）；本機跑時退回
# app.py 上溯的 repo 根。注意：別把 fallback 寫成 dict.get 的預設值——那會被「立即求值」，
# 在容器裡 app.py 位於 /app（parents 只有 /app、/），parents[2] 會 IndexError 而炸掉啟動。
def _default_repo_dir() -> Path:
    parents = Path(__file__).resolve().parents
    return parents[2] if len(parents) > 2 else Path("/repo")


REPO_DIR = Path(os.environ["POSEPLANNER_REPO"]) if os.environ.get("POSEPLANNER_REPO") else _default_repo_dir()
# 跟 scripts/build_skill_zip.sh 同一份清單；改一邊要同步另一邊。
SKILL_FILES = [
    "SKILL.md", "taxonomy.yaml", "requirements.txt",
    "scripts/backend.py", "scripts/add_pose.py", "scripts/fetch_post.py",
    "scripts/search.py", "scripts/vecdb.py", "scripts/schema.sql", "scripts/probe_net.py",
]
SKILL_DIRS = ["vendor/sqlite-vec"]

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


# ── 不合法請求一律回 400 ────────────────────────────────────────────
# FastAPI 預設對缺欄位／型別錯誤回 422；統一改成 400「不合法的請求」，
# 不回傳 pydantic 的詳細欄位結構（避免把內部 schema 形狀洩漏給呼叫端）。
def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "不合法的請求"})


app.add_exception_handler(RequestValidationError, _validation_handler)


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


# ── 區網限定（給 /skill 下載用）──────────────────────────────────────
# 額外放行的來源網段（VPN 子網之類），逗號分隔 CIDR；預設只放私有網段 + loopback。
_skill_extra_cidrs = []
for _c in os.environ.get("POSEPLANNER_SKILL_ALLOW_CIDRS", "").split(","):
    _c = _c.strip()
    if _c:
        try:
            _skill_extra_cidrs.append(ipaddress.ip_network(_c, strict=False))
        except ValueError:
            pass


def _peer_ip(request: Request):
    """取真實 TCP 對端 IP（不信任可偽造的 X-Forwarded-For）。IPv4-mapped 還原成 IPv4。"""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def require_lan(request: Request) -> None:
    """只放行區網（私有網段 / loopback / link-local）+ 自訂 CIDR 的來源，其餘 403。"""
    ip = _peer_ip(request)
    if ip is None:
        raise HTTPException(status_code=403, detail="無法判定來源位址")
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return
    if any(ip in net for net in _skill_extra_cidrs):
        return
    raise HTTPException(status_code=403, detail="這個端點僅限區網內存取")


def require_write_lan(
    request: Request,
    authorization: str | None = Header(default=None),
    t: str | None = None,
) -> None:
    """所有『寫入 DB』端點：必須來自區網（真實 TCP 對端 IP）**且**帶讀寫 token。

    對外只開放 web 搜尋（/search /stats /thumbs），入庫 / 改 tag / 刪除一律限內網。
    先擋來源網段再驗 token：外網即使猜中 token 也會在 require_lan 先被 403。
    注意：若本服務被反向代理在『同一台』後面，對端 IP 會變成 proxy 的私有/loopback
    位址而誤判為區網——務必別把寫入端點經由那個 proxy 對外轉發（見 server/README）。
    """
    require_lan(request)
    require_write(authorization, t)


# ── 縮圖 ────────────────────────────────────────────────────────────
def make_thumbnail(img_bytes: bytes, dst: Path) -> tuple[int, int] | None:
    """產縮圖，回傳縮圖 (寬, 高)；失敗回 None。尺寸給前端預留長寬比、避免捲動跳版。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(img_bytes)) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            im.save(dst, "JPEG", quality=85)
            return im.width, im.height
    except Exception:
        return None


def _img_dims(img_bytes: bytes) -> tuple[int, int] | None:
    """只讀圖檔長寬（不重存），給 fragment 回放時補既有縮圖尺寸。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(img_bytes)) as im:
            return im.width, im.height
    except Exception:
        return None


# ── 端點 ────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "poseplanner-private-db"}


@app.get("/skill")
def download_skill(
    request: Request,
    token: str = "rw",                       # rw（讀寫，預設）/ ro（唯讀）/ none（不烤 token）
    _: None = Depends(require_lan),          # ← 僅限區網
) -> Response:
    """即時打包並下載 skill zip（**限區網**）。

    會把連線設定即時烤進 zip 裡的 `data/config.json`，下載解開即為「私有 DB 已連線」狀態：
      base_url = 你存取本服務所用的網址（從這個請求推得）；token 由 ?token= 決定。
    """
    missing = [f for f in SKILL_FILES if not (REPO_DIR / f).is_file()]
    missing += [d for d in SKILL_DIRS if not (REPO_DIR / d).is_dir()]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"server 端找不到 repo 檔（需把 repo 掛進 {REPO_DIR}）：{', '.join(missing)}",
        )

    tok = {"rw": RW_TOKEN, "ro": RO_TOKEN, "none": ""}.get(token)
    if tok is None:
        raise HTTPException(status_code=400, detail="token 只能是 rw / ro / none")
    config = {
        "backend": "selfhost",
        "selfhost": {"base_url": str(request.base_url).rstrip("/"), "token": tok},
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data/config.json", json.dumps(config, ensure_ascii=False, indent=2))
        for rel in SKILL_FILES:
            z.write(REPO_DIR / rel, rel)
        for d in SKILL_DIRS:
            for f in sorted((REPO_DIR / d).rglob("*")):
                if f.is_file() and f.name != ".DS_Store":
                    z.write(f, f.relative_to(REPO_DIR).as_posix())
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="poseplanner-skill.zip"'},
    )


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
    _: None = Depends(require_write_lan),
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

    # 縮圖（一併取得縮圖長寬，給前端預留版面）
    thumb_dst = THUMBS_DIR / f"{content_hash}.jpg"
    thumb_rel = thumb_w = thumb_h = None
    tdims = make_thumbnail(raw, thumb_dst)
    if tdims:
        thumb_rel = f"thumbs/{content_hash}.jpg"
        thumb_w, thumb_h = tdims

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
        "thumb_w": thumb_w,
        "thumb_h": thumb_h,
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
    _: None = Depends(require_write_lan),
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

    # tombstone fragment（drive 模式的刪除單位）：manifest 是帶 op 的物件，回放即「刪除」。
    if isinstance(entries, dict) and entries.get("poseplanner_op") == "delete":
        hashes = [h.strip() for h in (entries.get("content_hashes") or []) if str(h).strip()]
        removed = 0
        with db.pool().connection() as conn:
            for h in hashes:
                info = db.delete_pose_by_hash(conn, h)
                if info is None:
                    continue
                removed += 1
                for rel in (info.get("image_path"), info.get("thumbnail_path")):
                    if rel:
                        try:
                            (DATA_DIR / rel).unlink(missing_ok=True)
                        except OSError:
                            pass
        return JSONResponse({"tombstone": True, "removed": removed, "requested": len(hashes)})

    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="manifest.json 應是陣列")

    dims = db.known_categories()
    added = dup = errors = 0
    with db.pool().connection() as conn:
        for e in entries:
            ch = (e.get("content_hash") or "").strip()
            # 落地縮圖
            thumb_rel = None
            thumb_w, thumb_h = e.get("thumb_w"), e.get("thumb_h")
            if e.get("thumbnail") and f"thumbs/{e['thumbnail']}" in names:
                tbytes = zf.read(f"thumbs/{e['thumbnail']}")
                thumb_dst = THUMBS_DIR / f"{ch}.jpg"
                if not thumb_dst.exists():
                    thumb_dst.write_bytes(tbytes)
                thumb_rel = f"thumbs/{ch}.jpg"
                # 舊 fragment 的 manifest 沒帶尺寸時，現場讀縮圖補上。
                if not (thumb_w and thumb_h):
                    d = _img_dims(tbytes)
                    if d:
                        thumb_w, thumb_h = d
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
                "thumb_w": thumb_w,
                "thumb_h": thumb_h,
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
    _: None = Depends(require_write_lan),
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
    _: None = Depends(require_write_lan),
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


@app.get("/poses/{pose_id}")
def get_pose(pose_id: int, _: None = Depends(require_read)) -> JSONResponse:
    """取一張 pose 的摘要（給刪除前的 dry-run 確認用）。"""
    with db.pool().connection() as conn:
        pose = db.get_pose(conn, pose_id)
    if pose is None:
        raise HTTPException(status_code=404, detail="找不到這張 pose")
    return JSONResponse(pose)


@app.delete("/poses/{pose_id}")
def delete_pose(pose_id: int, _: None = Depends(require_write_lan)) -> JSONResponse:
    """刪一張 pose（需讀寫 token）。連帶清 pose_tags / pose_creators（FK CASCADE）、
    補扣 tag usage_count，並把磁碟上的原圖 + 縮圖一併刪掉（檔名以 content_hash 命名、
    一圖一檔，所以安全）。回傳 {id, content_hash, deleted: true}。"""
    with db.pool().connection() as conn:
        info = db.delete_pose(conn, pose_id)
    if info is None:
        raise HTTPException(status_code=404, detail="找不到這張 pose")
    # 清磁碟檔（best-effort，刪不掉不影響 DB 已刪的事實）。
    for rel in (info.get("image_path"), info.get("thumbnail_path")):
        if not rel:
            continue
        try:
            (DATA_DIR / rel).unlink(missing_ok=True)
        except OSError:
            pass
    return JSONResponse({"id": info["id"], "content_hash": info["content_hash"], "deleted": True})


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
    # 自由文字：每個關鍵字需命中 description／tag 名稱(或分類)／創作者名(或 handle)其一，
    # 關鍵字之間維持 AND。（前端搜尋框 placeholder 承諾可搜「作品、創作者」，故不只查 description）
    for kw in q.split():
        like = f"%{kw}%"
        clauses.append(
            "(p.description ILIKE %s "
            "OR p.id IN (SELECT pt.pose_id FROM pose_tags pt JOIN tags t ON t.id=pt.tag_id "
            "WHERE t.name ILIKE %s OR t.category ILIKE %s) "
            "OR p.id IN (SELECT pc.pose_id FROM pose_creators pc JOIN creators c ON c.id=pc.creator_id "
            "WHERE c.name ILIKE %s OR c.handle ILIKE %s))"
        )
        params += [like, like, like, like, like]

    sql = ("SELECT p.id, p.image_path, p.thumbnail_path, p.thumb_w, p.thumb_h, "
           "p.description, p.favorite, p.rating FROM poses p")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += (" ORDER BY p.favorite DESC, p.rating IS NULL, p.rating DESC, p.created_at DESC "
            "LIMIT %s OFFSET %s")
    params.append(limit)
    params.append(offset)

    out = []
    with db.pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        for pid, image_path, thumb, thumb_w, thumb_h, desc, fav, rating in rows:
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
                "thumb_w": thumb_w,
                "thumb_h": thumb_h,
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


