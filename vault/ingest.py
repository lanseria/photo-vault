from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import hashutil, meta
from .config import Config

# Live Photo 配对：静帧 + 动态部分。别的组合（jfif+mpeg、jpg+mts…）只是同名巧合，
# 合并会把视频错算成图片的 live_motion。
LIVE_STILL_EXT = {".heic", ".heif", ".jpg", ".jpeg"}
LIVE_MOTION_EXT = {".mov", ".mp4"}

# RAW+JPEG：同一张照片拍了两种文件。列表只展示一条，出稿时两个一起给。
JPEG_EXT = {".jpg", ".jpeg"}
PAIR_WINDOW_SECONDS = 300      # 同目录同名之外，再要求拍摄时间接近（防同名重拍）


@dataclass
class Stats:
    scanned: int = 0
    imported: int = 0
    duplicate: int = 0
    skipped: int = 0
    failed: int = 0
    linked: int = 0
    copied: int = 0
    bytes: int = 0
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "imported": self.imported,
            "duplicate": self.duplicate,
            "skipped": self.skipped,
            "failed": self.failed,
            "linked": self.linked,
            "copied": self.copied,
            "bytes": self.bytes,
        }


def iter_media(root: Path, exclude: list[Path]):
    root = root.resolve()
    ex = [e.resolve() for e in exclude]
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        if any(str(cur).startswith(str(e)) for e in ex):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not d.startswith("@")
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            p = cur / name
            if meta.is_media(p):
                yield p


def _upsert_source(conn, kind: str, root: Path) -> int:
    row = conn.execute(
        "select id from sources where kind=%s and root_path=%s", (kind, str(root))
    ).fetchone()
    if row:
        conn.execute(
            "update sources set last_seen=now() where id=%s", (row["id"],)
        )
        return row["id"]
    row = conn.execute(
        "insert into sources (kind, label, root_path) values (%s,%s,%s) returning id",
        (kind, f"{kind}:{root.name}", str(root)),
    ).fetchone()
    return row["id"]


def _upsert_device(conn, make: str | None, model: str | None) -> int | None:
    if not model:
        return None
    make = (make or "").strip()
    model = model.strip()
    row = conn.execute(
        "select id from devices where make=%s and model_raw=%s", (make, model)
    ).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        "insert into devices (make, model_raw, model_canonical, kind) values (%s,%s,%s,%s) returning id",
        (make, model, model, _device_kind(make, model)),
    ).fetchone()
    return row["id"]


def _device_kind(make: str, model: str) -> str:
    s = f"{make} {model}".lower()
    if "dji" in s or "mavic" in s or "osmo" in s:
        return "drone"
    if "gopro" in s or "action" in s:
        return "actioncam"
    if "apple" in s or "iphone" in s or "ipad" in s:
        return "phone"
    if any(k in s for k in ("canon", "nikon", "sony", "fujifilm", "leica", "panasonic")):
        return "camera"
    return "other"


def _thumb_path(cfg: Config, sha: str) -> Path:
    return cfg.derived_root / "thumbs" / sha[0:2] / sha[2:4] / f"{sha}.webp"


def _video_poster(src: Path, dst: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-v", "quiet", "-y", "-ss", "1", "-i", str(src),
            "-frames:v", "1", "-vf", "scale=512:-2", str(dst),
        ]
        if subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0:
            return dst.exists()
        # 短视频可能没有 1s 处，退回第一帧
        cmd[cmd.index("1")] = "0"
        subprocess.run(cmd, capture_output=True, timeout=60)
        return dst.exists()
    except Exception:
        return False


def _live_pair_ok(conn, asset_id: str, video: Path) -> bool:
    """只有"iPhone 风格静帧 + .mov/.mp4"才认作 Live Photo。

    原来的规则是"同目录同 stem 的任意图片 + 任意视频"，会把素材库里
    `cover.jfif` + `cover.mpeg`、`clip.jpg` + `clip.mts` 这种同名巧合也合并成
    live_photo —— 视频被挂成 live_motion，分辨率/时长全按图片算。收紧后：
      · 静帧只认 .heic/.heif/.jpg/.jpeg（Live Photo 的两种导出）
      · 动态部分只认 .mov/.mp4
    真正的 iPhone Live Photo 仍然配对（HEIC+MOV 或 JPG+MOV）。
    """
    if video.suffix.lower() not in LIVE_MOTION_EXT:
        return False
    row = conn.execute(
        "select ext from files where asset_id=%s and role='original' limit 1",
        (asset_id,),
    ).fetchone()
    return bool(row) and (row["ext"] or "").lower() in LIVE_STILL_EXT


