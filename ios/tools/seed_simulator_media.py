#!/usr/bin/env python3
"""生成 iostest-* 小素材并播种进 iOS 模拟器相册（PhotoKit），可选核验导入结果。

为什么需要它
------------
iOS App 的「相册同步」只能从 PhotoKit 读素材。模拟器出厂只有 IMG_0001..0007 这几张
示例照片，没法覆盖 JPEG / HEIC / PNG / MOV / Live Photo 各种组合，也没法重复验证
「跳过服务器已有」这类逻辑。这个脚本把素材造出来、用 `xcrun simctl addmedia` 灌进
相册，然后**独立**核对 PhotoKit 里到底进了什么（读 Photos.sqlite 的一份只读副本），
免得后面拿 App 的显示结果既当输入又当证据。

安全
----
- 所有文件名都带 `iostest-` 前缀（服务器上只允许写/删带这个前缀的资产）。
- 只读模拟器的 Photos.sqlite（复制到临时目录再打开，绝不写原库）。
- 不碰 App 源码、不碰服务器。

用法
----
    # 造素材 + 导入 + 核验（默认）
    python3 ios/tools/seed_simulator_media.py

    # 只造素材，不导入（CI/离线检查）
    python3 ios/tools/seed_simulator_media.py --no-addmedia

    # 只核验当前相册里有哪些 iostest-* 资产
    python3 ios/tools/seed_simulator_media.py --verify-only

产物
----
    ios/qa/seed/           素材本体
    ios/qa/seed/manifest.json   sha256 / 尺寸 / 字节数，供 curl 契约测试复用
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo 根
DEFAULT_OUT = ROOT / "ios" / "qa" / "seed"
DEFAULT_UDID = "E96F2777-7A6B-4018-B7E5-BD3937CD17A8"
DEVICE_DATA = Path.home() / "Library/Developer/CoreSimulator/Devices"

PREFIX = "iostest-"


# --------------------------------------------------------------------------- #
# 素材生成
# --------------------------------------------------------------------------- #
def _gradient(w: int, h: int, hue: int):
    """生成有渐变的彩色图（纯色缩略图看不出解码问题，渐变+色块更容易发现黑图）。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (
                (hue + x * 255 // max(w, 1)) % 256,
                (hue * 2 + y * 255 // max(h, 1)) % 256,
                (hue * 3 + (x + y) * 255 // max(w + h, 1)) % 256,
            )
    d = ImageDraw.Draw(im)
    d.rectangle([w // 8, h // 8, w // 2, h // 2], fill=(255, 255, 255))
    d.ellipse([w // 2, h // 2, w - w // 8, h - h // 8], fill=(20, 20, 20))
    d.text((w // 6, h // 3), f"iostest {w}x{h}", fill=(0, 0, 0))
    return im


def _camera_exif(model: str = "iPhone 17 Pro"):
    """给 JPEG 塞一点相机 EXIF，顺便验证服务端的设备识别链路。"""
    from PIL import Image

    exif = Image.Exif()
    exif[271] = "Apple"          # Make
    exif[272] = model            # Model
    exif[305] = "PhotoVaultQA"   # Software
    exif[36867] = "2026:11:01 12:00:00"  # DateTimeOriginal
    exif[306] = "2026:11:01 12:00:00"
    return exif


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_assets(outdir: Path, live_mode: str = "crafted", verbose: bool = True) -> list[dict]:
    """生成全部素材，返回清单（不含 addmedia）。"""
    from PIL import Image

    outdir.mkdir(parents=True, exist_ok=True)
    made: list[dict] = []

    def emit(path: Path, kind: str, note: str = "") -> None:
        rec = {
            "file": path.name,
            "path": str(path),
            "kind": kind,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "note": note,
        }
        made.append(rec)
        if verbose:
            print(f"  生成 {rec['file']:<22} {rec['bytes']:>8} B  {rec['sha256'][:12]}  {note}")

    print("== 生成素材 ==")
    # 3 张不同尺寸的 JPEG
    for name, (w, h), hue in [
        ("iostest-a.jpg", (1024, 768), 30),
        ("iostest-b.jpg", (800, 600), 110),
        ("iostest-c.jpg", (640, 480), 210),
    ]:
        p = outdir / name
        _gradient(w, h, hue).save(p, "JPEG", quality=82, exif=_camera_exif())
        emit(p, "image/jpeg", f"{w}x{h} EXIF Make=Apple Model=iPhone 17 Pro")

    # 1 张 PNG（大尺寸、少颜色，压缩后很小）
    p = outdir / "iostest-e.png"
    _gradient(512, 512, 160).save(p, "PNG", optimize=True)
    emit(p, "image/png", "512x512 PNG（EXIF 无机型 → 服务端应识别为未识别设备）")

    # 1 张 HEIC（sips 从 JPEG 转，iOS 原生格式）
    heic = outdir / "iostest-d.heic"
    r = _run(["sips", "-s", "format", "heic", str(outdir / "iostest-c.jpg"), "--out", str(heic)])
    if heic.exists() and heic.stat().st_size > 0:
        emit(heic, "image/heic", "由 iostest-c.jpg 经 sips 转出")
    else:
        print(f"  !! HEIC 生成失败（sips）：{r.stderr.strip()[:200]}", file=sys.stderr)

    # 1 个 2 秒小 MOV（H.264 + 一条静音音轨，更像真实视频）
    mov = outdir / "iostest-v.mov"
    r = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-loop", "1", "-i", str(outdir / "iostest-a.jpg"),
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-t", "2", "-vf", "scale=320:-2", "-r", "24",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(mov)],
    )
    if mov.exists() and mov.stat().st_size > 0:
        emit(mov, "video/quicktime", "2s 320x240 H.264+AAC")
    else:
        print(f"  !! MOV 生成失败（ffmpeg）：{r.stderr.strip()[:300]}", file=sys.stderr)

    # Live Photo 配对：同一 stem 的静帧 + 动态
    make_live_pair(outdir, made, mode=live_mode, verbose=verbose)
    return made


def build_live_photo_tool(verbose: bool = True) -> Path:
    """编译 ios/qa/tools/make_live_photo.swift → ios/qa/.build/make_live_photo。

    实测结论（见 ios/qa/REPORT.md）：模拟器里想要一张**真** Live Photo，光把同名
    JPG+MOV 一起 addmedia 是不够的，必须
      1) 静帧写 MakerApple "17" = content id（ImageIO 的 kCGImagePropertyMakerAppleDictionary）；
      2) 动态写 QuickTime `com.apple.quicktime.content.identifier` = 同一个 id；
      3) 动态里还要有一条 timed metadata `com.apple.quicktime.still-image-time`。
    第 3 条 ffmpeg 写不了（它不是文件级 tag，而是 metadata 轨），所以用 AVFoundation 生成。
    """
    src = ROOT / "ios/qa/tools/make_live_photo.swift"
    out = ROOT / "ios/qa/.build/make_live_photo"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"  编译 Live Photo 生成器 → {out}")
    r = _run(["swiftc", "-O", "-o", str(out), str(src)])
    if not out.exists():
        raise SystemExit(f"swiftc 编译失败：\n{r.stderr[-1500:]}")
    return out


def make_live_pair(outdir: Path, made: list[dict], mode: str = "crafted", verbose: bool = True) -> None:
    """造一对 Live Photo。

    mode:
      crafted — 真 Live Photo：iostest-livep.HEIC + iostest-livep.MOV，两边带同一个
                content identifier，动态还有 still-image-time metadata 轨。
                addmedia 后 PhotoKit 会合成**一条** kind=live 的资产（带 pairedVideo 资源）。
      naive   — 只保证 stem 相同（iostest-live.JPG + iostest-live.MOV），验证"仅仅同名
                并不能配对"这个假设；实测会变成两张独立资产。
    """
    from PIL import Image

    content_id = str(uuid.uuid4()).upper()
    if mode == "crafted":
        stem = "iostest-livep"
        tool = build_live_photo_tool(verbose=verbose)
        src_still = outdir / ".livep-src.jpg"
        src_motion = outdir / "iostest-v.mov"
        if not src_motion.exists():
            print("  !! 缺 iostest-v.mov，跳过 Live Photo", file=sys.stderr)
            return
        _gradient(960, 720, 75).save(src_still, "JPEG", quality=85, exif=_camera_exif())
        r = _run([str(tool), str(src_still), str(src_motion), str(outdir), stem, content_id])
        src_still.unlink(missing_ok=True)
        if r.returncode != 0:
            print(f"  !! Live Photo 生成失败：{r.stderr.strip()[:400]}", file=sys.stderr)
            return
        parts = [("photo", outdir / f"{stem}.HEIC", "image/heic", "真 Live Photo 静帧（MakerApple 17）"),
                 ("motion", outdir / f"{stem}.MOV", "video/quicktime",
                  "真 Live Photo 动态（content-id + still-image-time 轨）")]
    else:
        stem = "iostest-live"
        still = outdir / f"{stem}.JPG"
        motion = outdir / f"{stem}.MOV"
        _gradient(960, 720, 75).save(still, "JPEG", quality=85, exif=_camera_exif())
        r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                  "-loop", "1", "-i", str(still), "-t", "2",
                  "-vf", "scale=320:-2", "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-movflags", "use_metadata_tags",
                  "-metadata", f"com.apple.quicktime.content.identifier={content_id}",
                  str(motion)])
        if r.returncode != 0:
            print(f"  !! Live 动态生成失败：{r.stderr.strip()[:300]}", file=sys.stderr)
            return
        parts = [("photo", still, "image/jpeg", "同名静帧（仅 stem 相同）"),
                 ("motion", motion, "video/quicktime", "同名动态（仅 stem 相同）")]

    for role, path, kind, note in parts:
        if not path.exists() or path.stat().st_size == 0:
            print(f"  !! 缺 {path.name}", file=sys.stderr)
            continue
        made.append({
            "file": path.name, "path": str(path), "kind": kind,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "note": note, "live_pair": stem, "live_role": role, "content_id": content_id,
        })
        if verbose:
            print(f"  生成 {path.name:<22} {path.stat().st_size:>8} B  {note}")


def _write_jpeg_xmp(src: Path, dst: Path, content_id: str) -> None:
    """在 JPEG 里插入一段 XMP APP1（apple:ContentIdentifier = content-id）。

    实测（2026-11-01 / iOS 26.2 模拟器）：**这条路不成立** —— 只在 JPEG 里写 XMP
    content identifier、视频侧只写 mdta 文件级 tag，Photos 仍把两张当成独立资产导入
    （见 REPORT.md 的 Live Photo 实验记录）。真 Live Photo 需要 MakerApple "17" +
    视频里 still-image-time 的 timed metadata 轨，见 make_live_photo.swift。
    这个函数留着是因为它把"只写 XMP 不行"这个结论固定在了代码里。
    """
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="PhotoVaultQA">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:apple="http://ns.apple.com/ns/1.0/"'
        f' apple:ContentIdentifier="{content_id}"/>'
        "</rdf:RDF></x:xmpmeta>"
    ).encode("utf-8")
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    data = src.read_bytes()
    assert data[:2] == b"\xff\xd8", "不是 JPEG"
    # XMP 放在 SOI 之后、其它段之前（APP0/JFIF 后面更保险，但没有 JFIF 也能读）
    insert_at = 2
    if data[2:4] == b"\xff\xe0":  # APP0/JFIF 存在则跳过它
        seg_len = int.from_bytes(data[4:6], "big")
        insert_at = 4 + seg_len
    dst.write_bytes(data[:insert_at] + seg + data[insert_at:])


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #
def addmedia(udid: str, files: list[Path], verbose: bool = True) -> bool:
    print(f"== simctl addmedia（{len(files)} 个文件）==")
    cmd = ["xcrun", "simctl", "addmedia", udid] + [str(f) for f in files]
    r = _run(cmd)
    if verbose:
        print(f"  $ {' '.join(cmd)}")
        if r.stdout.strip():
            print("  " + r.stdout.strip().replace("\n", "\n  "))
        if r.stderr.strip():
            print("  " + r.stderr.strip().replace("\n", "\n  "))
    ok = r.returncode == 0
    print(f"  → {'OK' if ok else f'失败 rc={r.returncode}'}")
    return ok


# --------------------------------------------------------------------------- #
# 核验（只读 Photos.sqlite 副本）
# --------------------------------------------------------------------------- #
def open_photos_db(udid: str):
    """把 Photos.sqlite 连 WAL/SHM 一起复制到临时目录后只读打开。"""
    import sqlite3

    src = DEVICE_DATA / udid / "data/Media/PhotoData"
    if not (src / "Photos.sqlite").exists():
        raise SystemExit(f"找不到模拟器 PhotoData：{src}")
    tmp = Path(tempfile.mkdtemp(prefix="qaphotos-"))
    for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
        if (src / name).exists():
            shutil.copy2(src / name, tmp / name)
    con = sqlite3.connect(f"file:{tmp/'Photos.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con, tmp


def verify(udid: str, prefix: str = PREFIX) -> list[dict]:
    """列出相册里所有原始文件名以 prefix 开头的资产，并判定 kind / live photo 配对。

    坑：`simctl addmedia` 会把照片改名成 IMG_000N.*，ZFILENAME 是**展示名**；真正的
    原始文件名（PHAssetResource.originalFilename）在 ZADDITIONALASSETATTRIBUTES.
    ZORIGINALFILENAME。只按 ZFILENAME 找 iostest-* 会一条都找不到。
    """
    con, tmp = open_photos_db(udid)
    try:
        rows = con.execute(
            """
            SELECT a.Z_PK, a.ZFILENAME, d.ZORIGINALFILENAME, a.ZKIND, a.ZKINDSUBTYPE,
                   a.ZPLAYBACKSTYLE, a.ZPLAYBACKVARIATION, a.ZWIDTH, a.ZHEIGHT, a.ZUUID,
                   a.ZMEDIAGROUPUUID, a.ZUNIFORMTYPEIDENTIFIER, a.ZVISIBILITYSTATE,
                   a.ZTRASHEDSTATE
            FROM ZASSET a
            LEFT JOIN ZADDITIONALASSETATTRIBUTES d ON d.ZASSET = a.Z_PK
            WHERE a.ZFILENAME LIKE ? OR d.ZORIGINALFILENAME LIKE ?
            ORDER BY a.Z_PK
            """,
            (prefix.replace("_", "\\_") + "%", prefix.replace("_", "\\_") + "%"),
        ).fetchall()
        # 每个资产挂了哪些资源。ZINTERNALRESOURCE.ZRESOURCETYPE 实测映射：
        #   0 = 静帧 / 视频的封面帧   1 = 视频本体   3 = pairedVideo（Live Photo 动态）
        res: dict[int, list[str]] = {}
        for r in con.execute(
            "SELECT ZASSET, ZRESOURCETYPE, ZDATALENGTH, ZCODECFOURCHARCODENAME FROM ZINTERNALRESOURCE"
        ):
            res.setdefault(r["ZASSET"], []).append(
                f"{RESOURCE_TYPE_NAMES.get(r['ZRESOURCETYPE'], r['ZRESOURCETYPE'])}"
                f"({r['ZDATALENGTH']}B{',' + r['ZCODECFOURCHARCODENAME'] if r['ZCODECFOURCHARCODENAME'] else ''})"
            )
        out = []
        for r in rows:
            d = dict(r)
            d["resources"] = res.get(d["Z_PK"], [])
            d["has_paired_video"] = any(x.startswith("pairedVideo") for x in d["resources"])
            d["live_photo"] = is_live_photo(d)
            out.append(d)
        return out
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


# Photos.sqlite 内部资源类型 → 人话（PHAssetResourceType 的枚举值不一样，别混用）
RESOURCE_TYPE_NAMES = {0: "photo", 1: "video", 2: "audio", 3: "pairedVideo"}


def is_live_photo(row: dict) -> bool:
    """Photos.sqlite 里判定 Live Photo 的几种信号（任一命中即认为是）。"""
    if row.get("ZPLAYBACKSTYLE") == 3:
        return True
    if (row.get("ZKINDSUBTYPE") or 0) & 2:
        return True
    u = (row.get("ZUNIFORMTYPEIDENTIFIER") or "").lower()
    return "live" in u


def summarize(rows: list[dict]) -> dict:
    """把核验结果折成结论：进了几张、kind 分布、Live Photo 有没有配对成功。"""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        g = r.get("ZMEDIAGROUPUUID") or f"solo-{r['Z_PK']}"
        groups.setdefault(g, []).append(r)
    paired = []
    for g, members in groups.items():
        stills = [m for m in members if (m["ZKIND"] or 0) == 0]
        videos = [m for m in members if (m["ZKIND"] or 0) == 1]
        if stills and videos:
            paired.append({
                "group": g,
                "stills": [s["ZFILENAME"] for s in stills],
                "videos": [v["ZFILENAME"] for v in videos],
                "still_is_live": any(s["live_photo"] for s in stills),
            })
    return {
        "count": len(rows),
        "kinds": {
            str(k): sum(1 for r in rows if (r["ZKIND"] or 0) == k) for k in (0, 1)
        },
        "live_photos": [r["ZORIGINALFILENAME"] or r["ZFILENAME"] for r in rows if r["live_photo"]],
        "live_paired": [r["ZORIGINALFILENAME"] or r["ZFILENAME"] for r in rows if r.get("has_paired_video")],
        "paired_groups": paired,
        "filenames": [r["ZORIGINALFILENAME"] or r["ZFILENAME"] for r in rows],
    }


def print_verify(rows: list[dict]) -> None:
    print("== 相册核验（Photos.sqlite 只读副本）==")
    if not rows:
        print("  相册里没有 iostest-* 资产")
        return
    for r in rows:
        flags = []
        if r["live_photo"]:
            flags.append("LIVE")
        if r.get("has_paired_video"):
            flags.append("pairedVideo")
        if (r["ZKIND"] or 0) == 1:
            flags.append("VIDEO")
        if r["ZTRASHEDSTATE"]:
            flags.append("TRASHED")
        print(
            f"  Z_PK={r['Z_PK']:<4} {(r['ZORIGINALFILENAME'] or '?'):<22} "
            f"展示名={r['ZFILENAME']:<16} "
            f"kind={r['ZKIND']} subtype={r['ZKINDSUBTYPE']} "
            f"playback={r['ZPLAYBACKSTYLE']}/{r['ZPLAYBACKVARIATION']} "
            f"{r['ZWIDTH']}x{r['ZHEIGHT']} group={str(r['ZMEDIAGROUPUUID'])[:8]} "
            f"{' '.join(flags)}"
        )
        if r["resources"]:
            print(f"        resources: {', '.join(r['resources'])}")
    s = summarize(rows)
    print(f"  → 共 {s['count']} 条；照片 {s['kinds'].get('0', 0)}，视频 {s['kinds'].get('1', 0)}；"
          f"Live Photo {len(s['live_photos'])} 条")
    if s["live_photos"]:
        print(f"  → Live Photo 资产：{s['live_photos']}（带 pairedVideo 资源：{s['live_paired']}）")
    # 配对成功时动态会被吸收成 pairedVideo，不会单独出现在 media group 里；
    # 只有"配对失败"才会看到 静帧+动态 两张独立资产（它们也没有共同 group）。
    if not s["live_photos"]:
        print("  → 没有合成出 Live Photo：静帧和动态很可能是两张独立资产")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--udid", default=os.environ.get("QA_SIM_UDID", DEFAULT_UDID))
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--live-mode", choices=["naive", "crafted", "skip"], default="crafted")
    ap.add_argument("--no-addmedia", action="store_true", help="只造素材不导入")
    ap.add_argument("--verify-only", action="store_true", help="只核验相册")
    ap.add_argument("--manifest-only", action="store_true",
                    help="不重新生成素材，只按 outdir 里现有的 iostest-* 文件刷新 manifest.json")
    ap.add_argument("--json-out", default=None, help="把核验结论写到 JSON")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    if args.manifest_only:
        # 目的：种子文件是之前某次生成的、相册里也是那批；重跑生成会换内容（Live Photo
        # 的 content id 每次不同），导致 manifest 的 sha 和相册里实际文件对不上。
        # 这个模式只做「按现有文件重新登记」。
        files = sorted(p for p in outdir.glob("iostest-*") if p.is_file() and not p.name.endswith(".json"))
        assets = []
        for p in files:
            role = None
            stem = p.stem
            if stem.lower().startswith("iostest-livep"):
                role = "photo" if p.suffix.lower() == ".heic" else "motion"
                stem = "iostest-livep"
            elif stem.lower().startswith("iostest-live"):
                role = "photo" if p.suffix.lower() in (".jpg", ".jpeg") else "motion"
                stem = "iostest-live"
            assets.append({
                "file": p.name, "path": str(p),
                "kind": {"jpg": "image/jpeg", "jpeg": "image/jpeg", "heic": "image/heic",
                         "png": "image/png", "mov": "video/quicktime"}.get(p.suffix.lstrip(".").lower(), "?"),
                "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "live_pair": stem if role else None,
                "live_role": role,
            })
        manifest = {"udid": args.udid, "prefix": PREFIX,
                    "generated_at": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                                                   capture_output=True, text=True).stdout.strip(),
                    "assets": assets, "note": "由 --manifest-only 按现有文件重新登记"}
        (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"已刷新 manifest（{len(assets)} 个文件）→ {outdir/'manifest.json'}")
        for a in assets:
            print(f"  {a['file']:<22} {a['bytes']:>8} B  {a['sha256'][:12]}  {a['live_role'] or ''}")
        return 0
    if args.verify_only:
        rows = verify(args.udid)
        print_verify(rows)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0

    made = make_assets(outdir, live_mode=args.live_mode)

    manifest = {
        "udid": args.udid,
        "prefix": PREFIX,
        "generated_at": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                                       capture_output=True, text=True).stdout.strip(),
        "assets": made,
        "live_pair": {
            "stem": "iostest-live",
            "still": "iostest-live.JPG",
            "motion": "iostest-live.MOV",
            "mode": args.live_mode,
            "content_id": next((m.get("content_id") for m in made if m.get("live_role") == "photo"), None),
        },
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"  manifest → {outdir/'manifest.json'}")

    ok = True
    if not args.no_addmedia:
        # 幂等：相册里已经有的原始文件名不再导入一遍（addmedia 不去重，会多出一份）
        existing = {r["ZORIGINALFILENAME"] for r in verify(args.udid) if r["ZORIGINALFILENAME"]}
        todo = [Path(m["path"]) for m in made if m["file"] not in existing]
        skipped = [m["file"] for m in made if m["file"] in existing]
        if skipped:
            print(f"  已在相册里，跳过 {len(skipped)} 个：{', '.join(skipped)}")
        if todo:
            ok = addmedia(args.udid, todo)
            import time
            time.sleep(2.0)
        else:
            print("  相册里已经全都有了，不需要 addmedia")
        rows = verify(args.udid)
        print_verify(rows)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str))

    total = sum(m["bytes"] for m in made)
    print(f"== 完成：{len(made)} 个文件，合计 {total} B；addmedia {'OK' if ok else '失败'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
