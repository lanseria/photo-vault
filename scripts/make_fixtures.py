"""
生成用于冒烟测试的素材（JPEG / HEIC / MP4），覆盖：
  - 带 OffsetTimeOriginal 的照片
  - 只有 GPS、没有 offset 的照片（考验 GPS 反算时区）
  - 完全没有 EXIF 的照片（考验兜底）
  - 完全重复的字节（考验精确去重）
  - 视频（ffprobe + creation_time）

用法（容器内）：
    python /scripts/make_fixtures.py /media
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass


def _gps_ifd(lat: float, lon: float, utc_hms: tuple[int, int, int], day: str) -> dict:
    def dms(v: float) -> tuple:
        v = abs(v)
        d = int(v)
        m = int((v - d) * 60)
        s = round(((v - d) * 60 - m) * 60, 4)
        return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(int(s * 10000), 10000))

    return {
        1: b"N" if lat >= 0 else b"S",
        2: dms(lat),
        3: b"E" if lon >= 0 else b"W",
        4: dms(lon),
        7: (
            IFDRational(utc_hms[0], 1),
            IFDRational(utc_hms[1], 1),
            IFDRational(utc_hms[2], 1),
        ),
        29: day.encode(),
    }


def make_photo(
    path: Path,
    dt_local: str,
    make: str,
    model: str,
    offset: str | None = None,
    gps: tuple[float, float] | None = None,
    gps_utc: tuple[int, int, int] | None = None,
    gps_day: str = "2024:05:01",
    color=(90, 120, 160),
    size=(1600, 1200),
) -> None:
    im = Image.new("RGB", size, color)
    # 加点可见结构，避免全部是纯色块
    for y in range(0, size[1], 40):
        for x in range(0, size[0], 40):
            im.putpixel((x, y), (color[0] ^ 0x30, color[1] ^ 0x20, color[2] ^ 0x10))

    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    exif[0x0112] = 1
    exif[0x0132] = dt_local

    sub: dict = {0x9003: dt_local, 0x829D: IFDRational(178, 100), 0x829A: IFDRational(1, 250),
                 0x920A: IFDRational(240, 10), 0x8827: 320, 0xA434: "Main Camera 24mm"}
    if offset:
        sub[0x9011] = offset
    exif[0x8769] = sub

    if gps:
        utc = gps_utc or (0, 0, 0)
        exif[0x8825] = _gps_ifd(gps[0], gps[1], utc, gps_day)

    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, quality=90, exif=exif)


def make_video(path: Path, creation_time: str, duration: int = 2) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-v", "quiet", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=30:duration={duration}",
        "-metadata", f"creation_time={creation_time}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def main(root: str) -> int:
    base = Path(root) / "fixtures"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)

    # 1) iPhone 风格：带 offset，北京时间下午
    make_photo(base / "iphone_a.jpg", "2024:05:01 14:20:31", "Apple", "iPhone 15 Pro",
               offset="+08:00", gps=(31.2304, 121.4737), gps_utc=(6, 20, 31))
    # 2) 同上，但连拍/相邻时间
    make_photo(base / "iphone_b.jpg", "2024:05:01 14:20:32", "Apple", "iPhone 15 Pro",
               offset="+08:00", gps=(31.2305, 121.4738), gps_utc=(6, 20, 32),
               color=(160, 110, 80))
    # 3) 只有 GPS 没有 offset：本地 12:34:56，GPS 04:34:56 → 应反算出 +480
    make_photo(base / "gps_only.jpg", "2024:06:10 12:34:56", "DJI", "FC3582",
               gps=(43.0618, 141.3545), gps_utc=(4, 34, 56), gps_day="2024:06:10",
               color=(70, 140, 90))
    # 4) 完全没有 EXIF → 兜底时区
    Image.new("RGB", (1200, 900), (200, 190, 150)).save(base / "no_exif.jpg", quality=88)
    # 5) HEIC（iPhone 主力格式）
    if "HEIF" in Image.registered_extensions().values() or True:
        try:
            make_photo(base / "iphone_c.heic", "2024:07:02 09:10:11", "Apple", "iPhone 15 Pro",
                       offset="+08:00", gps=(35.0116, 135.7681), gps_utc=(1, 10, 11),
                       color=(120, 90, 160))
        except Exception as exc:
            print(f"  HEIC 跳过: {exc}", file=sys.stderr)
    # 6) 完全重复的字节
    shutil.copy2(base / "iphone_a.jpg", base / "iphone_a_copy.jpg")
    # 7) 视频（ffmpeg 的 creation_time 必须是 RFC3339，否则 mov 封装器会静默丢弃）
    make_video(base / "clip.mp4", "2024-08-15T18:30:00Z")

    files = sorted(p.name for p in base.iterdir())
    print(f"生成 {len(files)} 个文件：{files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/media"))