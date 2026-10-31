from __future__ import annotations

import csv
import hashlib
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid as uuidlib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import cinema, meta
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
    pool = ConnectionPool(cfg.database_url, min_size=1, max_size=6,
                          kwargs={"row_factory": dict_row}, open=True)
    yield
    if pool:
        pool.close()


app = FastAPI(title="Photo Vault", lifespan=lifespan)


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
             (select count(*) from persons) people,
             (select coalesce(sum(bytes),0) from files where role='original') b,
             (select coalesce(sum(f.bytes),0) from files f join assets a on a.id=f.asset_id
               where f.role='original' and a.library='material' and not a.is_deleted) mb,
             (select coalesce(sum(a.duration_ms),0) from assets a
               where a.library='material' and not a.is_deleted) mdur,
             (select count(*) from cinema_titles) cinema_titles,
             (select count(*) from cinema_episodes) cinema_eps,
             (select min(taken_at_utc) from assets where not is_deleted) mn,
             (select max(taken_at_utc) from assets where not is_deleted) mx"""
    )[0]
    devs = q(
        """select coalesce(d.model_raw,'(未知)') model, count(*) c
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
        "media_root": str(cfg.media_root),
        "media_host_root": cfg.media_host_root,
        "bytes": int(row["b"]),
        "first": row["mn"].isoformat() if row["mn"] else None,
        "last": row["mx"].isoformat() if row["mx"] else None,
        "devices": [{"model": d["model"], "count": d["c"]} for d in devs],
    })


# ---------------------------------------------------------------- 资产列表

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
    library: str | None = None,
    min_sec: float | None = Query(None, ge=0),
    max_sec: float | None = Query(None, ge=0),
    min_px: int | None = Query(None, ge=0),
) -> JSONResponse:
    where = ["a.is_deleted" if trash else "not a.is_deleted"]
    params: list[Any] = []

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
        where.append(
            "exists (select 1 from faces fp where fp.asset_id=a.id "
            "and fp.person_id=%s::uuid)"
        )
        params.append(_asset_id(person_id))
    if qtext:
        # 剪辑场景的搜索必须能命中"内容"：AI/VLM 标签、视频描述、OCR、人物名，
        # 而不只是文件名。原来只搜文件名+描述，打标结果等于白存。
        like = "%" + qtext.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            """(f.src_path ilike %s or f.origin_rel ilike %s or f.ext ilike %s
                or exists (select 1 from video_summaries vs where vs.asset_id = a.id
                           and (vs.caption ilike %s or vs.ocr ilike %s))
                or exists (select 1 from asset_tags at4 join tags t4 on t4.id = at4.tag_id
                           where at4.asset_id = a.id and t4.name ilike %s)
                or exists (select 1 from faces f4 join persons p4 on p4.id = f4.person_id
                           where f4.asset_id = a.id and p4.name ilike %s))"""
        )
        params.extend([like] * 7)
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

    clause = " and ".join(where)
    base = f"""
        from assets a
        left join devices d on d.id = a.device_id
        join files f on f.asset_id = a.id and f.role = 'original'
        where {clause}
    """
    order = ("coalesce(a.deleted_at, a.taken_at_utc) desc nulls last"
             if trash else "a.taken_at_utc desc nulls last")
    total = q(f"select count(*) c {base}", tuple(params))[0]["c"]
    rows = q(
        f"""select a.id, a.kind, a.library, a.favorite, a.taken_at_local, a.tz_offset_min, a.tz_source,
                   a.width, a.height, a.duration_ms,
                   coalesce(d.model_raw,'未知设备') model,
                   f.sha256, f.bytes, f.ext, f.origin_rel, f.storage_mode,
                   encode(f.sha256,'hex') sha,
                   coalesce((select json_agg(t3.name order by t3.name)
                             from asset_tags at3 join tags t3 on t3.id = at3.tag_id
                             where at3.asset_id = a.id), '[]'::json) tags
            {base}
            order by {order}
            limit %s offset %s""",
        tuple(params + [limit, offset]),
    )

    items = []
    for r in rows:
        sha = r["sha"]
        items.append({
            "id": str(r["id"]),
            "kind": r["kind"],
            "library": r["library"],
            "sha": sha,
            "favorite": r["favorite"],
            "taken": r["taken_at_local"].isoformat() if r["taken_at_local"] else None,
            "tz_source": r["tz_source"],
            "model": r["model"],
            "width": r["width"],
            "height": r["height"],
            "duration_ms": r["duration_ms"],
            "bytes": r["bytes"],
            "has_thumb": _thumb_ok(sha),
            "origin": r["origin_rel"],
            "tags": r["tags"],
            "deleted": trash,
        })
    return JSONResponse({"total": total, "items": items})


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


_ITEM_COLS = """a.id, a.kind, a.library, a.favorite, a.taken_at_local, a.tz_source,
                a.width, a.height, a.duration_ms,
                coalesce(d.model_raw,'未知设备') model,
                f.sha256, f.bytes, f.ext, f.origin_rel,
                encode(f.sha256,'hex') sha"""


def _item_json(r: dict[str, Any], ord_: int | None = None) -> dict[str, Any]:
    item = {
        "id": str(r["id"]),
        "kind": r["kind"],
        "library": r["library"],
        "sha": r["sha"],
        "favorite": r["favorite"],
        "taken": r["taken_at_local"].isoformat() if r["taken_at_local"] else None,
        "tz_source": r["tz_source"],
        "model": r["model"],
        "width": r["width"],
        "height": r["height"],
        "duration_ms": r["duration_ms"],
        "bytes": r["bytes"],
        "has_thumb": _thumb_ok(r["sha"]),
        "origin": r["origin_rel"],
        "tags": r["tags"],
    }
    if ord_ is not None:
        item["ord"] = ord_
    return item


@app.get("/api/bins")
def api_bins() -> JSONResponse:
    rows = q("""select b.id, b.name, b.note, b.updated_at,
                       count(bi.asset_id) items,
                       coalesce(sum(a.duration_ms), 0) dur_ms,
                       coalesce(sum(f.bytes), 0) bytes
                from bins b
                left join bin_items bi on bi.bin_id = b.id
                left join assets a on a.id = bi.asset_id
                left join files f on f.asset_id = a.id and f.role = 'original'
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
                  coalesce((select string_agg(t3.name, ' ' order by t3.name)
                            from asset_tags at3 join tags t3 on t3.id = at3.tag_id
                            where at3.asset_id = a.id), '') tags
           from bin_items bi
           join assets a on a.id = bi.asset_id
           join files f on f.asset_id = a.id and f.role = 'original'
           where bi.bin_id = %s::uuid and not a.is_deleted
           order by bi.ord, bi.added_at""",
        (bid,),
    )
    if not rows:
        raise HTTPException(400, "剪辑箱是空的")

    out = cfg.derived_root / "cuts" / (_safe_name(bin_row[0]["name"]) or "剪辑箱")
    out.mkdir(parents=True, exist_ok=True)

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
        dst = out / name
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                raise HTTPException(409, f"{name} 被占用——剪映可能正打开着这个文件")
        if mode == "link":
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
        written.append((name, r))

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
        "items": len(written),
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


# ---------------------------------------------------------------- 标签

class TagBody(BaseModel):
    name: str


class BatchBody(BaseModel):
    ids: list[str]
    action: str                 # favorite | unfavorite | trash | restore | move | tag | untag
    tag: str | None = None
    library: str | None = None  # move 时必填：gallery | material


class AnalyzeBody(BaseModel):
    ids: list[str]


# 入队时跳过"已经排过 / 正在跑 / 已识别"的：重复点「AI 识别」不该把队列撑大
_ENQUEUE_JOB_SQL = """
insert into jobs (kind, payload)
select %s, jsonb_build_object('asset_id', a.id::text)
from assets a
where a.id = any(%s::uuid[]) and not a.is_deleted
  and (%s::text is null or a.kind = %s::text)
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
        elif body.action == "trash":
            n = conn.execute("update assets set is_deleted=true, deleted_at=now() "
                             "where id=any(%s::uuid[]) and not is_deleted", (ids,)).rowcount
        elif body.action == "restore":
            n = conn.execute("update assets set is_deleted=false, deleted_at=null "
                             "where id=any(%s::uuid[]) and is_deleted", (ids,)).rowcount
        elif body.action == "move":
            if body.library not in ("gallery", "material"):
                raise HTTPException(400, "移动目标只能是 gallery 或 material")
            n = conn.execute("update assets set library=%s "
                             "where id=any(%s::uuid[]) and not is_deleted",
                             (body.library, ids)).rowcount
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
            cur = conn.execute(_ENQUEUE_JOB_SQL,
                               (kind, ids, asset_kind, asset_kind, kind))
            by_kind[kind] = cur.rowcount
    total = sum(by_kind.values())
    return JSONResponse({"queued": total, "by_kind": by_kind})


# ---------------------------------------------------------------- 人物 / 人脸

def _clean_person_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 30:
        raise HTTPException(400, "名字需为 1-30 个字符")
    return name


def _refresh_cover(conn: Any, person_id: str) -> None:
    """封面没设或指向已删除的脸时，重选一张面积最大的。"""
    conn.execute(
        """update persons p
              set cover_face_id = (
                    select f.id from faces f where f.person_id = p.id
                     order by coalesce(f.quality, 0) desc, f.det_score desc limit 1),
                  updated_at = now()
            where p.id = %s::uuid
              and (p.cover_face_id is null
                   or not exists (select 1 from faces c where c.id = p.cover_face_id))""",
        (person_id,),
    )


@app.get("/api/persons")
def api_persons() -> JSONResponse:
    rows = q(
        """select p.id, coalesce(p.name, '') name, p.cover_face_id,
                  (select count(*) from faces f join assets a on a.id = f.asset_id
                    where f.person_id = p.id and not a.is_deleted) n_faces,
                  (select count(distinct f.asset_id) from faces f
                     join assets a on a.id = f.asset_id
                    where f.person_id = p.id and not a.is_deleted) n_assets
             from persons p
            order by n_assets desc, length(coalesce(p.name, '')), coalesce(p.name, ''),
                     p.created_at"""
    )
    unassigned = q(
        """select count(*) c from faces f join assets a on a.id = f.asset_id
            where f.person_id is null and not a.is_deleted"""
    )[0]["c"]
    return JSONResponse({
        "persons": [{
            "id": str(r["id"]),
            "name": r["name"],
            "cover": str(r["cover_face_id"]) if r["cover_face_id"] else None,
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


@app.get("/api/assets/{asset_id}/faces")
def api_asset_faces(asset_id: str) -> JSONResponse:
    aid = _asset_id(asset_id)
    rows = q(
        """select f.id, f.person_id, coalesce(p.name, '') person,
                  f.x1, f.y1, f.x2, f.y2, f.det_score
             from faces f left join persons p on p.id = f.person_id
            where f.asset_id = %s::uuid
            order by coalesce(f.quality, 0) desc, f.det_score desc""",
        (aid,),
    )
    return JSONResponse({"faces": [{
        "id": str(r["id"]),
        "person_id": str(r["person_id"]) if r["person_id"] else None,
        "person": r["person"],
        "bbox": [r["x1"], r["y1"], r["x2"], r["y2"]],
        "det_score": r["det_score"],
    } for r in rows]})


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
select a.id, a.kind, a.library, a.favorite, a.taken_at_local, a.tz_source,
       a.width, a.height, a.duration_ms,
       coalesce(d.model_raw, '未知设备') model,
       f.bytes, f.ext, f.origin_rel,
       encode(f.sha256, 'hex') sha,
       coalesce((select json_agg(t.name order by t.name)
                 from asset_tags at2 join tags t on t.id = at2.tag_id
                 where at2.asset_id = a.id), '[]'::json) tags
  from assets a
  left join devices d on d.id = a.device_id
  join files f on f.asset_id = a.id and f.role = 'original'
 where a.id = %s
"""


