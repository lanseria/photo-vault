from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import hashutil, meta
from .config import Config


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
) -> None:
    """
    mode:
      copy  —— 拷贝进 vault（默认，最安全，占双份空间）
      link  —— 硬链接进 vault（同卷零额外空间，秒级；原目录删掉也不影响库）
    """
    stats.scanned += 1
    ext = path.suffix.lower()

    # ---- 1. 取哈希
    #   copy: 边拷到 landing 边算（一次读、一次写）
    #   link: 只读一遍算哈希，随后直接建硬链接
    tmp: Path | None = None
    if dry_run or mode == "link":
        sha, nbytes = hashutil.sha256_file(path)
    else:
        tmp = hashutil.new_landing_path(cfg.landing_root)
        sha, nbytes = hashutil.copy_and_hash(path, tmp)

    # ---- 2. 精确去重
    dup = conn.execute(
        "select id from files where sha256=%s and role='original'", (bytes.fromhex(sha),)
    ).fetchone()
    if dup:
        stats.duplicate += 1
        if tmp:
            tmp.unlink(missing_ok=True)
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
    if mode == "link":
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
        origin_abs = str(path)
        try:
            origin_rel = path.relative_to(root).as_posix() if root else path.name
        except ValueError:
            origin_rel = path.name

        # ---- 5. Live Photo 配对（同目录同文件名，HEIC + MOV）
        stem_key = (path.parent, path.stem)
        kind = p.kind
        role = "original"
        asset_id = stem_map.get(stem_key)

        with conn.transaction():
            if asset_id and p.kind == "video":
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
                       device_id, lens, width, height, duration_ms, gps)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            case when %s::float8 is null or %s::float8 is null then null
                                 else point(%s::float8,%s::float8) end)
                    returning id
                    """,
                    (
                        kind, local, offset, tz_source, utc, device_id, p.lens,
                        p.width, p.height, p.duration_ms,
                        p.gps_lat, p.gps_lon, p.gps_lon, p.gps_lat,
                    ),
                ).fetchone()
                asset_id = row["id"]
                stem_map[stem_key] = asset_id

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
                 source_id, str(path), used_mode, origin_abs, origin_rel),
            )
    except Exception:
        final.unlink(missing_ok=True)
        raise

    # ---- 6. 衍生图
    if role == "original":
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
) -> Stats:
    cfg.ensure_dirs()
    stats = Stats()
    stem_map: dict[tuple[Path, str], str] = {}

    run_row = conn.execute(
        "insert into ingest_runs (root) values (%s) returning id",
        (", ".join(str(r) for r in roots),),
    ).fetchone()
    run_id = run_row["id"]
    conn.commit()

    exclude = [cfg.vault_root, cfg.derived_root, cfg.landing_root]

    try:
        for root in roots:
            source_id = _upsert_source(conn, kind, root)
            conn.commit()
            for i, path in enumerate(iter_media(root, exclude), 1):
                try:
                    ingest_file(
                        conn, cfg, path, source_id, stem_map, stats,
                        dry_run=dry_run, mode=mode, root=root,
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
