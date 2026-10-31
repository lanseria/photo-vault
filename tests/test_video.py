"""
视频打标链路的回归测试：不连数据库、不启模型，只锁纯逻辑。

    uv run --group ml python tests/test_video.py

覆盖：
  · 关键帧落盘路径与时间点均匀性（含短视频退化）
  · 最远点采样：挑差异最大的帧；全一样时只给一张
  · 黑场/纯色帧被丢弃
  · 标签别名收敛 + 词表外标签丢弃
  · 模型输出解析：JSON / 代码块围栏 / 思考块 / 坏 JSON 兜底
  · 视频级聚合：票数门槛、兜底前二、互斥词（室内/室外）、蕴含词（屏幕录制→屏幕）
  · 真跑一遍 ffmpeg 抽帧（没装 ffmpeg 就跳过）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml import keyframes                                            # noqa: E402
from ml.vlm import (FrameResult, aggregate, normalize_labels,       # noqa: E402
                    parse_result, strip_think)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(f"{name}  {detail}")
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------- 路径 / 时间点
print("\n[关键帧路径与时间点]")
sha = "ab" * 32
check("相对路径带两级分桶", keyframes.frame_rel(sha, 2)
      == f"keyframes/ab/ab/{sha}/f02.jpg", keyframes.frame_rel(sha, 2))
check("序号补零两位", keyframes.frame_rel(sha, 10).endswith("f10.jpg"))

ts = keyframes._timestamps(10.0, 5)
check("均匀 5 个时间点", len(ts) == 5 and ts == sorted(ts), str(ts))
check("跳过首尾 0.3s", abs(ts[0] - 0.3) < 1e-6 and abs(ts[-1] - 9.7) < 1e-6, str(ts))
check("相邻间隔相等", all(abs((ts[i + 1] - ts[i]) - 2.35) < 1e-6 for i in range(4)))
single = keyframes._timestamps(0.2, 12)
check("极短视频只取中间一帧", len(single) == 1, str(single))
check("0 时长不抽帧", keyframes._timestamps(0, 12) == [])


# ---------------------------------------------------------------- 最远点采样
print("\n[最远点采样]")
rng = np.random.default_rng(0)
vecs = [v / np.linalg.norm(v) for v in rng.normal(size=(8, 64))]
picked = keyframes.pick(vecs, 4)
check("挑够 4 张", len(picked) == 4, str(picked))
check("索引升序且不重复", picked == sorted(set(picked)), str(picked))
# 两两之间都得拉开距离（否则最远点采样没意义）
mind = min(1.0 - float(vecs[i] @ vecs[j]) for i in picked for j in picked if i < j)
check("选中的帧彼此差异 >= 阈值", mind >= keyframes.MIN_DIFF - 1e-9, f"{mind:.3f}")

same = [vecs[0]] * 6
check("全一样时只留一张", len(keyframes.pick(same, 4)) == 1, str(keyframes.pick(same, 4)))
check("候选不够就全要", keyframes.pick(vecs[:2], 4) == [0, 1])


# ---------------------------------------------------------------- 黑场检测
print("\n[黑场/纯色帧]")
with tempfile.TemporaryDirectory() as td:
    black = Path(td) / "black.jpg"
    Image.new("RGB", (200, 120), (0, 0, 0)).save(black)
    check("纯黑帧被丢弃", keyframes.fingerprint(black) is None)

    solid = Path(td) / "solid.jpg"
    Image.new("RGB", (200, 120), (180, 40, 40)).save(solid)
    check("纯色帧被丢弃", keyframes.fingerprint(solid) is None)

    grad = Path(td) / "grad.jpg"
    arr = np.tile(np.linspace(0, 255, 200, dtype=np.uint8), (120, 1))
    Image.fromarray(arr, "L").convert("RGB").save(grad)
    fp = keyframes.fingerprint(grad)
    check("有内容的帧返回指纹", fp is not None)
    check("指纹是单位向量", fp is not None and abs(float(np.linalg.norm(fp)) - 1) < 1e-5)


# ---------------------------------------------------------------- 标签收敛
print("\n[标签归一]")
check("别名映射 + 去重 + 词表外丢弃",
      normalize_labels(["人物", "汽车", "汽车", "挖掘机", "#室外"])
      == ["有人", "汽车", "室外"],
      str(normalize_labels(["人物", "汽车", "汽车", "挖掘机", "#室外"])))
check("空值安全", normalize_labels([None, "", "  "]) == [])


# ---------------------------------------------------------------- 输出解析
print("\n[模型输出解析]")
ok_json = '{"caption": "海边散步", "labels": ["海边", "散步"], "ocr": "海滩入口"}'
r = parse_result(ok_json)
check("标准 JSON", r.caption == "海边散步" and r.labels == ["海边", "散步"]
      and r.ocr == "海滩入口", f"{r}")

fenced = '```json\n{"caption": "室内", "labels": ["室内"]}\n```'
r = parse_result(fenced)
check("代码块围栏", r.caption == "室内" and r.labels == ["室内"], f"{r}")

think = (' thinking先看看图。<｜end▁of▁thinking｜>'
         '{"caption": "孩子在草地跑", "labels": ["孩子", "草地", "跑步"]}')
r = parse_result(think)
check("思考块被剥掉", r.caption == "孩子在草地跑" and r.labels == ["孩子", "草地", "跑步"],
      f"{r}")

r = parse_result("画面里有狗和草地，在公园")
check("坏 JSON 也不抛，文字兜底成描述", r.caption.startswith("画面里有狗") and "狗" in r.labels,
      f"{r}")
check("坏 JSON 的标签仍受词表约束", normalize_labels(r.labels) == r.labels, str(r.labels))
check("strip_think 保留后半段", strip_think("前面" + " A " + "后面").strip() == "前面 A 后面")


# ---------------------------------------------------------------- 视频级聚合
print("\n[视频级聚合]")
frames = [
    FrameResult(["有人", "草地", "跑步"], "孩子在草地跑", ""),
    FrameResult(["有人", "草地"], "草地上的孩子", ""),
    FrameResult(["有人", "汽车"], "汽车开过", "限速 40"),
    FrameResult(["有人"], "孩子在跑", "限速40; 公园东门"),
]
labels, caption, ocr = aggregate(frames)
cnt = {d["name"]: d["frames"] for d in labels}
check("≥2 票的留下", set(cnt) == {"有人", "草地"}, str(cnt))
check("票数正确", cnt.get("有人") == 4 and cnt.get("草地") == 2, str(cnt))
check("按票数降序", labels[0]["name"] == "有人", str(labels))
check("视频描述取标签最多的一帧", caption == "孩子在草地跑", caption)
check("OCR 去重合并", ocr == "限速 40; 公园东门", ocr)

odd = [FrameResult(["树"], "树", ""), FrameResult(["花"], "花", ""),
       FrameResult(["水"], "水", ""), FrameResult(["雪地"], "雪", "")]
labels, caption, _ = aggregate(odd)
check("没人过门槛时取前二（并列按出现顺序）",
      [d["name"] for d in labels] == ["树", "花"], str(labels))
check("前二仍带票数", labels[0]["frames"] == 1, str(labels))

excl = [FrameResult(["室内", "室外"], "", ""), FrameResult(["室内", "室外"], "", ""),
        FrameResult(["室外"], "", ""), FrameResult(["室外"], "", "")]
labels, _, _ = aggregate(excl)
check("互斥词只留票高的", [d["name"] for d in labels] == ["室外"], str(labels))
tie = [FrameResult(["室内"], "", ""), FrameResult(["室外"], "", "")]
labels, _, _ = aggregate(tie)
check("互斥词平票都不留", labels == [], str(labels))

impl = [FrameResult(["屏幕", "屏幕录制"], "", ""), FrameResult(["屏幕", "屏幕录制"], "", "")]
labels, _, _ = aggregate(impl)
check("有「屏幕录制」就不再留「屏幕」", [d["name"] for d in labels] == ["屏幕录制"], str(labels))

car = [FrameResult(["车内", "室内", "汽车"], "", ""), FrameResult(["车内", "室内"], "", ""),
       FrameResult(["车内", "汽车"], "", "")]
labels, _, _ = aggregate(car)
check("车内视角不再算「室内」", [d["name"] for d in labels] == ["车内", "汽车"], str(labels))

check("空帧列表不炸", aggregate([]) == ([], "", ""))


# ---------------------------------------------------------------- 真抽一次帧
print("\n[ffmpeg 抽帧]")
if not shutil.which("ffmpeg"):
    print("  skip 没装 ffmpeg")
else:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "clip.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=6",
            "-pix_fmt", "yuv420p", str(src)], check=True, timeout=60)
        root = Path(td) / "derived"
        got = keyframes.extract("cd" * 32, src, root, 6.0, n=3)
        check("抽出 3 帧", len(got) == 3, str([g.t_ms for g in got]))
        check("按时间升序", [g.t_ms for g in got] == sorted(g.t_ms for g in got))
        check("时间点落在视频内", all(0 < g.t_ms < 6000 for g in got))
        check("文件都写出来了", all((root / g.rel_path).exists() for g in got))
        check("首帧 diff 为 0", got[0].diff == 0.0, str(got[0].diff))
        check("尺寸是 768 这一档", max(got[0].width, got[0].height) <= 768)
        again = keyframes.extract("cd" * 32, src, root, 6.0, n=3)
        check("重跑幂等（同名覆盖、数量一致）", len(again) == 3
              and [g.rel_path for g in again] == [g.rel_path for g in got])


print()
if failures:
    print(f"{len(failures)} 项失败：")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("全部通过")
