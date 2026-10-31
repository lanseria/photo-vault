r"""视频关键帧抽取：均匀候选 → 去黑场/近重复 → 差异最大化挑 N 帧。

给 VLM 打标用的前置步骤。视频的信息量不在帧数上，一段 3 分钟的短视频里
可能只有两三个镜头变化；把 12 个候选帧全塞给 VLM 只是烧时间。所以：

  1. ffprobe 拿时长（调用方有 assets.duration_ms 时优先用库里的）；
  2. 按时间均匀取 K 个候选时间点（跳过开头/结尾的 0.3s 淡入淡出），
     每个点 `ffmpeg -ss t -i src -frames:v 1` 单独取一帧 —— 比整片解码快得多
     （27 分钟的家庭录像整片解码要几分钟，seek 取 12 帧只要两秒）；
  3. 每帧缩成 32×32 灰度指纹（去均值、单位化），余弦距离当"画面差异"；
  4. 丢弃黑场/纯色（方差过低）；
  5. 用最远点采样（farthest-point）从候选中挑出彼此差异最大的 N 帧，
     差异不够（< min_diff）就少给几张 —— 静止镜头重复喂图没有意义。

落盘：derived/keyframes/<sha[:2]>/<sha[2:4]>/<sha>/f00.jpg（长边 768，JPEG q3）
文件名只跟 sha + 序号有关，天然幂等，web 端 `/frames/<sha>/<ord>.jpg` 直出。

    python -m ml.keyframes --video E:\photo\vault\xx\yy.mp4     # 单文件自检
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")

# 给 VLM 看的帧：长边 768。再大只会拖慢 prefill（Qwen3-VL 会把图切 patch 过 ViT），
# 再小 OCR 和远景细节就没了。
FRAME_LONG_EDGE = 768
FRAME_QUALITY = "3"            # ffmpeg -q:v，约等于 JPEG q85
FP_SIZE = 32                   # 指纹边长
MIN_DIFF = 0.10                # 归一化余弦距离阈值：低于此值视为"同一个画面"
BLACK_MEAN = 10.0              # 灰度均值 < 10 且标准差 < 3 → 黑场/纯色
BLACK_STD = 3.0


@dataclass
class Keyframe:
    t_ms: int                  # 在视频里的时间点
    rel_path: str              # 相对 derived_root
    width: int
    height: int
    diff: float                # 与已选帧的最小差异（首帧 0），用来说明"这张代表新画面"

    @property
    def name(self) -> str:
        return Path(self.rel_path).name


def frame_dir(derived_root: Path, sha: str) -> Path:
    return derived_root / "keyframes" / sha[0:2] / sha[2:4] / sha


def frame_rel(sha: str, ord_: int) -> str:
    return f"keyframes/{sha[0:2]}/{sha[2:4]}/{sha}/f{ord_:02d}.jpg"


def probe_duration(path: Path) -> float | None:
    """秒；失败返回 None。调用方一般有 assets.duration_ms，这里只做兜底。"""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _timestamps(duration: float, k: int) -> list[float]:
    """均匀 K 个时间点，跳过首尾 0.3s；帧间隔不小于 0.25s（短视频不必硬塞）。"""
    if duration <= 0:
        return []
    pad = min(0.3, duration * 0.1)
    lo, hi = pad, max(pad, duration - pad)
    span = hi - lo
    if span < 0.3:
        return [(lo + hi) / 2]
    k = max(1, min(k, int(span / 0.25) + 1))
    if k <= 1:
        return [(lo + hi) / 2]
    step = span / (k - 1)
    return [lo + i * step for i in range(k)]


def _extract_one(src: Path, t: float, dst: Path) -> bool:
    scale = (f"scale={FRAME_LONG_EDGE}:{FRAME_LONG_EDGE}"
             f":force_original_aspect_ratio=decrease")
    cmd = [FFMPEG, "-v", "error", "-nostdin", "-y",
           "-ss", f"{t:.3f}", "-i", str(src),
           "-frames:v", "1", "-vf", scale, "-q:v", FRAME_QUALITY, str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return False
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def fingerprint(path: Path) -> np.ndarray | None:
    """32×32 灰度，去均值 + 单位化；返回 None 表示黑场/纯色/读不出。"""
    try:
        with Image.open(path) as im:
            a = np.asarray(im.convert("L").resize((FP_SIZE, FP_SIZE), Image.BILINEAR),
                           dtype=np.float32)
    except Exception:
        return None
    if a.mean() < BLACK_MEAN and a.std() < BLACK_STD:
        return None
    v = a.reshape(-1)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else None


def pick(fps: list[np.ndarray], n: int, min_diff: float = MIN_DIFF) -> list[int]:
    """最远点采样：先取中位帧做种子，之后每次挑"离已选帧最远"的候选。

    fps 里已经去过黑场，索引仍然对应候选时间点。
    """
    if not fps:
        return []
    if len(fps) <= n:
        return list(range(len(fps)))
    mat = np.stack(fps)
    dist = 1.0 - mat @ mat.T
    chosen = [len(fps) // 2]
    while len(chosen) < n:
        d = dist[:, chosen].min(axis=1)
        d[chosen] = -1.0
        i = int(np.argmax(d))
        if d[i] < min_diff:
            break
        chosen.append(i)
    return sorted(chosen)


def extract(sha: str, src: Path, derived_root: Path,
            duration_s: float | None = None,
            n: int = 4, candidates: int = 12) -> list[Keyframe]:
    """抽帧并落盘，返回时间顺序的关键帧列表（可能少于 n，甚至为空）。

    调用方负责决定要不要为此把任务标失败：没有关键帧 ≠ 任务失败，
    可能是纯黑/损坏的视频，记一条空结果就行。
    """
    dur = duration_s if duration_s and duration_s > 0 else probe_duration(src)
    if not dur:
        return []
    k = max(4, min(candidates, int(round(dur * 1.5)) or 4))
    out_dir = frame_dir(derived_root, sha)
    # 重跑时先清掉旧帧，避免 n 从 4 变 3 留下 f03.jpg
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    cands: list[tuple[float, Path, np.ndarray]] = []
    with tempfile.TemporaryDirectory(prefix="kv-") as tmp:
        for i, t in enumerate(_timestamps(dur, k)):
            p = Path(tmp) / f"c{i:02d}.jpg"
            if not _extract_one(src, t, p):
                continue
            fp = fingerprint(p)
            if fp is None:
                continue
            cands.append((t, p, fp))
        if not cands:
            shutil.rmtree(out_dir, ignore_errors=True)
            return []

        idx = pick([c[2] for c in cands], n)
        frames: list[Keyframe] = []
        kept_fps: list[np.ndarray] = []
        for ord_, i in enumerate(idx):
            t, tmp_path, fp = cands[i]
            dst = out_dir / f"f{ord_:02d}.jpg"
            shutil.copyfile(tmp_path, dst)
            with Image.open(dst) as im:
                w, h = im.size
            diff = 0.0 if not kept_fps else float(
                min(1.0 - float(fp @ kf) for kf in kept_fps))
            kept_fps.append(fp)
            frames.append(Keyframe(t_ms=int(t * 1000), rel_path=frame_rel(sha, ord_),
                                   width=w, height=h, diff=diff))
    return frames


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time

    ap = argparse.ArgumentParser(prog="python -m ml.keyframes", description="关键帧抽取自检")
    ap.add_argument("--video", required=True, help="视频文件路径")
    ap.add_argument("-n", type=int, default=4, help="最多保留几帧（默认 4）")
    ap.add_argument("--out", default=None, help="输出目录（默认临时目录）")
    args = ap.parse_args(argv)

    src = Path(args.video)
    if not src.exists():
        print(f"找不到 {src}")
        return 1
    root = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="kf-"))
    t0 = time.time()
    frames = extract("0" * 64, src, root, probe_duration(src), n=args.n)
    dt = time.time() - t0
    print(f"{len(frames)} 帧 / {dt*1000:.0f} ms  → {root}")
    for f in frames:
        p = root / f.rel_path
        print(f"  {f.t_ms/1000:7.2f}s  {f.width}×{f.height}  diff={f.diff:.3f}  "
              f"{p.stat().st_size//1024} KB  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
