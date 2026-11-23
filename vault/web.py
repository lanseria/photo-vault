from __future__ import annotations

import csv
import hashlib
import io
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid as uuidlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import cinema, hashutil, meta
from . import db as dbmod
from . import ingest as ingest_mod
from .config import Config

try:                                    # 让 PIL 能解 HEIC/HEIF（pillow-heif 已在依赖里）
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

cfg = Config.load()
pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    cfg.ensure_dirs()
    # 启动时补齐未应用的迁移：加列/加表之后不用记得先手动 initdb，
    # 否则新代码查一个还不存在的列会把整个列表接口打挂。
    dbmod.init_db(cfg.database_url)
    pool = ConnectionPool(cfg.database_url, min_size=1, max_size=6,
                          kwargs={"row_factory": dict_row}, open=True)
    yield
    if pool:
        pool.close()


app = FastAPI(title="Photo Vault", lifespan=lifespan)

# 跨域：原生 iOS / Android 客户端走 URLSession，本身不受 CORS 限制；
# 但 WKWebView 里加载的页面、浏览器里的调试页、以及将来的网页客户端会受。
# 本服务没有 Cookie / 鉴权，默认放开来源即可；要收紧就设 CORS_ORIGINS（逗号分隔）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")
                   if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # iOS 端要读这些头才能做分片/断点续传和进度显示
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)


def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    assert pool is not None
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def _asset_id(raw: str) -> str:
    try:
        return str(uuidlib.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "bad asset id")


def _validate_ids(raws: list[str]) -> list[str]:
    out: list[str] = []
    for r in raws:
        try:
            out.append(str(uuidlib.UUID(r)))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def _clean_tag(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise HTTPException(400, "标签名需为 1-40 个字符")
    return name


def _thumb_ok(sha: str) -> bool:
    return (cfg.derived_root / "thumbs" / sha[0:2] / sha[2:4] / f"{sha}.webp").exists()


def _display_name(label: str | None, canonical: str | None, raw: str | None) -> str:
    """设备展示名：用户命名 > 归一化型号 > EXIF 原始型号 > 未识别。"""
    for value in (label, canonical, raw):
        text = (value or "").strip()
        if text:
            return text
    return "未识别设备"


def _device_name(row: dict[str, Any]) -> str:
    return _display_name(row.get("device_label"), row.get("device_canonical"),
                         row.get("device_raw"))


# ---------------------------------------------------------------- 宿主路径映射
# app 跑在容器里，看到的是 /host/vault/ab/cd/x.mov；但"打开资源管理器"和"把文件塞进
# 剪映"需要的是宿主机路径 E:\photo\vault\ab\cd\x.mov。HOST_PATH_ROOT 只为生成这个
# 字符串而存在，从不用于落盘。

_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    """Windows 文件名安全化（保留中文，只去非法字符和结尾的点/空格）。"""
    s = _BAD_NAME.sub("_", (name or "").strip())
    return s.strip(" .")[:80]


def to_host_path(p: Path) -> str:
    root = (cfg.host_path_root or "").rstrip("\\/")
    if not root:
        return str(p)
    try:
        rel = p.relative_to(cfg.vault_root.parent)     # /host 之下
    except ValueError:
        rel = Path(p.name)
    return root + "\\" + str(rel).replace("/", "\\")


def _display_src_path(src: str | None) -> str | None:
    """原始地址给人看：能映射到宿主机盘符就映射（/host/x → E:\\photo\\x），否则原样。

    和 to_host_path 不同：src_path 可能是任意的导入来源路径（SMB、SD 卡挂载点…），
    不在 /host 下时不能瞎拼，原样返回。
    """
    if not src:
        return None
    try:
        rel = Path(src).relative_to(cfg.vault_root.parent)
    except ValueError:
        return src
    root = (cfg.host_path_root or "").rstrip("\\/")
    return (root + "\\" + str(rel).replace("/", "\\")) if root else src



# ---------------------------------------------------------------- 统计

@app.get("/api/stats")
def api_stats() -> JSONResponse:
    row = q(
        """select
             (select count(*) from assets where not is_deleted) total,
             (select count(*) from assets where not is_deleted and library='gallery') gallery,
             (select count(*) from assets where not is_deleted and library='material') material,
             (select count(*) from assets where favorite and not is_deleted) favs,
             (select count(*) from assets where is_deleted) trash,
             (select count(*) from persons p
               where exists (select 1 from faces f where f.person_id = p.id
                             and not f.ignored)) people,
             (select coalesce(sum(bytes),0) from files
               where role in ('original', 'raw')) b,
             (select coalesce(sum(f.bytes),0) from files f join assets a on a.id=f.asset_id
               where f.role = 'original' and a.library='material' and not a.is_deleted) mb,
             (select coalesce(sum(a.duration_ms),0) from assets a
               where a.library='material' and not a.is_deleted) mdur,
             (select count(*) from cinema_titles) cinema_titles,
             (select count(*) from cinema_episodes) cinema_eps,
             (select count(*) from devices) device_count,
             (select count(*) from assets where device_id is null and not is_deleted) unknown_device,
             (select min(taken_at_utc) from assets where not is_deleted) mn,
             (select max(taken_at_utc) from assets where not is_deleted) mx"""
    )[0]
    devs = q(
        """select coalesce(nullif(d.label,''), nullif(d.model_canonical,''),
                           d.model_raw, '(未识别)') model, count(*) c
           from assets a left join devices d on d.id=a.device_id
           where not a.is_deleted group by 1 order by c desc limit 12"""
    )
    return JSONResponse({
        "count": row["total"],
        "gallery": row["gallery"],
        "material": row["material"],
        "material_bytes": int(row["mb"]),
        "material_duration_ms": int(row["mdur"]),
        "favorites": row["favs"],
        "trash": row["trash"],
        "persons": row["people"],
        "cinema_titles": row["cinema_titles"],
        "cinema_episodes": row["cinema_eps"],
        "device_count": row["device_count"],
        "unknown_device": row["unknown_device"],
        "media_root": str(cfg.media_root),
        "media_host_root": cfg.media_host_root,
        "bytes": int(row["b"]),
        "first": row["mn"].isoformat() if row["mn"] else None,
        "last": row["mx"].isoformat() if row["mx"] else None,
        "devices": [{"model": d["model"], "count": d["c"]} for d in devs],
    })


# ---------------------------------------------------------------- 资产列表

def _asset_filters(
    *,
    trash: bool = False,
    show_hidden: bool = False,
    library: str | None = None,
    kind: str | None = None,
    favorite: bool | None = None,
    tag: str | None = None,
    person_id: str | None = None,
    qtext: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_sec: float | None = None,
    max_sec: float | None = None,
    min_px: int | None = None,
    device: str | None = None,
    folder: str | None = None,
) -> tuple[list[str], list[Any]]:
    """资产列表的 where 条件。列表 /api/assets 和日期分布 /api/assets/dates
    共用同一份，保证跳转面板看到的日期和实际列表完全一致。"""
    where = ["a.is_deleted" if trash else "not a.is_deleted"]
    params: list[Any] = []

    # 隐藏是软标记（019）：默认所有视图排除；前端「展示隐藏」开关打开后混入显示。
    # 回收站不加这个条件——隐藏过又删掉的也要能看到、能恢复。
    if not trash and not show_hidden:
        where.append("not a.hidden")

    if library:
        if library not in ("gallery", "material"):
            raise HTTPException(400, "library 只能是 gallery 或 material")
        where.append("a.library = %s")
        params.append(library)

    if kind:
        # 允许逗号分隔：剪辑要的"视频"其实是 video + live_photo
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
        where.append("a.kind = any(%s)")
        params.append(kinds)
    if favorite is not None:
        where.append("a.favorite = %s")
        params.append(favorite)
    if tag:
        where.append(
            "exists (select 1 from asset_tags at2 join tags t2 on t2.id=at2.tag_id "
            "where at2.asset_id=a.id and t2.name=%s)"
        )
        params.append(tag)
    if person_id:
        pid = _asset_id(person_id)
        # 检出脸的（未忽略）+ 手动关联的（person_assets，背影/远景这类识别不了的）都算
        where.append(
            """(exists (select 1 from faces fp where fp.asset_id=a.id
                        and fp.person_id=%s::uuid and not fp.ignored)
                or exists (select 1 from person_assets pa where pa.asset_id=a.id
                           and pa.person_id=%s::uuid))"""
        )
        params.extend([pid, pid])
    if qtext:
        # 剪辑场景的搜索必须能命中"内容"：AI/VLM 标签、视频描述、OCR、人物名（检出脸的
        # 和手动关联的都算），而不只是文件名。原来只搜文件名+描述，打标结果等于白存。
        like = "%" + qtext.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            """(f.src_path ilike %s or f.origin_rel ilike %s or f.ext ilike %s
                or exists (select 1 from video_summaries vs where vs.asset_id = a.id
                           and (vs.caption ilike %s or vs.ocr ilike %s))
                or exists (select 1 from asset_tags at4 join tags t4 on t4.id = at4.tag_id
                           where at4.asset_id = a.id and t4.name ilike %s)
                or exists (select 1 from faces f4 join persons p4 on p4.id = f4.person_id
                           where f4.asset_id = a.id and not f4.ignored
                           and p4.name ilike %s)
                or exists (select 1 from person_assets pk
                           join persons p5 on p5.id = pk.person_id
                           where pk.asset_id = a.id and p5.name ilike %s))"""
        )
        params.extend([like] * 8)
    if date_from:
        where.append("a.taken_at_local >= %s")
        params.append(date_from)
    if date_to:
        where.append("a.taken_at_local < (%s::date + 1)")
        params.append(date_to)
    if min_sec is not None:
        where.append("coalesce(a.duration_ms, 0) >= %s")
        params.append(int(min_sec * 1000))
    if max_sec is not None:
        where.append("coalesce(a.duration_ms, 0) <= %s")
        params.append(int(max_sec * 1000))
    if min_px:
        where.append("coalesce(least(a.width, a.height), 0) >= %s")
        params.append(int(min_px))
    if device:
        # 'unknown' = 还没识别出设备的照片；其余按设备 id 精确过滤
        if device == "unknown":
            where.append("a.device_id is null")
        elif device.isdigit():
            where.append("a.device_id = %s")
            params.append(int(device))
        else:
            raise HTTPException(400, "device 只能是设备 id 或 'unknown'")
    if folder:
        # 文件夹是素材库的整理维度：'none' = 未整理，其余按文件夹 id 精确过滤
        if folder == "none":
            where.append("a.folder_id is null")
        else:
            where.append("a.folder_id = %s::uuid")
            params.append(_asset_id(folder))

    return where, params


@app.get("/api/assets")
def api_assets(
    limit: int = Query(120, ge=1, le=300),
    offset: int = Query(0, ge=0),
    kind: str | None = None,
    qtext: str | None = Query(None, alias="q"),
    date_from: str | None = None,
    date_to: str | None = None,
    favorite: bool | None = None,
    tag: str | None = None,
    person_id: str | None = None,
    trash: bool = False,
    show_hidden: bool = False,
    library: str | None = None,
    min_sec: float | None = Query(None, ge=0),
    max_sec: float | None = Query(None, ge=0),
    min_px: int | None = Query(None, ge=0),
    device: str | None = Query(None, description="设备 id，或 'unknown' 表示未识别"),
    folder: str | None = Query(None, description="文件夹 id（任意库），或 'none' 表示未整理"),
) -> JSONResponse:
    where, params = _asset_filters(
        trash=trash, show_hidden=show_hidden, library=library, kind=kind, favorite=favorite, tag=tag,
        person_id=person_id, qtext=qtext, date_from=date_from, date_to=date_to,
        min_sec=min_sec, max_sec=max_sec, min_px=min_px, device=device, folder=folder,
    )
    clause = " and ".join(where)
    base = f"""
        from assets a
        left join devices d on d.id = a.device_id
        join files f on f.asset_id = a.id and f.role = 'original'
        {_RAW_JOIN}
        where {clause}
    """
    order = ("coalesce(a.deleted_at, a.taken_at_utc) desc nulls last"
             if trash else "a.taken_at_utc desc nulls last")
    total = q(f"select count(*) c {base}", tuple(params))[0]["c"]
    rows = q(
        f"""select a.id, a.kind, a.library, a.favorite, a.hidden, a.taken_at_local, a.tz_offset_min, a.tz_source,
                   a.width, a.height, a.duration_ms, a.folder_id,
                   a.device_id, d.label as device_label, d.model_raw as device_raw,
                   d.model_canonical as device_canonical, d.kind as device_kind,
                   f.sha256, f.bytes, f.ext, f.origin_rel, f.storage_mode,
                   encode(f.sha256,'hex') sha,
                   rf.ext raw_ext, rf.bytes raw_bytes,
                   {_HAS_PERSON_SQL} has_person,
                   coalesce((select json_agg(t3.name order by t3.name)
                             from asset_tags at3 join tags t3 on t3.id = at3.tag_id
                             where at3.asset_id = a.id), '[]'::json) tags
            {base}
            order by {order}
            limit %s offset %s""",
        tuple(params + [limit, offset]),
    )

    return JSONResponse({"total": total, "items": [_item_json(r, trash=trash) for r in rows]})


@app.get("/api/assets/dates")
def api_asset_dates(
    kind: str | None = None,
    qtext: str | None = Query(None, alias="q"),
    favorite: bool | None = None,
    tag: str | None = None,
    person_id: str | None = None,
    trash: bool = False,
    show_hidden: bool = False,
    library: str | None = None,
    min_sec: float | None = Query(None, ge=0),
    max_sec: float | None = Query(None, ge=0),
    min_px: int | None = Query(None, ge=0),
    device: str | None = Query(None, description="设备 id，或 'unknown' 表示未识别"),
    folder: str | None = Query(None, description="文件夹 id（任意库），或 'none' 表示未整理"),
) -> JSONResponse:
    """当前筛选下「哪天有几张」的日期分布（按拍摄日倒序）。

    日期跳转面板一次拿到全部分布，就能在客户端算出任意日期在流里的
    偏移量（排在它之后的张数之和），不用为了跳转从头翻分页。
    回收站的排序是删除时间不是拍摄时间，前端在回收站隐藏跳转入口。"""
    where, params = _asset_filters(
        trash=trash, show_hidden=show_hidden, library=library, kind=kind, favorite=favorite, tag=tag,
        person_id=person_id, qtext=qtext, min_sec=min_sec, max_sec=max_sec,
        min_px=min_px, device=device, folder=folder,
    )
    # 没有拍摄时间的资产归不到任何日期，跳转也落不进去，不进分布
    where.append("a.taken_at_local is not null")
    clause = " and ".join(where)
    rows = q(
        f"""select a.taken_at_local::date as day, count(*) as c
            from assets a
            left join devices d on d.id = a.device_id
            join files f on f.asset_id = a.id and f.role = 'original'
            {_RAW_JOIN}
            where {clause}
            group by 1
            order by 1 desc""",
        tuple(params),
    )
    return JSONResponse(
        {"days": [{"day": r["day"].isoformat(), "count": int(r["c"])} for r in rows]}
    )