def _upload_finish(tmp: Path, safe: str, library: str, batch: str,
                   sha: str, nbytes: int) -> dict[str, Any]:
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
        )
        row = conn.execute(
            "select asset_id from files where sha256=%s "
            "and role in ('original', 'live_motion') limit 1",
            (bytes.fromhex(sha),),
        ).fetchone()
        if row is None:
            raise RuntimeError("文件没有登记进库")
        item_row = conn.execute(_UPLOAD_ASSET_SQL, (row["asset_id"],)).fetchone()
        # 顺手排进 AI 队列：worker 在跑就会自动打标；没跑也只是 pending，不占资源
        queued = 0
        for kind, asset_kind in (("analyze", None), ("faces", None), ("video", "video")):
            queued += conn.execute(
                _ENQUEUE_JOB_SQL, (kind, [str(row["asset_id"])], asset_kind,
                                   asset_kind, kind)
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
) -> JSONResponse:
    """原始字节流上传（不用 multipart：GB 级视频不落容器临时盘，直接写 landing）。

    POST /api/upload?name=IMG_0001.HEIC&library=gallery&batch=<一次拖拽的 uuid>
    body = 文件内容。返回 {status: imported|duplicate, asset, queued}。
    """
    if library not in ("gallery", "material"):
        raise HTTPException(400, "library 只能是 gallery 或 material")
    safe = _upload_clean_name(name)
    if not meta.is_media(Path(safe)):
        raise HTTPException(415, "不支持的文件类型："
                            + (Path(safe).suffix.lower() or "(无扩展名)"))
    if batch and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", batch):
        batch = ""                       # 异常批次号当没有，不影响入库

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
            _upload_finish, tmp, safe, library, batch, h.hexdigest(), total
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return HTMLResponse(PAGE)


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


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Photo Vault</title>
<style>
  :root{--bg:#000;--panel:#1c1c1e;--panel2:#2c2c2e;--fg:#fff;--mut:#8e8e93;--acc:#0a84ff;--heart:#ff375f;--line:#38383a}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;padding:0}
  svg{display:block}

  /* ---------- 顶栏 ---------- */
  .titlebar{display:flex;align-items:center;gap:12px;padding:14px 16px 6px}
  .titlebar h1{font-size:26px;font-weight:700;margin:0;letter-spacing:.2px;flex:1}
  .titlebar .search{background:var(--panel);border:none;border-radius:10px;color:var(--fg);
    padding:8px 12px;font-size:14px;width:210px;outline:none}
  .titlebar .search::placeholder{color:var(--mut)}
  .btn-txt{color:var(--acc);font-size:16px;font-weight:500;padding:6px 4px}
  .btn-txt:disabled{color:var(--mut)}
  .chips{display:flex;gap:8px;align-items:center;padding:6px 16px 12px;flex-wrap:wrap}
  .chip{display:inline-flex;align-items:center;gap:5px;background:var(--panel);color:var(--fg);
    border-radius:999px;padding:6px 14px;font-size:13.5px}
  .chip.on{background:var(--acc);color:#fff}
  .chip .cnt{opacity:.55;font-size:12px}

  /* ---------- 剪辑筛选条 ---------- */
  .filters{display:flex;gap:10px;align-items:center;padding:0 16px 10px;overflow-x:auto;
    scrollbar-width:none;white-space:nowrap}
  .filters::-webkit-scrollbar{display:none}

  /* 窄屏：顶栏要同时放标题 / 搜索 / 导入 / 选择，把搜索框收窄 */
  @media (max-width:480px){
    .titlebar{gap:8px;padding:12px 12px 4px}
    .titlebar h1{font-size:22px}
    .titlebar .search{width:104px;padding:7px 10px}
    .btn-txt{font-size:15px}
  }
  .filters .lbl{font-size:12px;color:var(--mut)}
  .fgroup{display:inline-flex;background:var(--panel);border-radius:9px;padding:2px;flex:none}
  .fgroup button{padding:5px 10px;border-radius:7px;font-size:12.5px;color:var(--mut)}
  .fgroup button.on{background:var(--acc);color:#fff}
  #binbar{display:none;gap:8px;align-items:center;padding:0 16px 12px}
  #binbar.on{display:flex}
  #binbar .grow{flex:1}
  #binbar .prim{background:var(--acc);color:#fff;border-radius:9px;padding:8px 16px;font-size:14px;font-weight:600}
  #binbar .sec{color:var(--acc);padding:8px 4px;font-size:14px}
  .bdg.num{left:6px;top:5px;background:rgba(0,0,0,.6);border-radius:5px;padding:1px 5px;
    font-size:10px;text-shadow:none;letter-spacing:.5px}
  .bstat{font-size:11.5px;color:var(--mut);border:1px solid var(--line);border-radius:999px;
    padding:2px 8px;white-space:nowrap}
  .bstat.ok{color:#30d158;border-color:rgba(48,209,88,.45)}
  .bstat.bad{color:#ff9f0a;border-color:rgba(255,159,10,.45)}
  .binx{position:absolute;right:5px;top:5px;width:22px;height:22px;border-radius:50%;
    background:rgba(0,0,0,.6);color:#fff;font-size:12px;display:flex;align-items:center;
    justify-content:center;z-index:2}
  #tagMenu{position:fixed;left:16px;background:var(--panel2);border-radius:12px;
    padding:6px;min-width:200px;max-height:300px;overflow:auto;z-index:40;display:none;
    box-shadow:0 8px 30px rgba(0,0,0,.6)}
  #tagMenu.open{display:block}
  #tagMenu .mi{display:flex;justify-content:space-between;gap:14px;padding:9px 12px;border-radius:8px;cursor:pointer}
  #tagMenu .mi:hover{background:rgba(120,120,128,.25)}
  #tagMenu .mi .n{color:var(--mut);font-size:12.5px}

  /* ---------- 网格 ---------- */
  main{padding:0 2px 120px}
  .sec h2{position:sticky;top:0;z-index:5;margin:0;padding:14px 14px 8px;font-size:21px;font-weight:700;
    background:rgba(0,0,0,.86);backdrop-filter:blur(14px)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(106px,1fr));gap:2px}
  .cell{position:relative;aspect-ratio:1;background:var(--panel);overflow:hidden;cursor:pointer}
  .cell img{width:100%;height:100%;object-fit:cover;display:block}
  .cell .ph{display:flex;align-items:center;justify-content:center;height:100%;color:var(--mut);font-size:11px}
  .cell.trashed img{opacity:.45}
  .bdg{position:absolute;font-weight:600;font-size:11px;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.9)}
  .bdg.dur{left:6px;bottom:5px}
  .bdg.live{left:6px;top:5px;background:rgba(60,60,67,.55);border-radius:5px;padding:1px 5px;font-size:10px;text-shadow:none}
  .bdg.heart{right:6px;bottom:5px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.8))}
  .check{position:absolute;left:6px;top:6px;width:23px;height:23px;border-radius:50%;
    border:1.6px solid rgba(255,255,255,.85);background:rgba(0,0,0,.25);display:none;align-items:center;justify-content:center}
  .cell.sel .check{background:var(--acc);border-color:var(--acc)}
  .cell.sel img{opacity:.75}
  body.selecting .check{display:flex}
  body.selecting .cell.trashed img{opacity:.75}
  .empty{color:var(--mut);text-align:center;padding:80px 20px;font-size:15px}
  #sentinel{height:8px}

  /* ---------- 人物 ---------- */
  .pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:18px 8px;padding:18px 16px}
  .pcard{display:flex;flex-direction:column;align-items:center;gap:7px;cursor:pointer}
  .pcard .av{width:86px;height:86px;border-radius:50%;overflow:hidden;background:var(--panel2);
    display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:600;color:var(--mut)}
  .pcard .av img{width:100%;height:100%;object-fit:cover;display:block}
  .pcard .nm{font-size:13.5px;max-width:112px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pcard .ct{font-size:11.5px;color:var(--mut);margin-top:-4px}
  .pcard:active .av{transform:scale(.95)}
  #lb .facerow{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .facechip{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border-radius:999px;
    padding:3px 12px 3px 3px;font-size:13px;color:var(--fg)}
  .facechip img{width:30px;height:30px;border-radius:50%;object-fit:cover;background:var(--panel)}
  .facechip.none{opacity:.55}

  /* ---------- 视频关键帧 ---------- */
  .vidbox{display:flex;flex-direction:column;gap:7px}
  .vcap{font-size:13px;color:var(--fg);opacity:.9;line-height:1.45}
  .vframes{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px}
  .vframe{flex:0 0 auto;position:relative;border:2px solid transparent;border-radius:8px;
    overflow:hidden;padding:0;background:none;line-height:0;cursor:pointer}
  .vframe img{width:92px;height:52px;object-fit:cover;display:block;background:var(--panel)}
  .vframe.on{border-color:var(--acc)}
  .vframe .t{position:absolute;right:3px;bottom:3px;font-size:10px;line-height:1.4;color:#fff;
    background:rgba(0,0,0,.62);border-radius:4px;padding:0 3px}
  .vocr{font-size:12px;color:var(--mut);white-space:pre-wrap;max-height:46px;overflow:auto}

  /* ---------- 选择面板（合并人物用） ---------- */
  #pick{position:fixed;inset:0;z-index:140;background:rgba(0,0,0,.55);display:none;
    align-items:flex-end;justify-content:center}
  #pick.on{display:flex}
  #pick .box{width:100%;max-width:520px;background:var(--panel2);border-radius:16px 16px 0 0;
    padding:14px 16px calc(16px + env(safe-area-inset-bottom))}
  #pick h3{margin:0 0 10px;font-size:16px;text-align:center}
  #pick .list{max-height:50vh;overflow:auto}
  #pick .pi{display:flex;align-items:center;gap:10px;padding:9px 6px;border-radius:10px;cursor:pointer}
  #pick .pi:hover{background:rgba(120,120,128,.22)}
  #pick .pi img{width:36px;height:36px;border-radius:50%;object-fit:cover;background:var(--panel)}
  #pick .pi .n{flex:1}
  #pick .pi .c{color:var(--mut);font-size:12.5px}
  #pick .cancel{width:100%;margin-top:10px;background:var(--panel);border-radius:10px;padding:11px;
    color:var(--acc);font-weight:600}
  #pick .cancel.danger{color:var(--heart);font-weight:500;font-size:13.5px;margin-top:6px}

  /* ---------- 选择工具条 ---------- */
  #selbar{position:fixed;left:0;right:0;bottom:0;z-index:60;display:none;justify-content:space-around;
    align-items:center;padding:10px 8px calc(12px + env(safe-area-inset-bottom));
    background:rgba(22,22,24,.94);backdrop-filter:blur(18px);border-top:1px solid var(--line)}
  #selbar.on{display:flex}
  #selbar button{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:11px;color:var(--fg);padding:4px 9px}
  #selbar button.danger{color:var(--heart)}
  #selbar button:disabled{color:var(--mut)}

  /* ---------- 灯箱 ---------- */
  #lb{position:fixed;inset:0;z-index:100;background:#000;display:none;flex-direction:column}
  #lb.on{display:flex}
  #lb .top{display:flex;align-items:center;gap:10px;padding:12px 14px;z-index:3}
  #lb .top .dt{flex:1;text-align:center;font-size:14px;color:var(--fg);font-weight:600}
  #lb .top button{color:var(--acc);display:flex}
  #lb .stage{flex:1;display:flex;align-items:center;justify-content:center;min-height:0;position:relative}
  #lb .stage img,#lb .stage video{max-width:100%;max-height:100%;object-fit:contain}
  #lb .nav{position:absolute;top:50%;transform:translateY(-50%);width:46px;height:70px;display:flex;
    align-items:center;justify-content:center;color:#fff;opacity:.75;background:rgba(60,60,67,.4);
    border-radius:12px}
  #lb .nav:hover{opacity:1}
  #lb .nav.prev{left:10px}#lb .nav.next{right:10px}
  #lb .under{padding:10px 16px calc(16px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:10px}
  #lb .tagrow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;min-height:28px}
  .tagchip{background:var(--panel2);border-radius:999px;padding:4px 12px;font-size:13px;display:inline-flex;gap:6px;align-items:center}
  .tagchip .x{color:var(--mut);cursor:pointer}
  .tagchip .x:hover{color:var(--heart)}
  #lb .tools{display:flex;justify-content:space-around;align-items:center}
  #lb .tools button{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:11px;color:var(--fg)}
  #lb .tools button.on{color:var(--heart)}
  #lb .tools button.danger{color:var(--heart)}
  #lb .cap{text-align:center;font-size:12.5px;color:var(--mut)}

  /* ---------- 标签面板 ---------- */
  #sheet{position:fixed;left:0;right:0;bottom:0;z-index:120;background:var(--panel2);
    border-radius:16px 16px 0 0;padding:10px 16px calc(18px + env(safe-area-inset-bottom));
    transform:translateY(105%);transition:transform .22s ease;box-shadow:0 -10px 40px rgba(0,0,0,.6)}
  #sheet.on{transform:translateY(0)}
  #sheet .grab{width:38px;height:5px;border-radius:3px;background:#5a5a5e;margin:2px auto 12px}
  #sheet h3{margin:0 0 12px;font-size:16px;text-align:center;font-weight:600}
  #sheet .chips{display:flex;flex-wrap:wrap;gap:8px;padding:0 0 12px;max-height:200px;overflow:auto}
  #sheet .chip .cnt{opacity:.5}
  #sheet .newrow{display:flex;gap:8px}
  #sheet input{flex:1;background:var(--panel);border:none;border-radius:10px;color:var(--fg);
    padding:10px 12px;font-size:15px;outline:none}
  #sheet .go{background:var(--acc);border-radius:10px;padding:0 18px;font-weight:600}

  /* ---------- 影视库 ---------- */
  .cinebar{display:flex;align-items:center;gap:12px;padding:4px 16px 12px}
  .cineadd{background:var(--acc);color:#fff;border-radius:9px;padding:8px 14px;font-size:13.5px;font-weight:600}
  .cineinfo{font-size:12.5px;color:var(--mut)}
  .cinelink{color:var(--acc);font-size:13.5px;padding:6px 2px}
  .cinewrap{padding:0 12px 120px}
  .cinegroup h2{margin:16px 4px 10px;font-size:17px;font-weight:700}
  .cinegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:16px 10px}
  .cinecard{cursor:pointer}
  .cinecard:active .poster{transform:scale(.97)}
  .poster{position:relative;aspect-ratio:2/3;border-radius:10px;overflow:hidden;background:var(--panel2);
    display:flex;align-items:center;justify-content:center;transition:transform .12s ease}
  .poster img{width:100%;height:100%;object-fit:cover;display:block}
  .poster .ph{font-size:32px;color:var(--mut);font-weight:600}
  .pbadge{position:absolute;left:6px;top:6px;background:rgba(0,0,0,.62);border-radius:5px;padding:1px 6px;
    font-size:10.5px;letter-spacing:.3px}
  .pcount{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.62);border-radius:5px;padding:1px 6px;
    font-size:10.5px}
  .ctitle{margin-top:6px;font-size:13px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cmeta{font-size:11.5px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cineempty{color:var(--mut);text-align:center;padding:70px 24px;font-size:14px;line-height:1.8}
  .cineempty code{background:var(--panel);border-radius:4px;padding:1px 5px}
  .cinedetail{padding:2px 16px 120px}
  .cback{color:var(--acc);font-size:14.5px;padding:2px 0 14px}
  .chead{display:flex;gap:16px}
  .cposter{width:118px;flex:none;aspect-ratio:2/3;border-radius:10px;overflow:hidden;background:var(--panel2);
    display:flex;align-items:center;justify-content:center}
  .cposter img{width:100%;height:100%;object-fit:cover;display:block}
  .cposter .ph{font-size:34px;color:var(--mut);font-weight:600}
  .cmain{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px}
  .cmain h2{margin:0;font-size:20px;line-height:1.25}
  .cyear{color:var(--mut);font-weight:400;font-size:16px}
  .chead .cmeta{white-space:normal;line-height:1.5}
  .cacts{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
  .cacts button{border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:12.5px;color:var(--fg)}
  .cacts button.prim2{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
  .cacts button.danger{color:var(--heart);border-color:rgba(255,55,95,.4)}
  .coverview{margin:14px 0 6px;font-size:13.5px;color:#c7c7cc;line-height:1.6;white-space:pre-wrap}
  .cseasons{display:flex;gap:6px;overflow-x:auto;padding:12px 0 6px;scrollbar-width:none;white-space:nowrap}
  .cseasons::-webkit-scrollbar{display:none}
  .cseason{flex:none;background:var(--panel);border-radius:999px;padding:6px 14px;font-size:13px;color:var(--mut)}
  .cseason.on{background:var(--acc);color:#fff}
  .eplist{display:flex;flex-direction:column;gap:2px;margin-top:4px}
  .eprow{display:flex;align-items:center;gap:12px;padding:11px 4px;border-bottom:1px solid rgba(56,56,58,.6);cursor:pointer}
  .eprow:active{background:rgba(255,255,255,.04)}
  .eprow.missing{opacity:.45}
  .epno{flex:none;width:52px;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
  .epmain{flex:1;min-width:0}
  .epname{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .epmeta{font-size:11.5px;color:var(--mut);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .epplay{flex:none;width:34px;height:34px;border-radius:50%;background:var(--panel2);display:flex;
    align-items:center;justify-content:center;font-size:12px;color:var(--fg)}
  .epsubs{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
  .subtag{font-size:10.5px;color:#9ad;border:1px solid rgba(100,160,220,.35);border-radius:4px;padding:0 4px}

  /* ---------- 影视播放器 ---------- */
  #cinePlay{position:fixed;inset:0;z-index:130;background:#000;display:none;flex-direction:column}
  #cinePlay.on{display:flex}
  .cp-top{display:flex;align-items:center;gap:10px;padding:12px 14px}
  .cp-x{font-size:18px;color:var(--fg);width:30px}
  .cp-title{flex:1;font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cp-open{color:var(--acc);font-size:12.5px;border:1px solid rgba(10,132,255,.4);border-radius:8px;padding:5px 10px}
  .cp-stage{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;position:relative}
  .cp-stage video{max-width:100%;max-height:100%;width:100%;background:#000}
  .cp-err{display:none;position:absolute;left:20px;right:20px;bottom:20px;background:rgba(44,44,46,.96);
    border-radius:12px;padding:14px 16px;font-size:13px;line-height:1.55;color:#e5e5ea}
  .cp-err.on{display:block}
  .cp-bottom{display:flex;align-items:center;gap:10px;padding:10px 14px calc(14px + env(safe-area-inset-bottom))}
  .cpsel{background:var(--panel2);border:none;color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;max-width:48%}
  .cpnav{color:var(--acc);font-size:13.5px;padding:8px 6px}
  .cpnav:disabled{color:var(--mut)}

  /* ---------- 影视表单 / 弹层 ---------- */
  .cmodal{position:fixed;inset:0;z-index:150;background:rgba(0,0,0,.6);display:none;
    align-items:flex-end;justify-content:center}
  .cmodal.on{display:flex}
  .cbox{width:100%;max-width:560px;background:var(--panel2);border-radius:16px 16px 0 0;
    padding:16px 18px calc(18px + env(safe-area-inset-bottom))}
  .cbox h3{margin:0 0 6px;font-size:17px;text-align:center}
  .cbox .chint{margin:0 0 12px;font-size:12.5px;color:var(--mut);line-height:1.6}
  .cbox label{display:block;font-size:12.5px;color:var(--mut);margin:10px 0 5px}
  .cbox input[type=text],.cbox input[type=number],.cbox textarea,.cbox select{width:100%;
    background:var(--panel);border:1px solid var(--line);border-radius:9px;color:var(--fg);
    padding:10px 12px;font-size:14px;outline:none;font-family:inherit}
  .cbox textarea{min-height:82px;resize:vertical}
  .cbtns{display:flex;gap:10px;margin-top:16px}
  .cbtns button{flex:1;border-radius:10px;padding:11px;background:var(--panel);color:var(--fg);font-size:14.5px}
  .cbtns button.prim{background:var(--acc);color:#fff;font-weight:600}
  .cbtns button:disabled{opacity:.5}
  .cprog{display:none;margin-top:14px;background:var(--panel);border-radius:10px;padding:10px 12px;
    font-size:12.5px;color:var(--mut);line-height:1.6}
  .cprog.on{display:block}
  .cprog .line{color:var(--fg);font-size:13px;margin-bottom:4px}
  .cprog .err{color:var(--heart);margin-top:6px;word-break:break-all}

  /* ---------- 网页导入（拖拽 / 文件选择） ---------- */
  #dropHint{position:fixed;inset:0;z-index:170;display:none;align-items:center;justify-content:center;
    flex-direction:column;gap:8px;pointer-events:none;background:rgba(0,0,0,.62);
    border:2px dashed rgba(10,132,255,.85);backdrop-filter:blur(3px)}
  #dropHint.on{display:flex}
  #dropHint .dh1{font-size:22px;font-weight:700}
  #dropHint .dh1 b{color:var(--acc)}
  #dropHint .dh2{font-size:13px;color:var(--mut)}

  #upModal{position:fixed;inset:0;z-index:160;background:rgba(0,0,0,.6);display:none;
    align-items:flex-end;justify-content:center}
  #upModal.on{display:flex}
  .ubox{width:100%;max-width:640px;max-height:84vh;display:flex;flex-direction:column;
    background:var(--panel);border-radius:16px 16px 0 0;overflow:hidden}
  .uhead{display:flex;align-items:center;padding:16px 18px 8px}
  .uhead h3{margin:0;font-size:17px;flex:1}
  .uhead .ux{font-size:18px;color:var(--mut);width:28px;text-align:right}
  .useg{display:flex;gap:8px;padding:2px 18px 10px}
  .useg button{flex:1;padding:9px 6px;border-radius:10px;background:var(--panel2);
    color:var(--mut);font-size:13.5px}
  .useg button.on{background:rgba(10,132,255,.18);color:var(--acc);font-weight:600}
  .uhint{padding:0 18px 12px;font-size:12.5px;color:var(--mut);line-height:1.6}
  .uhint b{color:#c7c7cc;font-weight:500}
  .ulist{flex:1;min-height:110px;overflow:auto;padding:0 18px}
  .ulist .uempty{color:var(--mut);font-size:13px;text-align:center;padding:34px 0 40px;line-height:1.8}
  .urow{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
  .urow:last-child{border-bottom:none}
  .umain{flex:1;min-width:0}
  .un{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ubar{height:3px;background:var(--panel2);border-radius:2px;margin-top:6px;overflow:hidden}
  .ubar i{display:block;height:100%;width:0;background:var(--acc);border-radius:2px;
    transition:width .12s linear}
  .urow.ok .ubar i{background:#30d158}
  .urow.dup .ubar i{background:#636366}
  .urow.err .ubar i{background:var(--heart)}
  .us{flex:none;width:78px;text-align:right;font-size:12px;color:var(--mut)}
  .urow.ok .us{color:#30d158}
  .urow.dup .us{color:var(--mut)}
  .urow.err .us{color:var(--heart)}
  .ufoot{display:flex;align-items:center;gap:10px;padding:12px 18px calc(16px + env(safe-area-inset-bottom))}
  .ufoot .ustat{flex:1;font-size:12.5px;color:var(--mut);line-height:1.45}
  .ufoot button{border-radius:10px;padding:10px 15px;background:var(--panel2);font-size:14px;flex:none}
  .ufoot button.prim{background:var(--acc);color:#fff;font-weight:600}
  .ufoot button:disabled{opacity:.45}

  /* ---------- toast ---------- */
  #toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%) translateY(20px);z-index:200;
    background:rgba(50,50,54,.96);border-radius:999px;padding:10px 18px;font-size:14px;display:flex;
    gap:14px;align-items:center;opacity:0;pointer-events:none;transition:.2s}
  #toast.on{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto}
  #toast button{color:var(--acc);font-weight:600}

  .spin{width:26px;height:26px;margin:26px auto;border:3px solid var(--panel2);border-top-color:var(--acc);
    border-radius:50%;animation:sp 1s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>

<div class="titlebar">
  <h1 id="viewTitle">图库</h1>
  <input class="search" id="q" type="search" placeholder="搜索">
  <button class="btn-txt" id="btnRename" style="display:none">重命名</button>
  <button class="btn-txt" id="btnMerge" style="display:none">合并</button>
  <button class="btn-txt" id="btnAll" style="display:none">全选</button>
  <button class="btn-txt" id="btnUpload">导入</button>
  <button class="btn-txt" id="btnSel">选择</button>
</div>
<div class="chips">
  <button class="chip on" data-view="all">图库</button>
  <button class="chip" data-view="material">🗂 素材库 <span class="cnt" id="matCnt"></span></button>
  <button class="chip" data-view="fav">❤️ <span class="cnt" id="favCnt"></span></button>
  <button class="chip" data-view="bin">✂️ 剪辑箱 <span class="cnt" id="binCnt"></span></button>
  <button class="chip" data-view="people">人物 <span class="cnt" id="personCnt"></span></button>
  <button class="chip" id="tagChip">标签</button>
  <button class="chip" data-view="cinema">🎞 影视 <span class="cnt" id="cineCnt"></span></button>
  <button class="chip" data-view="trash">🗑 <span class="cnt" id="trashCnt"></span></button>
  <span style="flex:1"></span>
  <span class="chip" style="background:none;color:var(--mut)" id="count"></span>
</div>
<div class="filters" id="filters">
  <span class="lbl">类型</span>
  <div class="fgroup" id="fKind">
    <button data-k="" class="on">全部</button><button data-k="video">🎬 视频</button><button data-k="photo">照片</button>
  </div>
  <div class="fgroup" id="fDur">
    <button data-d="" class="on">任意时长</button><button data-d="0-15">&lt;15s</button>
    <button data-d="15-60">15–60s</button><button data-d="60-300">1–5min</button><button data-d="300-">5min+</button>
  </div>
  <div class="fgroup" id="fPx">
    <button data-p="" class="on">任意清晰度</button><button data-p="1080">≥1080p</button><button data-p="2160">≥4K</button>
  </div>
</div>
<div id="binbar">
  <span id="binInfo" style="font-size:13px;color:var(--mut)"></span>
  <span id="bridgeStat" class="bstat" title="桌面桥负责打开资源管理器">桥 · 检测中</span>
  <span class="grow"></span>
  <button class="sec" id="binOpen">打开目录</button>
  <button class="sec" id="binRename">重命名</button>
  <button class="sec" id="binClear">清空</button>
  <button class="prim" id="binDeliver">出稿</button>
</div>
<div id="tagMenu"></div>

<main id="feed">
  <div id="people" style="display:none"><div class="pgrid" id="peopleGrid"></div></div>
  <div id="cinema" style="display:none">
    <div class="cinebar">
      <button class="cineadd" id="cineAdd">＋ 添加影视</button>
      <span class="cineinfo" id="cineInfo"></span>
      <span style="flex:1"></span>
      <button class="cinelink" id="cineRefresh">刷新</button>
    </div>
    <div id="cineGrid" class="cinewrap"></div>
    <div id="cineDetail" class="cinedetail" style="display:none"></div>
  </div>
  <div id="sections"></div>
  <div id="sentinel"></div>
</main>

<div id="selbar">
  <span id="selCount" style="font-size:13px;color:var(--mut);min-width:64px"></span>
  <span style="flex:1"></span>
  <button data-act="fav" id="sbFav"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20.7l-1.2-1.1C6 15.4 3 12.7 3 9.3 3 6.6 5.1 4.5 7.8 4.5c1.5 0 3 .7 4.2 2 1.2-1.3 2.7-2 4.2-2 2.7 0 4.8 2.1 4.8 4.8 0 3.4-3 6.1-7.8 10.3L12 20.7z"/></svg>喜欢</button>
  <button data-act="bin" id="sbBin"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16v13H4zM8 7V4h8v3M9 12h6"/></svg>剪辑箱</button>
  <button data-act="move" id="sbMove"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7h6l2 2h10v11H3z"/><path d="M12 12v5M9.5 14.5L12 12l2.5 2.5"/></svg>素材库</button>
  <button data-act="ai" id="sbAI" style="display:none"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M18.5 16.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>AI 识别</button>
  <button data-act="tag"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20.6 13.4L12.6 21.4a2 2 0 01-2.8 0L3 14.6V3h11.6l6 6a2 2 0 010 2.8z"/><circle cx="8" cy="8" r="1.6"/></svg>标签</button>
  <button data-act="trash" class="danger" id="sbTrash"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>删除</button>
  <button data-act="restore" id="sbRestore" style="display:none"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 12a9 9 0 109-9M3 3v6h6"/></svg>恢复</button>
  <button data-act="purge" class="danger" id="sbPurge" style="display:none"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>彻底删除</button>
</div>

<div id="lb">
  <div class="top">
    <button id="lbBack"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M15 4l-8 8 8 8"/></svg></button>
    <div class="dt" id="lbDate"></div>
    <span style="width:26px"></span>
  </div>
  <div class="stage" id="lbStage">
    <button class="nav prev" id="lbPrev"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M15 4l-8 8 8 8"/></svg></button>
    <button class="nav next" id="lbNext"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M9 4l8 8-8 8"/></svg></button>
  </div>
  <div class="under">
    <div class="cap" id="lbCap"></div>
    <div class="facerow" id="lbFaces" style="display:none"></div>
    <div class="vidbox" id="lbVideo" style="display:none">
      <div class="vcap" id="lbVideoCap"></div>
      <div class="vframes" id="lbVideoFrames"></div>
      <div class="vocr" id="lbVideoOcr"></div>
    </div>
    <div class="tagrow" id="lbTags"></div>
    <div class="tools">
      <button id="lbFav"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20.7l-1.2-1.1C6 15.4 3 12.7 3 9.3 3 6.6 5.1 4.5 7.8 4.5c1.5 0 3 .7 4.2 2 1.2-1.3 2.7-2 4.2-2 2.7 0 4.8 2.1 4.8 4.8 0 3.4-3 6.1-7.8 10.3L12 20.7z"/></svg>喜欢</button>
      <button id="lbBin"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16v13H4zM8 7V4h8v3M9 12h6"/></svg>剪辑箱</button>
      <button id="lbTag"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20.6 13.4L12.6 21.4a2 2 0 01-2.8 0L3 14.6V3h11.6l6 6a2 2 0 010 2.8z"/><circle cx="8" cy="8" r="1.6"/></svg>标签</button>
      <button id="lbPlay" style="display:none">▶ 播放实况</button>
      <button id="lbRestore" style="display:none"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 12a9 9 0 109-9M3 3v6h6"/></svg>恢复</button>
      <button id="lbPurge" class="danger" style="display:none"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>彻底删除</button>
      <button id="lbTrash" class="danger"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>删除</button>
    </div>
  </div>
</div>

<div id="sheet">
  <div class="grab"></div>
  <h3 id="sheetTitle">标签</h3>
  <div class="chips" id="sheetChips"></div>
  <div class="newrow">
    <input id="newTag" placeholder="新建标签…">
    <button class="go" id="addTag">添加</button>
  </div>
</div>

<div id="pick"><div class="box">
  <h3 id="pickTitle"></h3>
  <div class="list" id="pickList"></div>
  <button class="cancel danger" id="pickDissolve" style="display:none">取消这个人物（脸回到未归类）</button>
  <button class="cancel" id="pickCancel">取消</button>
</div></div>

<div id="cineModal" class="cmodal">
  <div class="cbox">
    <h3 id="cmTitle">添加影视</h3>
    <div id="cmBody"></div>
    <div class="cbtns">
      <button id="cmCancel">取消</button>
      <button id="cmOk" class="prim">开始导入</button>
    </div>
  </div>
</div>

<div id="cinePlay">
  <div class="cp-top">
    <button id="cpClose" class="cp-x">✕</button>
    <div class="cp-title" id="cpTitle"></div>
    <button id="cpOpen" class="cp-open">系统播放器</button>
  </div>
  <div class="cp-stage">
    <video id="cpVideo" controls playsinline preload="metadata"></video>
    <div class="cp-err" id="cpErr"></div>
  </div>
  <div class="cp-bottom">
    <select id="cpSub" class="cpsel"><option value="">字幕：关</option></select>
    <span style="flex:1"></span>
    <button id="cpPrev" class="cpnav">上一集</button>
    <button id="cpNext" class="cpnav">下一集</button>
  </div>
</div>

<div id="dropHint">
  <div class="dh1">松开，导入到 <b id="dhLib">图库</b></div>
  <div class="dh2">图片 / 视频直接进库，重复内容按哈希跳过</div>
</div>

<div id="upModal">
  <div class="ubox">
    <div class="uhead">
      <h3>导入</h3>
      <button class="ux" id="upClose">✕</button>
    </div>
    <div class="useg" id="upSeg">
      <button data-lib="gallery">📷 图库</button>
      <button data-lib="material">🗂 素材库</button>
    </div>
    <div class="uhint">把文件或文件夹拖到窗口任意位置，也可以选择文件。
      <b>HEIC / RAW / 视频 / Live Photo</b> 都支持；已经导入过的会自动跳过。</div>
    <div class="ulist" id="upList"></div>
    <div class="ufoot">
      <span class="ustat" id="upStat"></span>
      <button id="upPick">选择文件</button>
      <button id="upDone" class="prim">完成</button>
    </div>
  </div>
</div>
<input type="file" id="upInput" multiple style="display:none"
  accept="image/*,video/*,.heic,.heif,.jfif,.jpe,.dng,.cr2,.cr3,.nef,.arw,.raf,.orf,.rw2,.pef,.srw,.mts,.m2ts,.insv,.360,.lrv">

<div id="toast"><span id="toastMsg"></span><button id="toastAct" style="display:none"></button></div>

<script>
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const state = {
  view: 'all', tag: null,           // all | material | fav | trash | tag | people | person | bin | cinema
  library: 'gallery',               // gallery | material
  personId: null, personName: '',
  kind: '', dur: '', px: '',        // 剪辑筛选轴：类型 / 时长区间 / 最小短边
  binId: null, binCount: 0,         // 剪辑箱
  lastDir: localStorage.getItem('vault.lastDir') || '',   // 上次出稿目录（宿主路径）
  bridgeOk: null,                   // 桌面桥在线？null=还没探
  items: new Map(), order: [],      // 当前列表顺序（灯箱导航用）
  offset: 0, total: 0, loading: false,
  selMode: false, sel: new Set(),
  lbId: null, tagsCache: [], people: [],
  cinema: { titles: [], titleId: null, detail: null, season: 0,
            playing: null, playList: [], flat: [], pending: null },
  cineModal: null,
  mediaHost: '', mediaRoot: '',
  toastTimer: null, toastFn: null,
};

const fmtBytes = b => b >= 1<<30 ? (b/1073741824).toFixed(1)+' GB' : b >= 1<<20 ? (b/1048576).toFixed(0)+' MB' : (b/1024).toFixed(0)+' KB';
const fmtDur = ms => { if (!ms) return ''; const s = Math.round(ms/1000);
  return s >= 60 ? Math.floor(s/60)+':'+String(s%60).padStart(2,'0') : '0:'+String(s).padStart(2,'0'); };
const fmtLong = ms => { if (!ms) return ''; const s = Math.round(ms/1000);
  if (s < 60) return s + ' 秒';
  const h = Math.floor(s/3600), m = Math.round((s%3600)/60);
  return h ? `${h} 小时 ${m} 分` : `${m} 分钟`; };
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function dateKey(it){ return it.taken ? it.taken.slice(0,10) : 'unknown'; }
const dayFmt = new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'short'});
function dateHeader(k){
  if (k === 'unknown') return '时间未知';
  const d = new Date(k+'T00:00:00'), today = new Date(); today.setHours(0,0,0,0);
  const diff = Math.round((today - d)/864e5);
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  return dayFmt.format(d);
}
function lbDateStr(it){
  if (!it.taken) return '时间未知';
  const d = new Date(it.taken);
  return dayFmt.format(d) + ' ' + d.toTimeString().slice(0,5);
}

async function api(url, opt){ const r = await fetch(url, opt); if (!r.ok) throw new Error(await r.text()); return r.json(); }

/* ---------------- 数据加载 ---------------- */
function viewParams(){
  const p = new URLSearchParams({limit:120, offset:state.offset});
  if (state.view === 'fav') p.set('favorite','true');
  if (state.view === 'trash') p.set('trash','true');
  if (state.view === 'tag') p.set('tag', state.tag);
  if (state.view === 'person' && state.personId) p.set('person_id', state.personId);
  // 素材库 = 同一个 assets 表上的 library 轴：标签、人脸、视频摘要全部复用，
  // 只是默认视图互斥。收藏/标签跟着当前所在的库走；人物是跨库的实体视图，
  // 回收站也是全局的。
  if (!['trash','people','person','bin','cinema'].includes(state.view)) p.set('library', state.library);
  if ($('#q').value.trim()) p.set('q', $('#q').value.trim());
  // 剪辑视角的"视频"含 Live Photo：它有视频流，能剪
  if (state.kind) p.set('kind', state.kind === 'video' ? 'video,live_photo' : 'photo,raw');
  if (state.dur){
    const [a, b] = state.dur.split('-');
    if (a) p.set('min_sec', a);
    if (b) p.set('max_sec', b);
  }
  if (state.px) p.set('min_px', state.px);
  return p;
}

async function load(reset){
  if (state.loading) return;
  if (state.view === 'people'){ loadPeople(); return; }
  if (state.view === 'bin'){ loadBin(); return; }
  if (state.view === 'cinema'){ loadCinema(); return; }
  if (reset){ state.offset=0; state.total=0; state.items.clear(); state.order=[]; $('#sections').innerHTML=''; }
  if (state.offset >= state.total && !reset && state.total) return;
  state.loading = true;
  try {
    const r = await (await fetch('/api/assets?' + viewParams())).json();
    state.total = r.total;
    r.items.forEach(it => { state.items.set(it.id, it); state.order.push(it.id); append(it); });
    state.offset += r.items.length;
    $('#count').textContent = state.total + ' 项';
    if (reset && !state.total && state.view === 'material'){
      $('#sections').innerHTML = '<div class="empty">素材库是空的。<br><br>'
        + '到「图库」点右上角「选择」→ 勾选素材 → 底部「素材库」，<br>'
        + '就能把资源移进来。移进来的素材保留 AI 标签、人脸和视频摘要。</div>';
    }
  } finally { state.loading = false; }
}

/* ---------------- 剪辑箱 ---------------- */
// 剪辑是"跨多次搜索攒素材"的动作，所以箱子必须持久化（表 bins/bin_items），
// 而不是 JS 里的临时数组。默认只有一个箱子，id 缓存在 localStorage。

async function ensureBin(){
  const cached = localStorage.getItem('vault.binId');
  const r = await api('/api/bins');
  if (cached && r.bins.some(b => b.id === cached)){
    state.binId = cached;
    state.binCount = r.bins.find(b => b.id === cached).items;
    return;
  }
  if (!cached && r.bins.length){        // 有箱子但本地没记 → 复用最新的
    state.binId = r.bins[0].id; state.binCount = r.bins[0].items;
    localStorage.setItem('vault.binId', state.binId);
    return;
  }
  const c = await api('/api/bins', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name:'剪辑箱'})});
  localStorage.setItem('vault.binId', c.id);
  state.binId = c.id; state.binCount = 0;
}

async function refreshBinCount(){
  try {
    const r = await api('/api/bins');
    const b = r.bins.find(x => x.id === state.binId) || r.bins[0];
    if (b && !state.binId){ state.binId = b.id; }
    state.binCount = b ? b.items : 0;
    $('#binCnt').textContent = state.binCount || '';
  } catch (e){ /* 统计失败不影响主流程 */ }
}

async function addToBin(ids){
  if (!ids || !ids.length) return;
  if (!state.binId) await ensureBin();
  const r = await api(`/api/bins/${state.binId}/items`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids})
  });
  await refreshBinCount();
  exitSel();
  const extra = r.moved ? `，${r.moved} 项移到末尾` : '';
  toast(`已加入剪辑箱 ${r.added} 项${extra}`, '查看', () => setView('bin'));
}

async function loadBin(){
  if (!state.binId) await ensureBin();
  const r = await api(`/api/bins/${state.binId}/items`);
  state.items.clear(); state.order = []; state.total = r.items.length; state.offset = r.items.length;
  $('#sections').innerHTML = '';
  const durMs = r.items.reduce((s, x) => s + (x.duration_ms || 0), 0);
  const nBytes = r.items.reduce((s, x) => s + (x.bytes || 0), 0);
  const sec = document.createElement('div'); sec.className = 'sec';
  sec.innerHTML = `<h2>${r.items.length} 项 · ${fmtDur(durMs) || '0:00'} · ${fmtBytes(nBytes)}</h2><div class="grid"></div>`;
  const grid = sec.querySelector('.grid');
  r.items.forEach(it => {
    state.items.set(it.id, it); state.order.push(it.id);
    const c = buildCard(it);
    const n = document.createElement('span');
    n.className = 'bdg num'; n.textContent = String((it.ord || 0) + 1).padStart(3, '0');
    c.appendChild(n);
    const x = document.createElement('button');
    x.className = 'binx'; x.textContent = '✕'; x.title = '移出剪辑箱';
    x.onclick = ev => { ev.stopPropagation(); removeFromBin(it.id); };
    c.appendChild(x);
    grid.appendChild(c);
  });
  if (!r.items.length){
    sec.querySelector('h2').textContent = '剪辑箱是空的';
    grid.innerHTML = '<div style="grid-column:1/-1;padding:26px 14px;color:var(--mut);font-size:14px">' +
      '在任意视图里点右上角「选择」→ 勾选素材 → 底部「剪辑箱」，或者灯箱里点「剪辑箱」。' +
      '攒够了一批再回来点「出稿」。</div>';
  }
  $('#sections').appendChild(sec);
  $('#count').textContent = r.items.length + ' 项';
  refreshBinName();
}

async function removeFromBin(id){
  await api(`/api/bins/${state.binId}/items/${id}`, {method:'DELETE'});
  await refreshBinCount();
  toast('已移出剪辑箱', '撤销', async () => {
    await api(`/api/bins/${state.binId}/items`, {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids:[id]})});
    await refreshBinCount(); loadBin();
  });
  loadBin();
}

async function refreshBinName(){
  try {
    const r = await api('/api/bins');
    const b = r.bins.find(x => x.id === state.binId);
    $('#binInfo').textContent = b ? `「${b.name}」 ${b.items} 项` : '';
  } catch (e){ }
}

// 出稿：把箱子物化成 <derived>/cuts/<箱名>/ 下的真实文件（硬链接，0 字节）。
// 剪映不吃"粘贴文件列表"，所以交付终点就是**资源管理器里选中这些文件**，用户直接拖。
// 打开资源管理器这件事浏览器自己做不了（http 页面不许跳 file://），只能靠：
//   · 宿主模式（app 直接跑在 Windows 上）→ 后端 ShellExecute，返回 opened=true；
//   · 容器模式 → 前端问 127.0.0.1:8099 的桌面桥。
async function revealOnHost(path){
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 900);
    const r = await fetch('http://127.0.0.1:8099/reveal', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}), signal: ctrl.signal
    });
    clearTimeout(t);
    return r.ok;
  } catch (e){ return false; }
}

async function checkBridge(){
  const el = $('#bridgeStat');
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 700);
    const r = await fetch('http://127.0.0.1:8099/health', {signal: ctrl.signal});
    clearTimeout(t);
    state.bridgeOk = r.ok;
  } catch (e){ state.bridgeOk = false; }
  el.className = 'bstat ' + (state.bridgeOk ? 'ok' : 'bad');
  el.textContent = state.bridgeOk ? '桥 · 在线' : '桥 · 未启动';
  el.title = state.bridgeOk
    ? '「出稿」会自动打开资源管理器'
    : '桌面桥没跑：出稿后只会复制路径。启动一次即开机自启： uv run photo-vault bridge --install';
  return state.bridgeOk;
}

async function deliverBin(){
  if (!state.binCount) return toast('剪辑箱是空的');
  const btn = $('#binDeliver');
  btn.disabled = true; btn.textContent = '出稿中…';
  try {
    const r = await api(`/api/bins/${state.binId}/deliver`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode:'link'})
    });
    state.lastDir = r.host_dir;
    localStorage.setItem('vault.lastDir', r.host_dir);
    let opened = !!r.opened;                       // 宿主模式：后端已经开了
    if (!opened) opened = await revealOnHost(r.host_dir);
    if (!opened) { try { await navigator.clipboard.writeText(r.host_dir); } catch (e){} }

    let msg = `已出稿 ${r.items} 项 · ${fmtBytes(r.bytes)}`;
    if (r.missing) msg += ` · ${r.missing} 项原件缺失已跳过`;
    msg += opened ? ' · 已打开文件夹' : ' · 桥未启动，路径已复制';
    toast(msg, '复制路径', () => navigator.clipboard.writeText(r.host_dir));
    if (!opened) checkBridge();
  } catch (e){
    toast('出稿失败：' + String(e.message || e).slice(0, 80));
  } finally {
    btn.disabled = false; btn.textContent = '出稿';
  }
}

