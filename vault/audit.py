"""
入库前的只读体检。

2TB 的库不要一上来就搬。先跑一遍 audit，拿到：
  - 到底有多少文件、多少 GB、分布在哪些目录
  - 格式构成（有多少 HEIC / 视频 / RAW / 未知）
  - 时间可信度（EXIF 覆盖、时区来源分布）
  - 重复浪费了多少空间
  - 哪些文件会出问题（0 字节、超长路径、iCloud 占位符、不支持格式）

再决定用 link 还是 copy 模式。
"""
from __future__ import annotations

import json
import os
import random
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import hashutil, meta

YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
ICLOUD_STUB = ".icloud"
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db"}
JUNK_EXT = {".db", ".ini", ".aae", ".thm", ".tmp", ".log", ".lnk", ".url"}


@dataclass
class Rec:
    root: str
    rel: str
    size: int
    ext: str
    mtime: float


@dataclass
class Audit:
    roots: list[str] = field(default_factory=list)
    files: list[Rec] = field(default_factory=list)
    dirs: int = 0
    unreadable: list[str] = field(default_factory=list)
    flags: dict[str, list[str]] = field(default_factory=dict)
    dup_groups: int = 0
    dup_files: int = 0
    dup_bytes: int = 0
    dup_capped: bool = False
    exif_sample: int = 0
    exif_hit: int = 0
    tz_sources: Counter = field(default_factory=Counter)
    devices: Counter = field(default_factory=Counter)
    years_by_source: dict[str, Counter] = field(default_factory=dict)
    unsupported: Counter = field(default_factory=Counter)
    exts: Counter = field(default_factory=Counter)
    years: Counter = field(default_factory=Counter)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def total_files(self) -> int:
        return len(self.files)


def _walk(root: Path, exclude: list[Path]) -> tuple[list[Rec], int, list[str]]:
    recs: list[Rec] = []
    unreadable: list[str] = []
    ndirs = 0
    ex = [str(e.resolve()) for e in exclude]
    root_s = str(root.resolve())

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: unreadable.append(str(e))):
        cur = str(Path(dirpath).resolve()) if dirpath != root_s else root_s
        if any(cur == e or cur.startswith(e + os.sep) for e in ex):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not d.startswith("$")]
        if cur != root_s:
            ndirs += 1
        rel_dir = os.path.relpath(cur, root_s)
        rel_dir = "" if rel_dir == "." else rel_dir.replace("\\", "/") + "/"
        for name in filenames:
            try:
                st = os.stat(os.path.join(cur, name), follow_symlinks=False)
            except OSError:
                continue
            recs.append(
                Rec(
                    root=root_s,
                    rel=rel_dir + name,
                    size=st.st_size,
                    ext=os.path.splitext(name)[1].lower(),
                    mtime=st.st_mtime,
                )
            )
    return recs, ndirs, unreadable


def _guess_year(rel: str, mtime: float) -> tuple[int, str]:
    for part in reversed(Path(rel).parts):
        m = YEAR_RE.search(part)
        if m:
            return int(m.group(1)), "path"
    try:
        return datetime.fromtimestamp(mtime).year, "mtime"
    except (OSError, ValueError, OverflowError):
        return 0, "unknown"


def _hash_one(p: Path) -> tuple[str, int]:
    try:
        return hashutil.sha256_file(p)
    except OSError:
        return "", 0