# ---------------------------------------------------------------- 剪辑箱 / 出稿
# 剪辑的真实动作是"跨多次搜索把素材攒起来，然后整批交给剪辑软件"。
# 搜索结果是一次性的，所以需要 bins 做持久化收纳；出稿把箱子物化成一个真实目录。

class BinBody(BaseModel):
    name: str
    note: str | None = None


class BinIdsBody(BaseModel):
    ids: list[str]


class DeliverBody(BaseModel):
    mode: str = "link"          # link=硬链接（0 字节、瞬时）；copy=独立副本


# 已归类人物（挂到某人的未忽略脸）或手动关联了人物 → 列表格子右下角的小人标记
_HAS_PERSON_SQL = """(exists (select 1 from faces fp where fp.asset_id = a.id
                              and fp.person_id is not null and not fp.ignored)
                     or exists (select 1 from person_assets pk where pk.asset_id = a.id))"""

_ITEM_COLS = """a.id, a.kind, a.library, a.favorite, a.hidden, a.taken_at_local, a.tz_source,
                a.width, a.height, a.duration_ms, a.folder_id,
                a.device_id, d.label as device_label, d.model_raw as device_raw,
                d.model_canonical as device_canonical, d.kind as device_kind,
                f.sha256, f.bytes, f.ext, f.origin_rel,
                encode(f.sha256,'hex') sha,
                rf.ext raw_ext, rf.bytes raw_bytes,
                """ + _HAS_PERSON_SQL + """ has_person"""

# RAW+JPEG 是一张照片两个文件：JPEG 是 original，RAW 以 role='raw' 挂在同一个资产上。
_RAW_JOIN = "left join files rf on rf.asset_id = a.id and rf.role = 'raw'"


def _item_json(r: dict[str, Any], ord_: int | None = None, trash: bool = False) -> dict[str, Any]:
    item = {
        "id": str(r["id"]),
        "kind": r["kind"],
        "library": r["library"],
        "sha": r["sha"],
        "favorite": r["favorite"],
        # 软隐藏（019）：默认视图不含隐藏项，前端「展示隐藏」开关打开后混入显示
        "hidden": bool(r.get("hidden")),
        "taken": r["taken_at_local"].isoformat() if r["taken_at_local"] else None,
        "tz_source": r["tz_source"],
        # 素材库文件夹：null = 未整理
        "folder_id": str(r["folder_id"]) if r.get("folder_id") else None,
        # 展示名（用户标签 > 归一化型号 > EXIF 原始型号）；未识别时是固定文案
        "model": _device_name(r),
        "device_id": r["device_id"],
        "device_unknown": r["device_id"] is None,
        "device_kind": r["device_kind"],
        "device_raw": r["device_raw"] or "",
        "width": r["width"],
        "height": r["height"],
        "duration_ms": r["duration_ms"],
        "bytes": r["bytes"],
        "has_thumb": _thumb_ok(r["sha"]),
        "origin": r["origin_rel"],
        # RAW+JPEG：raw_ext 非空表示这张照片还有一份 RAW 原件（出稿时一起给）
        "raw_ext": (r.get("raw_ext") or ""),
        "raw_bytes": int(r.get("raw_bytes") or 0),
        "tags": r["tags"],
        # 右下角小人标记：识别归类过（有挂到人物的未忽略脸）或手动关联过人物
        "has_person": bool(r.get("has_person")),
        "deleted": trash,
    }
    if ord_ is not None:
        item["ord"] = ord_
    return item


@app.get("/api/assets/{asset_id}")
def api_asset(asset_id: str) -> JSONResponse:
    """单个资产。深链（?photo=<id>）刷新时，这张可能还没出现在分页里。"""
    aid = _asset_id(asset_id)
    rows = q(
        f"""select {_ITEM_COLS}, a.is_deleted,
                   coalesce((select json_agg(t3.name order by t3.name)
                             from asset_tags at3 join tags t3 on t3.id = at3.tag_id
                             where at3.asset_id = a.id), '[]'::json) tags
            from assets a
            left join devices d on d.id = a.device_id
            join files f on f.asset_id = a.id and f.role = 'original'
            {_RAW_JOIN}
            where a.id = %s::uuid""",
        (aid,),
    )
    if not rows:
        raise HTTPException(404, "asset not found")
    item = _item_json(rows[0])
    item["deleted"] = bool(rows[0]["is_deleted"])
    return JSONResponse(item)


@app.get("/api/assets/{asset_id}/info")
def api_asset_info(asset_id: str) -> JSONResponse:
    """灯箱「详细信息」面板：拍摄/同步/来源/EXIF/GPS 一次给全。

    列表接口只带格子要用的字段；这里是单资产深度信息，join
    files(original) / sources / exif，单行查询，开销很小。
    """
    aid = _asset_id(asset_id)
    rows = q(
        """select a.id, a.kind, a.library, a.taken_at_local, a.tz_offset_min, a.tz_source,
                  a.lens, a.width, a.height, a.duration_ms, a.created_at,
                  a.gps[0]::float8 as gps_lon, a.gps[1]::float8 as gps_lat,
                  d.make, d.model_raw, d.model_canonical, d.kind as device_kind,
                  encode(f.sha256, 'hex') as sha, f.bytes, f.mime, f.ext, f.rel_path,
                  f.src_path, f.imported_at,
                  s.kind as source_kind, s.label as source_label, s.root_path as source_root,
                  e.iso, e.aperture, e.shutter, e.focal_mm, e.orientation,
                  rf.ext as raw_ext, rf.bytes as raw_bytes
           from assets a
           left join devices d on d.id = a.device_id
           join files f on f.asset_id = a.id and f.role = 'original'
           left join sources s on s.id = f.source_id
           left join exif e on e.asset_id = a.id
           left join files rf on rf.asset_id = a.id and rf.role = 'raw'
          where a.id = %s::uuid""",
        (aid,),
    )
    if not rows:
        raise HTTPException(404, "asset not found")
    r = rows[0]

    def num(v: Any) -> float | None:
        return float(v) if v is not None else None

    ts = lambda v: v.isoformat() if v else None  # noqa: E731
    exif_present = any(
        r[k] is not None for k in ("iso", "aperture", "shutter", "focal_mm", "orientation")
    )
    return JSONResponse({
        "id": str(r["id"]),
        "kind": r["kind"],
        "library": r["library"],
        "taken": ts(r["taken_at_local"]),
        "tz_offset_min": r["tz_offset_min"],
        "tz_source": r["tz_source"],
        # 同步时间 = 这份文件进库的时间；入库时间 = 资产首次登记
        # （同一份文件重复导入只追加 provenance，两个时间可能不同）
        "synced_at": ts(r["imported_at"]),
        "added_at": ts(r["created_at"]),
        # 原始地址：导入时文件在来源里的路径；在 /host 下就映射成宿主机盘符给人看
        "src_path": _display_src_path(r["src_path"]),
        "origin_rel": r["rel_path"],
        "source": (
            {"kind": r["source_kind"], "label": r["source_label"], "root": r["source_root"]}
            if (r["source_kind"] or r["source_label"]) else None
        ),
        "ext": r["ext"],
        "mime": r["mime"],
        "bytes": int(r["bytes"]),
        "raw_ext": r["raw_ext"] or "",
        "raw_bytes": int(r["raw_bytes"] or 0),
        "width": r["width"],
        "height": r["height"],
        "duration_ms": r["duration_ms"],
        "sha": r["sha"],
        "device": (
            {"make": r["make"], "model_raw": r["model_raw"],
             "model_canonical": r["model_canonical"], "kind": r["device_kind"]}
            if r["model_raw"] else None
        ),
        "lens": r["lens"],
        "exif": (
            {"iso": r["iso"], "aperture": num(r["aperture"]), "shutter": num(r["shutter"]),
             "focal_mm": num(r["focal_mm"]), "orientation": r["orientation"]}
            if exif_present else None
        ),
        "gps": {"lon": r["gps_lon"], "lat": r["gps_lat"]} if r["gps_lon"] is not None else None,
    })


# ---------------------------------------------------------------- 设备
# EXIF 里的机型号常常缺失：截图、微信保存的图、剪辑软件导出的图都没有。
# 这里给三件事：
#   · 设备列表（带使用量）：导入面板里直接挑；
#   · 用户标签：把 "iPhone15,2" 改成 "我的手机"，全站显示都跟着变；
#   · 手工指派：把一批没认出设备的照片指到某台设备上。

class DeviceLabelBody(BaseModel):
    label: str | None = None


class DeviceCreateBody(BaseModel):
    name: str
    kind: str | None = None


class DeviceMergeBody(BaseModel):
    into: int


class DeviceAssignBody(BaseModel):
    ids: list[str]
    device_id: int | None = None


DEVICE_KINDS = ("phone", "camera", "drone", "actioncam", "other")


_DEVICE_LIST_SQL = """
select d.id, d.make, d.model_raw, d.model_canonical, d.label, d.kind,
       count(a.id) filter (where not a.is_deleted) n_assets,
       count(a.id) filter (where not a.is_deleted and a.library = 'gallery') n_gallery,
       min(a.taken_at_local) filter (where not a.is_deleted) first_taken,
       max(a.taken_at_local) filter (where not a.is_deleted) last_taken
  from devices d
  left join assets a on a.device_id = d.id
 group by d.id
"""


def _device_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "name": _display_name(r["label"], r["model_canonical"], r["model_raw"]),
        "label": r["label"] or "",
        "make": r["make"] or "",
        "model_raw": r["model_raw"] or "",
        "model_canonical": r["model_canonical"] or "",
        "kind": r["kind"] or "other",
        "n_assets": r["n_assets"],
        "n_gallery": r["n_gallery"],
        "first": r["first_taken"].isoformat() if r["first_taken"] else None,
        "last": r["last_taken"].isoformat() if r["last_taken"] else None,
    }


@app.get("/api/devices")
def api_devices() -> JSONResponse:
    rows = q(f"{_DEVICE_LIST_SQL} order by n_assets desc, "
             f"lower(coalesce(d.label, d.model_canonical, d.model_raw))")
    unknown = q("select count(*) c from assets "
                "where device_id is null and not is_deleted")[0]["c"]
    return JSONResponse({"devices": [_device_json(r) for r in rows], "unknown": unknown})


@app.post("/api/devices")
def api_device_create(body: DeviceCreateBody) -> JSONResponse:
    """新建一台库里还没有的设备（EXIF 认不出来的机器，比如「大疆 Action 2」）。

    建出来是一台空设备：model_raw 存名字本身，label 也写上，之后可以随时改名。
    """
    name = (body.name or "").strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(400, "设备名需为 1-40 个字符")
    kind = body.kind if body.kind in DEVICE_KINDS else "other"
    taken = q(
        """select 1 from devices
            where lower(coalesce(nullif(label,''), nullif(model_canonical,''), model_raw))
                  = lower(%s)""",
        (name,),
    )
    if taken:
        raise HTTPException(409, "已经有同名设备了，可以直接用它（或者先合并）")
    row = q(
        """insert into devices (make, model_raw, model_canonical, label, kind)
           values (null, %s, %s, %s, %s) returning id""",
        (name, name, name, kind),
    )[0]
    rows = q(f"select * from ({_DEVICE_LIST_SQL}) t where t.id = %s", (row["id"],))
    return JSONResponse(_device_json(rows[0]))


@app.post("/api/devices/{device_id}/merge")
def api_device_merge(device_id: int, body: DeviceMergeBody) -> JSONResponse:
    """把一台设备的照片全部并到另一台，然后删掉源设备（去重用）。

    只改 assets.device_id：照片的 EXIF 原值不动，所以以后导入同型号的机器时，
    ingest 会按 EXIF 重新建出台设备 —— 这是内容寻址的代价，也是「不改原数据」的保证。
    """
    if device_id == body.into:
        raise HTTPException(400, "不能合并到自己")
    if not q("select 1 from devices where id=%s", (device_id,)):
        raise HTTPException(404, "源设备不存在")
    if not q("select 1 from devices where id=%s", (body.into,)):
        raise HTTPException(404, "目标设备不存在")
    assert pool is not None
    with pool.connection() as conn:
        moved = conn.execute(
            "update assets set device_id=%s where device_id=%s",
            (body.into, device_id),
        ).rowcount
        conn.execute("delete from devices where id=%s", (device_id,))
    rows = q(f"select * from ({_DEVICE_LIST_SQL}) t where t.id = %s", (body.into,))
    return JSONResponse({"into": body.into, "moved": moved, "device": _device_json(rows[0])})


@app.patch("/api/devices/{device_id}")
def api_device_rename(device_id: int, body: DeviceLabelBody) -> JSONResponse:
    """改名（label）。传空字符串 = 恢复默认展示名。"""
    label = (body.label or "").strip()
    if len(label) > 40:
        raise HTTPException(400, "设备名最多 40 个字符")
    updated = q("update devices set label=%s where id=%s returning id",
                (label or None, device_id))
    if not updated:
        raise HTTPException(404, "设备不存在")
    rows = q(f"select * from ({_DEVICE_LIST_SQL}) t where t.id = %s", (device_id,))
    return JSONResponse(_device_json(rows[0]))


@app.post("/api/devices/assign")
def api_device_assign(body: DeviceAssignBody) -> JSONResponse:
    """把一批照片指到某台设备（device_id=null 表示重新标记成未识别）。"""
    ids = _validate_ids(body.ids)
    if not ids:
        raise HTTPException(400, "没有有效的资产 id")
    if body.device_id is not None and not q(
            "select 1 from devices where id=%s", (body.device_id,)):
        raise HTTPException(404, "设备不存在")
    assert pool is not None
    with pool.connection() as conn:
        n = conn.execute(
            "update assets set device_id=%s where id = any(%s::uuid[]) and not is_deleted",
            (body.device_id, ids),
        ).rowcount
    return JSONResponse({"updated": n, "device_id": body.device_id})


@app.get("/api/bins")
def api_bins() -> JSONResponse:
    rows = q("""select b.id, b.name, b.note, b.updated_at,
                       count(bi.asset_id) items,
                       coalesce(sum(a.duration_ms), 0) dur_ms,
                       coalesce(sum(f.bytes), 0) + coalesce(sum(rf.bytes), 0) bytes
                from bins b
                left join bin_items bi on bi.bin_id = b.id
                left join assets a on a.id = bi.asset_id
                left join files f on f.asset_id = a.id and f.role = 'original'
                left join files rf on rf.asset_id = a.id and rf.role = 'raw'
                group by b.id
                order by b.updated_at desc""")
    return JSONResponse({"bins": [{
        "id": str(r["id"]), "name": r["name"], "note": r["note"],
        "items": r["items"], "dur_ms": r["dur_ms"], "bytes": int(r["bytes"]),
    } for r in rows]})


@app.post("/api/bins")
def api_bin_create(body: BinBody) -> JSONResponse:
    name = _safe_name(body.name) or "剪辑箱"
    row = q("insert into bins (name, note) values (%s, %s) returning id, name",
            (name, body.note))[0]
    return JSONResponse({"id": str(row["id"]), "name": row["name"]})


