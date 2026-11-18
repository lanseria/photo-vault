"""人脸聚类：把 faces 归到匿名人物（Person A/B/C），用户再在网页上手动改名。

    uv run --group ml python -m ml.cluster
    uv run --group ml python -m ml.cluster --eps 0.45 --min-faces 5 --dry-run

两步，增量安全：

    ① 认领：未归类的人脸先跟"已有人物"的所有脸比最近邻，相似度 >= --match
       就直接归给该人物。每天新导入的照片因此不用把全库重算一遍。
    ② 聚类：剩下的人脸之间建 eps 邻域图（余弦距离 <= eps 连边）+ 并查集取连通分量，
       只保留成员数 >= --min-faces 的簇。一两张脸的小簇留作"未归类"——宁可少建人物，
       也不要让 UI 里冒出几千个只有一张照片的人。
    ③ 命名：新簇按大小降序，依次取下一个没用过的 Person 字母（A..Z, AA..）；
       封面取簇内人脸面积占比最大的那张。

阈值取保守值（sim≈0.55）：拆散的人物还能手动合并，错并的两个人却很难拆开。
"""
from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row

from .pgvector import from_vector, to_vector

DSN_DEFAULT = "postgresql://vault:vault@localhost:5433/vault"

CANDIDATES_SQL = """
select f.id, f.asset_id, f.det_score, f.quality, f.created_at,
       f.embedding::text as emb
from faces f
join assets a on a.id = f.asset_id
where f.person_id is null and not a.is_deleted and not f.ignored
  and f.det_score >= %s and coalesce(f.quality, 0) >= %s
order by f.created_at, f.id
"""

MATCH_SQL = """
select f.person_id, 1 - (f.embedding <=> %s::vector) as sim
from faces f
where f.person_id is not null and not f.ignored
order by f.embedding <=> %s::vector
limit 1
"""

NEIGHBORS_SQL = """
select f.id, 1 - (f.embedding <=> %s::vector) as sim
from faces f
where f.person_id is null and not f.ignored
  and f.id <> %s::uuid
  and f.det_score >= %s and coalesce(f.quality, 0) >= %s
order by f.embedding <=> %s::vector
limit %s
"""

COVER_SQL = """
update persons p
   set cover_face_id = (
         select f.id from faces f
          where f.person_id = p.id and not f.ignored
          order by coalesce(f.quality, 0) desc, f.det_score desc
          limit 1)
 where p.cover_face_id is null
    or not exists (select 1 from faces c where c.id = p.cover_face_id
                   and c.person_id = p.id)
    or exists (select 1 from faces c where c.id = p.cover_face_id and c.ignored)
"""


# ---------------------------------------------------------------- 纯函数（可单测）

