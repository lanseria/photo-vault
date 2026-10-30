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
            mode=args.mode,
        )

    dur = time.time() - started
    print("")
    print("─" * 56)
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
        exclude=[cfg.derived_root, cfg.landing_root],
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

    s = sub.add_parser("serve", help="启动 Web 界面")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())