@app.patch("/api/bins/{bin_id}")
def api_bin_rename(bin_id: str, body: BinBody) -> JSONResponse:
    bid = _asset_id(bin_id)
    name = _safe_name(body.name)
    if not name:
        raise HTTPException(400, "名字不能为空")
    row = q("update bins set name=%s, note=coalesce(%s, note), updated_at=now() "
            "where id=%s::uuid returning id, name", (name, body.note, bid))
    if not row:
        raise HTTPException(404, "not found")
    return JSONResponse({"id": str(row[0]["id"]), "name": row[0]["name"]})


@app.delete("/api/bins/{bin_id}")
def api_bin_delete(bin_id: str) -> JSONResponse:
    bid = _asset_id(bin_id)
    with pool.connection() as conn:
        n = conn.execute("delete from bins where id=%s::uuid", (bid,)).rowcount
    return JSONResponse({"deleted": n})


@app.get("/api/bins/{bin_id}/items")
def api_bin_items(bin_id: str) -> JSONResponse:
    bid = _asset_id(bin_id)
    rows = q(
        f"""select bi.ord, {_ITEM_COLS},
                   coalesce((select json_agg(t3.name order by t3.name)
                             from asset_tags at3 join tags t3 on t3.id = at3.tag_id
                             where at3.asset_id = a.id), '[]'::json) tags
            from bin_items bi
            join assets a on a.id = bi.asset_id
            left join devices d on d.id = a.device_id
            join files f on f.asset_id = a.id and f.role = 'original'
            {_RAW_JOIN}
            where bi.bin_id = %s::uuid and not a.is_deleted
            order by bi.ord, bi.added_at""",
        (bid,),
    )
    return JSONResponse({"items": [_item_json(r, r["ord"]) for r in rows]})


@app.post("/api/bins/{bin_id}/items")
def api_bin_add(bin_id: str, body: BinIdsBody) -> JSONResponse:
    """批量加入。已在箱子里的会被移到末尾（重复加入 = 调整到最新顺序）。"""
    bid = _asset_id(bin_id)
    ids = _validate_ids(body.ids)
    if not ids:
        raise HTTPException(400, "没有有效的资产 id")
    with pool.connection() as conn:
        if not conn.execute("select 1 from bins where id=%s::uuid", (bid,)).fetchone():
            raise HTTPException(404, "not found")
        known = {
            str(r["asset_id"]) for r in conn.execute(
                "select asset_id from bin_items where bin_id=%s::uuid and asset_id = any(%s::uuid[])",
                (bid, ids),
            )
        }
        base = conn.execute(
            "select coalesce(max(ord), -1) + 1 m from bin_items where bin_id=%s::uuid",
            (bid,),
        ).fetchone()["m"]
        for i, aid in enumerate(ids):
            conn.execute(
                """insert into bin_items (bin_id, asset_id, ord)
                   values (%s::uuid, %s::uuid, %s)
                   on conflict (bin_id, asset_id) do update set ord = excluded.ord""",
                (bid, aid, base + i),
            )
        conn.execute("update bins set updated_at = now() where id=%s::uuid", (bid,))
    return JSONResponse({"added": len(ids) - len(known), "moved": len(known),
                         "total": len(ids)})


@app.delete("/api/bins/{bin_id}/items")
def api_bin_clear(bin_id: str) -> JSONResponse:
    bid = _asset_id(bin_id)
    with pool.connection() as conn:
        n = conn.execute("delete from bin_items where bin_id=%s::uuid", (bid,)).rowcount
        conn.execute("update bins set updated_at = now() where id=%s::uuid", (bid,))
    return JSONResponse({"cleared": n})


@app.delete("/api/bins/{bin_id}/items/{asset_id}")
def api_bin_remove(bin_id: str, asset_id: str) -> JSONResponse:
    bid, aid = _asset_id(bin_id), _asset_id(asset_id)
    with pool.connection() as conn:
        conn.execute("delete from bin_items where bin_id=%s::uuid and asset_id=%s::uuid",
                     (bid, aid))
        conn.execute("update bins set updated_at = now() where id=%s::uuid", (bid,))
    return JSONResponse({"ok": True})


@app.post("/api/bins/{bin_id}/reorder")
def api_bin_reorder(bin_id: str, body: BinIdsBody) -> JSONResponse:
    bid = _asset_id(bin_id)
    ids = _validate_ids(body.ids)
    with pool.connection() as conn:
        for i, aid in enumerate(ids):
            conn.execute("update bin_items set ord=%s where bin_id=%s::uuid and asset_id=%s::uuid",
                         (i, bid, aid))
        conn.execute("update bins set updated_at = now() where id=%s::uuid", (bid,))
    return JSONResponse({"ok": True, "count": len(ids)})


def _open_folder_on_windows(p: Path) -> bool:
    """app 直接跑在 Windows 上（宿主模式）时，出稿后自己开资源管理器。

    这种部署下不需要任何"桥"；容器里 os.name 是 posix，永远返回 False，
    改由前端去问 127.0.0.1:8099。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        return ctypes.windll.shell32.ShellExecuteW(None, "open", str(p), None, None, 1) > 32
    except Exception:
        return False


@app.post("/api/bins/{bin_id}/deliver")
def api_bin_deliver(bin_id: str, body: DeliverBody | None = None) -> JSONResponse:
    """把剪辑箱物化成 <derived>/cuts/<箱名>/ 下的真实文件。

    硬链接的取舍和 `ingest --mode link` 完全一致：同卷零额外空间、瞬时、是真文件
    （不是快捷方式），剪映当普通文件读；删掉出稿目录也不影响库里的原件。
    跨卷（EXDEV）自动退回拷贝。
    """
    bid = _asset_id(bin_id)
    mode = (body.mode if body else "link") or "link"
    if mode not in ("link", "copy"):
        raise HTTPException(400, "mode 只能是 link 或 copy")

    bin_row = q("select name from bins where id = %s::uuid", (bid,))
    if not bin_row:
        raise HTTPException(404, "剪辑箱不存在")
    rows = q(
        """select bi.ord, a.taken_at_local, a.duration_ms, a.width, a.height,
                  f.rel_path, f.ext, f.bytes, f.origin_rel,
                  rf.rel_path raw_rel, rf.ext raw_ext, rf.bytes raw_bytes,
                  rf.origin_rel raw_origin,
                  coalesce((select string_agg(t3.name, ' ' order by t3.name)
                            from asset_tags at3 join tags t3 on t3.id = at3.tag_id
                            where at3.asset_id = a.id), '') tags
           from bin_items bi
           join assets a on a.id = bi.asset_id
           join files f on f.asset_id = a.id and f.role = 'original'
           {_RAW_JOIN}
           where bi.bin_id = %s::uuid and not a.is_deleted
           order by bi.ord, bi.added_at""".format(_RAW_JOIN=_RAW_JOIN),
        (bid,),
    )
    if not rows:
        raise HTTPException(400, "剪辑箱是空的")

    out = cfg.derived_root / "cuts" / (_safe_name(bin_row[0]["name"]) or "剪辑箱")
    out.mkdir(parents=True, exist_ok=True)

    def write_one(src: Path, dst: Path) -> None:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                raise HTTPException(409, f"{dst.name} 被占用——剪映可能正打开着这个文件")
        if mode == "link":
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)

    written: list[tuple[str, dict[str, Any]]] = []
    missing = 0
    for i, r in enumerate(rows, 1):
        src = cfg.vault_root / r["rel_path"]
        if not src.exists():
            missing += 1
            continue
        # 001_20180814_142233_DNYZ7334.MP4
        # 序号 = 箱子里的顺序；时间 = 拍摄时间（剪映素材库按名排序就等于按时间排）
        ts = (r["taken_at_local"].strftime("%Y%m%d_%H%M%S")
              if r["taken_at_local"] else "00000000_000000")
        orig = Path(r["origin_rel"]) if r["origin_rel"] else None
        stem = _safe_name(orig.stem) if orig else ""
        ext = (orig.suffix if orig else "") or (r["ext"] or "")
        name = f"{i:03d}_{ts}" + (f"_{stem}" if stem else "") + ext
        write_one(src, out / name)
        written.append((name, r))

        # RAW+JPEG：同一序号、同一 stem，把 RAW 也放出去 —— 剪辑时拿到的还是一对
        if r["raw_rel"]:
            raw_src = cfg.vault_root / r["raw_rel"]
            if not raw_src.exists():
                missing += 1
                continue
            raw_origin = Path(r["raw_origin"]) if r["raw_origin"] else orig
            raw_stem = _safe_name(raw_origin.stem) if raw_origin else stem
            raw_ext = (r["raw_ext"] or ".raw").lower()
            raw_name = f"{i:03d}_{ts}" + (f"_{raw_stem}" if raw_stem else "") + raw_ext
            write_one(raw_src, out / raw_name)
            written.append((raw_name, {
                **r,
                "bytes": r["raw_bytes"],
                "duration_ms": None,
                "width": None,
                "height": None,
            }))

    # 清掉上一次出稿留下、这次已经不在箱子里的文件（只认我们自己的 001_ 命名）
    keep = {n for n, _ in written}
    stale = 0
    for p in out.iterdir():
        if p.is_file() and re.match(r"^\d{3}_", p.name) and p.name not in keep:
            try:
                p.unlink()
                stale += 1
            except OSError:
                pass

    cutlist = out / "cutlist.csv"
    with cutlist.open("w", encoding="utf-8-sig", newline="") as fh:   # BOM：Excel 直接开不乱码
        w = csv.writer(fh)
        w.writerow(["序号", "文件", "拍摄时间", "时长(秒)", "分辨率", "大小(MB)", "标签"])
        for name, r in written:
            w.writerow([
                name[:3],
                name,
                r["taken_at_local"].isoformat(sep=" ") if r["taken_at_local"] else "",
                round((r["duration_ms"] or 0) / 1000, 1) or "",
                f"{r['width'] or '?'}x{r['height'] or '?'}",
                round((r["bytes"] or 0) / 1048576, 1),
                r["tags"] or "",
            ])

    return JSONResponse({
        "dir": str(out),
        "host_dir": to_host_path(out),
        "items": len(rows) - missing,
        "files": len(written),
        "missing": missing,
        "stale_removed": stale,
        "bytes": sum(r["bytes"] or 0 for _, r in written),
        "dur_ms": sum(r["duration_ms"] or 0 for _, r in written),
        "mode": mode,
        "opened": _open_folder_on_windows(out),   # 宿主模式下直接开了，前端不必再找桥
    })


# ---------------------------------------------------------------- 喜欢 / 回收站

class FavoriteBody(BaseModel):
    favorite: bool


@app.post("/api/assets/{asset_id}/favorite")
def api_favorite(asset_id: str, body: FavoriteBody) -> JSONResponse:
    aid = _asset_id(asset_id)
    rows = q("update assets set favorite=%s where id=%s and not is_deleted returning id",
             (body.favorite, aid))
    if not rows:
        raise HTTPException(404, "asset not found")
    return JSONResponse({"id": aid, "favorite": body.favorite})


class HiddenBody(BaseModel):
    hidden: bool


@app.post("/api/assets/{asset_id}/hidden")
def api_hidden(asset_id: str, body: HiddenBody) -> JSONResponse:
    """软隐藏 / 取消隐藏：默认所有列表视图排除，前端「展示隐藏」开关打开后混入显示。"""
    aid = _asset_id(asset_id)
    rows = q("update assets set hidden=%s where id=%s and not is_deleted returning id",
             (body.hidden, aid))
    if not rows:
        raise HTTPException(404, "asset not found")
    return JSONResponse({"id": aid, "hidden": body.hidden})


@app.post("/api/assets/{asset_id}/trash")
def api_trash(asset_id: str) -> JSONResponse:
    aid = _asset_id(asset_id)
    rows = q("update assets set is_deleted=true, deleted_at=now() "
             "where id=%s and not is_deleted returning id", (aid,))
    if not rows:
        raise HTTPException(404, "asset not found")
    return JSONResponse({"id": aid, "deleted": True})


@app.post("/api/assets/{asset_id}/restore")
def api_restore(asset_id: str) -> JSONResponse:
    aid = _asset_id(asset_id)
    rows = q("update assets set is_deleted=false, deleted_at=null "
             "where id=%s and is_deleted returning id", (aid,))
    if not rows:
        raise HTTPException(404, "asset not in trash")
    return JSONResponse({"id": aid, "deleted": False})


@app.delete("/api/assets/{asset_id}")
def api_purge(asset_id: str) -> JSONResponse:
    """彻底删除：只删库里的资产记录（级联 files/exif/标签）。vault 里的物理文件保留，
    由将来的 `vault purge` 命令统一回收无引用内容——内容寻址存储不允许 Web 误删原件。"""
    aid = _asset_id(asset_id)
    assert pool is not None
    with pool.connection() as conn:
        if not conn.execute("select 1 from assets where id=%s and is_deleted",
                            (aid,)).fetchone():
            raise HTTPException(404, "asset not in trash")
        conn.execute("delete from assets where id=%s", (aid,))
    return JSONResponse({"id": aid, "purged": True})


# ---------------------------------------------------------------- 照片压缩

# 可压缩的照片格式（浏览器原生格式之外加上 HEIC，pillow-heif 已注册）
_COMPRESS_EXT = {".jpg", ".jpeg", ".jfif", ".jpe", ".png", ".webp", ".heic", ".heif"}
_COMPRESS_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jfif": "image/jpeg",
                  ".jpe": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                  ".heic": "image/heic", ".heif": "image/heif"}


def _sha_hex(v: Any) -> str:
    """files.sha256 是 bytea：psycopg 读出来是 bytes。"""
    return v.hex() if isinstance(v, (bytes, bytearray)) else str(v)


class CompressBody(BaseModel):
    """压缩选项（Squoosh 式）：格式 / 质量 / 最长边；全部可省略走默认。"""

    fmt: str = "keep"      # keep = 保持原格式；jpeg / webp = 转换格式
    quality: int = 80      # 40..100（PNG 无损格式忽略）
    max_side: int = 0      # 0 = 不缩放；>0 = 最长边缩到该值（只缩小不放大）


@app.post("/api/assets/{asset_id}/compress")
def api_compress(asset_id: str, body: CompressBody | None = None) -> JSONResponse:
    """照片压缩：按选项重编码，另存为一份新的照片（原件保留不动）。

    副本继承拍摄时间 / 时区 / 设备 / GPS / 标签 / EXIF，落在同一个库里，
    所以会出现在原件旁边的同一日期分组；确认副本没问题后可自行删除。
    输出内容与库里已有文件相同（比如同一张用同样选项压过两次）时直接复用，
    不生成第二份相同副本（duplicate=true）。
    """
    body = body or CompressBody()
    aid = _asset_id(asset_id)
    rows = q(
        """select a.kind, a.library, f.sha256, f.bytes, f.ext, f.rel_path,
                  f.source_id, f.src_path, f.origin_path
             from assets a join files f on f.asset_id = a.id and f.role = 'original'
            where a.id = %s and not a.is_deleted""",
        (aid,),
    )
    if not rows:
        raise HTTPException(404, "asset not found")
    r = rows[0]
    if r["kind"] not in ("photo", "live_photo"):
        raise HTTPException(400, "只能压缩照片")
    src_ext = (r["ext"] or "").lower()
    if src_ext not in _COMPRESS_EXT:
        raise HTTPException(400, f"不支持的格式：{src_ext or '未知'}")

    fmt = body.fmt if body.fmt in ("keep", "jpeg", "webp") else "keep"
    quality = max(40, min(100, int(body.quality or 80)))
    max_side = max(0, int(body.max_side or 0))
    # 转换格式的目标后缀；keep 就沿用原件（png 走无损 optimize，质量参数不适用）
    out_ext = src_ext if fmt == "keep" else f".{fmt}"

    from PIL import Image, ImageOps

    src = cfg.vault_root / r["rel_path"]
    if not src.exists():
        raise HTTPException(404, "file missing on disk")

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)      # 压缩顺带把旋转修正进像素里
        exif = im.info.get("exif")
        icc = im.info.get("icc_profile")
        if max_side > 0 and max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)  # 只缩小不放大
        buf = io.BytesIO()
        if out_ext == ".png":
            im.save(buf, "PNG", optimize=True)
        elif out_ext == ".webp":
            im.save(buf, "WEBP", quality=quality, method=4)
        elif out_ext in (".heic", ".heif"):
            im.save(buf, "HEIF", quality=quality)
        else:
            params: dict[str, Any] = {"quality": quality, "optimize": True}
            if exif:
                params["exif"] = exif
            if icc:
                params["icc_profile"] = icc
            im.save(buf, "JPEG", **params)
        data = buf.getvalue()
        width, height = im.size

    old_bytes = int(r["bytes"] or 0)
    # 省不出 3% 就不生成副本：反复重编码每次都会轻微劣化，没意义
    if len(data) >= old_bytes * 0.97:
        return JSONResponse({"created": False, "skipped": True, "before": old_bytes,
                             "after": old_bytes, "saved": 0})

    new_sha = hashlib.sha256(data).hexdigest()

    # 内容已在库里（同一张压过两次、或恰好有相同内容的文件）→ 复用那份副本
    dup = q("select asset_id from files where sha256=%s and role='original' limit 1",
            (bytes.fromhex(new_sha),))
    if dup:
        return JSONResponse({"created": False, "duplicate": True,
                             "asset_id": str(dup[0]["asset_id"]),
                             "before": old_bytes, "after": len(data),
                             "saved": old_bytes - len(data)})

    rel = hashutil.vault_rel_path(new_sha, out_ext)
    final = cfg.vault_root / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = hashutil.new_landing_path(cfg.landing_root)
    tmp.write_bytes(data)
    os.replace(tmp, final)                # landing 与 vault 同卷，原子

    # 缩略图先建好再写库，避免副本出现后缩略图 404 的窗口期
    if not meta.make_thumb(final, ingest_mod._thumb_path(cfg, new_sha),
                           cfg.thumb_size, cfg.thumb_quality):
        final.unlink(missing_ok=True)
        raise HTTPException(500, "缩略图生成失败，未创建副本")

    assert pool is not None
    with pool.connection() as conn, conn.transaction():
        # 副本继承拍摄信息落到同一个库；live_photo 的副本是静图，不再带 motion
        new_aid = conn.execute(
            """insert into assets
                 (kind, taken_at_local, tz_offset_min, tz_source, taken_at_utc,
                  device_id, lens, width, height, duration_ms, gps, library)
               select 'photo', taken_at_local, tz_offset_min, tz_source, taken_at_utc,
                      device_id, lens, %s, %s, null, gps, library
                 from assets where id = %s
               returning id""",
            (width, height, aid),
        ).fetchone()["id"]
        conn.execute(
            """insert into exif (asset_id, iso, aperture, shutter, focal_mm, orientation, raw)
               select %s, iso, aperture, shutter, focal_mm, orientation, raw
                 from exif where asset_id = %s""",
            (new_aid, aid),
        )
        conn.execute(
            """insert into files (asset_id, role, sha256, bytes, mime, ext, rel_path,
                                  width, height, phash, source_id, src_path,
                                  storage_mode, origin_path, origin_rel)
               values (%s, 'original', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'copy', %s, %s)""",
            (new_aid, bytes.fromhex(new_sha), len(data), _COMPRESS_MIME[out_ext], out_ext, rel,
             width, height, hashutil.average_hash(final),
             r["source_id"], r["src_path"], r["origin_path"], rel),
        )
        # 标签 / 人物关联跟着副本走：删掉原件后组织结构不丢
        conn.execute(
            """insert into asset_tags (asset_id, tag_id)
               select %s, tag_id from asset_tags where asset_id = %s
               on conflict do nothing""",
            (new_aid, aid),
        )
        conn.execute(
            """insert into person_assets (person_id, asset_id)
               select person_id, %s from person_assets where asset_id = %s
               on conflict do nothing""",
            (new_aid, aid),
        )

    return JSONResponse({"created": True, "asset_id": str(new_aid),
                         "before": old_bytes, "after": len(data),
                         "saved": old_bytes - len(data),
                         "ext": out_ext, "width": width, "height": height})


# ---------------------------------------------------------------- 标签

class TagBody(BaseModel):
    name: str


class BatchBody(BaseModel):
    action: str                 # favorite | unfavorite | hide | unhide | trash | restore | move | tag | untag
    ids: list[str]              #        | set_folder | clear_folder（folder_id 见下）
    tag: str | None = None
    library: str | None = None  # move 时必填：gallery | material
    folder_id: str | None = None  # set_folder 时必填：文件夹 id（和素材同一个库）


class AnalyzeBody(BaseModel):
    ids: list[str]


# 入队时跳过"已经排过 / 正在跑 / 已识别"的：重复点「AI 识别」不该把队列撑大。
# library 参数用来限定库：素材库不做人脸识别（faces 任务只认 gallery）。
_ENQUEUE_JOB_SQL = """
insert into jobs (kind, payload)
select %s, jsonb_build_object('asset_id', a.id::text)
from assets a
where a.id = any(%s::uuid[]) and not a.is_deleted
  and (%s::text is null or a.kind = %s::text)
  and (%s::text is null or a.library = %s::text)
  and not exists (
    select 1 from jobs j
    where j.kind = %s and j.state in ('pending', 'running', 'ok')
      and j.payload->>'asset_id' = a.id::text
  )
