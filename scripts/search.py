#!/usr/bin/env python3
"""PosePlanner — 檢索（Phase 3）

兩種模式：

1) **Claude 驅動（預設）**：做結構化檢索——tag 篩選 + 敘述關鍵字 + 上限——
   把候選 poses（含 description / tags / 圖路徑）吐出來，由 **Claude 在對話裡讀敘述
   做語意挑選**。不需要任何向量或本地模型，符合「Claude 當語意引擎」的設計。

     python3 scripts/search.py "想要回眸的帥氣站姿"
     python3 scripts/search.py --tag ip=鬼滅之刃 --tag framing=半身 --limit 30
     python3 scripts/search.py "逆光 慵懶" --tag people_count=單人 --json

   QUERY 會以空白拆成多個關鍵字，對 description 做 AND 的 LIKE 模糊比對（先粗篩，
   真正的語意排序交給 Claude）。給了 --json 就同時印出機器可讀的結果。

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
    }


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
        print(f"  img : {r['image_path']}")
        print()
    if mode == "claude":
        print("→ 以上是粗篩候選；請依使用者的描述語意挑出最貼近的幾張。")


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

    conn = add_pose.connect()
    try:
        if args.knn:
            results = search_knn(conn, query, tag_pairs, args.limit)
            mode = "knn"
        else:
            results = search_claude(conn, query, tag_pairs, args.limit)
            mode = "claude"
    finally:
        conn.close()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
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
    p.add_argument("--db", default=None)
    p.add_argument("--data", default=None)
    args = p.parse_args()
    add_pose.configure(args.db, args.data)
    cmd_search(args)


if __name__ == "__main__":
    sys.exit(main())
