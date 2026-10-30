from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Config

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


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    row = q(
        """select count(*) c,
                  coalesce(sum(sz),0) b,
                  min(taken_at_utc) mn, max(taken_at_utc) mx
           from (select a.taken_at_utc,
                        (select f.bytes from files f where f.asset_id=a.id and f.role='original' limit 1) sz
                 from assets a where not a.is_deleted) t"""
    )[0]
    devs = q(
        """select coalesce(d.model_raw,'(未知)') model, count(*) c
           from assets a left join devices d on d.id=a.device_id
           where not a.is_deleted group by 1 order by c desc limit 12"""
    )
    return JSONResponse({
        "count": row["c"],
        "bytes": int(row["b"]),
        "first": row["mn"].isoformat() if row["mn"] else None,
        "last": row["mx"].isoformat() if row["mx"] else None,
        "devices": [{"model": d["model"], "count": d["c"]} for d in devs],
    })


@app.get("/api/assets")
def api_assets(
    limit: int = Query(60, ge=1, le=300),
    offset: int = Query(0, ge=0),
    kind: str | None = None,
    device: str | None = None,
    qtext: str | None = Query(None, alias="q"),
    date_from: str | None = None,
    date_to: str | None = None,
    folder: str | None = None,
) -> JSONResponse:
    where = ["not a.is_deleted"]
    params: list[Any] = []

    if kind:
        where.append("a.kind = %s")
        params.append(kind)
    if device:
        where.append("d.model_raw = %s")
        params.append(device)
    if folder:
        where.append("(f.origin_rel = %s or f.origin_rel like %s)")
        params.extend([folder, folder.rstrip("/") + "/%"])
    if qtext:
        where.append("(f.src_path ilike %s or f.rel_path ilike %s or f.origin_rel ilike %s)")
        params.extend([f"%{qtext}%"] * 3)
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
    total = q(f"select count(*) c {base}", tuple(params))[0]["c"]
    rows = q(
        f"""select a.id, a.kind, a.taken_at_local, a.tz_offset_min, a.tz_source,
                   a.width, a.height, a.duration_ms,
                   coalesce(d.model_raw,'未知设备') model,
                   f.sha256, f.bytes, f.ext, f.origin_rel, f.storage_mode,
                   encode(f.sha256,'hex') sha
            {base}
            order by a.taken_at_utc desc nulls last
            limit %s offset %s""",
        tuple(params + [limit, offset]),
    )

    items = []
    for r in rows:
        sha = r["sha"]
        has_thumb = (cfg.derived_root / "thumbs" / sha[0:2] / sha[2:4] / f"{sha}.webp").exists()
        items.append({
            "id": str(r["id"]),
            "kind": r["kind"],
            "sha": sha,
            "taken": r["taken_at_local"].isoformat() if r["taken_at_local"] else None,
            "tz": r["tz_offset_min"],
            "tz_source": r["tz_source"],
            "model": r["model"],
            "width": r["width"],
            "height": r["height"],
            "duration_ms": r["duration_ms"],
            "bytes": r["bytes"],
            "has_thumb": has_thumb,
            "origin": r["origin_rel"],
            "storage": r["storage_mode"],
        })
    return JSONResponse({"total": total, "items": items})


@app.get("/api/folders")
def api_folders() -> JSONResponse:
    """按原始文件夹（来源顶层目录）聚合，方便"我原来放在哪"式浏览。"""
    rows = q(
        """
        select split_part(coalesce(origin_rel,''), '/', 1) as top, count(*) c
        from files where role='original'
        group by 1 having count(*) > 0
        order by c desc limit 40
        """
    )
    return JSONResponse({"folders": [{"name": r["top"], "count": r["c"]} for r in rows if r["top"]]})