"""


def _ensure_tag(conn: Any, name: str) -> int:
    conn.execute("insert into tags(name) values(%s) on conflict (name) do nothing", (name,))
    return conn.execute("select id from tags where name=%s", (name,)).fetchone()["id"]


@app.get("/api/tags")
def api_tags(limit: int = 400) -> JSONResponse:
    limit = max(1, min(limit, 5000))
    rows = q(
        """select t.id, t.name,
                  (select count(*) from asset_tags at2
                     join assets a on a.id = at2.asset_id
                   where at2.tag_id = t.id and not a.is_deleted) c
           from tags t order by c desc, t.name limit %s""",
        (limit,),
    )
    return JSONResponse({"tags": [{"id": r["id"], "name": r["name"], "count": r["c"]} for r in rows]})


@app.post("/api/assets/{asset_id}/tags")
def api_add_tag(asset_id: str, body: TagBody) -> JSONResponse:
    aid = _asset_id(asset_id)
    name = _clean_tag(body.name)
    assert pool is not None
    with pool.connection() as conn:
        if not conn.execute("select 1 from assets where id=%s and not is_deleted", (aid,)).fetchone():
            raise HTTPException(404, "asset not found")
        tid = _ensure_tag(conn, name)
        conn.execute("insert into asset_tags(asset_id, tag_id) values(%s,%s) "
                     "on conflict (asset_id, tag_id) do nothing", (aid, tid))
        tags = [r["name"] for r in conn.execute(
            """select t.name from asset_tags at2 join tags t on t.id=at2.tag_id
               where at2.asset_id=%s order by t.name""", (aid,)).fetchall()]
    return JSONResponse({"id": aid, "tags": tags})


@app.delete("/api/assets/{asset_id}/tags/{name}")
def api_remove_tag(asset_id: str, name: str) -> JSONResponse:
    aid = _asset_id(asset_id)
    assert pool is not None
    with pool.connection() as conn:
        conn.execute("""delete from asset_tags at2 using tags t
                        where at2.tag_id = t.id and at2.asset_id=%s and t.name=%s""",
                     (aid, name))
        tags = [r["name"] for r in conn.execute(
            """select t.name from asset_tags at2 join tags t on t.id=at2.tag_id
               where at2.asset_id=%s order by t.name""", (aid,))]
    return JSONResponse({"id": aid, "tags": tags})


@app.post("/api/assets/batch")
def api_batch(body: BatchBody) -> JSONResponse:
    ids = _validate_ids(body.ids)
    if not ids:
        return JSONResponse({"updated": 0})
    assert pool is not None
    with pool.connection() as conn:
        if body.action == "favorite":
            n = conn.execute("update assets set favorite=true "
                             "where id=any(%s::uuid[]) and not is_deleted", (ids,)).rowcount
        elif body.action == "unfavorite":
            n = conn.execute("update assets set favorite=false "
                             "where id=any(%s::uuid[]) and not is_deleted", (ids,)).rowcount
        elif body.action == "hide":
            n = conn.execute("update assets set hidden=true "
                             "where id=any(%s::uuid[]) and not is_deleted", (ids,)).rowcount
        elif body.action == "unhide":
            n = conn.execute("update assets set hidden=false "
                             "where id=any(%s::uuid[]) and not is_deleted", (ids,)).rowcount
        elif body.action == "trash":
            n = conn.execute("update assets set is_deleted=true, deleted_at=now() "
                             "where id=any(%s::uuid[]) and not is_deleted", (ids,)).rowcount
        elif body.action == "restore":
            n = conn.execute("update assets set is_deleted=false, deleted_at=null "
                             "where id=any(%s::uuid[]) and is_deleted", (ids,)).rowcount
        elif body.action == "move":
            if body.library not in ("gallery", "material"):
                raise HTTPException(400, "移动目标只能是 gallery 或 material")
            # 换库 = 重新整理：文件夹跟库走，挂在新库上只会是脏数据，回到「未整理」
            n = conn.execute("update assets set library=%s, folder_id=null "
                             "where id=any(%s::uuid[]) and not is_deleted",
                             (body.library, ids)).rowcount
            if body.library == "material":
                # 素材库不做人脸识别：已有的脸和排队中的脸任务一起清掉，
                # 人物页 / 人像检索里就不会再出现素材库的东西。
                conn.execute("delete from faces where asset_id = any(%s::uuid[])", (ids,))
                conn.execute(
                    """delete from jobs
                        where kind = 'faces' and state in ('pending', 'running')
                          and payload->>'asset_id' = any(%s::text[])""",
                    ([str(i) for i in ids],),
                )
        elif body.action in ("tag", "untag"):
            name = _clean_tag(body.tag or "")
            tid = _ensure_tag(conn, name)
            if body.action == "tag":
                conn.execute("""insert into asset_tags(asset_id, tag_id)
                                select id, %s from assets
                                where id=any(%s::uuid[]) and not is_deleted
                                on conflict (asset_id, tag_id) do nothing""", (tid, ids))
                n = conn.execute("select count(*) c from assets "
                                 "where id=any(%s::uuid[]) and not is_deleted", (ids,)).fetchone()["c"]
            else:
                n = conn.execute("""delete from asset_tags at2 using assets a
                                    where at2.asset_id=a.id and a.id=any(%s::uuid[])
                                      and at2.tag_id=%s""", (ids, tid)).rowcount
        elif body.action == "set_folder":
            if not body.folder_id:
                raise HTTPException(400, "set_folder 需要 folder_id")
            fid = _asset_id(body.folder_id)
            # 在事务里用同一个连接查（嵌套 q() 会从池里再抓一条连接）
            folder = conn.execute("select library from folders where id=%s::uuid",
                                  (fid,)).fetchone()
            if not folder:
                raise HTTPException(404, "文件夹不存在")
            # 只动和文件夹同一个库的资产：两库各有各的文件夹，不能串库
            n = conn.execute(
                "update assets set folder_id=%s "
                "where id=any(%s::uuid[]) and not is_deleted and library=%s",
                (fid, ids, folder["library"]),
            ).rowcount
        elif body.action == "clear_folder":
            n = conn.execute(
                "update assets set folder_id=null "
                "where id=any(%s::uuid[]) and not is_deleted",
                (ids,),
            ).rowcount
        else:
            raise HTTPException(400, "unknown action")
    return JSONResponse({"updated": n})


@app.post("/api/assets/analyze")
def api_analyze(body: AnalyzeBody) -> JSONResponse:
    """把选中资产排进 AI 队列（RAM++ 打标 / 人脸 / 视频关键帧）。

    素材库不需要第二套模型：换归属只改筛选轴，标签、人脸、视频摘要原样复用。
    但"从相册移过来的素材还没打标"是真实场景，所以给一个显式的入队入口。
    """
    ids = _validate_ids(body.ids)
    if not ids:
        raise HTTPException(400, "没有有效的资产 id")
    by_kind: dict[str, int] = {}
    assert pool is not None
    with pool.connection() as conn:
        for kind, asset_kind in (("analyze", None), ("faces", None), ("video", "video")):
            # 素材库不做人脸识别；打标 / 视频摘要仍然排
            lib = "gallery" if kind == "faces" else None
            cur = conn.execute(_ENQUEUE_JOB_SQL,
                               (kind, ids, asset_kind, asset_kind, lib, lib, kind))
            by_kind[kind] = cur.rowcount
    total = sum(by_kind.values())
    return JSONResponse({"queued": total, "by_kind": by_kind})


# ---------------------------------------------------------------- 文件夹（每个库一套）
# 时间轴只回答"什么时候存的"，剪辑时找素材靠的是"这批素材属于哪个项目"。
# 文件夹就是用户手动整理的项目维度：建文件夹 → 选中素材归进去。
# 图库和素材库各有自己的文件夹（folders.library），素材与文件夹单归属
# （assets.folder_id），删文件夹时素材自动回到「未整理」。

class FolderBody(BaseModel):
    name: str
    library: str = "material"  # gallery | material：建在哪个库


def _folder_list_sql(show_hidden: bool) -> str:
    """文件夹列表的计数 / 封面。

    show_hidden=False（默认，「展示隐藏」开关关闭）时：计数和封面都跳过隐藏素材——
    封面不能是一张默认看不到的照片；内容全被隐藏的文件夹由 api_folders 整体过滤掉。
    n_total 永远不含 hidden 条件，前端用它区分「全隐藏」（count=0 且 total>0）
    和「真空文件夹」（total=0，建了还没放素材，照常显示）。
    """
    vis = "" if show_hidden else " and not a.hidden"
    return f"""
select fo.id, fo.name, fo.library, fo.created_at, fo.updated_at,
       (select count(*) from assets a
         where a.folder_id = fo.id and not a.is_deleted{vis}
           and a.library = fo.library) n_assets,
       (select count(*) from assets a
         where a.folder_id = fo.id and not a.is_deleted
           and a.library = fo.library) n_total,
       -- 必须 encode：sha256 列是 bytea，直接 select 出来是 Python bytes，
       -- 塞进 f-string 会变成 b"\\x.." 的 repr，拼出的封面 URL 必然 404
       (select encode(f.sha256, 'hex') from assets a
          join files f on f.asset_id = a.id and f.role = 'original'
         where a.folder_id = fo.id and not a.is_deleted{vis} and a.library = fo.library
         order by a.taken_at_utc desc nulls last limit 1) cover_sha
  from folders fo