function sectionFor(k){
  let sec = document.getElementById('sec-'+k);
  if (!sec){
    sec = document.createElement('div'); sec.id = 'sec-'+k; sec.className = 'sec';
    sec.innerHTML = '<h2>'+esc(dateHeader(k))+'</h2><div class="grid"></div>';
    $('#sections').appendChild(sec);
  }
  return sec.querySelector('.grid');
}

const HEART_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="#fff"><path d="M12 20.7l-1.2-1.1C6 15.4 3 12.7 3 9.3 3 6.6 5.1 4.5 7.8 4.5c1.5 0 3 .7 4.2 2 1.2-1.3 2.7-2 4.2-2 2.7 0 4.8 2.1 4.8 4.8 0 3.4-3 6.1-7.8 10.3L12 20.7z"/></svg>';

function buildCard(it){
  const c = document.createElement('div');
  c.className = 'cell'; c.dataset.id = it.id;
  if (it.has_thumb){
    const img = document.createElement('img');
    img.loading = 'lazy'; img.src = `/thumbs/${it.sha}.webp`;
    c.appendChild(img);
  } else {
    const ph = document.createElement('div'); ph.className = 'ph';
    ph.textContent = it.kind === 'video' ? '▶' : 'RAW';
    c.appendChild(ph);
  }
  if (it.kind === 'video') { const b = document.createElement('span'); b.className='bdg dur'; b.textContent='▶ '+fmtDur(it.duration_ms); c.appendChild(b); }
  if (it.kind === 'live_photo'){ const b = document.createElement('span'); b.className='bdg live'; b.textContent='LIVE'; c.appendChild(b); }
  const h = document.createElement('span'); h.className='bdg heart'; h.innerHTML = HEART_SVG; h.style.display='none'; c.appendChild(h);
  const ck = document.createElement('div'); ck.className='check';
  ck.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3.2"><path d="M4.5 12.5l5 5 10-11"/></svg>';
  c.appendChild(ck);
  decorate(c, it);
  c.addEventListener('click', () => {
    if (state.selMode) toggleSel(it.id); else openLb(it.id);
  });
  return c;
}