@app.get("/thumbs/{sha}.webp")
def thumb(sha: str) -> FileResponse:
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        raise HTTPException(400, "bad sha")
    p = cfg.derived_root / "thumbs" / sha[0:2] / sha[2:4] / f"{sha}.webp"
    if not p.exists():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(p, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/file/{asset_id}")
def original(asset_id: str) -> FileResponse:
    rows = q(
        "select rel_path, ext from files where asset_id=%s and role='original' limit 1",
        (asset_id,),
    )
    if not rows:
        raise HTTPException(404, "not found")
    p = cfg.vault_root / rows[0]["rel_path"]
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(p)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return HTMLResponse(PAGE)


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo Vault</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--line:#262b35;--fg:#e6e9ef;--mut:#8b93a1;--acc:#4c8dff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;z-index:5;background:rgba(15,17,21,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:12px 20px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  h1{font-size:15px;margin:0 16px 0 0;font-weight:600;letter-spacing:.3px}
  h1 span{color:var(--mut);font-weight:400;margin-left:8px}
  input,select{background:var(--panel);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:7px 10px;font-size:13px;outline:none}
  input:focus,select:focus{border-color:var(--acc)}
  input[type=search]{width:240px}
  .pill{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:4px 11px;font-size:12px;color:var(--mut)}
  main{padding:18px 20px 60px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
  .card{position:relative;aspect-ratio:1;background:var(--panel);border-radius:10px;overflow:hidden;cursor:zoom-in;border:1px solid transparent}
  .card:hover{border-color:var(--acc)}
  .card img{width:100%;height:100%;object-fit:cover;display:block}
  .card .ph{display:flex;align-items:center;justify-content:center;height:100%;color:var(--mut);font-size:12px;text-align:center;padding:8px}
  .card .meta{position:absolute;left:0;right:0;bottom:0;padding:22px 9px 7px;font-size:11px;
    background:linear-gradient(transparent,rgba(0,0,0,.85));opacity:0;transition:.15s}
  .card:hover .meta{opacity:1}
  .badge{position:absolute;top:7px;left:7px;background:rgba(0,0,0,.65);border-radius:6px;padding:1px 6px;font-size:10px;color:#cfd6e3}
  .empty{color:var(--mut);padding:60px 0;text-align:center}
  #lb{position:fixed;inset:0;background:rgba(0,0,0,.93);display:none;align-items:center;justify-content:center;z-index:50}
  #lb.on{display:flex}
  #lb img,#lb video{max-width:94vw;max-height:88vh;border-radius:8px}
  #lb .cap{position:absolute;bottom:16px;left:0;right:0;text-align:center;font-size:12px;color:#aab3c2}
  #more{display:block;margin:26px auto;padding:9px 22px;background:var(--panel);border:1px solid var(--line);color:var(--fg);border-radius:8px;cursor:pointer}
  #more:disabled{opacity:.4;cursor:default}
</style></head><body>
<header><div class="row">
  <h1>Photo Vault<span id="sum"></span></h1>
  <input id="q" type="search" placeholder="文件名关键字…">
  <select id="kind">
    <option value="">全部类型</option><option value="photo">照片</option>
    <option value="video">视频</option><option value="live_photo">Live Photo</option>
    <option value="raw">RAW</option>
  </select>
  <select id="device"><option value="">全部设备</option></select>
  <select id="folder"><option value="">全部来源目录</option></select>
  <input id="from" type="date"><span style="color:var(--mut)">→</span><input id="to" type="date">
  <span class="pill" id="count">0</span>
</div></header>
<main><div class="grid" id="grid"></div><button id="more" disabled>加载更多</button></main>
<div id="lb"><img id="lbimg" alt=""><div class="cap" id="lbcap"></div></div>

<script>
const $ = s => document.querySelector(s);
let offset = 0, total = 0, loading = false, items = [];

const fmtBytes = b => b > 1<<30 ? (b/1073741824).toFixed(1)+' GB' : b > 1<<20 ? (b/1048576).toFixed(0)+' MB' : (b/1024).toFixed(0)+' KB';
const fmtDur = ms => !ms ? '' : (ms/1000 >= 60 ? Math.floor(ms/60000)+'m'+String(Math.floor(ms/1000)%60).padStart(2,'0')+'s' : (ms/1000).toFixed(0)+'s');

async function loadStats(){
  const s = await (await fetch('/api/stats')).json();
  $('#sum').textContent = s.count ? ` ${s.count} 项 · ${fmtBytes(s.bytes)}` : '';
  const sel = $('#device');
  s.devices.forEach(d => {
    const o = document.createElement('option');
    o.value = d.model; o.textContent = `${d.model} (${d.count})`;
    sel.appendChild(o);
  });
  try {
    const f = await (await fetch('/api/folders')).json();
    const fs = $('#folder');
    f.folders.forEach(d => {
      const o = document.createElement('option');
      o.value = d.name; o.textContent = `${d.name} (${d.count})`;
      fs.appendChild(o);
    });
  } catch (e) {}
}

function params(){
  const p = new URLSearchParams({limit:60, offset:offset});
  const v = {kind:$('#kind').value, device:$('#device').value, folder:$('#folder').value,
             q:$('#q').value.trim(), date_from:$('#from').value, date_to:$('#to').value};
  for (const k in v) if (v[k]) p.set(k, v[k]);
  return p;
}

async function load(reset){
  if (loading) return; loading = true;
  if (reset) { offset = 0; items = []; $('#grid').innerHTML = ''; }
  const r = await (await fetch('/api/assets?' + params())).json();
  total = r.total; $('#count').textContent = total + ' 张';
  r.items.forEach(it => { items.push(it); $('#grid').appendChild(card(it)); });
  offset += r.items.length;
  $('#more').disabled = offset >= total;
  $('#more').textContent = offset >= total ? '已到底部' : `加载更多（已显示 ${offset} / ${total}）`;
  if (!total) $('#grid').innerHTML = '<div class="empty">库里还没有内容。先跑 <code>vault ingest /media</code>。</div>';
  loading = false;
}

function card(it){
  const d = document.createElement('div');
  d.className = 'card';
  d.title = `${it.model}\n${it.taken || '时间未知'}\n${it.width||'?'}×${it.height||'?'}`;
  if (it.has_thumb) {
    const img = document.createElement('img');
    img.loading = 'lazy'; img.src = `/thumbs/${it.sha}.webp`; img.alt = '';
    d.appendChild(img);
  } else {
    const ph = document.createElement('div');
    ph.className = 'ph';
    ph.textContent = it.kind === 'video' ? '▶ 视频' : (it.kind === 'raw' ? 'RAW' : '无预览');
    d.appendChild(ph);
  }
  if (it.kind === 'video') {
    const b = document.createElement('span'); b.className = 'badge';
    b.textContent = '▶ ' + (fmtDur(it.duration_ms) || 'video'); d.appendChild(b);
  }
  const m = document.createElement('div'); m.className = 'meta';
  m.textContent = (it.taken || '').replace('T',' ').slice(0,16) + ' · ' + it.model;
  d.appendChild(m);
  d.onclick = () => open(it);
  return d;
}

function open(it){
  const lb = $('#lb');
  const isVid = it.kind === 'video' || it.kind === 'live_photo';
  lb.innerHTML = '';
  const el = document.createElement(isVid ? 'video' : 'img');
  el.src = `/file/${it.id}`;
  if (isVid) el.controls = true;
  lb.appendChild(el);
  const cap = document.createElement('div'); cap.className = 'cap';
  cap.textContent = `${it.model} · ${(it.taken||'').replace('T',' ')} · ${fmtBytes(it.bytes)}`;
  lb.appendChild(cap);
  lb.classList.add('on');
}
$('#lb').onclick = () => { $('#lb').classList.remove('on'); $('#lb').innerHTML = ''; };

let t; const debounce = f => { clearTimeout(t); t = setTimeout(f, 220); };
$('#q').oninput = () => debounce(() => load(true));
['kind','device','folder','from','to'].forEach(id => $('#'+id).onchange = () => load(true));
$('#more').onclick = () => load(false);

loadStats(); load(true);
</script></body></html>"""