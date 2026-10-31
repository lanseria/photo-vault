from __future__ import annotations

import uuid as uuidlib
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

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


# ---------------------------------------------------------------- 统计

@app.get("/api/stats")
def api_stats() -> JSONResponse:
    row = q(
        """select
             (select count(*) from assets where not is_deleted) total,
             (select count(*) from assets where favorite and not is_deleted) favs,
             (select count(*) from assets where is_deleted) trash,
             (select count(*) from persons) people,
             (select coalesce(sum(bytes),0) from files where role='original') b,
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
        "favorites": row["favs"],
        "trash": row["trash"],
        "persons": row["people"],
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
) -> JSONResponse:
    where = ["a.is_deleted" if trash else "not a.is_deleted"]
    params: list[Any] = []

    if kind:
        where.append("a.kind = %s")
        params.append(kind)
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
        where.append(
            "(f.src_path ilike %s or f.origin_rel ilike %s or f.ext ilike %s "
            " or exists (select 1 from video_summaries vs where vs.asset_id = a.id "
            "            and (vs.caption ilike %s or vs.ocr ilike %s)))"
        )
        params.extend([f"%{qtext}%"] * 5)
    if date_from:
        where.append("a.taken_at_local >= %s")
        params.append(date_from)
    if date_to:
        where.append("a.taken_at_local < (%s::date + 1)")
        params.append(date_to)

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
        f"""select a.id, a.kind, a.favorite, a.taken_at_local, a.tz_offset_min, a.tz_source,
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
    exists = q("select 1 from assets where id=%s and is_deleted", (aid,))
    if not exists:
        raise HTTPException(404, "asset not in trash")
    q("delete from assets where id=%s", (aid,))
    return JSONResponse({"id": aid, "purged": True})


# ---------------------------------------------------------------- 标签

class TagBody(BaseModel):
    name: str


class BatchBody(BaseModel):
    ids: list[str]
    action: str                 # favorite | unfavorite | trash | restore | tag | untag
    tag: str | None = None


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
    q("""delete from asset_tags at2 using tags t
         where at2.tag_id = t.id and at2.asset_id=%s and t.name=%s""", (aid, name))
    tags = [r["name"] for r in q(
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

_PREVIEW_NATIVE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}


def _preview_for(rel_path: str, ext: str) -> FileResponse:
    src = cfg.vault_root / rel_path
    if not src.exists():
        raise HTTPException(404, "file missing on disk")
    if ext in _PREVIEW_NATIVE:
        return FileResponse(src)
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
  #selbar button{display:flex;flex-direction:column;align-items:center;gap:3px;font-size:11px;color:var(--fg);padding:4px 14px}
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

  /* ---------- toast ---------- */
  #toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%) translateY(20px);z-index:150;
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
  <button class="btn-txt" id="btnSel">选择</button>
</div>
<div class="chips">
  <button class="chip on" data-view="all">图库</button>
  <button class="chip" data-view="fav">❤️ <span class="cnt" id="favCnt"></span></button>
  <button class="chip" data-view="people">人物 <span class="cnt" id="personCnt"></span></button>
  <button class="chip" id="tagChip">标签</button>
  <button class="chip" data-view="trash">🗑 <span class="cnt" id="trashCnt"></span></button>
  <span style="flex:1"></span>
  <span class="chip" style="background:none;color:var(--mut)" id="count"></span>
</div>
<div id="tagMenu"></div>

<main id="feed">
  <div id="people" style="display:none"><div class="pgrid" id="peopleGrid"></div></div>
  <div id="sections"></div>
  <div id="sentinel"></div>
</main>

<div id="selbar">
  <span id="selCount" style="font-size:13px;color:var(--mut);min-width:64px"></span>
  <span style="flex:1"></span>
  <button data-act="fav" id="sbFav"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20.7l-1.2-1.1C6 15.4 3 12.7 3 9.3 3 6.6 5.1 4.5 7.8 4.5c1.5 0 3 .7 4.2 2 1.2-1.3 2.7-2 4.2-2 2.7 0 4.8 2.1 4.8 4.8 0 3.4-3 6.1-7.8 10.3L12 20.7z"/></svg>喜欢</button>
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

<div id="toast"><span id="toastMsg"></span><button id="toastAct" style="display:none"></button></div>

<script>
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const state = {
  view: 'all', tag: null,           // all | fav | trash | tag | people | person
  personId: null, personName: '',
  items: new Map(), order: [],      // 当前列表顺序（灯箱导航用）
  offset: 0, total: 0, loading: false,
  selMode: false, sel: new Set(),
  lbId: null, tagsCache: [], people: [],
  toastTimer: null, toastFn: null,
};

const fmtBytes = b => b >= 1<<30 ? (b/1073741824).toFixed(1)+' GB' : b >= 1<<20 ? (b/1048576).toFixed(0)+' MB' : (b/1024).toFixed(0)+' KB';
const fmtDur = ms => { if (!ms) return ''; const s = Math.round(ms/1000);
  return s >= 60 ? Math.floor(s/60)+':'+String(s%60).padStart(2,'0') : '0:'+String(s).padStart(2,'0'); };
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
  if ($('#q').value.trim()) p.set('q', $('#q').value.trim());
  return p;
}

async function load(reset){
  if (state.loading) return;
  if (state.view === 'people'){ loadPeople(); return; }
  if (reset){ state.offset=0; state.total=0; state.items.clear(); state.order=[]; $('#sections').innerHTML=''; }
  if (state.offset >= state.total && !reset && state.total) return;
  state.loading = true;
  try {
    const r = await (await fetch('/api/assets?' + viewParams())).json();
    state.total = r.total;
    r.items.forEach(it => { state.items.set(it.id, it); state.order.push(it.id); append(it); });
    state.offset += r.items.length;
    $('#count').textContent = state.total + ' 项';
  } finally { state.loading = false; }
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
  state.view = v; state.tag = tag || null;
  if (person){ state.personId = person.id; state.personName = person.name; }
  exitSel();
  $$('.chips .chip[data-view]').forEach(ch => ch.classList.toggle('on', ch.dataset.view === v));
  const tagChip = $('#tagChip');
  tagChip.classList.toggle('on', v === 'tag');
  tagChip.innerHTML = v === 'tag'
    ? `🏷 ${esc(state.tag)} <span style="color:var(--mut)">✕</span>`
    : '标签';
  $('#viewTitle').textContent = v === 'fav' ? '喜欢'
    : v === 'trash' ? '最近删除'
    : v === 'tag' ? state.tag
    : v === 'people' ? '人物'
    : v === 'person' ? (state.personName || '人物')
    : '图库';
  const isPeople = v === 'people';
  $('#people').style.display = isPeople ? '' : 'none';
  $('#sections').style.display = isPeople ? 'none' : '';
  $('#sentinel').style.display = isPeople ? 'none' : '';
  $('#btnRename').style.display = v === 'person' ? '' : 'none';
  $('#btnMerge').style.display = v === 'person' ? '' : 'none';
  $('#btnSel').style.display = isPeople ? 'none' : '';
  $('#sbFav').style.display = v === 'trash' ? 'none' : '';
  $$('#selbar [data-act=tag]').forEach(b => b.style.display = v === 'trash' ? 'none' : '');
  $('#sbTrash').style.display = v === 'trash' ? 'none' : '';
  $('#sbRestore').style.display = v === 'trash' ? '' : 'none';
  $('#sbPurge').style.display = v === 'trash' ? '' : 'none';
  if (isPeople){ loadPeople(); return; }
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
}
function enterSel(){ state.selMode = true; document.body.classList.add('selecting'); $('#btnSel').textContent='取消'; $('#selbar').classList.add('on'); updateSelbar(); }
function exitSel(){ state.selMode = false; state.sel.clear(); document.body.classList.remove('selecting'); $('#btnSel').textContent='选择'; $('#selbar').classList.remove('on'); $$('.cell.sel').forEach(c => c.classList.remove('sel')); }

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

$('#selbar').addEventListener('click', e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const act = b.dataset.act;
  if (act === 'fav'){
    const one = state.sel.size === 1 ? state.items.get([...state.sel][0]) : null;
    batch(one && one.favorite ? 'unfavorite' : 'favorite');
  }
  else if (act === 'tag') openSheet([...state.sel]);
  else if (act === 'trash') batch('trash');
  else if (act === 'restore') batch('restore');
  else if (act === 'purge') { if (confirm(`彻底删除 ${state.sel.size} 项？库记录将被移除，不可恢复。`)) batch('purge'); }
});
$('#btnSel').onclick = () => state.selMode ? exitSel() : enterSel();

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

/* ---------------- 统计 / 无限滚动 ---------------- */
async function loadStats(){
  const s = await api('/api/stats');
  $('#favCnt').textContent = s.favorites || '';
  $('#trashCnt').textContent = s.trash || '';
  $('#personCnt').textContent = s.persons || '';
}

const io = new IntersectionObserver(es => { if (es[0].isIntersecting) load(false); }, {rootMargin:'900px'});
io.observe($('#sentinel'));

const usp = new URLSearchParams(location.search);   // 深链：/?q=... /?view=fav|trash|people /?view=tag&tag=名称 /?person=<id>
if (usp.get('q')) $('#q').value = usp.get('q');
const deepPerson = usp.get('person');
if (deepPerson){
  setView('people');
  api('/api/persons').then(r => {
    const p = r.persons.find(x => x.id === deepPerson);
    if (p) openPerson(p);
  });
} else if (usp.get('view')) setView(usp.get('view'), usp.get('tag'));
else setView('all');
loadStats();
</script></body></html>"""