"""


def _folder_json(r: dict[str, Any]) -> dict[str, Any]:
    cover_sha = r["cover_sha"]
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "library": r["library"],
        # 「展示隐藏」关闭时 n_assets 是可见计数（不含隐藏素材）
        "count": r["n_assets"],
        # 不含 hidden 条件的总数：前端据此把「内容全隐藏」的文件夹从网格里摘掉
        "total": r["n_total"],
        # 封面 = 文件夹里最新一条（可见）素材的缩略图；空文件夹、或缩略图还没生成的都不给 URL，
        # 前端用 🗂 占位 —— 总比一张 404 的破图好
        "cover": (f"/thumbs/{cover_sha}.webp" if cover_sha and _thumb_ok(cover_sha) else None),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _clean_library(value: str | None) -> str:
    if value not in ("gallery", "material"):
        raise HTTPException(400, "library 只能是 gallery 或 material")
    return value


def _clean_folder_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise HTTPException(400, "文件夹名需为 1-40 个字符")
    return name


@app.get("/api/folders")
def api_folders(library: str = "material", show_hidden: bool = False) -> JSONResponse:
    lib = _clean_library(library)
    # 新建的排前面：整理素材时刚建的文件夹最常用
    rows = q(f"{_folder_list_sql(show_hidden)} where fo.library = %s order by fo.created_at desc",
             (lib,))
    # 「展示隐藏」关闭：内容全被隐藏的文件夹跟着隐藏；真空文件夹（还没放过素材）照常显示
    if not show_hidden:
        rows = [r for r in rows if r["n_assets"] > 0 or r["n_total"] == 0]
    unfiled = q(
        """select count(*) c from assets
            where library = %s and folder_id is null and not is_deleted"""
        + ("" if show_hidden else " and not hidden"),
        (lib,),
    )[0]["c"]
    return JSONResponse({"folders": [_folder_json(r) for r in rows], "unfiled": unfiled})


@app.post("/api/folders")
def api_folder_create(body: FolderBody, show_hidden: bool = False) -> JSONResponse:
    name = _clean_folder_name(body.name)
    lib = _clean_library(body.library)
    # 同名只在本库内查重：图库和素材库可以各有一个「毕业旅拍」
    taken = q("select 1 from folders where lower(name) = lower(%s) and library = %s",
              (name, lib))
    if taken:
        raise HTTPException(409, "这个库里已经有同名文件夹了")
    row = q("insert into folders (name, library) values (%s, %s) returning id",
            (name, lib))[0]
    out = q(f"select * from ({_folder_list_sql(show_hidden)}) t where t.id = %s", (row["id"],))[0]
    return JSONResponse(_folder_json(out))


@app.patch("/api/folders/{folder_id}")
def api_folder_rename(folder_id: str, body: FolderBody, show_hidden: bool = False) -> JSONResponse:
    fid = _asset_id(folder_id)
    name = _clean_folder_name(body.name)
    folder = q("select library from folders where id=%s::uuid", (fid,))
    if not folder:
        raise HTTPException(404, "文件夹不存在")
    taken = q("""select 1 from folders
                  where lower(name) = lower(%s) and library = %s and id <> %s::uuid""",
              (name, folder[0]["library"], fid))
    if taken:
        raise HTTPException(409, "这个库里已经有同名文件夹了")
    rows = q("update folders set name=%s, updated_at=now() "
             "where id=%s::uuid returning id", (name, fid))
    if not rows:
        raise HTTPException(404, "文件夹不存在")
    out = q(f"select * from ({_folder_list_sql(show_hidden)}) t where t.id = %s", (fid,))[0]
    return JSONResponse(_folder_json(out))


@app.delete("/api/folders/{folder_id}")
def api_folder_delete(folder_id: str) -> JSONResponse:
    """删文件夹只删登记：素材回到「未整理」（FK on delete set null），原件不动。"""
    fid = _asset_id(folder_id)
    assert pool is not None
    with pool.connection() as conn:
        n = conn.execute("delete from folders where id=%s::uuid", (fid,)).rowcount
    if not n:
        raise HTTPException(404, "文件夹不存在")
    return JSONResponse({"deleted": n})


# ---------------------------------------------------------------- 人脸队列控制
# web 只负责入队和启停标记：真正的提取在宿主机 worker 里跑（ml.worker）。
# 暂停 = ml_meta.faces_paused='1'，worker 每次认领前检查——正在跑的批次收尾后
# 不再取新任务，pending 全部原样保留，继续时接着跑。聚类由 worker 在队列清空后自动做。

_FACES_QUEUE_SQL = """
insert into jobs (kind, payload)
select 'faces', jsonb_build_object('asset_id', a.id::text)
from assets a
where not a.is_deleted and a.library = 'gallery'
  and not exists (
    select 1 from jobs j
    where j.kind = 'faces' and j.state in ('pending', 'running', 'ok')
      and j.payload->>'asset_id' = a.id::text
  )
