"""
网页导入（拖拽上传）的边界回归测试。无外部依赖，直接跑：

    python tests/test_upload.py

锁死三件最容易悄悄坏掉的事：
  1. 上传文件名只取 basename —— 不能把 `..\\..\\x.jpg` 写成库里的相对路径；
  2. 前端过滤用的 UPLOAD_EXT 必须覆盖后端 meta 认可的全部图片/视频格式，
     否则用户拖进来的 .mts 会被浏览器静默丢掉（后端根本没机会拒绝）；
  3. taken 拍摄时间参数（iOS 同步 / 网页上传共用）能解析成 UTC 瞬时，
     解析不了要安静地返回 None，走原有的"文件内嵌时间 → mtime"探测链。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from vault import meta  # noqa: E402
from vault.web import _parse_taken, _upload_clean_name  # noqa: E402

# 前端已经抽成 Vite 工程，UPLOAD_EXT 白名单在 files.ts 里（不再有内嵌 PAGE HTML）
FRONTEND_FILES_TS = (Path(__file__).resolve().parent.parent
                     / "frontend" / "src" / "utils" / "files.ts")

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'ok ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def expect_400(raw: str) -> None:
    try:
        _upload_clean_name(raw)
    except HTTPException as exc:
        check(f"拒绝 {raw!r}", exc.status_code == 400)
    else:
        check(f"拒绝 {raw!r}", False)


print("[文件名]")
check("Windows 路径只留文件名",
      _upload_clean_name(r"C:\fakepath\IMG_0001.HEIC") == "IMG_0001.HEIC")
check("POSIX 路径只留文件名",
      _upload_clean_name("/tmp/a/../b/照片 01.jpg") == "照片 01.jpg")
check("路径穿越被吃掉",
      _upload_clean_name("../../etc/passwd.jpg") == "passwd.jpg")
check("去掉 NUL", _upload_clean_name("a\x00b.jpg") == "ab.jpg")
expect_400("")
expect_400("   ")
expect_400("..")
expect_400(".hidden.jpg")
expect_400("a/")

long_name = "x" * 260 + ".jpg"
got = _upload_clean_name(long_name)
check("超长文件名截断但保留扩展名", len(got) <= 200 and got.endswith(".jpg"))

print("\n[前端扩展名白名单]")
page = FRONTEND_FILES_TS.read_text(encoding="utf-8")
m = re.search(r"UPLOAD_EXT\s*=\s*/(.+?)/i", page, re.S)
check("files.ts 里有 UPLOAD_EXT", m is not None)
if m:
    rx = re.compile(m.group(1), re.I)
    for ext in sorted(meta.IMAGE_EXT | meta.RAW_EXT | meta.VIDEO_EXT):
        check(f"前端接受 {ext}", bool(rx.search("x" + ext)))
    for bad in (".txt", ".xmp", ".aae", ".ts", ".json"):
        check(f"前端忽略 {bad}", not rx.search("x" + bad))

print("\n[后端类型判定]")
check(".HEIC 是媒体", meta.is_media(Path("a.HEIC")))
check(".mts 是媒体", meta.is_media(Path("a.mts")))
check(".ts 不是媒体（TypeScript 源码）", not meta.is_media(Path("a.ts")))
check(".xmp 不是媒体", not meta.is_media(Path("a.xmp")))

print("\n[拍摄时间 taken 参数]")
expected_instant = datetime(2024, 9, 1, 1, 30, 0, tzinfo=timezone.utc)
check("ISO8601 UTC（iOS）",
      _parse_taken("2024-09-01T01:30:00Z") == expected_instant)
check("ISO8601 带偏移",
      _parse_taken("2024-09-01T09:30:00+08:00") == expected_instant)
check("毫秒时间戳（网页 File.lastModified）",
      _parse_taken("1725154200000") == expected_instant)
check("多余精度截到毫秒",
      _parse_taken("1725154200123456") ==
      datetime(2024, 9, 1, 1, 30, 0, 123000, tzinfo=timezone.utc))
check("naive ISO 当 UTC",
      _parse_taken("2024-09-01T01:30:00") == expected_instant)
check("空串 → None", _parse_taken("") is None)
check("乱码 → None（不当错误，走原探测逻辑）", _parse_taken("abc") is None)
check("负数时间戳不崩溃", _parse_taken("-123") is None)

if failures:
    print(f"\n{len(failures)} 个失败：")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("\n全部通过")