def _stem_key(path: str) -> str:
    """目录 + 文件名（不含扩展名），小写。RAW/JPEG 配对和重扫都靠它。"""
    return re.sub(r"\.[^.]+$", "", path or "").lower()


def _pair_sibling(conn, asset_id: str, ext: str, origin_rel: str,
                  library: str, local: datetime | None) -> dict | None:
    """找这张照片的另一半：RAW 来了找同名的 JPEG，JPEG 来了找同名的 RAW。

    条件：同库、同目录同文件名（忽略大小写）、拍摄时间相差 ≤ 5 分钟，
    并且对方还没有挂过 RAW（避免重复配对）。返回对方资产里那条 original 文件。
    """
    if ext in meta.RAW_EXT:
        wanted = list(JPEG_EXT)
    elif ext in JPEG_EXT:
        wanted = list(meta.RAW_EXT)
    else:
        return None
    key = _stem_key(origin_rel)
    if not key:
        return None
    rows = conn.execute(
        """
        select f.asset_id, f.id file_id, f.ext, a.taken_at_local, a.device_id
          from files f join assets a on a.id = f.asset_id
         where f.role = 'original'
           and lower(f.ext) = any(%s)
           and lower(regexp_replace(f.origin_rel, '\\.[^.]+$', '')) = %s
           and a.library = %s and not a.is_deleted
           and f.asset_id <> %s
           and not exists (select 1 from files x
                            where x.asset_id = f.asset_id and x.role = 'raw')
        """,
        (wanted, key, library, asset_id),
    ).fetchall()
    best = None
    for row in rows:
        other = row["taken_at_local"]
        if local is not None and other is not None:
            delta = abs((other - local).total_seconds())
            if delta > PAIR_WINDOW_SECONDS:
                continue
        else:
            delta = float("inf")      # 有一边没时间：同目录同名仍然认
        if best is None or delta < best[0]:
            best = (delta, row)
    return best[1] if best else None


def merge_pair(conn, raw_id: str, jpeg_id: str) -> None:
    """把两条资产合成一条：RAW 以 role='raw' 挂到 JPEG 那条上，另一条删掉。

    保留 JPEG 是因为缩略图、预览、宽高都按它算；RAW 只在出稿时使用。
    标签、喜欢、设备、剪辑箱都并到保留的那条，不丢用户数据。
    """
    conn.execute(
        "update files set asset_id=%s, role='raw' where asset_id=%s and role='original'",
        (jpeg_id, raw_id),
    )
    conn.execute(
        """insert into asset_tags (asset_id, tag_id)
           select %s, tag_id from asset_tags where asset_id=%s
           on conflict do nothing""",
        (jpeg_id, raw_id),
    )
    conn.execute(
        """insert into bin_items (bin_id, asset_id, ord)
           select bin_id, %s, ord from bin_items where asset_id=%s
           on conflict do nothing""",
        (jpeg_id, raw_id),
    )
    conn.execute(
        """update assets set
             favorite = favorite or (select favorite from assets where id=%s),
             device_id = coalesce(device_id, (select device_id from assets where id=%s)),
             width = coalesce(width, (select width from assets where id=%s)),
             height = coalesce(height, (select height from assets where id=%s))
           where id=%s""",
        (raw_id, raw_id, raw_id, raw_id, jpeg_id),
    )
    conn.execute("delete from assets where id=%s", (raw_id,))


def pair_with_sibling(conn, asset_id: str, ext: str, origin_rel: str,
                      library: str, local: datetime | None) -> str | None:
    """刚入库的文件如果有配对的另一半，就地合并成一个资产。

    返回活下来的资产 id（被合并掉的那条已删除），没配上返回 None。
    """
    sibling = _pair_sibling(conn, asset_id, ext, origin_rel, library, local)
    if not sibling:
        return None
    keep = sibling["asset_id"] if ext in meta.RAW_EXT else asset_id
    drop = asset_id if ext in meta.RAW_EXT else sibling["asset_id"]
    merge_pair(conn, drop, keep)
    return keep


