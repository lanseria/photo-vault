from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from .config import Config
from . import db as dbmod
from . import ingest as ingestmod


def cmd_initdb(args: argparse.Namespace) -> int:
    cfg = Config.load()
    cfg.ensure_dirs()
    applied = dbmod.init_db(cfg.database_url)
    print(f"DB 就绪：{dbmod.ping(cfg.database_url)}")
    print(f"已应用迁移：{applied or '（无新增）'}")
    print(f"vault   = {cfg.vault_root}")
    print(f"landing = {cfg.landing_root}")
    print(f"derived = {cfg.derived_root}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = Config.load()
    roots = [Path(p) for p in args.paths]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        print(f"路径不存在：{missing}", file=sys.stderr)
        return 2

    started = time.time()
    with dbmod.connect(cfg.database_url) as conn:
        stats = ingestmod.run_ingest(
            conn, cfg, roots, kind=args.kind,
            dry_run=args.dry_run, progress_every=args.progress,
            mode=args.mode, library=args.library,
        )

    dur = time.time() - started
    print("")
    print("─" * 56)
    print(f"目标库    {'素材库' if args.library == 'material' else '图库'}")
    print(f"扫描      {stats.scanned}")
    print(f"入库      {stats.imported}")
    print(f"  硬链接  {stats.linked}")
    print(f"  拷贝    {stats.copied}")
    print(f"重复跳过  {stats.duplicate}")
    print(f"失败      {stats.failed}")
    print(f"写入      {stats.bytes / 1024 / 1024:.1f} MB")
    print(f"耗时      {dur:.1f}s" + (f"  ({stats.scanned/dur:.1f} 文件/s)" if dur > 0 else ""))
    if args.mode == "link" and stats.copied:
        print(f"\n注意：有 {stats.copied} 个文件退化成了拷贝（跨卷或硬链接不可用）。")
    if stats.errors:
        print("")
        print("失败样例（最多 10 条）：")
        for e in stats.errors[:10]:
            print(f"  - {e['path']}: {e['error']}")
    return 1 if stats.failed else 0


def cmd_audit(args: argparse.Namespace) -> int:
    from . import audit as auditmod

    cfg = Config.load()
    roots = [Path(p) for p in args.paths]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        print(f"路径不存在：{missing}", file=sys.stderr)
        return 2

    print(f"正在扫描 {len(roots)} 个路径（只读，不会改动任何文件）…", flush=True)
    started = time.time()
    result = auditmod.audit(
        roots, cfg.vault_root,
        exclude=[cfg.derived_root, cfg.landing_root, cfg.media_root],
        max_hash=args.max_hash,
        sample_exif=args.sample_exif,
        workers=args.workers,
    )

    link_ok, link_reason = auditmod.probe_link(roots[0], cfg.vault_root)

    out_dir = Path(args.out) if args.out else (cfg.derived_root / "audit")
    out_dir.mkdir(parents=True, exist_ok=True)
    md = auditmod.render_markdown(result, bool(link_ok))
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "report.json").write_text(
        __import__("json").dumps(auditmod.render_json(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n扫描完成，耗时 {time.time()-started:.1f}s")
    print(f"  文件 {result.total_files:,}    "
          f"大小 {auditmod._fmt_bytes(result.total_bytes)}    "
          f"重复浪费 {auditmod._fmt_bytes(result.dup_bytes)}")
    print(f"  硬链接可行性：{link_reason}")
    print(f"\n报告已写入：")
    print(f"  {out_dir / 'report.md'}")
    print(f"  {out_dir / 'report.json'}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = Config.load()
    with dbmod.connect(cfg.database_url) as conn:
        total = conn.execute(
            "select count(*) c, coalesce(sum(f.bytes),0) b from assets a "
            "left join files f on f.asset_id=a.id and f.role='original' "
            "where not a.is_deleted"
        ).fetchone()
        by_kind = conn.execute(
            "select kind, count(*) c from assets where not is_deleted group by kind order by c desc"
        ).fetchall()
        by_device = conn.execute(
            """select coalesce(d.model_raw,'(unknown)') model, d.kind, count(*) c,
                      min(a.taken_at_utc) first_at, max(a.taken_at_utc) last_at
               from assets a left join devices d on d.id=a.device_id
               where not a.is_deleted group by d.model_raw, d.kind order by c desc limit 20"""
        ).fetchall()
        tz = conn.execute(
            "select tz_source, count(*) c from assets where not is_deleted group by tz_source order by c desc"
        ).fetchall()

    print(f"资产总数 {total['c']}    占用 {total['b']/1024/1024/1024:.2f} GB")
    print("\n按类型：")
    for r in by_kind:
        print(f"  {r['kind']:<12} {r['c']}")
    print("\n按时区来源（正确性体检）：")
    for r in tz:
        print(f"  {r['tz_source'] or '?':<10} {r['c']}")
    print("\nTop 设备：")
    for r in by_device:
        span = ""
        if r["first_at"]:
            span = f"  {r['first_at']:%Y-%m-%d} → {r['last_at']:%Y-%m-%d}"
        print(f"  {r['model'][:28]:<30} {r['c']:>7}{span}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.load()
    cfg.ensure_dirs()
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(f"[{'OK ' if cond else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")

    check("vault 可写", os.access(cfg.vault_root, os.W_OK), str(cfg.vault_root))
    check("landing 可写", os.access(cfg.landing_root, os.W_OK), str(cfg.landing_root))

    try:
        same = os.stat(cfg.vault_root).st_dev == os.stat(cfg.landing_root).st_dev
    except OSError:
        same = False
    check("landing 与 vault 同文件系统（原子提交前提）", same)

    du = shutil.disk_usage(cfg.vault_root)
    check("vault 剩余空间 > 10GB", du.free > 10 * 1024**3, f"{du.free/1024**3:.1f} GB")

    try:
        print(f"[OK ] DB  {dbmod.ping(cfg.database_url)[:60]}")
    except Exception as exc:
        ok = False
        print(f"[FAIL] DB  {type(exc).__name__}: {exc}")

    check("ffprobe（视频元数据）", shutil.which("ffprobe") is not None)
    check("ffmpeg（视频封面）", shutil.which("ffmpeg") is not None)

    media_ok = cfg.media_root.exists()
    print(f"[{'INFO' if media_ok else 'WARN'}] 影视目录 MEDIA_ROOT={cfg.media_root}"
          + (f"（宿主机 {cfg.media_host_root}）" if cfg.media_host_root else "")
          + ("" if media_ok else "  不存在：影视模块暂时为空；配好 MEDIA_HOST_ROOT 后重建容器"))

    stale = ingestmod.cleanup_landing(cfg)
    print(f"[INFO] 清理遗留 landing 临时文件：{stale}")

    with dbmod.connect(cfg.database_url) as conn:
        recorded = conn.execute("select count(*) c from files").fetchone()["c"]
        orphans = ingestmod.vault_orphans(conn, cfg)
    print(f"[INFO] 文件记录 {recorded}，vault 中有 {len(orphans)} 个文件未登记")
    for rel, _ in orphans[:5]:
        print(f"       - {rel}")
    if orphans:
        print("       跑 `vault reconcile` 可重新登记（会顺带校验哈希）")

    print("\n" + ("体检通过" if ok else "存在问题，见上"))
    return 0 if ok else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    cfg = Config.load()
    with dbmod.connect(cfg.database_url) as conn:
        stats = ingestmod.reconcile(conn, cfg)
    print(f"重新登记 {stats.imported} 个文件，失败 {stats.failed}，校验 {stats.bytes/1024/1024:.1f} MB")
    for e in stats.errors[:10]:
        print(f"  - {e['path']}: {e['error']}")
    return 1 if stats.failed else 0


def cmd_prune_live_dups(args: argparse.Namespace) -> int:
    """清理 Live Photo 重扫/重传产生的重复视频登记。

    背景：去重最初只看 role='original'，而 Live Photo 的动态部分是以
    role='live_motion' 入库的。于是同一段 MOV 每次重扫都会再登记成一条独立
    视频资产（重扫 7000 个文件就多出 700+ 条）。
    这里只删「原件 sha 同时出现在 live_motion」的资产记录：物理文件还在 vault 里、
    仍属于那张 Live Photo；带标签/人脸/剪辑箱的资产不动，留给人工确认。
    """
    where = """
        exists (select 1 from files f1 join files f2 on f1.sha256 = f2.sha256
                 where f1.asset_id = a.id and f1.role = 'original'
                   and f2.role = 'live_motion')
        and not exists (select 1 from asset_tags at2 where at2.asset_id = a.id)
        and not exists (select 1 from faces fa where fa.asset_id = a.id)
        and not exists (select 1 from bin_items bi where bi.asset_id = a.id)
    """
    cfg = Config.load()
    with dbmod.connect(cfg.database_url) as conn:
        n = conn.execute(f"select count(*) c from assets a where {where}").fetchone()["c"]
        if not n:
            print("没有需要清理的重复登记。")
            return 0
        if not args.yes:
            print(f"发现 {n} 条重复视频登记（只删库记录，不动物理文件）。")
            print("确认无误后加 --yes 执行。")
            return 0
        cur = conn.execute(f"delete from assets a where {where}")
        print(f"已清理 {cur.rowcount} 条重复视频登记。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "vault.web:app",
        host=args.host,
        port=args.port,
        log_level="info",
        workers=1,
    )
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from . import bridge

    if args.install:
        cmd = bridge.install_autostart()
        print(f"已设为开机自启（HKCU\\...\\Run\\{bridge.RUN_NAME}）：\n  {cmd}")
        print("下次登录自动起在 127.0.0.1:8099；现在想立刻用就再跑一次 `photo-vault bridge`。")
        return 0
    if args.uninstall:
        ok = bridge.uninstall_autostart()
        print("已移除开机自启" if ok else "本来就没设过")
        return 0
    bridge.run(args.host, args.port)
    return 0


# ---------------------------------------------------------------- 影视库

def cmd_cinema_import(args: argparse.Namespace) -> int:
    from . import cinema

    cfg = Config.load()
    cfg.ensure_dirs()
    roots: list[Path] = []
    for raw in args.paths:
        try:
            roots.append(cinema.resolve_input(cfg, raw))
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.dry_run:
        print(f"干跑：只解析文件名，不写库（{len(roots)} 个路径）\n")
        n = 0
        for root in roots:
            for p in cinema.walk_videos(root):
                parsed = cinema.parse_name(p, forced=args.kind)
                where = (f"S{parsed.season:02d}E{parsed.episode:03d}"
                         if parsed.kind == "series" else "电影")
                ep = f"  「{parsed.episode_title}」" if parsed.episode_title else ""
                print(f"  {where:<10} {parsed.title}"
                      f"{f'  ({parsed.year})' if parsed.year else ''}{ep}")
                n += 1
        print(f"\n共 {n} 个视频文件。确认无误后去掉 --dry-run 正式导入。")
        return 0

    total = cinema.ImportStats()
    started = time.time()
    with dbmod.connect(cfg.database_url) as conn:
        for root in roots:
            files = list(cinema.walk_videos(root))
            print(f"导入 {root}（{len(files)} 个视频）…", flush=True)

            def progress(p: dict, _n=len(files)) -> None:
                if p["done"] and args.progress and p["done"] % args.progress == 0:
                    print(f"  .. {p['done']}/{p['total']} 扫描 / "
                          f"{p['imported']} 新增 / {p['updated']} 更新 / "
                          f"{p['failed']} 失败", file=sys.stderr, flush=True)

            stats = cinema.run_import(
                conn, cfg, root, forced=args.kind,
                posters=not args.no_posters, progress=progress)
            for key in total.as_dict():
                setattr(total, key, getattr(total, key) + getattr(stats, key))
            total.errors.extend(stats.errors)

    dur = time.time() - started
    print("")
    print("─" * 56)
    print(f"扫描      {total.scanned}")
    print(f"新增      {total.imported}")
    print(f"更新      {total.updated}")
    print(f"影视条目  {total.titles}")
    print(f"字幕      {total.subtitles}")
    print(f"失败      {total.failed}")
    print(f"耗时      {dur:.1f}s")
    if total.errors:
        print("\n失败样例（最多 10 条）：")
        for e in total.errors[:10]:
            print(f"  - {e['path']}: {e['error']}")
    return 1 if total.failed else 0


def cmd_cinema_list(args: argparse.Namespace) -> int:
    from . import cinema

    cfg = Config.load()
    with dbmod.connect(cfg.database_url) as conn:
        where = "where 1=1"
        params: tuple = ()
        if args.kind:
            where += " and c.kind = %s"
            params = (args.kind,)
        rows = conn.execute(
            f"""select c.kind, c.title, c.year, c.poster_rel,
                       count(e.id) episodes,
                       coalesce(sum(e.duration_ms), 0) dur_ms,
                       coalesce(sum(e.bytes), 0) bytes,
                       coalesce(sum((select count(*) from cinema_subtitles s
                                      where s.episode_id = e.id)), 0) subs
                  from cinema_titles c
                  left join cinema_episodes e on e.title_id = c.id
                  {where}
                 group by c.id
                 order by c.kind desc, c.title_key""", params).fetchall()
    if not rows:
        print("影视库是空的。先跑 `photo-vault cinema-import <目录>`。")
        return 0
    print(f"{len(rows)} 个影视条目：\n")
    for r in rows:
        kind = "剧集" if r["kind"] == "series" else "电影"
        meta = f"{kind}"
        if r["kind"] == "series":
            meta += f" · {r['episodes']} 集"
        if r["year"]:
            meta += f" · {r['year']}"
        dur = (r["dur_ms"] or 0) / 60000
        print(f"  {r['title']:<32} {meta:<18} "
              f"{dur:>7.0f} min  {r['bytes'] / 1024**3:>6.1f} GB  "
              f"字幕 {r['subs']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vault", description="Photo Vault P0")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("initdb", help="建表 / 迁移")
    s.set_defaults(func=cmd_initdb)

    s = sub.add_parser("ingest", help="导入目录")
    s.add_argument("paths", nargs="+")
    s.add_argument("--kind", default="folder",
                   help="来源类型：folder|sd|photokit|smb|dji")
    s.add_argument("--mode", default="copy", choices=["copy", "link"],
                   help="copy=拷贝进 vault（安全）；link=硬链接（同卷零额外空间，秒级）")
    s.add_argument("--library", default="gallery", choices=["gallery", "material"],
                   help="导入到哪个库：gallery=图库相册（默认），material=素材库")
    s.add_argument("--dry-run", action="store_true", help="只扫描，不写库不落盘")
    s.add_argument("--progress", type=int, default=200, help="每 N 个文件打印进度")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("audit", help="入库前体检（只读）：规模、格式、重复、时间可信度")
    s.add_argument("paths", nargs="+")
    s.add_argument("--out", default=None,
                   help="报告输出目录（默认 <derived>/audit，容器外可见）")
    s.add_argument("--max-hash", type=int, default=50_000,
                   help="重复检测最多哈希多少个候选文件")
    s.add_argument("--sample-exif", type=int, default=500,
                   help="随机抽样多少个文件读 EXIF（0=不抽样）")
    s.add_argument("--workers", type=int, default=8, help="哈希并发数")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("doctor", help="环境体检")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("reconcile", help="把 vault 里未登记的文件重新入库（并校验哈希）")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("prune-live-dups",
                       help="清理 Live Photo 重扫产生的重复视频登记（只删库记录）")
    s.add_argument("--yes", action="store_true", help="真的删除；不加只统计")
    s.set_defaults(func=cmd_prune_live_dups)

    s = sub.add_parser("serve", help="启动 Web 界面")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("bridge", help="宿主机桥：打开资源管理器 / 剪贴板（Windows）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8099)
    s.add_argument("--install", action="store_true",
                   help="设为开机自启（HKCU Run，pythonw 无窗口）")
    s.add_argument("--uninstall", action="store_true", help="移除开机自启")
    s.set_defaults(func=cmd_bridge)

    s = sub.add_parser("cinema-import", help="导入电影/剧集（只登记路径，不搬文件、不跑 AI）")
    s.add_argument("paths", nargs="+",
                   help="宿主机路径（MEDIA_HOST_ROOT 之下）或容器内路径（/media/…）")
    s.add_argument("--kind", default="auto", choices=["auto", "movie", "series"],
                   help="auto=靠文件名/季目录识别；movie/series=强制")
    s.add_argument("--no-posters", action="store_true", help="不抽封面帧（更快）")
    s.add_argument("--dry-run", action="store_true", help="只解析文件名，不写库")
    s.add_argument("--progress", type=int, default=50, help="每 N 个文件打印进度（0=不打印）")
    s.set_defaults(func=cmd_cinema_import)

    s = sub.add_parser("cinema-list", help="列出影视库条目")
    s.add_argument("--kind", choices=["movie", "series"], default=None)
    s.set_defaults(func=cmd_cinema_list)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())