function decorate(c, it){
  c.querySelector('.heart').style.display = it.favorite ? '' : 'none';
  c.classList.toggle('trashed', !!it.deleted);
  c.classList.toggle('sel', state.sel.has(it.id));
}

function append(it){
  sectionFor(dateKey(it)).appendChild(buildCard(it));
}

function refreshCard(id){
  const it = state.items.get(id); if (!it) return;
  const c = document.querySelector(`.cell[data-id="${id}"]`);
  if (c) decorate(c, it);
}
function removeCard(id){
  state.items.delete(id);
  state.order = state.order.filter(x => x !== id);
  state.sel.delete(id);
  const c = document.querySelector(`.cell[data-id="${id}"]`);
  if (c){ const sec = c.closest('.sec'); c.remove(); if (sec && !sec.querySelector('.cell')) sec.remove(); }
  state.total = Math.max(0, state.total - 1);
  $('#count').textContent = state.total + ' 项';
}

/* ---------------- 人物 ---------------- */
async function loadPeople(){
  const r = await api('/api/persons');
  state.people = r.persons;
  const grid = $('#peopleGrid');
  grid.innerHTML = '';
  if (!r.persons.length){
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1">还没有识别出人物。<br><br>'
      + '先跑 <code>ml.enqueue --kind faces</code> 入队、<code>ml.worker --task faces</code> 提取，<br>'
      + '再跑 <code>ml.cluster</code> 聚类。<br><br>'
      + (r.unassigned_faces ? `当前有 ${r.unassigned_faces} 张未归类的人脸。` : '') + '</div>';
    return;
  }
  r.persons.forEach(p => {
    const c = document.createElement('div');
    c.className = 'pcard';
    const letter = (p.name || '?').replace(/^Person\s+/i, '').slice(0, 1) || '?';
    c.innerHTML = `<div class="av">${p.cover
        ? `<img loading="lazy" src="/faces/${p.cover}.jpg" alt="" onerror="this.parentNode.textContent='${esc(letter)}'">`
        : esc(letter)}</div>
      <div class="nm">${esc(p.name || '未命名')}</div>
      <div class="ct">${p.n_assets} 张</div>`;
    c.onclick = () => openPerson(p);
    grid.appendChild(c);
  });
}

