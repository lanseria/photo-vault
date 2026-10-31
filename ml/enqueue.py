"""把还没处理过的资产入队：

    uv run --group ml python -m ml.enqueue                 # 打标 + 人脸 + 视频（默认）
    uv run --group ml python -m ml.enqueue --kind faces    # 只排队人脸
    uv run --group ml python -m ml.enqueue --kind video    # 只排视频（需要 Qwen3-VL）
    uv run --group ml python -m ml.enqueue --limit 300

幂等：同一 (kind, asset) 已经有 pending/running/ok 任务就不重复入队；
要重算就先把旧任务和结果删掉（换模型时见 README）：
    delete from jobs where kind='video';
    delete from video_frames; delete from video_summaries;
    delete from asset_tags where source='vlm';
"""
from __future__ import annotations

import argparse
import os

import psycopg

DSN_DEFAULT = "postgresql://vault:vault@localhost:5433/vault"

ENQUEUE_SQL = """
insert into jobs (kind, payload)
select %(kind)s, jsonb_build_object('asset_id', a.id::text)
from assets a
where not a.is_deleted
  and (%(asset_kind)s::text is null or a.kind = %(asset_kind)s::text)
  and not exists (
    select 1 from jobs j
    where j.kind = %(kind)s and j.state in ('pending', 'running', 'ok')
      and j.payload->>'asset_id' = a.id::text
  )
order by a.taken_at_utc desc nulls last
limit %(limit)s
"""

# kind → (job kind, 只取哪类资产)；video 以外的两类都不限资产类型
KINDS = {
    "tags": [("analyze", None)],
    "faces": [("faces", None)],
    "video": [("video", "video")],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="把待分析资产写入 jobs 队列")
    ap.add_argument("--kind", choices=["all", "tags", "faces", "video"], default="all",
                    help="tags=RAM++ 打标，faces=人脸，video=视频关键帧打标，all=全部")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DSN_DEFAULT))
    args = ap.parse_args()

    kinds = [k for ks in KINDS.values() for k in ks] if args.kind == "all" \
        else KINDS[args.kind]
    total = 0
    with psycopg.connect(args.dsn) as conn:
        for kind, asset_kind in kinds:
            cur = conn.execute(ENQUEUE_SQL, {
                "kind": kind, "asset_kind": asset_kind, "limit": args.limit})
            total += cur.rowcount
            print(f"{kind:8s} 入队 {cur.rowcount} 个")
    print(f"合计 {total} 个任务")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
