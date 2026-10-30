"""
时区判定的回归测试。无外部依赖，直接跑：

    docker compose run --rm app python tests/test_time.py

这些用例锁死的是本项目最容易出错、也最影响"整理"体验的一段逻辑：
  - 有 OffsetTimeOriginal → 直接用
  - 只有 GPS → 反算并吸附到 15 分钟
  - 只有 UTC 瞬时 → 用默认时区还原本地墙钟，但瞬时不变
  - 什么都没有 → mtime 兜底（在 ingest 里）
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.meta import Probe, resolve_time, to_utc  # noqa: E402

DEFAULT = 480  # Asia/Shanghai
failures: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        failures.append(f"{name}\n    got  = {got!r}\n    want = {want!r}")
    else:
        print(f"  ok  {name}")


# 1) EXIF 直接给了偏移，最高优先级
p = Probe(
    taken_local=datetime(2024, 5, 1, 14, 20, 31),
    tz_offset_min=480,
    tz_source="exif",
    gps_utc=datetime(2024, 5, 1, 6, 20, 31, tzinfo=timezone.utc),
)
local, off, src, utc = resolve_time(p, DEFAULT)
check("exif offset", (local, off, src), (datetime(2024, 5, 1, 14, 20, 31), 480, "exif"))
check("exif offset → utc", to_utc(local, off), datetime(2024, 5, 1, 6, 20, 31, tzinfo=timezone.utc))

# 2) 没有 offset，但 GPS 的 UTC 与本地墙钟差 8 小时 → 应反算出 480
p = Probe(
    taken_local=datetime(2024, 6, 10, 12, 34, 56),
    gps_utc=datetime(2024, 6, 10, 4, 34, 56, tzinfo=timezone.utc),
)
local, off, src, utc = resolve_time(p, DEFAULT)
check("gps derived offset", (off, src), (480, "gps"))
check("gps derived → utc", to_utc(local, off), datetime(2024, 6, 10, 4, 34, 56, tzinfo=timezone.utc))

# 2b) GPS 差 5.5 小时（印度 +330）仍应吸附正确
p = Probe(
    taken_local=datetime(2024, 3, 1, 18, 0, 0),
    gps_utc=datetime(2024, 3, 1, 12, 30, 0, tzinfo=timezone.utc),
)
_, off, src, _ = resolve_time(p, DEFAULT)
check("half-hour tz (+330)", (off, src), (330, "gps"))

# 2c) GPS 明显异常（差 20 小时）→ 不采信，回落默认
p = Probe(
    taken_local=datetime(2024, 3, 1, 18, 0, 0),
    gps_utc=datetime(2024, 2, 29, 22, 0, 0, tzinfo=timezone.utc),
)
_, off, src, _ = resolve_time(p, DEFAULT)
check("absurd gps offset rejected", (off, src), (DEFAULT, "default"))

# 3) 只有 UTC 瞬时（视频 creation_time …Z）：本地墙钟按默认时区还原，瞬时不变
p = Probe(taken_utc=datetime(2024, 8, 15, 18, 30, 0, tzinfo=timezone.utc))
local, off, src, utc = resolve_time(p, DEFAULT)
check("utc only → local", local, datetime(2024, 8, 16, 2, 30, 0))
check("utc only → source", src, "utc")
check("utc only → instant preserved", utc, datetime(2024, 8, 15, 18, 30, 0, tzinfo=timezone.utc))

# 4) 什么都没有
p = Probe()
local, off, src, utc = resolve_time(p, DEFAULT)
check("no time info", (local, src, utc), (None, "unknown", None))

print()
if failures:
    print(f"FAILED {len(failures)} case(s):")
    for f in failures:
        print("  ✗ " + f)
    raise SystemExit(1)
print("all time tests passed")