function openPerson(p){
  if (!p) return;
  setView('person', null, p);
}

function showPick(title, people, onPick, onDissolve){
  $('#pickTitle').textContent = title;
  const list = $('#pickList');
  list.innerHTML = '';
  people.forEach(p => {
    const row = document.createElement('div');
    row.className = 'pi';
    row.innerHTML = `${p.cover ? `<img src="/faces/${p.cover}.jpg" alt="" onerror="this.style.visibility='hidden'">` : ''}
      <span class="n">${esc(p.name || '未命名')}</span><span class="c">${p.n_assets} 张</span>`;
    row.onclick = () => onPick(p);
    list.appendChild(row);
  });
  if (!people.length){
    list.innerHTML = '<div class="pi" style="color:var(--mut);cursor:default">没有其他人物</div>';
  }
  const dis = $('#pickDissolve');
  if (onDissolve){
    dis.style.display = '';
    dis.onclick = onDissolve;
  } else {
    dis.style.display = 'none';
    dis.onclick = null;
  }
  $('#pick').classList.add('on');
}
function hidePick(){ $('#pick').classList.remove('on'); }
$('#pickCancel').onclick = hidePick;
$('#pick').addEventListener('click', e => { if (e.target.id === 'pick') hidePick(); });

$('#btnRename').onclick = async () => {
  const name = prompt('给这个人物起个名字', state.personName || '');
  if (name == null) return;
  try {
    const r = await api(`/api/persons/${state.personId}`, {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})
    });
    state.personName = r.name;
    $('#viewTitle').textContent = r.name;
    toast('已改名');
  } catch (e){ toast('改名失败：' + String(e.message).replace(/^.*detail"?:"?/,'').slice(0,40)); }
};

$('#btnMerge').onclick = async () => {
  await loadPeople();
  const others = state.people.filter(p => p.id !== state.personId);
  const pid = state.personId, pname = state.personName;
  showPick(`把「${pname}」合并到…`, others, async p => {
    hidePick();
    try {
      await api(`/api/persons/${pid}/merge`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({into: p.id})
      });
      state.personId = p.id; state.personName = p.name;
      setView('person', null, p);
      toast(`已合并到「${p.name}」`);
    } catch (e){ toast('合并失败'); }
  }, async () => {
    if (!confirm(`取消「${pname}」？\n它的脸会回到"未归类"，下次 ml.cluster 可能重新分组。`)) return;
    hidePick();
    try {
      await api(`/api/persons/${pid}`, {method:'DELETE'});
      setView('people');
      toast('已取消这个人物');
    } catch (e){ toast('操作失败'); }
  });
};

/* ---------------- 视图切换 ---------------- */
function setView(v, tag, person){
  if (v === 'person' && !person) v = 'people';
  if (v === 'all') state.library = 'gallery';
  else if (v === 'material') state.library = 'material';
  state.view = v; state.tag = tag || null;
  if (person){ state.personId = person.id; state.personName = person.name; }
  exitSel();
  closeCinePlayer();
  if (v !== 'cinema'){ state.cinema.titleId = null; }
  const libViews = ['fav', 'tag'];
  const libChip = state.library === 'material' ? 'material' : 'all';
  $$('.chips .chip[data-view]').forEach(ch => {
    let on = ch.dataset.view === v;
    if (!on && libViews.includes(v)) on = ch.dataset.view === libChip;
    ch.classList.toggle('on', on);
  });
  const tagChip = $('#tagChip');
  tagChip.classList.toggle('on', v === 'tag');
  tagChip.innerHTML = v === 'tag'
    ? `🏷 ${esc(state.tag)} <span style="color:var(--mut)">✕</span>`
    : '标签';
  let title = v === 'fav' ? '喜欢'
    : v === 'trash' ? '最近删除'
    : v === 'tag' ? state.tag
    : v === 'people' ? '人物'
    : v === 'person' ? (state.personName || '人物')
    : v === 'bin' ? '剪辑箱'
    : v === 'cinema' ? '影视'
    : v === 'material' ? '素材库'
    : '图库';
  if (state.library === 'material' && libViews.includes(v)) title += ' · 素材库';
  $('#viewTitle').textContent = title;
  const isPeople = v === 'people';
  const isBin = v === 'bin';
  const isCinema = v === 'cinema';
  $('#cinema').style.display = isCinema ? '' : 'none';
  $('#people').style.display = isPeople ? '' : 'none';
  $('#sections').style.display = (isPeople || isCinema) ? 'none' : '';
  $('#sentinel').style.display = (isPeople || isBin || isCinema) ? 'none' : '';
  $('#filters').style.display = (isPeople || isBin || isCinema) ? 'none' : '';
  $('#binbar').classList.toggle('on', isBin);
  $('#btnRename').style.display = v === 'person' ? '' : 'none';
  $('#btnMerge').style.display = v === 'person' ? '' : 'none';
  $('#btnSel').style.display = (isPeople || isBin || isCinema) ? 'none' : '';
  $('#q').placeholder = isCinema ? '搜索影视' : '搜索';
  $('#sbFav').style.display = v === 'trash' ? 'none' : '';
  $$('#selbar [data-act=tag]').forEach(b => b.style.display = v === 'trash' ? 'none' : '');
  $('#sbTrash').style.display = v === 'trash' ? 'none' : '';
  $('#sbRestore').style.display = v === 'trash' ? '' : 'none';
  $('#sbPurge').style.display = v === 'trash' ? '' : 'none';
  const movable = !['trash', 'people', 'bin', 'cinema'].includes(v);
  $('#sbMove').style.display = movable ? '' : 'none';
  $('#sbMove').lastChild.textContent = state.library === 'material' ? '图库' : '素材库';
  $('#sbAI').style.display = (movable && state.library === 'material') ? '' : 'none';
  if (isPeople){ loadPeople(); return; }
  if (isCinema){ loadCinema(); return; }
  load(true);
}

/* ---------------- 选择模式 ---------------- */
function toggleSel(id){
  state.sel.has(id) ? state.sel.delete(id) : state.sel.add(id);
  refreshCard(id);
  updateSelbar();
}
function updateSelbar(){
  $('#selCount').textContent = state.sel.size ? '已选 '+state.sel.size+' 项' : '';
  const one = state.sel.size === 1 ? state.items.get([...state.sel][0]) : null;
  $('#sbFav').lastChild.textContent = one && one.favorite ? '取消喜欢' : '喜欢';
  $('#btnAll').textContent = state.sel.size && state.sel.size === state.order.length ? '取消全选' : '全选';
}
function enterSel(){ state.selMode = true; document.body.classList.add('selecting'); $('#btnSel').textContent='取消'; $('#btnAll').style.display=''; $('#selbar').classList.add('on'); updateSelbar(); }
function exitSel(){ state.selMode = false; state.sel.clear(); document.body.classList.remove('selecting'); $('#btnSel').textContent='选择'; $('#btnAll').style.display='none'; $('#selbar').classList.remove('on'); $$('.cell.sel').forEach(c => c.classList.remove('sel')); }
$('#btnAll').onclick = () => {
  if (state.sel.size === state.order.length) state.sel.clear();
  else state.order.forEach(id => state.sel.add(id));
  $$('.cell').forEach(c => { const it = state.items.get(c.dataset.id); if (it) decorate(c, it); });
  updateSelbar();
};

async function batch(action, tag){
  const ids = [...state.sel]; if (!ids.length) return;
  const r = await api('/api/assets/batch', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids, action, tag})
  });
  if (action === 'trash' && r.updated){
    const undone = () => api('/api/assets/batch',{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids, action:'restore'})}).then(()=>{ ids.forEach(refreshCardAfterRestore); loadStats(); });
    ids.forEach(removeCard);
    toast(`已将 ${r.updated} 项移入最近删除`, '撤销', undone);
  } else if (action === 'restore'){
    ids.forEach(removeCard);
    toast(`已恢复 ${r.updated} 项`);
  } else if (action === 'purge'){
    ids.forEach(removeCard);
    toast(`已彻底删除 ${r.updated} 项`);
  } else if (action === 'favorite' || action === 'unfavorite'){
    ids.forEach(id => { const it = state.items.get(id); if (it){ it.favorite = action==='favorite'; refreshCard(id); } });
    toast(action==='favorite' ? `已喜欢 ${r.updated} 项` : `已取消 ${r.updated} 项`);
  } else if (action === 'tag'){ toast(`已为 ${r.updated} 项添加「${tag}」`); loadTags(); }
  else if (action === 'untag'){ toast(`已移除 ${r.updated} 项的「${tag}」`); loadTags(); }
  loadStats();
  exitSel();
}
function refreshCardAfterRestore(id){ /* 恢复后当前视图（回收站）里不再显示，无需刷新卡片 */ }

/* ---------------- 素材库 ---------------- */
// 换归属，不是复制：同一件资产不会同时出现在两个库里。AI 结果（标签/人脸/视频摘要）
// 跟着资产走，所以"移进素材库"之后照样能按标签、人物筛。
async function moveSelection(target){
  const ids = [...state.sel];
  if (!ids.length) return;
  try {
    const r = await api('/api/assets/batch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids, action:'move', library: target})
    });
    ids.forEach(removeCard);
    exitSel();
    loadStats();
    if (!state.total) load(true);          // 搬空了就显示空状态提示
    toast(target === 'material' ? `已移入素材库 ${r.updated} 项` : `已移回图库 ${r.updated} 项`,
          '去看看', () => setView(target === 'material' ? 'material' : 'all'));
  } catch (e){ toast('移动失败：' + errText(e).slice(0, 60)); }
}

async function runAI(){
  const ids = state.sel.size ? [...state.sel] : state.order.slice();
  if (!ids.length) return toast('没有可识别的素材');
  try {
    const r = await api('/api/assets/analyze', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids})
    });
    if (r.queued) toast(`已排队 ${r.queued} 个 AI 任务（打标 / 人脸 / 视频）`);
    else toast('这些素材已经识别过，或已经在队列里了');
  } catch (e){ toast('入队失败：' + errText(e).slice(0, 60)); }
}

$('#selbar').addEventListener('click', async e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const act = b.dataset.act;
  if (act === 'fav'){
    const one = state.sel.size === 1 ? state.items.get([...state.sel][0]) : null;
    batch(one && one.favorite ? 'unfavorite' : 'favorite');
  }
  else if (act === 'tag') openSheet([...state.sel]);
  else if (act === 'bin') addToBin([...state.sel]);
  else if (act === 'move') moveSelection(state.library === 'material' ? 'gallery' : 'material');
  else if (act === 'ai') { await runAI(); exitSel(); }
  else if (act === 'trash') batch('trash');
  else if (act === 'restore') batch('restore');
  else if (act === 'purge') { if (confirm(`彻底删除 ${state.sel.size} 项？库记录将被移除，不可恢复。`)) batch('purge'); }
});
$('#btnSel').onclick = () => state.selMode ? exitSel() : enterSel();

/* ---------------- 剪辑箱操作条 ---------------- */
$('#binDeliver').onclick = deliverBin;
$('#binOpen').onclick = async () => {
  if (!state.lastDir) return toast('还没出稿过——先点「出稿」');
  if (await revealOnHost(state.lastDir)) return;
  toast('桌面桥未启动', '复制命令', () =>
    navigator.clipboard.writeText('uv run photo-vault bridge --install'));
};
$('#binClear').onclick = async () => {
  if (!state.binCount || !confirm('清空剪辑箱？素材库和出稿目录都不受影响。')) return;
  await api(`/api/bins/${state.binId}/items`, {method:'DELETE'});
  await refreshBinCount(); loadBin(); toast('剪辑箱已清空');
};
$('#binRename').onclick = async () => {
  const name = prompt('剪辑箱名字（同时决定出稿目录名）');
  if (!name) return;
  try {
    await api(`/api/bins/${state.binId}`, {method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})});
    refreshBinName(); toast('已改名');
  } catch (e){ toast('改名失败'); }
};