def ingest_file(
    conn,
    cfg: Config,
    path: Path,
    source_id: int,
    stem_map: dict[tuple[Path, str], str],
    stats: Stats,
    dry_run: bool = False,
    mode: str = "copy",
    root: Path | None = None,
    library: str = "gallery",
    *,
    sha_hint: tuple[str, int] | None = None,
    origin: tuple[str, str] | None = None,
    stem_key: tuple[Path, str] | None = None,
) -> None:
    """
    mode:
      copy  —— 拷贝进 vault（默认，最安全，占双份空间）
      link  —— 硬链接进 vault（同卷零额外空间，秒级；原目录删掉也不影响库）
      move  —— 文件已经在 landing（网页上传边收边写），直接 os.replace 进 vault。
               全流程只写一次盘；重复文件在 landing 里删掉。

    library:
      gallery  —— 相册（默认）
      material —— 素材库。直属落库，不经过"先进相册再移动"那一步。

    sha_hint / origin / stem_key:
      网页上传专用。sha_hint 是收流时已经算好的哈希（避免再读一遍 GB 级视频）；
      origin 覆盖 files 里的来源信息（上传没有磁盘路径）；stem_key 让同一批上传里
      的 HEIC+MOV 也能配成 Live Photo（临时文件名是随机的，不能拿它当 stem）。
    """
    if library not in ("gallery", "material"):
        raise ValueError("library 只能是 gallery 或 material")
    stats.scanned += 1
    ext = path.suffix.lower()
    key = stem_key or (path.parent, path.stem)

    # 来源信息：origin_rel 用相对路径，换盘/换挂载点后仍然稳定。
    origin_abs = origin[0] if origin else str(path)
    if origin:
        origin_rel = origin[1]
    elif root is not None:
        try:
            origin_rel = path.relative_to(root).as_posix()
        except ValueError:
            origin_rel = path.name
    else:
        origin_rel = path.name

    # ---- 0. 快速去重（重扫 / 断点续跑）
    #  同一来源相对路径 + 同样大小 → 这份内容之前就入过库了，不必再把整份文件读一遍。
    #  2 TB 的重扫从“读两小时”变成“查几千次库、几秒”，断点续跑也不会从头算哈希。
    #  网页上传（origin 不为空）不走这条路：不同设备可能有同名同大小的照片。
    if origin is None and root is not None:
        quick = conn.execute(
            """select f.asset_id, a.kind, a.library
                 from files f join assets a on a.id = f.asset_id
                where f.role in ('original', 'live_motion', 'raw')
                  and f.origin_rel = %s and f.bytes = %s
                limit 1""",
            (origin_rel, path.stat().st_size),
        ).fetchone()
        if quick:
            stats.duplicate += 1
            if (quick["kind"] in ("photo", "raw") and quick["library"] == library
                    and ext in LIVE_MOTION_EXT):
                stem_map.setdefault(key, str(quick["asset_id"]))
            return

    # ---- 1. 取哈希
    #   copy: 边拷到 landing 边算（一次读、一次写）
    #   link: 只读一遍算哈希，随后直接建硬链接
    #   move: 已经在 landing，哈希由调用方在收流时算好
    tmp: Path | None = None
    if mode == "move":
        tmp = path
        sha, nbytes = sha_hint or hashutil.sha256_file(path)
    elif dry_run or mode == "link":
        sha, nbytes = hashutil.sha256_file(path)
    else:
        tmp = hashutil.new_landing_path(cfg.landing_root)
        sha, nbytes = hashutil.copy_and_hash(path, tmp)

    # ---- 2. 精确去重
    #  查任意 role：Live Photo 的 MOV 是以 live_motion 入库的，只看 role='original'
    #  会让同一段视频在每次重扫/重传时再登记成一条独立视频（实测重扫会把 773 个
    #  live_motion 又存一遍）。
    dup = conn.execute(
        """select f.asset_id, a.kind, a.library
             from files f join assets a on a.id=f.asset_id
            where f.sha256=%s limit 1""",
        (bytes.fromhex(sha),),
    ).fetchone()
    if dup:
        stats.duplicate += 1
        if tmp:
            tmp.unlink(missing_ok=True)      # move: 上传的临时文件直接丢掉
        # 静帧已经在库里（比如上次导过）时，这次补传的 MOV 仍然能挂成 live_motion。
        # 只在同一个库内配对：跨库合并会让"导入素材库"看起来像没导进去。
        if (dup["kind"] in ("photo", "raw") and dup["library"] == library
                and ext in LIVE_MOTION_EXT):
            stem_map.setdefault(key, str(dup["asset_id"]))
        return

    # ---- 3. 探测元数据 + 时区
    p = meta.probe(path)
    local, offset, tz_source, utc_known = meta.resolve_time(p, cfg.default_tz_offset_min)
    if local is None:
        # 完全没有时间信息（截图导出、聊天保存图）→ 退回文件 mtime，至少能排序
        local = datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0)
        tz_source = "mtime"
    utc = utc_known or meta.to_utc(local, offset)

    if dry_run:
        stats.imported += 1
        stats.bytes += nbytes
        return

    # ---- 4. 入 vault（原子）
    rel = hashutil.vault_rel_path(sha, ext)
    final = cfg.vault_root / rel
    final.parent.mkdir(parents=True, exist_ok=True)

    used_mode = "copy"
    if mode == "move":
        assert tmp is not None
        os.replace(tmp, final)            # landing 与 vault 同文件系统，原子且不复制
        stats.copied += 1
        stats.bytes += nbytes
    elif mode == "link":
        try:
            os.link(path, final)          # 同卷：O(1)，不占额外空间
            used_mode = "link"
            stats.linked += 1
        except OSError:
            # 跨卷 / 不支持硬链接 / 源只读挂载 → 退回拷贝
            shutil.copy2(path, final)
            used_mode = "copy"
            stats.copied += 1
            stats.bytes += nbytes
    else:
        assert tmp is not None
        os.replace(tmp, final)            # 同一文件系统，原子
        stats.copied += 1
        stats.bytes += nbytes

    try:
        # ---- 5. Live Photo 配对（同目录同文件名，HEIC + MOV）
        kind = p.kind
        role = "original"
        asset_id = stem_map.get(key)
        paired_id = None

        with conn.transaction():
            if asset_id and p.kind == "video" and _live_pair_ok(conn, asset_id, path):
                role = "live_motion"
                kind = "live_photo"
                conn.execute(
                    "update assets set kind='live_photo' where id=%s", (asset_id,)
                )
            else:
                device_id = _upsert_device(conn, p.make, p.model)
                row = conn.execute(
                    """
                    insert into assets
                      (kind, taken_at_local, tz_offset_min, tz_source, taken_at_utc,
                       device_id, lens, width, height, duration_ms, gps, library)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            case when %s::float8 is null or %s::float8 is null then null
                                 else point(%s::float8,%s::float8) end,
                            %s)
                    returning id
                    """,
                    (
                        kind, local, offset, tz_source, utc, device_id, p.lens,
                        p.width, p.height, p.duration_ms,
                        p.gps_lat, p.gps_lon, p.gps_lon, p.gps_lat,
                        library,
                    ),
                ).fetchone()
                asset_id = row["id"]
                stem_map[key] = asset_id

                conn.execute(
                    """insert into exif (asset_id, iso, aperture, shutter, focal_mm, orientation, raw)
                       values (%s,%s,%s,%s,%s,%s,%s)""",
                    (asset_id, p.iso, p.aperture, p.shutter, p.focal_mm,
                     p.orientation, _jsonb(p.raw)),
                )

            conn.execute(
                """insert into files
                     (asset_id, role, sha256, bytes, mime, ext, rel_path, width, height,
                      phash, source_id, src_path, storage_mode, origin_path, origin_rel)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (sha256, role) do nothing""",
                (asset_id, role, bytes.fromhex(sha), nbytes, p.mime, ext, rel,
                 p.width, p.height, hashutil.average_hash(final),
                 source_id, origin_abs, used_mode, origin_abs, origin_rel),
            )

            # ---- 5b. RAW+JPEG 配对：同目录同名的另一半已经在库里就合成一条
            paired_id = None
            if role == "original" and (ext in meta.RAW_EXT or ext in JPEG_EXT):
                paired_id = pair_with_sibling(conn, asset_id, ext, origin_rel,
                                              library, local)
                if paired_id:
                    asset_id = paired_id
                    stem_map[key] = paired_id
    except Exception:
        final.unlink(missing_ok=True)
        raise

    # ---- 6. 衍生图（配对进 JPEG 的那张 RAW 不用再出缩略图）
    if role == "original" and not (paired_id and ext in meta.RAW_EXT):
        dst = _thumb_path(cfg, sha)
        ok = _video_poster(final, dst) if p.kind == "video" else meta.make_thumb(
            final, dst, cfg.thumb_size, cfg.thumb_quality
        )
        if not ok:
            stats.skipped += 1

    stats.imported += 1


