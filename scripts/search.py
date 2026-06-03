#!/usr/bin/env python3
"""PosePlanner — 檢索（Phase 3）

兩種模式：

1) **Claude 驅動（預設）**：做結構化檢索——tag 篩選 + 敘述關鍵字 + 上限——
   把候選 poses（含 description / tags / 圖路徑）吐出來，由 **Claude 在對話裡讀敘述
   做語意挑選**。不需要任何向量或本地模型，符合「Claude 當語意引擎」的設計。

     python3 scripts/search.py "想要回眸的帥氣站姿"
     python3 scripts/search.py --tag ip=鬼滅之刃 --tag framing=半身 --limit 30
     python3 scripts/search.py "逆光 慵懶" --tag people_count=單人 --json
     python3 scripts/search.py --tag framing=全身 --table   # ← Markdown 表（含縮圖），直接貼進對話

   QUERY 會以空白拆成多個關鍵字，對 description 做 AND 的 LIKE 模糊比對（先粗篩，
   真正的語意排序交給 Claude）。給了 --json 就同時印出機器可讀的結果；給了 --table
   會印一張 Markdown 表格（縮圖 / 描述 / 標籤 / 訊號），貼進對話串就會渲染成圖表。

2) **向量 KNN（選配，--knn）**：若 pose_vectors（sqlite-vec 的 vec0 表）裡有向量，
   用 fastembed 把 QUERY 算成向量做真正的最近鄰搜尋。需要 `pip install fastembed`，
   且庫裡得先有向量（入庫時加 --embed，或 manifest 帶 embedding）。

     python3 scripts/search.py "持刀回眸" --knn --limit 10

路徑旗標 --db / --data 與 add_pose.py 相同（雲端持久化時指到沙箱副本）。
"""
from __future__ import annotations

import argparse
import json
import sys

import add_pose  # 共用 configure / connect / 向量設定（同目錄）
import vecdb


def _tag_filter_ids(conn, tag_pairs: list[tuple[str, str]]) -> set[int] | None:
    """回傳同時帶有所有指定 (category, name) 的 pose id 集合；沒給篩選回 None（不限）。"""
    if not tag_pairs:
        return None
    where = " OR ".join(["(t.category=? AND t.name=?)"] * len(tag_pairs))
    params: list[str] = [x for pair in tag_pairs for x in pair]
    rows = conn.execute(
        f"""SELECT p.id FROM poses p
            JOIN pose_tags pt ON pt.pose_id=p.id
            JOIN tags t       ON t.id=pt.tag_id
            WHERE {where}
            GROUP BY p.id
            HAVING COUNT(DISTINCT t.category || ':' || t.name) = ?""",
        (*params, len(tag_pairs)),
    ).fetchall()
    return {r[0] for r in rows}


def _tags_of(conn, pose_id: int) -> list[str]:
    return [
        f"{cat}:{name}"
        for cat, name in conn.execute(
            "SELECT t.category, t.name FROM pose_tags pt "
            "JOIN tags t ON t.id=pt.tag_id WHERE pt.pose_id=? ORDER BY t.category",
            (pose_id,),
        )
    ]


def _creators_of(conn, pose_id: int) -> list[dict]:
    return [
        {"name": name, "role": role, "handle": handle, "url": url}
        for name, role, handle, url in conn.execute(
            "SELECT c.name, pc.role, c.handle, c.url FROM pose_creators pc "
            "JOIN creators c ON c.id=pc.creator_id WHERE pc.pose_id=? ORDER BY pc.role, c.name",
            (pose_id,),
        )
    ]


def _fmt_creators(creators: list[dict]) -> str:
    """把創作者清單排成易讀字串，如 'model:小詩、photographer:阿攝'。"""
    return "、".join(f"{c['role']}:{c['name']}" for c in creators)


def _row_dict(conn, row) -> dict:
    pid, image_path, thumb, desc, fav, rating = row
    return {
        "id": pid,
        "image_path": image_path,
        "thumbnail_path": thumb,
        "description": desc,
        "favorite": bool(fav),
        "rating": rating,
        "tags": _tags_of(conn, pid),
        "creators": _creators_of(conn, pid),
    }