/* ---------------- 灯箱 ---------------- */
function openLb(id){
  state.lbId = id;
  renderLb();
  $('#lb').classList.add('on');
  document.body.style.overflow = 'hidden';
}
function closeLb(){
  $('#lb').classList.remove('on');
  $('#lbStage').querySelectorAll('img,video').forEach(e => e.remove());
  state.lbId = null;
  document.body.style.overflow = '';
}
function lbIdx(){ return state.order.indexOf(state.lbId); }
function navLb(d){
  const i = lbIdx() + d;
  if (i >= 0 && i < state.order.length) { state.lbId = state.order[i]; renderLb(); }
}
function renderLb(){
  const it = state.items.get(state.lbId); if (!it) return closeLb();
  const stage = $('#lbStage');
  stage.querySelectorAll('img,video').forEach(e => e.remove());
  const before = stage.querySelector('.nav.prev');
  let el;
  if (it.kind === 'video'){
    el = document.createElement('video');
    el.src = `/file/${it.id}`; el.controls = true; el.autoplay = true;
  } else {
    el = document.createElement('img');
    el.src = `/preview/${it.id}`;
  }
  stage.insertBefore(el, before);
  $('#lbDate').textContent = lbDateStr(it);
  $('#lbCap').textContent = `${it.model} · ${it.width||'?'}×${it.height||'?'} · ${fmtBytes(it.bytes)}`;
  $('#lbTrash').style.display = state.view === 'trash' ? 'none' : '';
  $('#lbFav').style.display = state.view === 'trash' ? 'none' : '';
  $('#lbTag').style.display = state.view === 'trash' ? 'none' : '';
  $('#lbRestore').style.display = state.view === 'trash' ? '' : 'none';
  $('#lbPurge').style.display = state.view === 'trash' ? '' : 'none';
  const favBtn = $('#lbFav');
  favBtn.classList.toggle('on', !!it.favorite);
  favBtn.querySelector('svg').setAttribute('fill', it.favorite ? 'currentColor' : 'none');
  $('#lbPlay').style.display = it.kind === 'live_photo' ? '' : 'none';
  renderLbTags(it);
  renderLbFaces(it);
  renderLbVideo(it);
}
async function renderLbVideo(it){
  const box = $('#lbVideo'), row = $('#lbVideoFrames');
  box.style.display = 'none'; row.innerHTML = '';
  $('#lbVideoCap').textContent = ''; $('#lbVideoOcr').textContent = '';
  if (it.kind !== 'video') return;
  let d = null;
  try { d = await api(`/api/assets/${it.id}/video`); } catch (e){ return; }
  if (state.lbId !== it.id || !d || !d.summary) return;   // 已经翻页 / 没打标
  const s = d.summary;
  if (!s.caption && !s.ocr && !d.frames.length) return;
  if (s.caption) $('#lbVideoCap').textContent = 'AI 描述：' + s.caption;
  if (s.ocr) $('#lbVideoOcr').textContent = '画面文字：' + s.ocr;
  const tstr = ms => ms >= 60000 ? fmtDur(ms) : (ms/1000).toFixed(1) + 's';
  d.frames.forEach(f => {
    const b = document.createElement('button');
    b.className = 'vframe';
    b.innerHTML = `<img loading="lazy" src="${f.url}" alt=""><span class="t">${tstr(f.t_ms)}</span>`;
    b.onclick = () => {
      const v = $('#lbStage video'); if (!v) return;
      v.currentTime = f.t_ms / 1000;
      v.play().catch(() => {});
      $$('#lbVideoFrames .vframe').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
    };
    row.appendChild(b);
  });
  box.style.display = '';
}
async function renderLbFaces(it){
  const row = $('#lbFaces');
  row.innerHTML = '';
  row.style.display = 'none';
  let faces = [];
  try { faces = (await api(`/api/assets/${it.id}/faces`)).faces; } catch (e){ return; }
  if (state.lbId !== it.id || !faces.length) return;   // 已经翻页 / 没有脸
  row.style.display = '';
  faces.forEach(f => {
    const b = document.createElement('button');
    b.className = 'facechip' + (f.person_id ? '' : ' none');
    b.innerHTML = `<img loading="lazy" src="/faces/${f.id}.jpg" alt=""><span>${esc(f.person || '未归类')}</span>`;
    if (f.person_id) b.onclick = () => { closeLb(); openPerson({id: f.person_id, name: f.person}); };
    row.appendChild(b);
  });
}
function renderLbTags(it){
  const row = $('#lbTags'); row.innerHTML = '';
  (it.tags || []).forEach(t => {
    const s = document.createElement('span'); s.className = 'tagchip';
    s.innerHTML = esc(t) + ' <span class="x">✕</span>';
    s.querySelector('.x').onclick = async () => {
      const r = await api(`/api/assets/${it.id}/tags/${encodeURIComponent(t)}`, {method:'DELETE'});
      it.tags = r.tags; renderLbTags(it);
    };
    row.appendChild(s);
  });
  const add = document.createElement('button'); add.className = 'tagchip'; add.textContent = '＋ 标签';
  add.onclick = () => openSheet([it.id]);
  row.appendChild(add);
}
$('#lbBack').onclick = closeLb;
$('#lbPrev').onclick = () => navLb(-1);
$('#lbNext').onclick = () => navLb(1);
$('#lbFav').onclick = async () => {
  const it = state.items.get(state.lbId);
  const r = await api(`/api/assets/${it.id}/favorite`, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({favorite: !it.favorite})});
  it.favorite = r.favorite; refreshCard(it.id); renderLb();
};
$('#lbBin').onclick = () => addToBin([state.lbId]);
$('#lbTrash').onclick = async () => {
  const it = state.items.get(state.lbId);
  await api(`/api/assets/${it.id}/trash`, {method:'POST'});
  const id = it.id, idx = lbIdx();
  removeCard(id);
  closeLb();
  toast('已移入最近删除', '撤销', async () => {
    await api(`/api/assets/${id}/restore`, {method:'POST'});
    load(true); loadStats();
  });
  const next = Math.min(idx, state.order.length - 1);
  if (next >= 0){ state.lbId = state.order[next]; renderLb(); }
  else { load(true); }
};
$('#lbRestore').onclick = async () => {
  const id = state.lbId;
  await api(`/api/assets/${id}/restore`, {method:'POST'});
  removeCard(id); closeLb(); toast('已恢复'); loadStats();
};
$('#lbPurge').onclick = async () => {
  const it = state.items.get(state.lbId);
  if (!confirm('彻底删除这项资产？库记录将被移除，不可恢复。')) return;
  await api(`/api/assets/${it.id}`, {method:'DELETE'});
  removeCard(it.id); closeLb(); toast('已彻底删除'); loadStats();
};
$('#lbPlay').onclick = () => {
  const it = state.items.get(state.lbId);
  const stage = $('#lbStage');
  stage.querySelectorAll('img,video').forEach(e => e.remove());
  const v = document.createElement('video');
  v.src = `/file/${it.id}?role=live_motion`; v.controls = true; v.autoplay = true;
  stage.insertBefore(v, stage.querySelector('.nav.prev'));
};

document.addEventListener('keydown', e => {
  if ($('#lb').classList.contains('on')){
    if (e.key === 'Escape') closeLb();
    if (e.key === 'ArrowLeft') navLb(-1);
    if (e.key === 'ArrowRight') navLb(1);
  } else if (e.key === 'Escape' && state.selMode) exitSel();
});

/* ---------------- 影视库 ---------------- */
// 影视只保存：不搬文件、不算哈希、不跑 AI。登记的是 (root_key, rel_path)，
// 播放靠 /api/cinema/episodes/{id}/stream（支持 Range 拖动），字幕按需转 VTT。
// MKV/AVI 浏览器多半放不了 → 播放器右上角「系统播放器」走宿主机桥。

function errText(e){
  const s = String((e && e.message) || e || '');
  try {
    const j = JSON.parse(s);
    if (j && j.detail) return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
  } catch (_){ }
  return s;
}
const pad2 = n => String(n || 0).padStart(2, '0');

async function loadCinema(){
  const qtext = $('#q').value.trim();
  const r = await api('/api/cinema' + (qtext ? '?q=' + encodeURIComponent(qtext) : ''));
  state.cinema.titles = r.titles;
  if (r.media_host_root) state.mediaHost = r.media_host_root;
  if (r.media_root) state.mediaRoot = r.media_root;
  state.cinema.titleId = null;
  state.cinema.detail = null;
  $('#cineDetail').style.display = 'none';
  $('#cineDetail').innerHTML = '';
  $('#cineGrid').style.display = '';
  renderCinemaGrid(r.titles);
  const nMovie = r.titles.filter(t => t.kind === 'movie').length;
  const nSeries = r.titles.length - nMovie;
  $('#cineInfo').textContent = r.titles.length ? `${nSeries} 部剧集 · ${nMovie} 部电影` : '';
  $('#count').textContent = r.titles.length ? r.titles.length + ' 个条目' : '';
  if (state.cinema.pending){
    const id = state.cinema.pending;
    state.cinema.pending = null;
    await openCinema(id);
  }
}

function renderCinemaGrid(titles){
  const wrap = $('#cineGrid');
  wrap.innerHTML = '';
  if (!titles.length){
    const dir = state.mediaHost || state.mediaRoot;
    wrap.innerHTML = '<div class="cineempty">影视库还是空的。<br><br>'
      + (dir ? `把电影/剧集放进影视目录 <b>${esc(dir)}</b>，<br>` : '把电影/剧集放进影视目录，<br>')
      + '点「＋ 添加影视」填目录，或者命令行跑 '
      + '<code>photo-vault cinema-import E:\\Movies</code>。<br><br>'
      + '只登记路径：不搬文件、不算哈希、不跑 AI。</div>';
    return;
  }
  [['series', '剧集'], ['movie', '电影']].forEach(([kind, label]) => {
    const group = titles.filter(t => t.kind === kind);
    if (!group.length) return;
    const sec = document.createElement('div'); sec.className = 'cinegroup';
    const h = document.createElement('h2'); h.textContent = `${label} · ${group.length}`;
    const grid = document.createElement('div'); grid.className = 'cinegrid';
    group.forEach(t => grid.appendChild(cineCard(t)));
    sec.appendChild(h); sec.appendChild(grid);
    wrap.appendChild(sec);
  });
}

function cineCard(t){
  const el = document.createElement('div');
  el.className = 'cinecard';
  const meta = t.kind === 'series'
    ? [t.year, `${t.n_seasons || 1} 季 ${t.n_episodes} 集`].filter(Boolean).join(' · ')
    : [t.year, t.duration_ms ? fmtDur(t.duration_ms) : ''].filter(Boolean).join(' · ');
  el.innerHTML = '<div class="poster">'
    + (t.poster ? `<img loading="lazy" src="${t.poster}" alt="" onerror="this.style.display='none'">` : '')
    + `<span class="ph" style="${t.poster ? 'display:none' : ''}">${esc((t.title || '?').slice(0, 1))}</span>`
    + `<span class="pbadge">${t.kind === 'series' ? '剧集' : '电影'}</span>`
    + (t.kind === 'series' ? `<span class="pcount">${t.n_episodes} 集</span>` : '')
    + '</div>'
    + `<div class="ctitle">${esc(t.title)}</div>`
    + `<div class="cmeta">${esc(meta)}</div>`;
  el.onclick = () => openCinema(t.id);
  return el;
}

async function openCinema(id){
  try {
    const d = await api('/api/cinema/' + id);
    state.cinema.titleId = id;
    state.cinema.detail = d;
    const seasons = d.seasons.filter(s => s.season > 0);
    state.cinema.season = seasons.length ? seasons[0].season : 0;
    renderCinemaDetail(d);
  } catch (e){ toast('打不开这个条目：' + errText(e).slice(0, 60)); }
}

function renderCinemaDetail(d){
  const box = $('#cineDetail');
  const isMovie = d.kind === 'movie';
  const seasons = d.seasons.filter(s => s.season > 0);
  const specials = !isMovie
    ? d.seasons.find(s => s.season === 0 && (s.episodes || []).length) : null;
  const tabs = specials ? seasons.concat(specials) : seasons;
  const tabLabel = s => s.season === 0 ? '特别篇' : `第 ${s.season} 季`;
  $('#cineGrid').style.display = 'none';
  box.style.display = '';
  box.innerHTML = ''
    + '<button class="cback" id="cBack">‹ 影视库</button>'
    + '<div class="chead">'
    + `<div class="cposter">${d.poster
        ? `<img src="${d.poster}" alt="" onerror="this.style.display='none'">`
        : `<span class="ph">${esc((d.title || '?').slice(0, 1))}</span>`}</div>`
    + '<div class="cmain">'
    + `<h2>${esc(d.title)}${d.year ? ` <span class="cyear">(${d.year})</span>` : ''}</h2>`
    + `<div class="cmeta">${isMovie ? '电影' : `剧集 · ${seasons.length} 季 · ${d.n_episodes} 集`}`
    + ` · ${fmtLong(d.duration_ms) || '时长未知'} · ${fmtBytes(d.bytes)}</div>`
    + '<div class="cacts">'
    + '<button class="prim2" id="cPlay">▶ 播放</button>'
    + (d.can_rescan ? '<button id="cRescan">重新扫描</button>' : '')
    + '<button id="cEdit">编辑</button>'
    + '<button class="danger" id="cDel">删除</button>'
    + '</div></div></div>'
    + (d.overview ? `<div class="coverview">${esc(d.overview)}</div>` : '')
    + (tabs.length > 1
        ? `<div class="cseasons">${tabs.map(s => `<button class="cseason${s.season === state.cinema.season ? ' on' : ''}" data-season="${s.season}">${tabLabel(s)}</button>`).join('')}</div>`
        : '')
    + '<div class="eplist" id="cEplist"></div>';
  $('#cineInfo').textContent = '';
  $('#count').textContent = `${d.n_episodes} 集`;

  $('#cBack').onclick = () => loadCinema();
  const first = () => {
    const ep = (state.cinema.flat || []).find(x => !x.missing);
    if (ep) playEpisode(d, ep, state.cinema.flat);
    else toast('这一季没有可播放的剧集');
  };
  $('#cPlay').onclick = first;
  if ($('#cEdit')) $('#cEdit').onclick = () => openCineModal('edit');
  if ($('#cDel')) $('#cDel').onclick = deleteCine;
  if ($('#cRescan')) $('#cRescan').onclick = rescanCine;
  $$('#cineDetail .cseason').forEach(b => b.onclick = () => {
    state.cinema.season = parseInt(b.dataset.season, 10);
    $$('#cineDetail .cseason').forEach(x => x.classList.toggle('on', x === b));
    renderEpisodes(d, state.cinema.season);
  });
  renderEpisodes(d, state.cinema.season);
}