def audit(
    roots: list[Path],
    vault_root: Path,
    exclude: list[Path],
    max_hash: int = 50_000,
    sample_exif: int = 500,
    workers: int = 8,
    seed: int = 0,
) -> Audit:
    a = Audit(roots=[str(r) for r in roots])
    ex = exclude + [vault_root]

    for root in roots:
        recs, ndirs, bad = _walk(root, ex)
        a.files.extend(recs)
        a.dirs += ndirs
        a.unreadable.extend(bad)

    exts = Counter(f.ext or "(无扩展名)" for f in a.files)

    # ---- 重复检测：先按 size 分组，只对同 size 的哈希
    by_size: dict[int, list[Rec]] = defaultdict(list)
    for f in a.files:
        if f.size > 1024:              # 小于 1KB 的多半是占位/图标，噪音大
            by_size[f.size].append(f)

    candidates = [(sz, g) for sz, g in by_size.items() if len(g) > 1]
    candidates.sort(key=lambda x: -x[0] * len(x[1]))

    to_hash: list[tuple[int, Rec]] = []
    for sz, g in candidates:
        if len(to_hash) + len(g) > max_hash:
            a.dup_capped = True
            break
        for f in g:
            to_hash.append((sz, f))

    hashes: dict[tuple[int, str], list[Rec]] = defaultdict(list)
    if to_hash:
        def work(item: tuple[int, Rec]):
            sz, rec = item
            h, _ = _hash_one(Path(rec.root) / rec.rel)
            return sz, h, rec

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for sz, h, rec in pool.map(work, to_hash):
                if h:
                    hashes[(sz, h)].append(rec)

    for (sz, h), g in hashes.items():
        if len(g) > 1:
            a.dup_groups += 1
            a.dup_files += len(g) - 1
            a.dup_bytes += sz * (len(g) - 1)

    # ---- 风险标记
    def flag(name: str, pred) -> None:
        hits = [f.rel for f in a.files if pred(f)]
        if hits:
            a.flags[name] = hits[:200]

    flag("零字节", lambda f: f.size == 0)
    flag("超过 4GB", lambda f: f.size >= 4 * 1024**3)
    flag("路径超过 240 字符（Windows 可能打不开）", lambda f: len(f.rel) > 240)
    flag("iCloud 占位符（未下载到本地）", lambda f: f.ext == ICLOUD_STUB or f.rel.endswith(ICLOUD_STUB))
    flag("系统垃圾文件", lambda f: Path(f.rel).name.lower() in JUNK_NAMES or f.ext in JUNK_EXT)
    flag("未知格式（不会入库）", lambda f: not meta.is_media(Path(f.rel)))

    for f in a.files:
        if not meta.is_media(Path(f.rel)):
            a.unsupported[f.ext or "(无扩展名)"] += 1

    # ---- 时间分布（路径里的年份优先，其次 mtime）
    src_years: dict[str, Counter] = {"path": Counter(), "mtime": Counter()}
    for f in a.files:
        if not meta.is_media(Path(f.rel)):
            continue
        y, src = _guess_year(f.rel, f.mtime)
        if 1950 <= y <= 2050:
            a.years[y] += 1
            src_years.setdefault(src, Counter())[y] += 1
    a.years_by_source = dict(src_years)

    # ---- EXIF 抽样
    imgs = [f for f in a.files if meta.is_media(Path(f.rel))]
    if sample_exif and imgs:
        rnd = random.Random(seed)
        sample = rnd.sample(imgs, min(sample_exif, len(imgs)))
        for f in sample:
            a.exif_sample += 1
            try:
                p = meta.probe(Path(f.root) / f.rel)
            except Exception:
                continue
            _, _, tzsrc, _ = meta.resolve_time(p, 480)
            a.tz_sources[tzsrc] += 1
            if p.taken_local is not None or p.taken_utc is not None:
                a.exif_hit += 1
            if p.model:
                a.devices[p.model] += 1

    a.exts = exts
    return a


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def probe_link(root: Path, vault_root: Path) -> tuple[bool | None, str]:
    """
    真刀真枪试一次硬链接，比比较 st_dev 可靠（容器里 bind mount 的 st_dev 不可信）。
    返回 (能否链接, 说明)。None 表示无法判定。
    """
    import errno

    try:
        vault_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"vault 不可写：{exc}"

    target: Path | None = None
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if meta.is_media(p):
                target = p
                break
        if target:
            break
    if target is None:
        return None, "源目录里没有可用文件做探针"

    probe = vault_root / ".hardlink_probe"
    try:
        os.link(target, probe)
        probe.unlink(missing_ok=True)
        return True, "可硬链接（素材与 vault 同卷，零额外空间）"
    except OSError as exc:
        code = exc.errno
        if code == errno.EXDEV:
            return False, (
                "跨卷（EXDEV）。若宿主机上素材与 vault 确实在同一块盘，"
                "多半是 Docker 分别挂载导致 —— 改成只挂一个父目录即可解决"
            )
        if code == errno.EROFS:
            return None, "源目录是只读挂载，改成可写才能用 link 模式"
        if code in (errno.EPERM, errno.EACCES, errno.EOPNOTSUPP):
            return False, (
                f"该文件系统不支持硬链接（errno {code}: {exc.strerror}）。"
                "Windows 下请把素材和 vault 放在同一个挂载点内（单挂载），"
                "或改用 copy 模式"
            )
        return False, f"硬链接失败（errno {code}: {exc.strerror}）"