"""

_SET_FACES_PAUSED_SQL = """
insert into ml_meta (key, value, updated_at) values ('faces_paused', %s, now())
on conflict (key) do update set value = excluded.value, updated_at = now()
"""


@app.get("/api/faces/queue")
def api_faces_queue() -> JSONResponse:
    paused = q("select value from ml_meta where key = 'faces_paused'")
    by_state = {r["state"]: r["c"] for r in q(
        "select state, count(*) c from jobs where kind = 'faces' group by state")}
    # 进度按「图库资产里已有 ok 任务的」算：素材库不跑人脸，dead 允许重排不算完成
    total = q("select count(*) c from assets "
              "where not is_deleted and library = 'gallery'")[0]["c"]
    done = q("""select count(*) c from assets a
                where not a.is_deleted and a.library = 'gallery'
                  and exists (select 1 from jobs j
                               where j.kind = 'faces' and j.state = 'ok'
                                 and j.payload->>'asset_id' = a.id::text)""")[0]["c"]
    return JSONResponse({
        "paused": bool(paused and paused[0]["value"] == "1"),
        "pending": by_state.get("pending", 0),
        "running": by_state.get("running", 0),
        "ok": by_state.get("ok", 0),
        "failed": by_state.get("failed", 0) + by_state.get("dead", 0),
        "assets_total": total,
        "assets_done": done,
    })


class FacesRunBody(BaseModel):
    """可选 asset_id：只对这一张手动入队（灯箱「识别人脸」按钮用）。"""

    asset_id: str | None = None


@app.post("/api/faces/queue/run")
def api_faces_run(body: FacesRunBody | None = None) -> JSONResponse:
    """人脸识别入队，顺带清掉暂停标记。

    不带 asset_id 就是全量入队（人物页「开始识别」），幂等：已排队 / 已完成的
    资产不重复，且只排图库——素材库不做批量人脸识别。
    带 asset_id 只对这一张入队，是用户点名要识别：payload 带 manual，worker 不分库
    照常识别；已经有 ok 任务也照样再排一次（上次没检出脸的、移库时脸被清掉的都得能
    重跑），只在它正排队 / 正在跑时不重复。
    dead 任务不拦着重新入队（换模型重跑的场景）。
    """
    assert pool is not None
    with pool.connection() as conn:
        if body and body.asset_id:
            aid = _asset_id(body.asset_id)
            if not conn.execute("select 1 from assets where id = %s::uuid and not is_deleted",
                                (aid,)).fetchone():
                raise HTTPException(404, "资产不存在")
            # priority=1 插队：认领按 priority, id 排，不插队的话这一张会排在
            # 全量打标 / 识别的几千个任务后面，用户点了要干等
            cur = conn.execute(
                """insert into jobs (kind, priority, payload)
                   select 'faces', 1, jsonb_build_object('asset_id', %s::text, 'manual', true)
                   where not exists (
                     select 1 from jobs j
                     where j.kind = 'faces' and j.state in ('pending', 'running')
                       and j.payload->>'asset_id' = %s::text
                   )""",
                (aid, aid),
            )
        else:
            cur = conn.execute(_FACES_QUEUE_SQL)
        conn.execute(_SET_FACES_PAUSED_SQL, ("0",))
        queued = cur.rowcount
    return JSONResponse({"queued": queued})


class FacesPauseBody(BaseModel):
    paused: bool


@app.post("/api/faces/queue/pause")
def api_faces_pause(body: FacesPauseBody) -> JSONResponse:
    assert pool is not None
    with pool.connection() as conn:
        conn.execute(_SET_FACES_PAUSED_SQL, ("1" if body.paused else "0",))
    return JSONResponse({"paused": body.paused})


# ---------------------------------------------------------------- 人物 / 人脸

def _clean_person_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 30:
        raise HTTPException(400, "名字需为 1-30 个字符")
    return name


def _refresh_cover(conn: Any, person_id: str) -> None:
    """封面没设、指向已删除/已忽略的脸、或指向已移到别人名下的脸时，重选一张。"""
    conn.execute(
        """update persons p
              set cover_face_id = (
                    select f.id from faces f where f.person_id = p.id
                      and not f.ignored
                     order by coalesce(f.quality, 0) desc, f.det_score desc limit 1),
                  updated_at = now()
            where p.id = %s::uuid
              and (p.cover_face_id is null
                   or not exists (select 1 from faces c where c.id = p.cover_face_id
                                  and c.person_id = p.id)
                   or exists (select 1 from faces c where c.id = p.cover_face_id
                              and c.ignored))""",
        (person_id,),
    )


@app.get("/api/persons")
def api_persons() -> JSONResponse:
    # n_assets = 检出脸的素材 ∪ 手动关联的素材（person_assets，见 db/018_person_links.sql）；
    # 只有关联没有脸的人物也在列表里（识别不出来但用户挂了素材的）
    rows = q(
        """select p.id, coalesce(p.name, '') name, p.cover_face_id, p.custom_cover,
                  p.backup_cover,
                  (select count(*) from faces f join assets a on a.id = f.asset_id
                    where f.person_id = p.id and not a.is_deleted
                      and not f.ignored) n_faces,
                  (select count(*) from (
                     select f.asset_id from faces f
                       join assets a on a.id = f.asset_id
                      where f.person_id = p.id and not a.is_deleted and not f.ignored
                     union
                     select k.asset_id from person_assets k
                       join assets a on a.id = k.asset_id
                      where k.person_id = p.id and not a.is_deleted) s) n_assets
             from persons p
            order by n_assets desc, length(coalesce(p.name, '')), coalesce(p.name, ''),
                     p.created_at"""
    )
    unassigned = q(
        """select count(*) c from faces f join assets a on a.id = f.asset_id
            where f.person_id is null and not a.is_deleted and not f.ignored"""
    )[0]["c"]
    return JSONResponse({
        "persons": [{
            "id": str(r["id"]),
            "name": r["name"],
            "cover": str(r["cover_face_id"]) if r["cover_face_id"] else None,
            "custom_cover": r["custom_cover"],
            "backup_cover": r["backup_cover"],
            "n_faces": r["n_faces"],
            "n_assets": r["n_assets"],
        } for r in rows if r["n_assets"] > 0],
        "unassigned_faces": unassigned,
    })


class PersonNameBody(BaseModel):
    name: str


@app.patch("/api/persons/{person_id}")
def api_rename_person(person_id: str, body: PersonNameBody) -> JSONResponse:
    pid = _asset_id(person_id)
    name = _clean_person_name(body.name)
    taken = q("select 1 from persons where lower(name) = lower(%s) and id <> %s::uuid",
              (name, pid))
    if taken:
        raise HTTPException(409, "这个名字已经有人用了")
    rows = q("update persons set name = %s, updated_at = now() "
             "where id = %s::uuid returning id, name", (name, pid))
    if not rows:
        raise HTTPException(404, "person not found")
    return JSONResponse({"id": pid, "name": rows[0]["name"]})


class MergeBody(BaseModel):
    into: str


@app.post("/api/persons/{person_id}/merge")
def api_merge_person(person_id: str, body: MergeBody) -> JSONResponse:
    """把人物 A 的所有脸并给人物 B（聚类拆散后的手工补救）。"""
    src, dst = _asset_id(person_id), _asset_id(body.into)
    if src == dst:
        raise HTTPException(400, "不能合并到自己")
    assert pool is not None
    with pool.connection() as conn:
        if not conn.execute("select 1 from persons where id = %s::uuid", (dst,)).fetchone():
            raise HTTPException(404, "目标人物不存在")
        moved = conn.execute("update faces set person_id = %s::uuid "
                             "where person_id = %s::uuid", (dst, src)).rowcount
        # 手动关联也一起搬过去：合并后素材还挂在合并后的人物名下
        conn.execute("""insert into person_assets (person_id, asset_id)
                        select %s::uuid, asset_id from person_assets
                         where person_id = %s::uuid
                        on conflict (person_id, asset_id) do nothing""", (dst, src))
        conn.execute("delete from persons where id = %s::uuid", (src,))
        _refresh_cover(conn, dst)
    return JSONResponse({"into": dst, "faces": moved})


@app.delete("/api/persons/{person_id}")
def api_dissolve_person(person_id: str) -> JSONResponse:
    """取消归类：脸回到"未归类"，下次 ml.cluster 会重新分。"""
    pid = _asset_id(person_id)
    assert pool is not None
    with pool.connection() as conn:
        n = conn.execute("update faces set person_id = null "
                         "where person_id = %s::uuid", (pid,)).rowcount
        conn.execute("delete from persons where id = %s::uuid", (pid,))
    return JSONResponse({"id": pid, "faces": n})


# ---- 人物封面：上传的自定义图，两张（derived/person_covers/）：
#      slot=1 主封面 {id}.jpg，slot=2 备用封面 {id}_2.jpg；没传就回退人脸裁剪

_PERSON_COVER_MAX = 20 * 1024 * 1024  # 上传图上限 20MB


def _person_cover_path(person_id: str, slot: int = 1) -> Path:
    return cfg.derived_root / "person_covers" / (f"{person_id}.jpg" if slot == 1
                                                 else f"{person_id}_2.jpg")


def _cover_slot(slot: int) -> int:
    if slot not in (1, 2):
        raise HTTPException(400, "slot 只能是 1（主封面）或 2（备用封面）")
    return slot


def _person_out(person_id: str) -> dict[str, Any]:
    """单个人物的 JSON（封面接口用）；不存在时 404。"""
    r = q(
        """select p.id, coalesce(p.name, '') name, p.cover_face_id, p.custom_cover,
                  p.backup_cover,
                  (select count(*) from faces f join assets a on a.id = f.asset_id
                    where f.person_id = p.id and not a.is_deleted
                      and not f.ignored) n_faces,
                  (select count(*) from (
                     select f.asset_id from faces f
                       join assets a on a.id = f.asset_id
                      where f.person_id = p.id and not a.is_deleted and not f.ignored
                     union
                     select k.asset_id from person_assets k
                       join assets a on a.id = k.asset_id
                      where k.person_id = p.id and not a.is_deleted) s) n_assets
             from persons p
            where p.id = %s::uuid""",
        (person_id,),
    )
    if not r:
        raise HTTPException(404, "person not found")
    row = r[0]
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "cover": str(row["cover_face_id"]) if row["cover_face_id"] else None,
        "custom_cover": row["custom_cover"],
        "backup_cover": row["backup_cover"],
        "n_faces": row["n_faces"],
        "n_assets": row["n_assets"],
    }


def _save_person_cover(data: bytes, person_id: str, slot: int) -> None:
    """PIL 解码校验 + 居中裁方（≤640px）→ 临时文件写完 os.replace 原子落盘。

    阻塞，走 run_in_threadpool。文件在 derived（可再生），库里只存
    custom_cover / backup_cover 开关：文件丢了前端就回退到人脸裁剪。
    """
    import io
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            side = min(min(im.size), 640)
            out = ImageOps.fit(im, (side, side))
    except Exception:
        raise HTTPException(400, "图片格式无法识别，换一张 JPG / PNG / WebP 试试")
    dst = _person_cover_path(person_id, slot)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    out.save(tmp, "JPEG", quality=88)
    os.replace(tmp, dst)


@app.post("/api/persons/{person_id}/cover")
async def api_upload_person_cover(person_id: str, request: Request,
                                  slot: int = Query(1)) -> JSONResponse:
    """原始字节流上传（和 /api/upload 一个路数，不走 multipart）。

    body = 图片内容。slot=1 主封面、slot=2 备用封面。服务端居中裁成方形
    压到 ≤640px 存 JPEG，对应开关置 true，之后前端从 /covers/{id}.jpg 取图。
    """
    pid = _asset_id(person_id)
    _cover_slot(slot)
    _person_out(pid)  # 人物存在性检查：不存在直接 404，不写文件
    buf = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        buf += chunk
        if len(buf) > _PERSON_COVER_MAX:
            raise HTTPException(413, "图片太大（上限 20MB）")
    if not buf:
        raise HTTPException(400, "空文件")
    await run_in_threadpool(_save_person_cover, bytes(buf), pid, slot)
    flag = "custom_cover" if slot == 1 else "backup_cover"
    rows = q(f"update persons set {flag} = true, updated_at = now() "
             "where id = %s::uuid returning id", (pid,))
    if not rows:
        raise HTTPException(404, "person not found")
    return JSONResponse(_person_out(pid))


@app.delete("/api/persons/{person_id}/cover")
def api_reset_person_cover(person_id: str, slot: int = Query(1)) -> JSONResponse:
    """删掉某一张自定义封面（slot=1 主 / slot=2 备用），另一张不受影响。"""
    pid = _asset_id(person_id)
    _cover_slot(slot)
    flag = "custom_cover" if slot == 1 else "backup_cover"
    rows = q(f"update persons set {flag} = false, updated_at = now() "
             "where id = %s::uuid returning id", (pid,))
    if not rows:
        raise HTTPException(404, "person not found")
    _person_cover_path(pid, slot).unlink(missing_ok=True)
    return JSONResponse(_person_out(pid))


# ---- 人脸忽略：误检/杂物脸移入"忽略"，重跑识别不再回来（见 db/017_ignore_faces.sql）

IGNORE_SIM = 0.8  # 新检出脸跟忽略规则的余弦相似度达到它就自动忽略


class FaceIdsBody(BaseModel):
    ids: list[str]


def _uuid_list(raw: list[str]) -> list[str]:
    out: list[str] = []
    for x in raw:
        try:
            out.append(str(uuidlib.UUID(x)))
        except ValueError:
            raise HTTPException(400, f"bad uuid: {x}") from None
    return list(dict.fromkeys(out))  # 去重保序


@app.get("/api/persons/{person_id}/faces")
def api_person_faces(person_id: str) -> JSONResponse:
    """人物的全部脸（「人脸」抽屉用）：裁剪图走 /faces/{id}.jpg，已忽略的排后面。

    kind + sha 给 hover 预览用：/thumbs/{sha}.webp 看原图海报，/file/{asset_id} 播视频。
    ext/bytes/width/height/duration_ms 给预览框的简要信息行。
    """
    pid = _asset_id(person_id)
    rows = q(
        """select f.id, f.asset_id, f.ignored, f.det_score, f.quality,
                  a.taken_at_local, a.kind, a.width, a.height, a.duration_ms,
                  fo.ext, fo.bytes, fo.rel_path
             from faces f join assets a on a.id = f.asset_id
             left join files fo on fo.asset_id = a.id and fo.role = 'original'
            where f.person_id = %s::uuid and not a.is_deleted
            order by f.ignored,
                     coalesce(a.taken_at_local, a.created_at::timestamp) desc nulls last,
                     f.det_score desc""",
        (pid,),
    )
    return JSONResponse({"faces": [{
        "id": str(r["id"]),
        "asset_id": str(r["asset_id"]),
        "ignored": r["ignored"],
        "det_score": float(r["det_score"]),
        "quality": float(r["quality"]) if r["quality"] is not None else None,
        "taken": r["taken_at_local"].isoformat() if r["taken_at_local"] else None,
        "kind": r["kind"],
        "sha": Path(r["rel_path"]).stem if r["rel_path"] else None,
        "ext": r["ext"],
        "bytes": int(r["bytes"] or 0),
        "width": r["width"],
        "height": r["height"],
        "duration_ms": r["duration_ms"],
    } for r in rows]})


@app.post("/api/persons/{person_id}/faces/ignore")
def api_ignore_person_faces(person_id: str, body: FaceIdsBody) -> JSONResponse:
    """把若干脸标记为忽略，并按 embedding 记住规则：
    重跑识别再检出同一张脸（相似度 >= 0.8）会自动忽略。

    人物剩 0 张未忽略脸时列表里自然消失（n_assets=0 被过滤），
    脸仍挂着 person_id，恢复接口能把它们找回来。
    """
    pid = _asset_id(person_id)
    ids = _uuid_list(body.ids)
    if not ids:
        raise HTTPException(400, "没有选择任何脸")
    assert pool is not None
    with pool.connection() as conn:
        rows = conn.execute(
            """select f.id, f.asset_id, f.embedding::text emb, f.model
                 from faces f
                where f.person_id = %s::uuid and f.id = any(%s::uuid[])
                  and not f.ignored""",
            (pid, ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(400, "有些脸不存在、已忽略或不属于这个人物")
        conn.execute("update faces set ignored = true where id = any(%s::uuid[])", (ids,))
        for r in rows:
            conn.execute(
                """insert into ignored_faces (embedding, model, face_id, asset_id)
                   values (%s::vector, %s, %s::uuid, %s::uuid)""",
                (r["emb"], r["model"], r["id"], r["asset_id"]),
            )
        conn.execute("update persons set updated_at = now() where id = %s::uuid", (pid,))
        _refresh_cover(conn, pid)
    return JSONResponse({**_person_out(pid), "ignored": len(rows)})


@app.post("/api/persons/{person_id}/faces/restore")
def api_restore_person_faces(person_id: str, body: FaceIdsBody) -> JSONResponse:
    """恢复被忽略的脸：去掉 ignored 标记，同时删掉匹配的忽略规则
    （按 embedding 相似度匹配删，规则里的 face_id 可能是重跑前的旧 id）。"""
    pid = _asset_id(person_id)
    ids = _uuid_list(body.ids)
    if not ids:
        raise HTTPException(400, "没有选择任何脸")
    assert pool is not None
    with pool.connection() as conn:
        owned = conn.execute(
            """select f.id from faces f
                where f.person_id = %s::uuid and f.id = any(%s::uuid[])
                  and f.ignored""",
            (pid, ids),
        ).fetchall()
        restored = [str(r["id"]) for r in owned]
        if restored:
            conn.execute("update faces set ignored = false "
                         "where id = any(%s::uuid[])", (restored,))
            conn.execute(
                """delete from ignored_faces g
                    where exists (select 1 from faces f
                                   where f.id = any(%s::uuid[])
                                     and 1 - (g.embedding <=> f.embedding) >= %s)""",
                (restored, IGNORE_SIM),
            )
            _refresh_cover(conn, pid)
    return JSONResponse({"restored": len(restored), "person": _person_out(pid)})


class MoveFacesBody(BaseModel):
    ids: list[str]
    into: str


@app.post("/api/persons/{person_id}/faces/move")
def api_move_person_faces(person_id: str, body: MoveFacesBody) -> JSONResponse:
    """把若干脸从当前人物移到另一个人物——聚类拆散/并错的手工修正。

    移过去的脸保持原有归属（聚类只处理未归类的脸，不会再抢走）；
    两边人物各重选封面。已忽略的脸不支持移动（先恢复再移）。
    """
    pid = _asset_id(person_id)
    into = _asset_id(body.into)
    ids = _uuid_list(body.ids)
    if not ids:
        raise HTTPException(400, "没有选择任何脸")
    if into == pid:
        raise HTTPException(400, "不能移动到当前人物自己")
    assert pool is not None
    with pool.connection() as conn:
        if not conn.execute("select 1 from persons where id = %s::uuid",
                            (into,)).fetchone():
            raise HTTPException(404, "目标人物不存在")
        rows = conn.execute(
            """select f.id from faces f
                where f.person_id = %s::uuid and f.id = any(%s::uuid[])
                  and not f.ignored""",
            (pid, ids),
        ).fetchall()
        moved = [str(r["id"]) for r in rows]
        if len(moved) != len(ids):
            raise HTTPException(400, "有些脸不存在、已忽略或不属于这个人物")
        conn.execute("update faces set person_id = %s::uuid "
                     "where id = any(%s::uuid[])", (into, moved))
        _refresh_cover(conn, pid)
        _refresh_cover(conn, into)
    return JSONResponse({"moved": len(moved), "from": _person_out(pid),
                         "into": _person_out(into)})


class AssignFacesBody(BaseModel):
    ids: list[str]
    person_id: str


@app.post("/api/faces/assign")
def api_assign_faces(body: AssignFacesBody) -> JSONResponse:
    """把人脸挂到某个人物——灯箱里 ✎ 修归属用。

    不看这张脸现在归谁：未归类的直接挂上；已经挂在别人名下的改挂过来（等同 move，
    两边人物各重选封面）；已经在目标人物名下的原样成功。网页手里的归属可能是旧的
    ——worker 的聚类随时会把未归类的脸认领走——按旧状态分 assign / move 两条路会撞
    「已归类」。只有不存在或已忽略的脸不收（忽略的先恢复再归类）。
    """
    pid = _asset_id(body.person_id)
    ids = _uuid_list(body.ids)
    if not ids:
        raise HTTPException(400, "没有选择任何脸")
    assert pool is not None
    with pool.connection() as conn:
        if not conn.execute("select 1 from persons where id = %s::uuid",
                            (pid,)).fetchone():
            raise HTTPException(404, "目标人物不存在")
        rows = conn.execute(
            """select f.id, f.person_id from faces f
                where f.id = any(%s::uuid[]) and not f.ignored""",
            (ids,),
        ).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(400, "有些脸不存在或已忽略")
        conn.execute("update faces set person_id = %s::uuid "
                     "where id = any(%s::uuid[])", (pid, ids))
        # 改挂会让原人物的封面/计数失效，目标人物也一样
        for touched in {str(r["person_id"]) for r in rows if r["person_id"]} | {pid}:
            _refresh_cover(conn, touched)
    return JSONResponse({"assigned": len(rows), "person": _person_out(pid)})


# ---- 人物关联素材：没检出人脸（背影/远景）但确实与人物有关的照片视频，手动挂到人物名下。
#      与 faces 解耦（见 db/018_person_links.sql）：重跑识别、聚类都不动关联；
#      人物页照片流 / 计数 / 按人名搜索把关联素材一起算。


def _asset_persons_out(asset_id: str) -> list[dict[str, Any]]:
    """这张媒体的关联人物 + 检出脸的人物（前端只渲染前者，后者已在脸 chips 里）。

    source: link = 手动关联；face = 这张图里检出过此人未忽略的脸（识别事实，不可取消，
    只会随识别变化）。一张素材同时存在两种时以 face 为准显示。
    """
    rows = q(
        """select p.id, coalesce(p.name, '') name,
                  (exists (select 1 from faces f where f.asset_id = %s::uuid
                            and f.person_id = p.id and not f.ignored)) has_face
             from (
               select pa.person_id from person_assets pa where pa.asset_id = %s::uuid
               union
               select f.person_id from faces f
                where f.asset_id = %s::uuid and f.person_id is not null and not f.ignored
             ) s join persons p on p.id = s.person_id
            order by has_face desc, p.name""",
        (asset_id, asset_id, asset_id),
    )
    return [{
        "id": str(r["id"]),
        "name": r["name"],
        "source": "face" if r["has_face"] else "link",
    } for r in rows]


@app.get("/api/assets/{asset_id}/persons")
def api_asset_persons(asset_id: str) -> JSONResponse:
    return JSONResponse({"persons": _asset_persons_out(_asset_id(asset_id))})


class LinkPersonBody(BaseModel):
    person_id: str


@app.post("/api/assets/{asset_id}/persons")
def api_link_person(asset_id: str, body: LinkPersonBody) -> JSONResponse:
    """把这张媒体关联到某个人物（幂等：重复关联原样成功）。"""
    aid = _asset_id(asset_id)
    pid = _asset_id(body.person_id)
    assert pool is not None
    with pool.connection() as conn:
        if not conn.execute("select 1 from persons where id = %s::uuid", (pid,)).fetchone():
            raise HTTPException(404, "目标人物不存在")
        if not conn.execute("select 1 from assets where id = %s::uuid", (aid,)).fetchone():
            raise HTTPException(404, "资产不存在")
        conn.execute("""insert into person_assets (person_id, asset_id)
                        values (%s::uuid, %s::uuid)
                        on conflict (person_id, asset_id) do nothing""", (pid, aid))
    return JSONResponse({"ok": True, "persons": _asset_persons_out(aid)})


@app.delete("/api/assets/{asset_id}/persons/{person_id}")
def api_unlink_person(asset_id: str, person_id: str) -> JSONResponse:
    """取消关联。只动 person_assets：识别出来的人脸归属不归这里管。"""
    aid = _asset_id(asset_id)
    pid = _asset_id(person_id)
    n = q("delete from person_assets where person_id = %s::uuid and asset_id = %s::uuid "
          "returning asset_id", (pid, aid))
    if not n:
        raise HTTPException(404, "这张媒体没有关联这个人物")
    return JSONResponse({"ok": True, "persons": _asset_persons_out(aid)})


@app.get("/api/assets/{asset_id}/faces")
def api_asset_faces(asset_id: str) -> JSONResponse:
    aid = _asset_id(asset_id)
    rows = q(
        """select f.id, f.person_id, coalesce(p.name, '') person,
                  f.x1, f.y1, f.x2, f.y2, f.det_score, f.ignored
             from faces f left join persons p on p.id = f.person_id
            where f.asset_id = %s::uuid
            order by coalesce(f.quality, 0) desc, f.det_score desc""",
        (aid,),
    )
    # 识别任务状态：null = 还没入过队；pending/running = 识别中；ok = 已识别完
    job = q(
        """select state from jobs
            where kind = 'faces' and payload->>'asset_id' = %s
            order by id desc limit 1""",
        (aid,),
    )
    # 聚类进行中（ml_meta 标记，worker 写新脸的同一个事务里立）：这批脸的归属还会变，
    # 灯箱手动识别见到它就继续等，不拿「未归类」的中间状态当结论
    clustering = q("select 1 from ml_meta where key = 'faces_cluster_pending'")
    return JSONResponse({
        "job": job[0]["state"] if job else None,
        "clustering": bool(clustering),
        "faces": [{
            "id": str(r["id"]),
            "person_id": str(r["person_id"]) if r["person_id"] else None,
            "person": r["person"],
            "bbox": [r["x1"], r["y1"], r["x2"], r["y2"]],
            "det_score": r["det_score"],
            "ignored": r["ignored"],
        } for r in rows],
    })


@app.get("/api/assets/{asset_id}/video")
def api_asset_video(asset_id: str) -> JSONResponse:
    """视频的关键帧 + AI 摘要（没打标过就返回 summary=null）。"""
    aid = _asset_id(asset_id)
    s = q("""select n_frames, caption, labels, ocr, model, ms
               from video_summaries where asset_id = %s::uuid""", (aid,))
    rows = q("""select ord, t_ms, width, height, diff::float8 diff, caption, labels, ocr
                  from video_frames where asset_id = %s::uuid order by ord""", (aid,))
    sha = q("""select encode(sha256,'hex') sha from files
                where asset_id = %s::uuid and role = 'original' limit 1""", (aid,))
    sha = sha[0]["sha"] if sha else None
    frames = [{
        "ord": r["ord"],
        "t_ms": r["t_ms"],
        "width": r["width"],
        "height": r["height"],
        "diff": r["diff"],
        "caption": r["caption"],
        "labels": r["labels"],
        "ocr": r["ocr"],
        "url": f"/frames/{sha}/{r['ord']}.jpg" if sha else None,
    } for r in rows]
    summary = None
    if s:
        r = s[0]
        summary = {"n_frames": r["n_frames"], "caption": r["caption"],
                   "labels": r["labels"], "ocr": r["ocr"],
                   "model": r["model"], "ms": r["ms"]}
    return JSONResponse({"summary": summary, "frames": frames})


# ---------------------------------------------------------------- 网页导入
# 新增素材不再靠"扫盘"：浏览器把照片/视频直接推进来。
#   收流 → landing（与 vault 同一文件系统，边收边算 sha256）
#        → 重复就删掉 / 新文件 os.replace 进 vault（全程只写一次盘）
#   元数据、时区、Live Photo 配对、缩略图全部复用 ingest.ingest_file，
#   和 `vault ingest` 走同一条流水线，所以上传的资产和扫盘进来的完全等价。
#
# 一次拖拽 = 一个 batch。同一 batch 内记住"文件名 → 资产"，这样 iPhone 导出的
# HEIC + MOV 两个文件也能配成一张 Live Photo（临时文件名是随机的，不能当 stem）。

_UP_STEMS: dict[str, tuple[float, dict[tuple[Path, str], str]]] = {}
_UP_STEM_LOCK = threading.Lock()
_UP_BATCH_TTL = 6 * 3600


def _upload_clean_name(raw: str) -> str:
    """只取最后一段文件名：浏览器可能给 `C:\\fakepath\\a.jpg`。"""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    if not name or name in (".", "..") or name.startswith("."):
        raise HTTPException(400, "文件名无效")
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[: 200 - len(ext)] + ext
    return name


def _upload_stem_map(batch: str) -> dict[tuple[Path, str], str]:
    now = time.time()
    with _UP_STEM_LOCK:
        for k, (seen, _) in list(_UP_STEMS.items()):
            if now - seen > _UP_BATCH_TTL:
                _UP_STEMS.pop(k, None)
        ent = _UP_STEMS.get(batch)
        if ent is None:
            ent = (now, {})
            _UP_STEMS[batch] = ent
        return ent[1]


def _upload_source(conn: Any) -> int:
    row = conn.execute(
        "select id from sources where kind='upload' and root_path='web://upload'"
    ).fetchone()
    if row:
        conn.execute("update sources set last_seen=now() where id=%s", (row["id"],))
        return row["id"]
    return conn.execute(
        """insert into sources (kind, label, root_path)
           values ('upload', '网页导入', 'web://upload') returning id"""
    ).fetchone()["id"]


_UPLOAD_ASSET_SQL = """
select a.id, a.kind, a.library, a.favorite, a.hidden, a.taken_at_local, a.tz_source,
       a.width, a.height, a.duration_ms, a.folder_id,
       a.device_id, d.label as device_label, d.model_raw as device_raw,
       d.model_canonical as device_canonical, d.kind as device_kind,
       f.bytes, f.ext, f.origin_rel,
       encode(f.sha256, 'hex') sha,
       rf.ext raw_ext, rf.bytes raw_bytes,
       """ + _HAS_PERSON_SQL + """ has_person,
       coalesce((select json_agg(t.name order by t.name)
                 from asset_tags at2 join tags t on t.id = at2.tag_id
                 where at2.asset_id = a.id), '[]'::json) tags
  from assets a
  left join devices d on d.id = a.device_id
  join files f on f.asset_id = a.id and f.role = 'original'
  left join files rf on rf.asset_id = a.id and rf.role = 'raw'
 where a.id = %s