function renderEpisodes(d, season){
  const list = $('#cEplist');
  const eps = (d.seasons.find(s => s.season === season) || {}).episodes || [];
  state.cinema.flat = eps;
  list.innerHTML = '';
  if (!eps.length){ list.innerHTML = '<div class="cineempty">这一季还没有登记剧集</div>'; return; }
  eps.forEach(ep => {
    const subs = sortSubs(ep.subtitles || []);
    const meta = [
      ep.duration_ms ? fmtDur(ep.duration_ms) : '',
      ep.bytes ? fmtBytes(ep.bytes) : '',
      ep.width ? `${ep.width}×${ep.height}` : '',
      ep.missing ? '文件缺失' : '',
    ].filter(Boolean).join(' · ');
    const row = document.createElement('div');
    row.className = 'eprow' + (ep.missing ? ' missing' : '');
    row.innerHTML = `<div class="epno">${d.kind === 'series' ? 'E' + pad2(ep.episode) : '正片'}</div>`
      + '<div class="epmain">'
      + `<div class="epname">${esc(ep.name || (d.kind === 'series' ? `第 ${ep.episode} 集` : d.title))}</div>`
      + `<div class="epmeta">${esc(meta)}</div>`
      + (subs.length ? `<div class="epsubs">${subs.map(s => `<span class="subtag">${esc(s.label)}</span>`).join('')}</div>` : '')
      + '</div><div class="epplay">▶</div>';
    row.onclick = () => { if (!ep.missing) playEpisode(d, ep, eps); };
    list.appendChild(row);
  });
}

function sortSubs(subs){
  const score = s => (s.forced ? 4 : 0)
    + (s.lang && s.lang.indexOf('zh') === 0 ? 0 : 1)
    + (s.label === '字幕' ? 2 : 0);
  return subs.slice().sort((a, b) => score(a) - score(b));
}

function playEpisode(d, ep, list){
  state.cinema.playing = ep;
  state.cinema.playList = list || state.cinema.flat || [ep];
  const v = $('#cpVideo');
  v.pause();
  v.removeAttribute('src');
  try { v.load(); } catch (_){ }
  $('#cpErr').classList.remove('on');
  $('#cpTitle').textContent = d.title
    + (d.kind === 'series' ? ` · S${pad2(ep.season)}E${pad2(ep.episode)}` : '');
  const subs = sortSubs(ep.subtitles || []);
  const sel = $('#cpSub');
  sel.innerHTML = '<option value="">字幕：关</option>'
    + subs.map(s => `<option value="${s.id}">${esc(s.label)}${s.format === 'ass' ? ' · ASS' : ''}</option>`).join('');
  v.src = `/api/cinema/episodes/${ep.id}/stream`;
  v.load();
  v.play().catch(() => { });
  if (subs.length){ sel.value = String(subs[0].id); applySubtitle(); }
  else { sel.value = ''; removeTracks(); }
  const idx = state.cinema.playList.findIndex(x => x.id === ep.id);
  $('#cpPrev').disabled = idx <= 0;
  $('#cpNext').disabled = idx < 0 || idx >= state.cinema.playList.length - 1;
  $('#cinePlay').classList.add('on');
  document.body.style.overflow = 'hidden';
}

function removeTracks(){
  const v = $('#cpVideo');
  if (v) v.querySelectorAll('track').forEach(t => t.remove());
}
function applySubtitle(){
  removeTracks();
  const id = $('#cpSub').value;
  if (!id) return;
  const opt = $('#cpSub').selectedOptions[0];
  const t = document.createElement('track');
  t.kind = 'subtitles';
  t.label = opt ? opt.textContent : '字幕';
  t.srclang = 'zh';
  t.default = true;
  t.src = `/api/cinema/subtitles/${id}`;
  $('#cpVideo').appendChild(t);
  setTimeout(() => { try { if (t.track) t.track.mode = 'showing'; } catch (_){ } }, 200);
}
function closeCinePlayer(){
  const v = $('#cpVideo');
  if (!v) return;
  v.pause();
  v.removeAttribute('src');
  removeTracks();
  try { v.load(); } catch (_){ }
  $('#cinePlay').classList.remove('on');
  document.body.style.overflow = '';
  state.cinema.playing = null;
}
function stepEpisode(delta){
  const list = state.cinema.playList || [];
  const cur = state.cinema.playing;
  if (!cur) return;
  const idx = list.findIndex(x => x.id === cur.id);
  const next = list[idx + delta];
  if (next && !next.missing) playEpisode(state.cinema.detail, next, list);
}

async function bridgeOpen(path){
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 1500);
    const r = await fetch('http://127.0.0.1:8099/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }), signal: ctrl.signal
    });
    clearTimeout(t);
    return r.ok;
  } catch (_){ return false; }
}

async function openExternal(){
  const ep = state.cinema.playing;
  if (!ep) return;
  try {
    const r = await api(`/api/cinema/episodes/${ep.id}/open`, { method: 'POST' });
    if (r.opened){ toast('已用系统播放器打开'); return; }
    if (await bridgeOpen(r.host_path)){ toast('已用系统播放器打开'); return; }
    try { await navigator.clipboard.writeText(r.host_path); } catch (_){ }
    toast('桌面桥未启动，路径已复制；也可在资源管理器里打开');
  } catch (e){ toast('打不开：' + errText(e).slice(0, 60)); }
}

/* ---------- 添加 / 编辑 / 重新扫描 ---------- */
function openCineModal(mode){
  state.cineModal = mode;
  const body = $('#cmBody');
  if (mode === 'add'){
    $('#cmTitle').textContent = '添加影视';
    const dirHint = state.mediaHost
      ? `<br>影视目录：<b>${esc(state.mediaHost)}</b>`
        + (state.mediaRoot ? `（容器内 ${esc(state.mediaRoot)}）` : '')
      : '';
    body.innerHTML = '<p class="chint">填宿主机上的文件或目录。影视文件留在原地，'
      + '只登记路径，不搬动、不跑 AI。<br>目录里可以混放电影和剧集'
      + '（S01E02 / 第01集 / Season 1 都能识别）。' + dirHint + '</p>'
      + '<label>路径</label>'
      + `<input type="text" id="ciPath" placeholder="E:\\Movies 或 E:\\Movies\\电影.mp4" value="${esc(localStorage.getItem('vault.cinePath') || '')}">`
      + '<label>类型</label>'
      + '<select id="ciKind"><option value="auto">自动识别</option>'
      + '<option value="movie">全部当电影</option><option value="series">全部当剧集</option></select>'
      + '<label style="display:flex;gap:8px;align-items:center;margin-top:14px;color:var(--fg)">'
      + '<input type="checkbox" id="ciPosters" checked style="width:auto"> 抽封面帧（每部片/剧一次，稍慢）</label>'
      + '<div class="cprog" id="ciProg"></div>';
    $('#cmOk').textContent = '开始导入';
    $('#cmOk').disabled = false;
  } else {
    const t = state.cinema.detail;
    if (!t) return;
    $('#cmTitle').textContent = '编辑影视信息';
    body.innerHTML = '<label>片名</label>'
      + `<input type="text" id="ceTitle" value="${esc(t.title)}">`
      + '<label>年份</label>'
      + `<input type="number" id="ceYear" min="1888" max="2100" value="${t.year == null ? '' : t.year}">`
      + '<label>简介</label>'
      + `<textarea id="ceOverview" placeholder="可不填">${esc(t.overview || '')}</textarea>`;
    $('#cmOk').textContent = '保存';
    $('#cmOk').disabled = false;
  }
  $('#cineModal').classList.add('on');
  setTimeout(() => {
    const el = $('#ciPath') || $('#ceTitle');
    if (el) el.focus();
  }, 60);
}

function renderRunProgress(el, p){
  el.classList.add('on');
  const pct = p.total ? Math.round(p.done * 100 / p.total) : 0;
  const label = p.state === 'running' ? '导入中' : p.state === 'done' ? '完成' : '失败';
  el.innerHTML = `<div class="line">${label} · ${p.done}/${p.total}（${pct}%）</div>`
    + `新增 ${p.imported} · 更新 ${p.updated} · 条目 ${p.titles} · 字幕 ${p.subtitles} · 失败 ${p.failed}`
    + (p.current ? `<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(String(p.current).split(/[\\/]/).pop())}</div>` : '');
}

function watchRun(runId, onTick){
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const r = await api('/api/cinema/import/' + runId);
        if (onTick) onTick(r.run);
        if (r.run.state === 'running') setTimeout(tick, 700);
        else resolve(r.run);
      } catch (e){ reject(e); }
    };
    tick();
  });
}

async function startImport(){
  const path = $('#ciPath').value.trim();
  if (!path) return toast('请填路径');
  const kind = $('#ciKind').value;
  const posters = $('#ciPosters').checked;
  localStorage.setItem('vault.cinePath', path);
  const btn = $('#cmOk'), prog = $('#ciProg');
  btn.disabled = true; btn.textContent = '导入中…';
  prog.classList.add('on'); prog.innerHTML = '正在启动…';
  try {
    const r = await api('/api/cinema/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, kind, posters })
    });
    const run = await watchRun(r.run.id, p => renderRunProgress(prog, p));
    btn.disabled = false; btn.textContent = '开始导入';
    renderRunProgress(prog, run);
    if (run.state === 'error'){
      prog.innerHTML += `<div class="err">${esc(run.error || '导入失败')}</div>`;
      return;
    }
    if (run.errors && run.errors.length){
      prog.innerHTML += `<div class="err">${esc(run.errors[0].path + '：' + run.errors[0].error)}</div>`;
    }
    toast(`导入完成：新增 ${run.imported} · 更新 ${run.updated}`);
    setTimeout(() => {
      $('#cineModal').classList.remove('on');
      loadCinema();
      loadStats();
    }, 900);
  } catch (e){
    btn.disabled = false; btn.textContent = '开始导入';
    prog.innerHTML = `<div class="err">${esc(errText(e).slice(0, 400))}</div>`;
  }
}

