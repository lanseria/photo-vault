from __future__ import annotations

import json
import shutil
import subprocess
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ExifTags, ImageOps

try:  # HEIC/HEIF 支持（iPhone 主力格式）
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

EXIF_IFD = 0x8769
GPS_IFD = 0x8825

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
    ".tif", ".tiff", ".bmp", ".gif", ".avif",
}
RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef", ".srw"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".insv", ".360", ".lrv", ".3gp"}
SKIP_EXT = {".xmp", ".aae", ".thm", ".lrv.tmp", ".db", ".ini", ".txt", ".json", ".xml"}


@dataclass
class Probe:
    kind: str = "other"
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    make: str | None = None
    model: str | None = None
    lens: str | None = None
    taken_local: datetime | None = None          # 无时区墙上时间
    taken_utc: datetime | None = None            # 直接已知的 UTC 瞬时（视频 creation_time …Z）
    tz_offset_min: int | None = None             # 由 EXIF/GPS 直接给出
    tz_source: str | None = None
    gps_utc: datetime | None = None              # GPS 给出的 UTC 时间
    gps_lat: float | None = None
    gps_lon: float | None = None
    iso: int | None = None
    aperture: float | None = None
    shutter: float | None = None
    focal_mm: float | None = None
    orientation: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- helpers

def _num(v: Any) -> float | None:
    """IFDRational / Fraction / tuple / int → float"""
    if v is None:
        return None
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if v[1] else None
        return float(v)
    except Exception:
        return None


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    s = str(v).strip().strip("\x00")
    return s or None


def _dms_to_deg(dms: Any, ref: Any) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except Exception:
        return None
    deg = d + m / 60.0 + s / 3600.0
    r = (_text(ref) or "").upper()
    if r in ("S", "W"):
        deg = -deg
    return deg


def _parse_exif_dt(s: str) -> datetime | None:
    s = s.strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_offset(s: str) -> int | None:
    """'+08:00' → 480"""
    s = s.strip()
    if not s:
        return None
    sign = -1 if s[0] == "-" else 1
    body = s[1:] if s[0] in "+-" else s
    parts = body.replace(":", "").ljust(4, "0")
    try:
        return sign * (int(parts[0:2]) * 60 + int(parts[2:4]))
    except ValueError:
        return None


# ---------------------------------------------------------------- image

def probe_image(path: Path) -> Probe:
    p = Probe(kind="photo", mime=mimetypes.guess_type(path.name)[0])
    try:
        with Image.open(path) as im:
            p.width, p.height = im.size
            exif = im.getexif()
    except Exception as exc:
        p.kind = "raw" if path.suffix.lower() in RAW_EXT else "other"
        p.raw["error"] = f"{type(exc).__name__}: {exc}"
        return p

    try:
        sub = exif.get_ifd(EXIF_IFD) or {}
    except Exception:
        sub = {}
    try:
        gps = exif.get_ifd(GPS_IFD) or {}
    except Exception:
        gps = {}

    p.make = _text(exif.get(0x010F))
    p.model = _text(exif.get(0x0110))
    p.orientation = exif.get(0x0112)
    p.lens = _text(sub.get(0xA434))

    dt_raw = _text(sub.get(0x9003)) or _text(sub.get(0x9004)) or _text(exif.get(0x0132))
    if dt_raw:
        p.taken_local = _parse_exif_dt(dt_raw)

    off_raw = _text(sub.get(0x9011)) or _text(sub.get(0x9010)) or _text(sub.get(0x9012))
    if off_raw:
        p.tz_offset_min = _parse_offset(off_raw)
        if p.tz_offset_min is not None:
            p.tz_source = "exif"

    iso = sub.get(0x8827)
    p.iso = int(iso) if isinstance(iso, (int, float)) else None
    p.aperture = _num(sub.get(0x829D))
    p.shutter = _num(sub.get(0x829A))
    p.focal_mm = _num(sub.get(0x920A))

    if gps:
        lat = _dms_to_deg(gps.get(2), gps.get(1))
        lon = _dms_to_deg(gps.get(4), gps.get(3))
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            p.gps_lat, p.gps_lon = lat, lon

        d = _text(gps.get(29))          # "YYYY:MM:DD"
        t = gps.get(7)                  # (h, m, s)
        if d and t:
            try:
                hh, mm, ss = (float(x) for x in t)
                day = datetime.strptime(d, "%Y:%m:%d")
                p.gps_utc = day.replace(
                    hour=int(hh), minute=int(mm), second=int(ss),
                    tzinfo=timezone.utc,
                )
            except Exception:
                pass

    p.raw = {"make": p.make, "model": p.model, "lens": p.lens, "dt": dt_raw}
    return p


# ---------------------------------------------------------------- video