def fetch_by_ids(conn, ids: list[int]) -> list[dict]:
    """依指定 id 取回 poses，**保留傳入的順序**（給語意挑選後重排呈現用）。"""
    out: list[dict] = []
    for pid in ids:
        row = conn.execute(
            "SELECT id, image_path, thumbnail_path, description, favorite, rating "
            "FROM poses WHERE id=?", (pid,),
        ).fetchone()
        if row:
            out.append(_row_dict(conn, row))
    return out


def search_claude(conn, query: str, tag_pairs, limit: int) -> list[dict]:
    """粗篩候選（tag + 關鍵字 LIKE），交給 Claude 做語意排序。"""
    ids = _tag_filter_ids(conn, tag_pairs)

    sql = ("SELECT id, image_path, thumbnail_path, description, favorite, rating "
           "FROM poses")
    clauses, params = [], []
    if ids is not None:
        if not ids:
            return []
        clauses.append(f"id IN ({','.join('?' * len(ids))})")
        params += list(ids)
    for kw in query.split():
        clauses.append("description LIKE ?")
        params.append(f"%{kw}%")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY favorite DESC, rating IS NULL, rating DESC, created_at DESC LIMIT ?"
    params.append(limit)

    return [_row_dict(conn, r) for r in conn.execute(sql, params)]


def search_knn(conn, query: str, tag_pairs, limit: int) -> list[dict]:
    """真正的向量最近鄰：fastembed 算 QUERY 向量 → vec0 MATCH。"""
    n_vec = conn.execute("SELECT COUNT(*) FROM pose_vectors").fetchone()[0]
    if n_vec == 0:
        raise SystemExit(
            "庫裡還沒有任何向量，--knn 無從比對。\n"
            "請先在入庫時加 --embed（需 fastembed），或改用預設的 Claude 驅動檢索。"
        )
    emb = add_pose.embed_text(query)
    if not emb:
        raise SystemExit("無法算 QUERY 向量（fastembed 未安裝？）。改用預設檢索或 pip install fastembed。")
    _model, blob = emb

    # vec0 的 KNN 要 k 限制；tag 篩選在取回後做交集
    ids = _tag_filter_ids(conn, tag_pairs)
    k = limit if ids is None else max(limit, 50)  # 有 tag 篩選時多撈一點再交集
    knn = conn.execute(
        "SELECT pose_id, distance FROM pose_vectors "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (blob, k),
    ).fetchall()

    out: list[dict] = []
    for pid, dist in knn:
        if ids is not None and pid not in ids:
            continue
        row = conn.execute(
            "SELECT id, image_path, thumbnail_path, description, favorite, rating "
            "FROM poses WHERE id=?", (pid,),
        ).fetchone()
        if row:
            d = _row_dict(conn, row)
            d["distance"] = round(dist, 4)
            out.append(d)
        if len(out) >= limit:
            break
    return out


def _print_human(results: list[dict], mode: str) -> None:
    if not results:
        print("（沒有符合的 pose）")
        return
    print(f"找到 {len(results)} 筆候選（{mode}）：\n")
    for r in results:
        head = f"#{r['id']}"
        if "distance" in r:
            head += f"  dist={r['distance']}"
        if r["favorite"]:
            head += "  ★"
        if r["rating"]:
            head += f"  {r['rating']}/5"
        print(head)
        print(f"  {r['description']}")
        print(f"  tags: {'、'.join(r['tags']) or '（無）'}")
        if r.get("creators"):
            print(f"  創作者: {_fmt_creators(r['creators'])}")
        print(f"  img : {r['image_path']}")
        print()
    if mode == "claude":
        print("→ 以上是粗篩候選；請依使用者的描述語意挑出最貼近的幾張。")


