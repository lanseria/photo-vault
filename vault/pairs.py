"""RAW+JPEG 配对的历史数据整理。

新导入的照片由 ingest 自动配对（`ingest.pair_with_sibling`）；这个模块处理
配对功能上线之前就已经入库的照片 —— 同一张照片当时被存成了两条资产。

判断规则和导入时一致：同库、同目录、同文件名（忽略大小写）、拍摄时间相差 ≤ 5 分钟。
合并方式：JPEG 那条保留（缩略图 / 预览 / 宽高都以它为准），RAW 以 role='raw'
挂上去，标签 / 喜欢 / 设备 / 剪辑箱一并搬过去，然后删掉 RAW 那条资产记录。

物理文件不动：两条资产的文件本来就在 vault 里，合并只是改库记录。
"""

from __future__ import annotations

from typing import Any

from . import meta
from .ingest import merge_pair

JPEG_EXT = [".jpg", ".jpeg"]


def find_pairs(conn) -> list[dict[str, Any]]:
    """找所有「同目录同名的一张 RAW + 一张 JPEG」的候选对。

    一对只出现一次：按 raw 资产去重，多个 JPEG 时取时间最近的那张。
    """
    rows = conn.execute(
        """
        with r as (
            select f.asset_id rid, f.origin_rel rpath,
                   a.library, a.taken_at_local rt
              from files f join assets a on a.id = f.asset_id
             where f.role = 'original'
               and lower(f.ext) = any(%s)
               and not a.is_deleted
               and not exists (select 1 from files x
                                where x.asset_id = a.id and x.role = 'raw')
        ), j as (
            select f.asset_id jid, f.origin_rel jpath,
                   a.library, a.taken_at_local jt
              from files f join assets a on a.id = f.asset_id
             where f.role = 'original'
               and lower(f.ext) = any(%s)
               and not a.is_deleted
        )
        select r.rid, j.jid, r.rpath, j.jpath,
               case when r.rt is null or j.jt is null then null
                    else abs(extract(epoch from (r.rt - j.jt))) end dt
          from r join j
            on j.library = r.library
           and lower(regexp_replace(j.jpath, '\\.[^.]+$', ''))
             = lower(regexp_replace(r.rpath, '\\.[^.]+$', ''))
           and j.jid <> r.rid
           and (r.rt is null or j.jt is null
                or abs(extract(epoch from (r.rt - j.jt))) <= 300)
         order by r.rid, dt nulls last
        """,
        (list(meta.RAW_EXT), JPEG_EXT),
    ).fetchall()

    pairs: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_id = str(row["rid"])
        pairs.setdefault(raw_id, {
            "raw_id": raw_id,
            "jpeg_id": str(row["jid"]),
            "raw_path": row["rpath"],
            "jpeg_path": row["jpath"],
        })
    return list(pairs.values())


def backfill(conn, dry_run: bool = True) -> dict[str, Any]:
    """合并历史 RAW+JPEG。dry_run=True 只统计，不写库。"""
    pairs = find_pairs(conn)
    merged: list[dict[str, Any]] = []
    for pair in pairs:
        if not dry_run:
            # 同一张 RAW 只可能出现一组；前一组没处理成的跳过
            if not conn.execute("select 1 from assets where id = %s::uuid",
                                (pair["raw_id"],)).fetchone():
                continue
            merge_pair(conn, pair["raw_id"], pair["jpeg_id"])
        merged.append(pair)
    return {"candidates": len(pairs), "merged": merged}