async function saveCineEdit(){
  const d = state.cinema.detail;
  if (!d) return;
  const title = $('#ceTitle').value.trim();
  const yearRaw = $('#ceYear').value.trim();
  const overview = $('#ceOverview').value;
  try {
    await api(`/api/cinema/${d.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, year: yearRaw ? parseInt(yearRaw, 10) : null, overview })
    });
    $('#cineModal').classList.remove('on');
    toast('已保存');
    await openCinema(d.id);
    loadStats();
  } catch (e){ toast('保存失败：' + errText(e).slice(0, 80)); }
}

async function rescanCine(){
  const d = state.cinema.detail;
  if (!d) return;
  const btn = $('#cRescan');
  if (btn){ btn.disabled = true; btn.textContent = '扫描中…'; }
  try {
    const r = await api(`/api/cinema/${d.id}/rescan`, { method: 'POST' });
    const run = await watchRun(r.run.id);
    toast(run.failed ? `扫描完成，${run.failed} 个失败` : `扫描完成：更新 ${run.updated} 个`);
    await openCinema(d.id);
    loadStats();
  } catch (e){
    toast('扫描失败：' + errText(e).slice(0, 60));
    if (btn){ btn.disabled = false; btn.textContent = '重新扫描'; }
  }
}

async function deleteCine(){
  const d = state.cinema.detail;
  if (!d) return;
  if (!confirm(`从影视库删除「${d.title}」？\n只删登记记录，磁盘上的视频文件不会动。`)) return;
  try {
    await api(`/api/cinema/${d.id}`, { method: 'DELETE' });
    toast('已从影视库移除');
    loadCinema();
    loadStats();
  } catch (e){ toast('删除失败：' + errText(e).slice(0, 60)); }
}

$('#cineAdd').onclick = () => openCineModal('add');
$('#cineRefresh').onclick = () => {
  if (state.cinema.titleId) openCinema(state.cinema.titleId);
  else loadCinema();
};
$('#cmCancel').onclick = () => $('#cineModal').classList.remove('on');
$('#cineModal').addEventListener('click', e => {
  if (e.target.id === 'cineModal') $('#cineModal').classList.remove('on');
});
$('#cmOk').onclick = () => {
  if (state.cineModal === 'edit') saveCineEdit();
  else startImport();
};
$('#cpClose').onclick = closeCinePlayer;
$('#cpOpen').onclick = openExternal;
$('#cpPrev').onclick = () => stepEpisode(-1);
$('#cpNext').onclick = () => stepEpisode(1);
$('#cpSub').onchange = applySubtitle;
$('#cpVideo').addEventListener('error', () => {
  const v = $('#cpVideo');
  if (!v.currentSrc) return;
  const err = $('#cpErr');
  err.textContent = '浏览器放不了这个容器（MKV / AVI / TS 很常见）。'
    + '点右上角「系统播放器」用本机播放器打开，同目录的字幕会跟着走。';
  err.classList.add('on');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#cinePlay').classList.contains('on')) closeCinePlayer();
  else if (e.key === 'Escape' && $('#cineModal').classList.contains('on'))
    $('#cineModal').classList.remove('on');
});

/* ---------------- 标签面板 ---------------- */
let sheetIds = [];
async function loadTags(){
  const r = await api('/api/tags?limit=400');   // 按使用次数降序，AI 标签多了也不会撑爆下拉
  state.tagsCache = r.tags;
}
async function openSheet(ids){
  sheetIds = ids;
  await loadTags();
  const it = ids.length === 1 ? state.items.get(ids[0]) : null;
  $('#sheetTitle').textContent = ids.length === 1 ? '编辑标签' : `为 ${ids.length} 项添加标签`;
  renderSheet(it ? (it.tags||[]) : []);
  $('#sheet').classList.add('on');
}
function renderSheet(assetTags){
  const box = $('#sheetChips'); box.innerHTML = '';
  state.tagsCache.forEach(t => {
    const b = document.createElement('button');
    const has = assetTags ? assetTags.includes(t.name) : null;
    b.className = 'chip' + (has ? ' on' : '');
    b.innerHTML = `${esc(t.name)} <span class="cnt">${t.count}</span>`;
    b.onclick = async () => {
      if (assetTags){
        // 单资产：有则删，无则加
        if (has){ const r = await api(`/api/assets/${sheetIds[0]}/tags/${encodeURIComponent(t.name)}`, {method:'DELETE'}); assetTags.length = 0; assetTags.push(...r.tags); }
        else { const r = await api(`/api/assets/${sheetIds[0]}/tags`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name:t.name})}); assetTags.length = 0; assetTags.push(...r.tags); }
        const it = state.items.get(sheetIds[0]); if (it) it.tags = assetTags;
        renderSheet(assetTags); renderLbTagsIfOpen();
      } else {
        await batch(sheetIds.every(id => (state.items.get(id)?.tags||[]).includes(t.name)) ? 'untag' : 'tag', t.name);
        $('#sheet').classList.remove('on');
      }
    };
    box.appendChild(b);
  });
  if (!state.tagsCache.length) box.innerHTML = '<span style="color:var(--mut)">还没有标签，下面新建一个</span>';
}
function renderLbTagsIfOpen(){ if (state.lbId && $('#lb').classList.contains('on')){ const it = state.items.get(state.lbId); if (it) renderLbTags(it); } }
function closeSheet(){ $('#sheet').classList.remove('on'); }
$('#addTag').onclick = async () => {
  const name = $('#newTag').value.trim(); if (!name) return;
  $('#newTag').value = '';
  if (sheetIds.length === 1){
    const it = state.items.get(sheetIds[0]);
    const r = await api(`/api/assets/${sheetIds[0]}/tags`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
    if (it){ it.tags = r.tags; }
    await loadTags(); renderSheet(r.tags); renderLbTagsIfOpen();
  } else {
    await batch('tag', name);
    await loadTags(); closeSheet();
  }
};
$('#newTag').addEventListener('keydown', e => { if (e.key === 'Enter') $('#addTag').click(); });

/* ---------------- toast ---------------- */
function toast(msg, actText, fn){
  $('#toastMsg').textContent = msg;
  const b = $('#toastAct');
  if (actText){ b.style.display=''; b.textContent = actText; state.toastFn = fn; }
  else { b.style.display='none'; state.toastFn = null; }
  $('#toast').classList.add('on');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => $('#toast').classList.remove('on'), 4200);
}
$('#toastAct').onclick = () => { const f = state.toastFn; $('#toast').classList.remove('on'); if (f) f(); };

/* ---------------- 网页导入（拖拽 / 选择文件） ---------------- */
// 新增素材不再扫盘：文件直接推进来。入库仍然走 vault.ingest 的同一条流水线
// （sha256 去重、EXIF/ffprobe、时区、Live Photo 配对、缩略图），
// 只是数据来源从"目录"换成"浏览器上传"。
const UPLOAD_EXT = /\.(jpe?g|jfif|jpe|png|heic|heif|webp|tiff?|bmp|gif|avif|dng|cr2|cr3|nef|arw|raf|orf|rw2|pef|srw|mp4|mov|m4v|avi|mkv|insv|360|lrv|3gp|mpeg|mpg|flv|wmv|mts|m2ts)$/i;
const up = {
  queue: [], seq: 0, running: false, batch: '',
  library: localStorage.getItem('vault.upLib') || null,
  added: 0, dup: 0, fail: 0, skipped: 0,
};

function newBatchId(){
  try { return crypto.randomUUID().replace(/-/g, ''); }
  catch (e) { return Date.now().toString(36) + Math.random().toString(36).slice(2); }
}

function upDefaultLib(){
  const inMaterial = state.view === 'material'
    || (state.library === 'material' && ['fav', 'tag'].includes(state.view));
  return localStorage.getItem('vault.upLib') || (inMaterial ? 'material' : 'gallery');
}

function syncUpLib(){
  $$('#upSeg button').forEach(b => {
    b.classList.toggle('on', b.dataset.lib === up.library);
    b.disabled = up.running;
  });
  $('#dhLib').textContent = up.library === 'material' ? '素材库' : '图库';
}

function openUpload(){
  if (!up.running) up.library = upDefaultLib();
  $('#upModal').classList.add('on');
  syncUpLib();
  renderUpList();
}

function closeUpload(){
  $('#upModal').classList.remove('on');
  if (!up.running){        // 跑完了才清空：中途关掉时列表还留着，重新打开能接着看
    up.queue = []; up.added = up.dup = up.fail = 0; up.skipped = 0;
  }
}

function addUploadFiles(entries){
  const picks = [];
  let skipped = 0;
  for (const e of entries){
    const f = e.file, name = e.name || (f && f.name) || '';
    if (!f || !f.size || !UPLOAD_EXT.test(name)){ skipped++; continue; }
    picks.push({ id: ++up.seq, file: f, name, status: 'wait', pct: 0, note: '', el: null });
  }
  up.skipped += skipped;
  if (!picks.length){
    if (skipped) toast(`已跳过 ${skipped} 个非图片/视频文件`);
    return;
  }
  if (!up.running) up.batch = newBatchId();
  up.queue.push(...picks);
  openUpload();
  pumpUploads();
}

function makeUpRow(job){
  const el = document.createElement('div');
  el.className = 'urow';
  el.innerHTML = '<div class="umain"><div class="un"></div><div class="ubar"><i></i></div></div>'
    + '<div class="us"></div>';
  el.querySelector('.un').textContent = job.name;   // 文件名是用户输入，只能走 textContent
  job.el = el;
  renderUpRow(job);
  return el;
}

function renderUpRow(job){
  if (!job.el) return;
  const done = job.status === 'ok' || job.status === 'dup' || job.status === 'err';
  const pct = done ? 1 : (job.pct || 0);
  job.el.className = 'urow' + (job.status === 'ok' ? ' ok'
    : job.status === 'dup' ? ' dup' : job.status === 'err' ? ' err' : '');
  job.el.querySelector('.ubar i').style.width = (pct * 100).toFixed(1) + '%';
  const s = job.el.querySelector('.us');
  s.textContent = job.status === 'ok' ? '已入库'
    : job.status === 'dup' ? (job.note || '已在库中')
    : job.status === 'err' ? '失败'
    : job.status === 'up' ? (pct >= 1 ? '入库中…' : Math.round(pct * 100) + '%')
    : '等待';
  s.title = job.status === 'err' ? (job.note || '') : '';
}

function updateUpStat(){
  const el = $('#upStat');
  if (!up.queue.length){ el.textContent = ''; return; }
  const fin = up.queue.filter(j => j.status === 'ok' || j.status === 'dup' || j.status === 'err').length;
  const parts = [`${fin}/${up.queue.length}`];
  if (up.added) parts.push(`新增 ${up.added}`);
  if (up.dup) parts.push(`重复 ${up.dup}`);
  if (up.fail) parts.push(`失败 ${up.fail}`);
  if (!up.running && fin === up.queue.length){
    parts.push(fmtBytes(up.queue.reduce((s, j) => s + j.file.size, 0)));
  }
  el.textContent = parts.join(' · ');
}

function renderUpList(){
  const box = $('#upList');
  box.innerHTML = '';
  if (!up.queue.length){
    box.innerHTML = '<div class="uempty">还没有文件。<br>把图片 / 视频拖进这个窗口，或点下面「选择文件」。</div>';
    updateUpStat();
    return;
  }
  const frag = document.createDocumentFragment();
  const shown = up.queue.slice(0, 400);
  shown.forEach(j => frag.appendChild(j.el || makeUpRow(j)));
  if (up.queue.length > shown.length){
    const more = document.createElement('div');
    more.className = 'uempty';
    more.textContent = `还有 ${up.queue.length - shown.length} 个文件未显示（仍会继续上传）`;
    frag.appendChild(more);
  }
  box.appendChild(frag);
  updateUpStat();
}

function uploadOne(job, onProgress){
  return new Promise((resolve, reject) => {
    const qs = new URLSearchParams({ name: job.name, library: up.library, batch: up.batch });
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload?' + qs.toString());
    xhr.upload.onprogress = ev => { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    xhr.upload.onload = () => onProgress(1);          // 字节传完，服务器还在探测/做缩略图
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (e) { /* 非 JSON 错误页 */ }
      if (xhr.status >= 200 && xhr.status < 300 && body) resolve(body);
      else reject(new Error((body && body.detail) || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = () => reject(new Error('网络中断'));
    xhr.ontimeout = () => reject(new Error('上传超时'));
    xhr.send(job.file);
  });
}

async function pumpUploads(){
  if (up.running) return;
  up.running = true;
  const run = { added: 0, dup: 0, fail: 0 };   // 本批的计数：面板里保留累计值，toast 只报这一批
  syncUpLib();
  updateUpStat();
  try {
    for (;;){
      const job = up.queue.find(j => j.status === 'wait');
      if (!job) break;
      job.status = 'up'; job.pct = 0;
      renderUpRow(job); updateUpStat();
      try {
        const r = await uploadOne(job, pct => { job.pct = pct; renderUpRow(job); });
        if (r.status === 'duplicate'){
          job.status = 'dup';
          job.note = (r.asset && r.asset.library) === 'material' ? '已在素材库' : '已在图库';
          up.dup++; run.dup++;
        } else {
          job.status = 'ok'; up.added++; run.added++;
        }
      } catch (e){
        job.status = 'err'; job.note = errText(e); up.fail++; run.fail++;
      }
      job.pct = 1;
      renderUpRow(job); updateUpStat();
    }
  } finally {
    up.running = false;
    syncUpLib(); updateUpStat();
  }
  const parts = [`新增 ${run.added}`, `重复 ${run.dup}`];
  if (run.fail) parts.push(`失败 ${run.fail}`);
  if (up.skipped) parts.push(`跳过 ${up.skipped}`);
  toast(`导入完成：${parts.join('，')}`);
  await loadStats();
  if (['all', 'material', 'fav', 'tag'].includes(state.view)) load(true);
}

/* 拖文件夹进来时递归展开。webkitGetAsEntry 必须在 drop 事件里同步取，
   一旦 await 过，DataTransferItem 就失效了。 */
async function walkEntry(entry, prefix, out){
  if (entry.isFile){
    const f = await new Promise(res => entry.file(res, () => res(null)));
    if (f) out.push({ file: f, name: prefix + f.name });
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  const sub = prefix + entry.name + '/';
  for (;;){
    const batch = await new Promise(res => reader.readEntries(res, () => res([])));
    if (!batch.length) break;
    for (const child of batch) await walkEntry(child, sub, out);
  }
}

async function filesFromDrop(dt){
  const entries = [];
  for (const it of [...(dt.items || [])]){
    const en = it.kind === 'file' && it.webkitGetAsEntry && it.webkitGetAsEntry();
    if (en) entries.push(en);
  }
  if (!entries.length){
    return [...(dt.files || [])].map(f => ({ file: f, name: f.name }));
  }
  const out = [];
  for (const en of entries) await walkEntry(en, '', out);
  return out;
}

let dragDepth = 0;
const dragHasFiles = e => !!(e.dataTransfer && [...e.dataTransfer.types].includes('Files'));

window.addEventListener('dragenter', e => {
  if (!dragHasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  $('#dhLib').textContent = upDefaultLib() === 'material' ? '素材库' : '图库';
  $('#dropHint').classList.add('on');
});
window.addEventListener('dragover', e => {
  if (!dragHasFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});
window.addEventListener('dragleave', e => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('#dropHint').classList.remove('on');
});
window.addEventListener('drop', async e => {
  e.preventDefault();
  dragDepth = 0;
  $('#dropHint').classList.remove('on');
  if (!dragHasFiles(e)) return;
  const files = await filesFromDrop(e.dataTransfer);
  if (files.length) addUploadFiles(files);
  else toast('没有可导入的图片或视频');
});

$('#btnUpload').onclick = () => openUpload();
$('#upClose').onclick = closeUpload;
$('#upDone').onclick = closeUpload;
$('#upPick').onclick = () => $('#upInput').click();
$('#upModal').addEventListener('click', e => { if (e.target.id === 'upModal') closeUpload(); });
$('#upInput').onchange = e => {
  const fs = [...e.target.files];
  e.target.value = '';
  if (fs.length) addUploadFiles(fs.map(f => ({ file: f, name: f.name })));
};
$$('#upSeg button').forEach(b => b.onclick = () => {
  if (up.running) return;
  up.library = b.dataset.lib;
  localStorage.setItem('vault.upLib', up.library);
  syncUpLib();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#upModal').classList.contains('on')) closeUpload();
});

/* ---------------- 筛选 chips / 搜索 ---------------- */
$$('.chips .chip[data-view]').forEach(ch => ch.onclick = () => setView(ch.dataset.view));
$('#tagChip').onclick = async e => {
  if (state.view === 'tag'){ setView('all'); return; }
  await loadTags();
  const m = $('#tagMenu');
  m.innerHTML = '';
  state.tagsCache.forEach(t => {
    const mi = document.createElement('div'); mi.className = 'mi';
    mi.innerHTML = `<span>${esc(t.name)}</span><span class="n">${t.count}</span>`;
    mi.onclick = () => { m.classList.remove('open'); setView('tag', t.name); };
    m.appendChild(mi);
  });
  if (!state.tagsCache.length) m.innerHTML = '<div class="mi" style="color:var(--mut)">还没有标签</div>';
  const r = e.currentTarget.getBoundingClientRect();
  m.style.top = r.bottom + 'px';
  m.classList.toggle('open');
};
document.addEventListener('click', e => {
  if (!e.target.closest('#tagMenu') && !e.target.closest('#tagChip')) $('#tagMenu').classList.remove('open');
  if (!e.target.closest('#sheet') && !e.target.closest('#selbar') && !e.target.closest('#lbTags')) $('#sheet').classList.remove('on');
});

let qt; $('#q').oninput = () => { clearTimeout(qt); qt = setTimeout(() => load(true), 250); };

/* ---------------- 剪辑筛选条 ---------------- */
function bindFilter(groupSel, attr, stateKey){
  const g = $(groupSel);
  g.querySelectorAll('button').forEach(b => b.onclick = () => {
    g.querySelectorAll('button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    state[stateKey] = b.dataset[attr] || '';
    if (state.view === 'bin'){ setView('all'); return; }
    load(true);
  });
}
bindFilter('#fKind', 'k', 'kind');
bindFilter('#fDur', 'd', 'dur');
bindFilter('#fPx', 'p', 'px');

/* ---------------- 统计 / 无限滚动 ---------------- */
async function loadStats(){
  const s = await api('/api/stats');
  $('#favCnt').textContent = s.favorites || '';
  $('#trashCnt').textContent = s.trash || '';
  $('#personCnt').textContent = s.persons || '';
  $('#matCnt').textContent = s.material || '';
  $('#cineCnt').textContent = s.cinema_titles || '';
  state.mediaHost = s.media_host_root || '';
  state.mediaRoot = s.media_root || '';
}

const io = new IntersectionObserver(es => { if (es[0].isIntersecting) load(false); }, {rootMargin:'900px'});
io.observe($('#sentinel'));

const usp = new URLSearchParams(location.search);   // 深链：/?q=... /?view=fav|trash|people|bin|material|cinema /?view=tag&tag=名称 /?person=<id> /?title=<影视条目>
if (usp.get('q')) $('#q').value = usp.get('q');
const deepPerson = usp.get('person');
const deepTitle = usp.get('title');
if (deepPerson){
  setView('people');
  api('/api/persons').then(r => {
    const p = r.persons.find(x => x.id === deepPerson);
    if (p) openPerson(p);
  });
} else if (deepTitle){
  setView('cinema');
  state.cinema.pending = deepTitle;   // loadCinema 拉完列表后自动打开详情，避免竞态
} else if (usp.get('view')) setView(usp.get('view'), usp.get('tag'));
else setView('all');
loadStats();
ensureBin().then(refreshBinCount).catch(() => {});   // 箱子懒创建，失败不影响浏览
checkBridge();                                        // 顺带探一下桌面桥
if (location.hash === '#upload') openUpload();         // 书签直达导入面板
</script></body></html>"""