"""


def _parse_taken(raw: str) -> datetime | None:
    """
    客户端上传时给的拍摄时间 → aware datetime（UTC）。解析不了返回 None（不当错误）。

    两种来源：
      * iOS 同步：ISO8601 UTC，如 2024-09-01T01:30:00Z（PHAsset.creationDate）；
      * 网页上传：File.lastModified 的毫秒时间戳，如 1725154200000。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():                       # 浏览器毫秒时间戳
        try:
            return datetime.fromtimestamp(int(text[:13]) / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _upload_finish(tmp: Path, safe: str, library: str, batch: str,
                   sha: str, nbytes: int,
                   taken_hint: datetime | None = None) -> dict[str, Any]:
    """把 landing 里的上传文件入库（在 threadpool 里跑：ffprobe/PIL 都是阻塞的）。"""
    assert pool is not None
    stats = ingest_mod.Stats()
    stem_map = _upload_stem_map(batch) if batch else {}
    key = (Path(f"upload:{batch or '-'}"), Path(safe).stem)
    with pool.connection() as conn:
        source_id = _upload_source(conn)
        ingest_mod.ingest_file(
            conn, cfg, tmp, source_id, stem_map, stats,
            mode="move", library=library,
            sha_hint=(sha, nbytes),
            origin=(f"web://{safe}", safe),
            stem_key=key,
            taken_hint=taken_hint,
        )
        row = conn.execute(
            "select asset_id from files where sha256=%s "
            "and role in ('original', 'live_motion', 'raw') limit 1",
            (bytes.fromhex(sha),),
        ).fetchone()
        if row is None:
            raise RuntimeError("文件没有登记进库")
        item_row = conn.execute(_UPLOAD_ASSET_SQL, (row["asset_id"],)).fetchone()
        # 顺手排进 AI 队列：worker 在跑就会自动打标；没跑也只是 pending，不占资源
        queued = 0
        for kind, asset_kind in (("analyze", None), ("faces", None), ("video", "video")):
            # 素材库不做人脸识别
            lib = "gallery" if kind == "faces" else None
            queued += conn.execute(
                _ENQUEUE_JOB_SQL, (kind, [str(row["asset_id"])], asset_kind,
                                   asset_kind, lib, lib, kind)
            ).rowcount
    return {
        "status": "duplicate" if stats.duplicate else "imported",
        "asset": _item_json(item_row),
        "queued": queued,
        "bytes": nbytes,
    }


@app.post("/api/upload")
async def api_upload(
    request: Request,
    name: str = Query(..., min_length=1),
    library: str = Query("gallery"),
    batch: str = Query(""),
    taken: str = Query(""),
) -> JSONResponse:
    """原始字节流上传（不用 multipart：GB 级视频不落容器临时盘，直接写 landing）。

    POST /api/upload?name=IMG_0001.HEIC&library=gallery&batch=<一次拖拽的 uuid>
                &taken=<可选：ISO8601 UTC 或毫秒时间戳>
    body = 文件内容。返回 {status: imported|duplicate, asset, queued}。

    taken 是客户端已知的拍摄时间（iOS 相册 PHAsset.creationDate / 网页
    File.lastModified）。文件自身没有内嵌时间时（聊天保存图、截图重存）用它，
    避免这些资产的日期退化成"上传那一刻"；文件有 EXIF 时以内嵌时间为准，taken 被忽略。
    """
    if library not in ("gallery", "material"):
        raise HTTPException(400, "library 只能是 gallery 或 material")
    safe = _upload_clean_name(name)
    if not meta.is_media(Path(safe)):
        raise HTTPException(415, "不支持的文件类型："
                            + (Path(safe).suffix.lower() or "(无扩展名)"))
    if batch and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", batch):
        batch = ""                       # 异常批次号当没有，不影响入库
    taken_hint = _parse_taken(taken)     # 解析不了就当没有，走原来的探测逻辑

    cfg.ensure_dirs()
    suffix = Path(safe).suffix.lower()
    tmp = cfg.landing_root / f".upload-{uuidlib.uuid4().hex}{suffix}"
    h = hashlib.sha256()
    total = 0
    try:
        with tmp.open("wb") as fp:
            async for chunk in request.stream():
                if not chunk:
                    continue
                h.update(chunk)
                total += len(chunk)
                await run_in_threadpool(fp.write, chunk)
        if total == 0:
            raise HTTPException(400, "空文件")
        result = await run_in_threadpool(
            _upload_finish, tmp, safe, library, batch, h.hexdigest(), total, taken_hint
        )
        return JSONResponse(result)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"导入失败：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 影视库
# 只保存、不识别：影视文件留在原地（不搬进 vault、不算哈希、不跑 AI），
# 库里只存元数据 + (root_key, rel_path)。扫描/海报/字幕解析全在 vault/cinema.py。

_CINE_RUNS: dict[str, dict[str, Any]] = {}
_CINE_LOCK = threading.Lock()
_CINE_BUSY = False


def _cine_public(run: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in run.items() if k != "errors"}
    out["errors"] = run.get("errors") or []
    return out


def _cine_progress(run_id: str, p: dict[str, Any]) -> None:
    with _CINE_LOCK:
        run = _CINE_RUNS.get(run_id)
        if not run:
            return
        for key in ("total", "done", "scanned", "imported", "updated",
                    "failed", "titles", "subtitles", "current"):
            if key in p:
                run[key] = p[key]


def _cine_start(path: Path, kind: str, posters: bool) -> dict[str, Any]:
    global _CINE_BUSY
    with _CINE_LOCK:
        if _CINE_BUSY:
            raise HTTPException(409, "已经有一个导入在跑，等它结束再试")
        _CINE_BUSY = True
        run_id = uuidlib.uuid4().hex[:12]
        run = {
            "id": run_id, "state": "running", "kind": kind,
            "path": str(path),
            "host_path": cfg.host_path_for(*cfg.classify(path)) if _in_roots(path) else str(path),
            "total": 0, "done": 0, "scanned": 0, "imported": 0, "updated": 0,
            "failed": 0, "titles": 0, "subtitles": 0, "current": "",
            "errors": [], "error": None,
            "started_at": time.time(), "ended_at": None,
        }
        _CINE_RUNS[run_id] = run
        if len(_CINE_RUNS) > 30:                 # 只留最近的，进程内存不涨
            for old in sorted(_CINE_RUNS, key=lambda k: _CINE_RUNS[k]["started_at"])[:-30]:
                _CINE_RUNS.pop(old, None)
    threading.Thread(target=_cine_run, args=(run_id, path, kind, posters),
                     daemon=True).start()
    return run


def _in_roots(path: Path) -> bool:
    try:
        cfg.classify(path)
        return True
    except ValueError:
        return False


def _cine_run(run_id: str, path: Path, kind: str, posters: bool) -> None:
    global _CINE_BUSY
    try:
        assert pool is not None
        with pool.connection() as conn:
            stats = cinema.run_import(
                conn, cfg, path, forced=kind, posters=posters,
                progress=lambda p: _cine_progress(run_id, p))
        with _CINE_LOCK:
            run = _CINE_RUNS[run_id]
            run.update(stats.as_dict())
            run["state"] = "done"
            run["errors"] = stats.errors[:20]
            run["ended_at"] = time.time()
    except Exception as exc:                        # noqa: BLE001 —— 线程里必须兜住
        with _CINE_LOCK:
            run = _CINE_RUNS[run_id]
            run["state"] = "error"
            run["error"] = f"{type(exc).__name__}: {exc}"
            run["ended_at"] = time.time()
    finally:
        with _CINE_LOCK:
            _CINE_BUSY = False


class CinemaImportBody(BaseModel):
    path: str
    kind: str = "auto"          # auto | movie | series
    posters: bool = True


class CinemaTitleBody(BaseModel):
    title: str | None = None
    year: int | None = None
    overview: str | None = None


def _cine_title_or_404(title_id: str) -> str:
    tid = _asset_id(title_id)
    if not q("select 1 from cinema_titles where id=%s::uuid", (tid,)):
        raise HTTPException(404, "影视条目不存在")
    return tid


@app.get("/api/cinema")
def api_cinema_list(qtext: str | None = Query(None, alias="q"),
                    kind: str | None = None) -> JSONResponse:
    where = ["1=1"]
    params: list[Any] = []
    if kind in ("movie", "series"):
        where.append("c.kind = %s")
        params.append(kind)
    if qtext:
        like = "%" + qtext.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(c.title ilike %s or c.overview ilike %s)")
        params.extend([like, like])
    rows = q(
        f"""select c.id, c.kind, c.title, c.year, c.overview, c.poster_rel,
                   c.source_rel, c.updated_at,
                   count(e.id) n_episodes,
                   count(distinct e.season) filter (where e.season > 0) n_seasons,
                   coalesce(sum(e.duration_ms), 0) dur_ms,
                   coalesce(sum(e.bytes), 0) bytes,
                   coalesce(min(e.season) filter (where e.season > 0), 0) first_season,
                   (select count(*) from cinema_subtitles s
                      join cinema_episodes e2 on e2.id = s.episode_id
                     where e2.title_id = c.id) subs
              from cinema_titles c
              left join cinema_episodes e on e.title_id = c.id
             where {' and '.join(where)}
             group by c.id
             order by c.kind desc, c.title_key""",
        tuple(params),
    )
    return JSONResponse({"titles": [{
        "id": str(r["id"]),
        "kind": r["kind"],
        "title": r["title"],
        "year": r["year"],
        "overview": r["overview"],
        # 海报会随重新扫描覆盖同一个文件，URL 带上更新时间，浏览器才敢长缓存
        "poster": (f"/api/cinema/poster/{r['id']}?v={int(r['updated_at'].timestamp())}"
                   if r["poster_rel"] else None),
        "n_episodes": r["n_episodes"],
        "n_seasons": r["n_seasons"],
        "first_season": r["first_season"],
        "duration_ms": int(r["dur_ms"]),
        "bytes": int(r["bytes"]),
        "subtitles": r["subs"],
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    } for r in rows],
        # 空库时的提示要告诉用户把片子放哪
        "media_root": str(cfg.media_root),
        "media_host_root": cfg.media_host_root})


@app.get("/api/cinema/poster/{title_id}")
def api_cinema_poster(title_id: str) -> FileResponse:
    tid = _asset_id(title_id)
    rows = q("select poster_rel from cinema_titles where id=%s::uuid", (tid,))
    if not rows or not rows[0]["poster_rel"]:
        raise HTTPException(404, "no poster")
    p = cfg.derived_root / rows[0]["poster_rel"]
    if not p.exists():
        raise HTTPException(404, "poster missing on disk")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/cinema/{title_id}")
def api_cinema_title(title_id: str) -> JSONResponse:
    tid = _cine_title_or_404(title_id)
    t = q("""select id, kind, title, year, overview, poster_rel, source_rel, updated_at
               from cinema_titles where id=%s::uuid""", (tid,))[0]
    eps = q("""select e.id, e.season, e.episode, e.name, e.root_key, e.rel_path,
                      e.bytes, e.duration_ms, e.width, e.height, e.codec,
                      e.updated_at,
                      coalesce((select json_agg(json_build_object(
                                     'id', s.id, 'lang', s.lang, 'label', s.label,
                                     'format', s.format, 'forced', s.forced)
                                   order by s.forced, s.label)
                                 from cinema_subtitles s
                                where s.episode_id = e.id), '[]'::json) subtitles
                 from cinema_episodes e
                where e.title_id = %s::uuid
                order by e.season, e.episode""", (tid,))
    seasons: dict[int, list[dict[str, Any]]] = {}
    for r in eps:
        path = cfg.path_for(r["root_key"], r["rel_path"])
        item = {
            "id": str(r["id"]),
            "season": r["season"],
            "episode": r["episode"],
            "name": r["name"],
            "bytes": int(r["bytes"] or 0),
            "duration_ms": r["duration_ms"],
            "width": r["width"],
            "height": r["height"],
            "codec": r["codec"],
            "subtitles": r["subtitles"],
            "missing": not path.exists(),
            "host_path": cfg.host_path_for(r["root_key"], r["rel_path"]),
        }
        seasons.setdefault(r["season"], []).append(item)
    return JSONResponse({
        "id": str(t["id"]), "kind": t["kind"], "title": t["title"], "year": t["year"],
        "overview": t["overview"],
        "poster": (f"/api/cinema/poster/{t['id']}?v={int(t['updated_at'].timestamp())}"
                   if t["poster_rel"] else None),
        "can_rescan": bool(t["source_rel"]),
        "seasons": [{"season": s, "episodes": v} for s, v in sorted(seasons.items())],
        "n_episodes": len(eps),
        "duration_ms": sum(int(r["duration_ms"] or 0) for r in eps),
        "bytes": sum(int(r["bytes"] or 0) for r in eps),
    })


@app.patch("/api/cinema/{title_id}")
def api_cinema_edit(title_id: str, body: CinemaTitleBody) -> JSONResponse:
    tid = _cine_title_or_404(title_id)
    fields = body.model_dump(exclude_unset=True)
    sets: list[str] = []
    params: list[Any] = []
    if "title" in fields:
        name = (body.title or "").strip()
        if not 1 <= len(name) <= 120:
            raise HTTPException(400, "片名需为 1-120 个字符")
        sets += ["title = %s", "title_key = %s"]
        params += [name, cinema.title_key(name)]
    if "year" in fields:
        if body.year is not None and not (1888 <= body.year <= 2100):
            raise HTTPException(400, "年份不合法")
        sets.append("year = %s")
        params.append(body.year)
    if "overview" in fields:
        sets.append("overview = %s")
        params.append((body.overview or "").strip() or None)
    if not sets:
        raise HTTPException(400, "没有要改的字段")
    try:
        row = q(f"update cinema_titles set {', '.join(sets)}, updated_at=now() "
                f"where id=%s::uuid returning title, year, overview",
                tuple(params + [tid]))
    except Exception:
        raise HTTPException(409, "改不了：可能已有同名条目")
    return JSONResponse({"id": tid, **row[0]})


@app.delete("/api/cinema/{title_id}")
def api_cinema_delete(title_id: str) -> JSONResponse:
    """只删库里的登记，磁盘上的影视文件一个字节都不动。"""
    tid = _cine_title_or_404(title_id)
    poster = q("select poster_rel from cinema_titles where id=%s::uuid", (tid,))[0]["poster_rel"]
    with pool.connection() as conn:
        n = conn.execute("delete from cinema_titles where id=%s::uuid", (tid,)).rowcount
    if poster:                                   # 海报是我们生成的，跟着删
        try:
            (cfg.derived_root / poster).unlink(missing_ok=True)
        except OSError:
            pass
    return JSONResponse({"deleted": n})


@app.delete("/api/cinema/episodes/{episode_id}")
def api_cinema_episode_delete(episode_id: str) -> JSONResponse:
    eid = _asset_id(episode_id)
    with pool.connection() as conn:
        n = conn.execute("delete from cinema_episodes where id=%s::uuid", (eid,)).rowcount
    if not n:
        raise HTTPException(404, "这一集不存在")
    return JSONResponse({"deleted": n})


@app.post("/api/cinema/import")
def api_cinema_import(body: CinemaImportBody) -> JSONResponse:
    if body.kind not in ("auto", "movie", "series"):
        raise HTTPException(400, "kind 只能是 auto / movie / series")
    try:
        path = cinema.resolve_input(cfg, body.path)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    if not path.exists():
        raise HTTPException(400, f"路径不存在：{path}")
    run = _cine_start(path, body.kind, body.posters)
    return JSONResponse({"run": _cine_public(run)})


@app.get("/api/cinema/import/{run_id}")
def api_cinema_import_status(run_id: str) -> JSONResponse:
    with _CINE_LOCK:
        run = _CINE_RUNS.get(run_id)
        if not run:
            raise HTTPException(404, "没有这个导入任务（服务重启过？）")
        return JSONResponse({"run": _cine_public(run)})


@app.post("/api/cinema/{title_id}/rescan")
def api_cinema_rescan(title_id: str) -> JSONResponse:
    tid = _cine_title_or_404(title_id)
    rows = q(
        """select c.kind, c.source_rel,
                  (select e.root_key from cinema_episodes e
                    where e.title_id = c.id order by e.season, e.episode limit 1) root_key
             from cinema_titles c where c.id=%s::uuid""", (tid,))
    row = rows[0]
    if not row["source_rel"] or not row["root_key"]:
        raise HTTPException(400, "这条影视没有记录目录，无法重新扫描")
    path = cfg.path_for(row["root_key"], row["source_rel"])
    if not path.exists():
        raise HTTPException(400, f"目录不在了：{path}")
    run = _cine_start(path, row["kind"], True)
    return JSONResponse({"run": _cine_public(run)})


@app.get("/api/cinema/episodes/{episode_id}/stream")
def api_cinema_stream(episode_id: str) -> FileResponse:
    """浏览器直接播（mp4/webm）。mkv/avi 浏览器多半放不了，前端会引导用系统播放器。"""
    eid = _asset_id(episode_id)
    rows = q("select root_key, rel_path from cinema_episodes where id=%s::uuid", (eid,))
    if not rows:
        raise HTTPException(404, "这一集不存在")
    p = cfg.path_for(rows[0]["root_key"], rows[0]["rel_path"])
    if not p.exists():
        raise HTTPException(404, "文件不在磁盘上")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=mime,
                        headers={"Cache-Control": "private, max-age=600"})