def _md_cell(text: str) -> str:
    """把任意文字塞進 Markdown 表格的一格：跳脫 |、把換行壓成空白。"""
    return (text or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _abs_path(rel: str | None) -> str | None:
    """把相對 data/ 的路徑轉成絕對路徑（讓對話裡的 Markdown 圖片/連結能解析）。"""
    if not rel:
        return None
    return str((add_pose.DATA_DIR / rel).resolve())


def _print_table(results: list[dict], mode: str) -> None:
    """印一張 Markdown 表格，直接貼進對話串就會渲染成表（含縮圖）。"""
    if not results:
        print("（沒有符合的 pose）")
        return
    print(f"找到 **{len(results)}** 張相關圖片（{mode}）：\n")
    print("| 縮圖 | # | 描述 | 標籤 | 創作者 | 訊號 |")
    print("|---|---|---|---|---|---|")
    missing = 0
    for r in results:
        thumb = _abs_path(r.get("thumbnail_path"))
        thumb_cell = f"![#{r['id']}]({thumb})" if thumb else "—"
        if not thumb:
            missing += 1

        signals = []
        if "distance" in r:
            signals.append(f"dist={r['distance']}")
        if r["favorite"]:
            signals.append("★")
        if r["rating"]:
            signals.append(f"{r['rating']}/5")

        # tags 形如 "framing:全身"，表格裡只留值、用、串接，較好讀
        tag_vals = [t.split(":", 1)[-1] for t in r["tags"]]
        creators_cell = _md_cell(_fmt_creators(r.get("creators", []))) or "—"
        print(
            f"| {thumb_cell} | {r['id']} | {_md_cell(r['description'])} "
            f"| {_md_cell('、'.join(tag_vals)) or '—'} | {creators_cell} "
            f"| {_md_cell(' '.join(signals)) or '—'} |"
        )
    print()
    if missing:
        print(f"> ⚠ 有 {missing} 張缺縮圖（入庫時可能沒裝 Pillow）；該列圖欄顯示 —。")
    if mode == "claude":
        print("> 以上是粗篩候選；已依語意排序，挑最貼近你描述的幾張即可。")


def cmd_search(args) -> None:
    tag_pairs: list[tuple[str, str]] = []
    for kv in args.tag or []:
        if "=" not in kv:
            raise SystemExit(f"--tag 格式要 category=name，收到：{kv}")
        cat, name = kv.split("=", 1)
        tag_pairs.append((cat.strip(), name.strip()))

    query = (args.query or "").strip()
    if args.knn and not query:
        raise SystemExit("--knn 需要一段 QUERY 文字來算向量。")

    if not add_pose.DB_PATH.exists():
        raise SystemExit("尚未建庫，先用 add_pose.py 入庫。")

    ids: list[int] = []
    for chunk in args.ids or []:
        for tok in chunk.replace("，", ",").split(","):
            tok = tok.strip().lstrip("#")
            if tok:
                if not tok.isdigit():
                    raise SystemExit(f"--ids 只能是數字 id（逗號分隔），收到：{tok}")
                ids.append(int(tok))

    conn = add_pose.connect()
    try:
        if ids:
            # 語意挑選後重新呈現：只取這些 id，保留你給的順序
            results = fetch_by_ids(conn, ids)
            mode = "selected"
        elif args.knn:
            results = search_knn(conn, query, tag_pairs, args.limit)
            mode = "knn"
        else:
            results = search_claude(conn, query, tag_pairs, args.limit)
            mode = "claude"
    finally:
        conn.close()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.table:
        _print_table(results, mode)
    else:
        _print_human(results, mode)


def main() -> None:
    vecdb.ensure_capable_interpreter()
    p = argparse.ArgumentParser(description="PosePlanner 檢索")
    p.add_argument("query", nargs="?", default="", help="自然語言描述 / 關鍵字（可省略，純靠 --tag）")
    p.add_argument("--tag", action="append", help="category=name 篩選，可重複（AND）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--knn", action="store_true", help="走 sqlite-vec 向量最近鄰（需庫裡有向量）")
    p.add_argument("--json", action="store_true", help="同時輸出機器可讀 JSON")
    p.add_argument("--table", action="store_true",
                   help="輸出 Markdown 表格（含縮圖），可直接貼進對話串渲染")
    p.add_argument("--ids", action="append",
                   help="只呈現這些 pose id（逗號分隔，可重複），保留順序；"
                        "語意挑選後用它把選中的圖連縮圖一起印出來")
    p.add_argument("--db", default=None)
    p.add_argument("--data", default=None)
    args = p.parse_args()
    add_pose.configure(args.db, args.data)
    cmd_search(args)


if __name__ == "__main__":
    sys.exit(main())