def render_markdown(a: Audit, link_possible: bool) -> str:
    exts = a.exts
    years = a.years
    total = a.total_files or 1
    out: list[str] = []

    out.append("# 素材库体检报告\n")
    out.append(f"扫描路径：{'、'.join(f'`{r}`' for r in a.roots)}\n")
    out.append(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}\n")

    out.append("\n## 总览\n")
    out.append("| 指标 | 数值 |\n|---|---|")
    out.append(f"| 文件数 | {a.total_files:,} |")
    out.append(f"| 总大小 | {_fmt_bytes(a.total_bytes)} |")
    out.append(f"| 目录数 | {a.dirs:,} |")
    out.append(f"| 无法读取的目录 | {len(a.unreadable)} |")
    out.append(f"| 重复浪费 | {_fmt_bytes(a.dup_bytes)}（{a.dup_files:,} 个多余副本 / {a.dup_groups:,} 组）|")
    if a.dup_capped:
        out.append("| ⚠️ | 重复检测已截断（候选过多），实际浪费可能更多 |")

    out.append("\n## 格式分布\n")
    out.append("| 扩展名 | 数量 | 占比 | 大小 |\n|---|---:|---:|---:|")
    size_by_ext: Counter = Counter()
    for f in a.files:
        size_by_ext[f.ext or "(无扩展名)"] += f.size
    for ext, cnt in exts.most_common(20):
        out.append(f"| `{ext}` | {cnt:,} | {cnt/total*100:.1f}% | {_fmt_bytes(size_by_ext[ext])} |")

    out.append("\n## 时间分布（按路径年份优先，其次文件时间）\n")
    out.append("| 年份 | 数量 |\n|---|---:|")
    for y in sorted(years, reverse=True)[:20]:
        out.append(f"| {y} | {years[y]:,} |")
    if years:
        out.append(f"\n> 跨度 {min(years)} – {max(years)}")

    out.append("\n## 元数据可信度（随机抽样 {0} 个）\n".format(a.exif_sample))
    if a.exif_sample:
        out.append("| 时区来源 | 数量 | 占比 |\n|---|---:|---:|")
        label = {
            "exif": "EXIF 自带偏移（最可靠）",
            "gps": "GPS 反算（可靠）",
            "utc": "仅 UTC 瞬时",
            "default": "回落默认时区（可能偏差）",
            "mtime": "文件时间兜底（不可信）",
            "unknown": "完全无时间信息",
        }
        for k, v in a.tz_sources.most_common():
            out.append(f"| {label.get(k, k)} | {v} | {v/a.exif_sample*100:.0f}% |")
        out.append(f"\n> EXIF 中有拍摄时间的比例：{a.exif_hit/a.exif_sample*100:.0f}%")
        if a.devices:
            out.append("\n机型 Top 10：\n")
            for m, c in a.devices.most_common(10):
                out.append(f"- {m}：{c}")

    if a.unsupported:
        out.append("\n## 不会入库的格式\n")
        for ext, c in a.unsupported.most_common(12):
            out.append(f"- `{ext}`：{c:,}")

    if a.flags:
        out.append("\n## 需要留意的文件\n")
        for name, hits in a.flags.items():
            out.append(f"- **{name}**：{len(hits)} 个")
            for h in hits[:3]:
                out.append(f"  - `{h}`")

    out.append("\n## 建议\n")
    if link_possible:
        out.append(
            "- 素材与 vault **在同一磁盘卷** → 可以用 `--mode link`：硬链接进库，"
            "**不额外占空间**，耗时约等于读一遍（只算哈希）。"
        )
    else:
        out.append(
            "- 素材与 vault 不在同一卷 → 只能用 `--mode copy`，"
            f"需要额外约 **{_fmt_bytes(a.total_bytes)}** 空间（重复的 {_fmt_bytes(a.dup_bytes)} 会被自动去掉）。"
        )
    if a.dup_bytes > 0:
        out.append(f"- 去重后预计净占用：**{_fmt_bytes(a.total_bytes - a.dup_bytes)}**")
    if a.flags.get("iCloud 占位符（未下载到本地）"):
        out.append("- ⚠️ 存在 iCloud 占位符，需先在 Finder/资源管理器里「下载原件」，否则入库的是空壳。")
    if a.flags.get("路径超过 240 字符（Windows 可能打不开）"):
        out.append("- ⚠️ 有超长路径，Windows 上可能读取失败；建议先开启长路径支持。")
    out.append(
        "- 执行顺序：先 `--dry-run` 跑一遍（只扫描不落盘），确认数字对得上，再去掉 `--dry-run` 正式入库。"
    )
    return "\n".join(out)


def render_json(a: Audit) -> dict:
    exts = a.exts
    years = a.years
    return {
        "roots": a.roots,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "files": a.total_files,
            "bytes": a.total_bytes,
            "dirs": a.dirs,
            "unreadable": len(a.unreadable),
        },
        "by_ext": dict(exts.most_common()),
        "by_year": {str(k): v for k, v in sorted(years.items())},
        "duplicates": {
            "groups": a.dup_groups,
            "redundant_files": a.dup_files,
            "wasted_bytes": a.dup_bytes,
            "capped": a.dup_capped,
        },
        "exif_sample": {
            "sampled": a.exif_sample,
            "with_capture_time": a.exif_hit,
            "tz_source": dict(a.tz_sources),
            "devices": dict(a.devices.most_common(30)),
        },
        "unsupported": dict(a.unsupported.most_common()),
        "flags": {k: len(v) for k, v in a.flags.items()},
    }