def _jsonb(obj: dict):
    from psycopg.types.json import Jsonb

    return Jsonb(obj)


def run_ingest(
    conn,
    cfg: Config,
    roots: list[Path],
    kind: str = "folder",
    dry_run: bool = False,
    progress_every: int = 200,
    mode: str = "copy",
    library: str = "gallery",
) -> Stats:
    if library not in ("gallery", "material"):
        raise ValueError("library 只能是 gallery 或 material")
    cfg.ensure_dirs()
    stats = Stats()
    stem_map: dict[tuple[Path, str], str] = {}

    run_row = conn.execute(
        "insert into ingest_runs (root) values (%s) returning id",
        (", ".join(str(r) for r in roots),),
    ).fetchone()
    run_id = run_row["id"]
    conn.commit()

    # 影视库目录永远不进相册：片子不是"照片资产"，也不该被算哈希/拷贝/打标。
    # 它通常挂在 HOST_ROOT 下面的 media/，不排除的话 `ingest <HOST_ROOT>` 会顺手吃进去。
    exclude = [cfg.vault_root, cfg.derived_root, cfg.landing_root]
    if cfg.media_root not in exclude:
        exclude.append(cfg.media_root)

    try:
        for root in roots:
            source_id = _upsert_source(conn, kind, root)
            conn.commit()
            for i, path in enumerate(iter_media(root, exclude), 1):
                try:
                    ingest_file(
                        conn, cfg, path, source_id, stem_map, stats,
                        dry_run=dry_run, mode=mode, root=root, library=library,
                    )
                    if not dry_run:
                        conn.commit()
                except Exception as exc:
                    conn.rollback()
                    stats.failed += 1
                    stats.errors.append(
                        {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
                    )
                    if len(stats.errors) > 500:
                        stats.errors = stats.errors[-500:]
                if progress_every and i % progress_every == 0:
                    print(
                        f"  .. {i} 扫描 / {stats.imported} 入库 / "
                        f"{stats.linked} 链接 / {stats.duplicate} 重复 / "
                        f"{stats.failed} 失败",
                        file=sys.stderr, flush=True,
                    )
    finally:
        conn.execute(
            "update ingest_runs set ended_at=now(), stats=%s, errors=%s where id=%s",
            (_jsonb(stats.as_dict()), _jsonb(stats.errors), run_id),
        )
        conn.commit()

    return stats


def cleanup_landing(cfg: Config, older_than_seconds: int = 3600) -> int:
    """清理崩溃遗留的 landing 临时文件。"""
    import time

    if not cfg.landing_root.exists():
        return 0
    now = time.time()
    n = 0
    for f in cfg.landing_root.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > older_than_seconds:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n

# ---------------------------------------------------------------- 反向对账
# 场景：数据库被重建 / 事务提交前崩溃 / 手动往 vault 里塞了文件。
# 目标：让"磁盘上有、库里没有"的文件重新登记，而不是变成幽灵数据。

def iter_vault_files(cfg: Config):
    """遍历 vault 中的物理文件，跳过 landing 暂存区。"""
    if not cfg.vault_root.exists():
        return
    landing = cfg.landing_root.resolve()
    for p in sorted(cfg.vault_root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            if p.resolve().is_relative_to(landing):
                continue
        except (OSError, ValueError):
            pass
        yield p.relative_to(cfg.vault_root).as_posix(), p


def vault_orphans(conn, cfg: Config) -> list[tuple[str, Path]]:
    known = {r["rel_path"] for r in conn.execute("select rel_path from files")}
    return [(rel, p) for rel, p in iter_vault_files(cfg) if rel not in known]


def reconcile(conn, cfg: Config) -> Stats:
    """
    重新登记 vault 中的孤儿文件。
    直接复用 ingest_file：它会重新算哈希、探测元数据，
    再把文件原子替换回同一路径（内容相同，等于顺带做了一次完整性校验）。
    """
    cfg.ensure_dirs()
    stats = Stats()
    orphans = vault_orphans(conn, cfg)
    if not orphans:
        return stats

    source_id = _upsert_source(conn, "reconcile", cfg.vault_root)
    conn.commit()
    stem_map: dict[tuple[Path, str], str] = {}

    for rel, path in orphans:
        try:
            ingest_file(conn, cfg, path, source_id, stem_map, stats,
                        mode="copy", root=cfg.vault_root)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            stats.failed += 1
            stats.errors.append(
                {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
            )
    return stats