def _shell_open(p: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return ctypes.windll.shell32.ShellExecuteW(
            None, "open", str(p), None, None, 1) > 32
    except Exception:
        return False


@app.post("/api/cinema/episodes/{episode_id}/open")
def api_cinema_open(episode_id: str) -> JSONResponse:
    """用系统默认播放器打开（mkv/ass 这类浏览器放不了的，走这条路最稳）。"""
    eid = _asset_id(episode_id)
    rows = q("select root_key, rel_path from cinema_episodes where id=%s::uuid", (eid,))
    if not rows:
        raise HTTPException(404, "这一集不存在")
    p = cfg.path_for(rows[0]["root_key"], rows[0]["rel_path"])
    host = cfg.host_path_for(rows[0]["root_key"], rows[0]["rel_path"])
    if not p.exists():
        raise HTTPException(404, "文件不在磁盘上")
    return JSONResponse({"opened": _shell_open(p), "host_path": host})


def _ass_to_vtt(text: str) -> str:
    """ASS/SSA → WebVTT（只取对白文本，样式丢弃）。"""
    cues: list[str] = []
    for line in text.splitlines():
        if not line.lower().startswith("dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start, end, body = parts[1].strip(), parts[2].strip(), parts[9]
        body = re.sub(r"\{[^}]*\}", "", body)
        body = body.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        body = body.strip()
        if not body:
            continue
        cues.append(f"{_ass_ts(start)} --> {_ass_ts(end)}\n{body}")
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


def _ass_ts(ts: str) -> str:
    m = re.match(r"^(\d+):(\d{2}):(\d{2})[.:](\d{1,3})$", ts)
    if not m:
        return "00:00:00.000"
    h, mi, s, frac = m.groups()
    return f"{int(h):02d}:{mi}:{s}.{frac.ljust(3, '0')[:3]}"


@app.get("/api/cinema/subtitles/{sub_id}")
def api_cinema_subtitle(sub_id: str) -> Response:
    """字幕转 WebVTT 给 <track> 用。SRT / ASS / SSA / VTT 都支持，原文件不动。"""
    sid = sub_id
    if not sid.isdigit():
        raise HTTPException(400, "bad subtitle id")
    rows = q("""select root_key, rel_path, format from cinema_subtitles
                 where id=%s""", (int(sid),))
    if not rows:
        raise HTTPException(404, "字幕不存在")
    row = rows[0]
    p = cfg.path_for(row["root_key"], row["rel_path"])
    if not p.exists():
        raise HTTPException(404, "字幕文件不在磁盘上")
    fmt = (row["format"] or p.suffix.lstrip(".")).lower()
    if fmt == "vtt":
        text = cinema.read_text_auto(p)
        vtt = text if text.lstrip("\ufeff").startswith("WEBVTT") else "WEBVTT\n\n" + text
    elif fmt == "srt":
        vtt = cinema.srt_to_vtt(cinema.read_text_auto(p))
    elif fmt in ("ass", "ssa"):
        vtt = _ass_to_vtt(cinema.read_text_auto(p))
    else:
        raise HTTPException(415, f".{fmt} 字幕暂不支持网页播放，用系统播放器打开")
    return Response(vtt, media_type="text/vtt; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- 静态文件

@app.get("/thumbs/{sha}.webp")
def thumb(sha: str) -> FileResponse:
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        raise HTTPException(400, "bad sha")
    p = cfg.derived_root / "thumbs" / sha[0:2] / sha[2:4] / f"{sha}.webp"
    if not p.exists():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(p, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/faces/{face_id}.jpg")
def face_crop(face_id: str) -> FileResponse:
    """112×112 对齐后放大到 160 的人脸头像（worker 生成）。"""
    fid = _asset_id(face_id)
    p = cfg.derived_root / "faces" / f"{fid}.jpg"
    if not p.exists():
        raise HTTPException(404, "no face crop")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/covers/{person_id}.jpg")
def person_cover_image(person_id: str, slot: int = Query(1)) -> FileResponse:
    """人物自定义封面（上传图，≤640px 方图）。slot=1 主封面、slot=2 备用封面。
    重新上传会覆盖同一路径，不能 immutable，用 no-cache 走 etag 协商缓存：
    没变就 304，变了立刻生效。"""
    pid = _asset_id(person_id)
    _cover_slot(slot)
    p = _person_cover_path(pid, slot)
    if not p.exists():
        raise HTTPException(404, "no custom cover")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, no-cache"})


@app.get("/frames/{sha}/{frame}.jpg")
def video_frame(sha: str, frame: str) -> FileResponse:
    """视频关键帧。文件名只跟 sha + 序号有关，重跑会覆盖同一路径，所以不能 immutable。"""
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        raise HTTPException(400, "bad sha")
    if not frame.isdigit():
        raise HTTPException(400, "bad frame")
    p = (cfg.derived_root / "keyframes" / sha[0:2] / sha[2:4] / sha
         / f"f{int(frame):02d}.jpg")
    if not p.exists():
        raise HTTPException(404, "no keyframe")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/file/{asset_id}")
def original(asset_id: str, role: str = "original") -> FileResponse:
    aid = _asset_id(asset_id)
    if role not in ("original", "live_motion"):
        raise HTTPException(400, "bad role")
    rows = q("select rel_path from files where asset_id=%s and role=%s limit 1", (aid, role))
    if not rows:
        raise HTTPException(404, "not found")
    p = cfg.vault_root / rows[0]["rel_path"]
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(p)


# ---------------------------------------------------------------- 浏览器可显示的预览
# HEIC/DNG/TIFF 浏览器放不出来 → 首次访问时转成 JPEG（≤2048px）缓存到 derived/previews。
# jpeg/png/gif/webp 本身浏览器能显示，直接回原件（gif 的动画不能丢）。

_PREVIEW_NATIVE = {".jpg", ".jpeg", ".jfif", ".jpe", ".png", ".gif", ".webp", ".bmp", ".avif"}


def _preview_for(rel_path: str, ext: str) -> FileResponse:
    src = cfg.vault_root / rel_path
    if not src.exists():
        raise HTTPException(404, "file missing on disk")
    if ext in _PREVIEW_NATIVE:
        # .jfif / .jpe 就是 JPEG，但部分 Linux mimetype 表不认识，显式给 image/jpeg
        mime = "image/jpeg" if ext in (".jfif", ".jpe") else None
        return FileResponse(src, media_type=mime)
    from PIL import Image

    sha = rel_path.split("/")[-1].rsplit(".", 1)[0]
    dst = cfg.derived_root / "previews" / sha[0:2] / sha[2:4] / f"{sha}.jpg"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((2048, 2048))
            im.save(dst, "JPEG", quality=88)
    return FileResponse(dst, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/preview/{asset_id}")
def preview(asset_id: str) -> FileResponse:
    aid = _asset_id(asset_id)
    rows = q("select rel_path, ext from files where asset_id=%s and role='original' limit 1", (aid,))
    if not rows:
        raise HTTPException(404, "not found")
    return _preview_for(rows[0]["rel_path"], (rows[0]["ext"] or "").lower())


# ---------------------------------------------------------------- 前端
# 前端是独立的 Vue 3 应用（frontend/，构建产物落在 vault/static）。
# 后端只做三件事：/assets 静态文件、根路径 index.html、其余非 /api 路径交给
# vue-router（刷新 /gallery 这类深层路径不会 404）。

STATIC_DIR = Path(__file__).parent / "static"

if (STATIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


def _spa_index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(503, "前端还没构建：cd frontend && pnpm install && pnpm build")
    # 构建产物里 JS/CSS 的文件名带内容哈希，index.html 本身不能缓存
    return HTMLResponse(index_file.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return _spa_index()


@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(path: str) -> Response:
    """SPA 兜底：静态文件存在就发文件，否则一律发 index.html。"""
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "not found")
    candidate = (STATIC_DIR / path).resolve()
    static_root = STATIC_DIR.resolve()
    if candidate != static_root and static_root in candidate.parents and candidate.is_file():
        return FileResponse(candidate)
    return _spa_index()