class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:      # 路径压缩
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def components(self) -> list[list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            groups[self.find(item)].append(item)
        return list(groups.values())


def person_label(index: int) -> str:
    """0 → Person A，25 → Person Z，26 → Person AA。"""
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"Person {letters}"


def next_labels(existing_names: list[str], count: int) -> list[str]:
    """按顺序取 count 个没被占用的 Person 字母。"""
    used = {n.lower() for n in existing_names if n}
    labels: list[str] = []
    index = 0
    while len(labels) < count:
        label = person_label(index)
        if label.lower() not in used:
            labels.append(label)
        index += 1
    return labels


# ---------------------------------------------------------------- 数据库

def load_candidates(conn, min_score: float, min_quality: float) -> list[dict]:
    rows = conn.execute(CANDIDATES_SQL, (min_score, min_quality)).fetchall()
    for row in rows:
        row["vec"] = from_vector(row["emb"])
        del row["emb"]
    return rows


def assign_existing(conn, candidates: list[dict], match: float,
                    progress_every: int = 500) -> dict[str, int]:
    """① 把明显属于已有人物的脸直接挂上去。"""
    assigned: dict[str, int] = {}
    for i, row in enumerate(candidates, 1):
        vec = to_vector(row["vec"])
        best = conn.execute(MATCH_SQL, (vec, vec)).fetchone()
        if best and float(best["sim"]) >= match:
            pid = str(best["person_id"])
            conn.execute("update faces set person_id = %s::uuid where id = %s::uuid",
                         (pid, row["id"]))
            assigned[pid] = assigned.get(pid, 0) + 1
        if progress_every and i % progress_every == 0:
            print(f"  认领 {i}/{len(candidates)}…", flush=True)
    conn.commit()
    return assigned


def build_components(conn, candidates: list[dict], eps: float,
                     k: int, min_score: float, min_quality: float,
                     progress_every: int = 500) -> list[list[dict]]:
    """② eps 邻域图 + 并查集，返回成员数达标的簇。"""
    by_id = {str(row["id"]): row for row in candidates}
    uf = UnionFind()
    for key in by_id:
        uf.add(key)

    for i, row in enumerate(candidates, 1):
        vec = to_vector(row["vec"])
        neighbors = conn.execute(NEIGHBORS_SQL, (
            vec, str(row["id"]), min_score, min_quality, vec, k,
        )).fetchall()
        for nb in neighbors:
            other = str(nb["id"])
            if other not in by_id:
                continue
            if float(nb["sim"]) >= 1.0 - eps:
                uf.union(str(row["id"]), other)
        if progress_every and i % progress_every == 0:
            print(f"  聚类 {i}/{len(candidates)}…", flush=True)

    groups = [[by_id[item] for item in comp] for comp in uf.components()]
    groups.sort(key=lambda g: (-len(g), min(r["created_at"] for r in g)))
    return groups


def run(conn, eps: float = 0.45, match: float = 0.45, min_faces: int = 3,
        min_score: float = 0.5, min_quality: float = 0.004, k: int = 8,
        dry_run: bool = False) -> dict:
    started = time.time()
    stats = {"candidates": 0, "claimed": 0, "persons": 0, "assigned": 0,
             "skipped_small": 0}

    candidates = load_candidates(conn, min_score, min_quality)
    stats["candidates"] = len(candidates)
    print(f"待归类人脸 {len(candidates)} 张（score>={min_score}, quality>={min_quality}）")
    if not candidates:
        print("没有可聚类的人脸。先跑 `python -m ml.enqueue --kind faces` 和 worker。")
        return stats

    has_persons = conn.execute("select exists(select 1 from persons) f").fetchone()["f"]
    if has_persons and not dry_run:
        claimed = assign_existing(conn, candidates, match)
        stats["claimed"] = sum(claimed.values())
        stats["assigned"] = stats["claimed"]
        print(f"① 认领到已有人物：{stats['claimed']} 张，覆盖 {len(claimed)} 个人物")
        candidates = load_candidates(conn, min_score, min_quality)
        print(f"   剩余待聚类 {len(candidates)} 张")
    elif has_persons:
        print("① 演练模式：跳过认领，按全量未归类人脸估算")
    else:
        print("① 还没有任何人物，跳过认领")

    if not candidates:
        _finalize(conn, dry_run, stats)
        return stats

    groups = build_components(conn, candidates, eps, k, min_score, min_quality)
    big = [g for g in groups if len(g) >= min_faces]
    stats["skipped_small"] = len(candidates) - sum(len(g) for g in big)
    stats["persons"] = len(big)
    print(f"② {len(groups)} 个簇，其中 {len(big)} 个 >= {min_faces} 张（"
          f"最大 {len(big[0]) if big else 0} 张；{stats['skipped_small']} 张留作未归类）")

    if dry_run:
        for g in big[:20]:
            print(f"   {len(g):5d} 张  {min(r['created_at'] for r in g):%Y-%m-%d} 起")
        return stats

    existing = [r["name"] for r in conn.execute(
        "select name from persons where name is not null").fetchall()]
    labels = next_labels(existing, len(big))

    with conn.transaction():
        for label, group in zip(labels, big):
            pid = conn.execute("insert into persons (name) values (%s) returning id",
                               (label,)).fetchone()["id"]
            ids = [str(r["id"]) for r in group]
            conn.execute("update faces set person_id = %s::uuid where id = any(%s::uuid[])",
                         (str(pid), ids))
            best = max(group, key=lambda r: (float(r["quality"] or 0),
                                             float(r["det_score"])))
            conn.execute("update persons set cover_face_id = %s::uuid where id = %s::uuid",
                         (str(best["id"]), str(pid)))
            stats["assigned"] += len(ids)
    print(f"③ 新建 {len(big)} 个人物：{', '.join(labels[:8])}"
          + ("…" if len(labels) > 8 else ""))

    _finalize(conn, dry_run, stats)
    print(f"完成，用时 {time.time()-started:.1f}s。"
          f"打开网页「人物」即可改名。")
    return stats


def _finalize(conn, dry_run: bool, stats: dict) -> None:
    if dry_run:
        return
    with conn.transaction():
        conn.execute(COVER_SQL)
        removed = conn.execute(
            """delete from persons p
               where not exists (select 1 from faces f where f.person_id = p.id
                                 and not f.ignored)"""
        ).rowcount
    if removed:
        print(f"   清理空人物 {removed} 个")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ml.cluster",
                                 description="人脸聚类 → 匿名人物")
    ap.add_argument("--eps", type=float, default=0.45,
                    help="聚类余弦距离阈值（越小越严；默认 0.45 ≈ 相似度 0.55）")
    ap.add_argument("--match", type=float, default=0.45,
                    help="认领到已有人物的余弦相似度阈值")
    ap.add_argument("--min-faces", type=int, default=3,
                    help="少于这么多张脸的簇不建人物")
    ap.add_argument("--min-score", type=float, default=0.5, help="检测置信度下限")
    ap.add_argument("--min-quality", type=float, default=0.004,
                    help="人脸面积占图像比例下限（默认 0.4%%）")
    ap.add_argument("--k", type=int, default=8, help="每张脸取多少个最近邻")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DSN_DEFAULT))
    args = ap.parse_args(argv)

    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        run(conn, eps=args.eps, match=args.match, min_faces=args.min_faces,
            min_score=args.min_score, min_quality=args.min_quality, k=args.k,
            dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