def _ffprobe(path: Path) -> dict[str, Any] | None:
    if not shutil.which("ffprobe"):
        return None
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout or b"{}")
    except Exception:
        return None


def _parse_iso_dt(s: str) -> tuple[datetime | None, int | None]:
    """返回 (本地墙钟, 偏移分钟)。支持 '...+0800' 与 '...Z'。"""
    s = s.strip()
    if not s:
        return None, None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).replace(tzinfo=None), 0
        # Python 3.11+ 能吃 "+0800"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            off = int(dt.utcoffset().total_seconds() // 60)
            return dt.replace(tzinfo=None), off
        return dt, None
    except Exception:
        return None, None


def probe_video(path: Path) -> Probe:
    p = Probe(kind="video", mime=mimetypes.guess_type(path.name)[0])
    info = _ffprobe(path)
    if not info:
        p.raw["error"] = "ffprobe unavailable or failed"
        return p

    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v:
        p.width = v.get("width")
        p.height = v.get("height")
        rot = (v.get("tags") or {}).get("rotate")
        if rot in ("90", "270") and p.width and p.height:
            p.width, p.height = p.height, p.width

    fmt = info.get("format") or {}
    dur = fmt.get("duration") or (v or {}).get("duration")
    if dur:
        try:
            p.duration_ms = int(float(dur) * 1000)
        except Exception:
            pass

    tags: dict[str, str] = {}
    for t in [fmt.get("tags") or {}] + [(s.get("tags") or {}) for s in streams]:
        for k, val in t.items():
            tags.setdefault(str(k), str(val))

    # iPhone 视频：带时区的本地时间，最可靠
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        raw = tags.get(key)
        if not raw:
            continue
        local, off = _parse_iso_dt(raw)
        if local is None:
            continue
        if off is not None and not raw.rstrip().endswith("Z"):
            p.taken_local = local
            p.tz_offset_min = off
            p.tz_source = "exif"
        else:
            # 只有 UTC 瞬时（…Z），本地墙钟时间未知，留给 resolve_time 用默认时区还原
            p.taken_utc = local.replace(tzinfo=timezone.utc)
        break

    p.make = _text(tags.get("make"))
    p.model = _text(tags.get("model")) or _text(tags.get("com.apple.quicktime.model"))

    loc = tags.get("location") or tags.get("com.apple.quicktime.location.ISO6709")
    if loc:
        # 形如 +43.0618+141.3545+030.000/
        import re

        m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", loc.replace("/", ""))
        if m:
            p.gps_lat, p.gps_lon = float(m.group(1)), float(m.group(2))

    p.raw = {"format": fmt.get("format_name"), "codec": (v or {}).get("codec_name")}
    return p


# ---------------------------------------------------------------- dispatch

def probe(path: Path) -> Probe:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return probe_video(path)
    if ext in IMAGE_EXT or ext in RAW_EXT:
        return probe_image(path)
    return Probe(kind="other", raw={"note": "unsupported extension"})


def is_media(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in IMAGE_EXT or ext in RAW_EXT or ext in VIDEO_EXT


# ---------------------------------------------------------------- 时区判定

def resolve_time(
    p: Probe, default_offset_min: int
) -> tuple[datetime | None, int, str, datetime | None]:
    """
    返回 (本地墙钟, 偏移分钟, 来源, 已知的 UTC 瞬时或 None)。
    优先级：已知 UTC 瞬时 > EXIF OffsetTime* > GPS 反算 > 默认时区 > 文件 mtime。
    """
    if p.taken_utc is not None:
        off = p.tz_offset_min if p.tz_offset_min is not None else default_offset_min
        local = (p.taken_utc + timedelta(minutes=off)).replace(tzinfo=None)
        return local, off, "utc", p.taken_utc

    if p.taken_local is None:
        return None, default_offset_min, "unknown", None

    if p.tz_offset_min is not None:
        return p.taken_local, p.tz_offset_min, p.tz_source or "exif", None

    if p.gps_utc is not None:
        delta = p.taken_local - p.gps_utc.replace(tzinfo=None)
        minutes = round(delta.total_seconds() / 60 / 15) * 15
        if -720 <= minutes <= 840:
            return p.taken_local, minutes, "gps", None

    return p.taken_local, default_offset_min, "default", None


def to_utc(local: datetime | None, offset_min: int) -> datetime | None:
    if local is None:
        return None
    return (local - timedelta(minutes=offset_min)).replace(tzinfo=timezone.utc)


def make_thumb(src: Path, dst: Path, size: int, quality: int) -> bool:
    try:
        from PIL import Image as _Image

        with _Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((size, size), _Image.Resampling.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, "WEBP", quality=quality, method=4)
        return True
    except Exception:
        return False