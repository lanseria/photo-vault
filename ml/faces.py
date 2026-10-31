"""人脸引擎：InsightFace 的 SCRFD 检测 + ArcFace 识别，纯 ONNX Runtime（CUDA）。

模型（buffalo_l 包里的两个文件）：

    det_10g.onnx      SCRFD 10G 检测器，640×640 输入，输出 5 关键点
    w600k_r50.onnx    ArcFace R50 识别器，112×112 输入，输出 512 维向量

预/后处理与官方 insightface 2.1 逐行对齐（数值验证见 README「人脸识别」）：
    检测：RGB，mean=127.5 std=128，letterbox 到 640×640，stride 8/16/32 × 2 anchors，NMS 0.4
    对齐：5 点 → Umeyama 相似变换 → 112×112（arcface_dst 模板）
    识别：RGB，(x-127.5)/127.5，输出 L2 归一化后存库（余弦相似度 = 点积）

为什么不用 `pip install insightface`：它硬依赖 CPU 版 onnxruntime，和 onnxruntime-gpu
同装时两个发行版写同一个 `onnxruntime/` 目录，版本与 DLL 随机对不上；而本项目只需要
两张 ONNX 图，直接 session.run 少一层依赖，也跟 RAM++ 打标保持同一条技术栈。

GPU：onnxruntime-gpu 的可用窗口很窄（1070 Ti = Pascal，SM 6.1 实测）——
1.24.4 以下没有 INT8 图要的 ConvInteger（RAM++ 会因为共用同一个 EP 一起坏），
1.27 以上官方转 CUDA 13、Pascal 不再支持，CUDA EP 会静默退回 CPU。
所以锁在 >=1.24.4,<1.27。也**不要**开 TensorRT EP（TRT 10 要求 SM 7.5+）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

try:                                    # HEIC/HEIF 原件解码（与 web.py / model.py 同款）
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

_REPO = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.environ.get("VAULT_MODELS_DIR", str(_REPO / "models")))
FACE_MODEL_DIRS = (
    MODELS_DIR / "insightface" / "buffalo_l",
    Path.home() / ".insightface" / "models" / "buffalo_l",
)

DET_FILE = "det_10g.onnx"
REC_FILE = "w600k_r50.onnx"
FACE_MODEL_ID = "buffalo_l:det_10g+w600k_r50"

MODEL_ZIP_URL = ("https://github.com/deepinsight/insightface/releases/download/"
                 "v0.7/buffalo_l.zip")
MODEL_ZIP_SHA256 = "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"

# det_10g 的 ONNX 输出是「按 640×640 特化」的静态形状（12800/3200/800 个 anchor），
# 输入虽然是动态维度，喂别的尺寸输出就对不上——所以 640 是唯一可用值。
DET_SIZE = 640
_STRIDES = (8, 16, 32)
_NUM_ANCHORS = 2
_DET_MEAN = 127.5
_DET_STD = 128.0
_NMS_THRESH = 0.4
_DET_THRESH = 0.5

DEFAULT_PROVIDERS = ("CUDAExecutionProvider", "CPUExecutionProvider")

# ArcFace 112×112 的 5 点模板（对齐目标）
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
ARCFACE_SIZE = 112


@dataclass
class DetectedFace:
    """一张图里检测到的一张脸。"""

    bbox: tuple[float, float, float, float]      # 原图像素坐标（左上、右下）
    det_score: float
    kps: np.ndarray                              # (5, 2) 关键点，原图坐标
    embedding: np.ndarray                        # (512,) L2 归一化
    quality: float                               # 人脸面积 / 图像面积
    crop: np.ndarray = field(repr=False)         # 112×112 对齐后 RGB 裁剪


def find_model_dir() -> Path:
    for candidate in FACE_MODEL_DIRS:
        if (candidate / DET_FILE).exists() and (candidate / REC_FILE).exists():
            return candidate
    raise FileNotFoundError(
        "找不到 InsightFace 模型，找过:\n"
        + "\n".join(f"  {c}" for c in FACE_MODEL_DIRS)
        + "\n  一条命令搞定：uv run --group ml python -m ml.faces --download"
    )


def load_rgb(path: str | Path) -> np.ndarray:
    """读成 RGB uint8 数组，并应用 EXIF 旋转（否则竖拍照片里的脸是躺着的）。"""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return np.asarray(im.convert("RGB"))


# ---------------------------------------------------------------- 检测器（SCRFD）

def _distance2bbox(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """anchor 中心 + 四边距离 → 框。与官方实现一致。"""
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """anchor 中心 + 10 个偏移 → 5 个关键点。"""
    preds = []
    for i in range(0, distance.shape[1], 2):
        preds.append(points[:, i % 2] + distance[:, i])
        preds.append(points[:, i % 2 + 1] + distance[:, i + 1])
    return np.stack(preds, axis=-1)


def nms(dets: np.ndarray, thresh: float = _NMS_THRESH) -> list[int]:
    """单类 NMS。dets: (N, 5) = x1,y1,x2,y2,score。"""
    x1, y1, x2, y2, scores = (
        dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4])
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = np.argsort(-scores, kind="stable")
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep


# ---------------------------------------------------------------- 对齐（Umeyama）

def estimate_norm(landmark: np.ndarray) -> np.ndarray:
    """5 关键点 → 112×112 的仿射矩阵（等价 skimage SimilarityTransform.estimate）。

    官方实现用 skimage，这里按 _umeyama 的原始算法手写，避免为一个 4 行公式
    把 scikit-image 拖进依赖。
    """
    src = np.asarray(landmark, dtype=np.float64)
    dst = ARCFACE_DST.astype(np.float64)
    num, dim = src.shape
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_demean = src - src_mean
    dst_demean = dst - dst_mean

    A = dst_demean.T @ src_demean / num
    d = np.ones(dim, dtype=np.float64)
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1

    U, S, Vt = np.linalg.svd(A)
    rank = np.linalg.matrix_rank(A)
    T = np.eye(dim + 1, dtype=np.float64)
    if rank == 0:
        return np.full((3, 3), np.nan)
    if rank == dim - 1:
        if np.linalg.det(U) * np.linalg.det(Vt) > 0:
            T[:dim, :dim] = U @ Vt
        else:
            saved = d[dim - 1]
            d[dim - 1] = -1
            T[:dim, :dim] = U @ np.diag(d) @ Vt
            d[dim - 1] = saved
    else:
        T[:dim, :dim] = U @ np.diag(d) @ Vt

    scale = (S @ d) / src_demean.var(axis=0).sum()
    T[:dim, dim] = dst_mean - scale * (T[:dim, :dim] @ src_mean)
    T[:dim, :dim] *= scale
    return T[:2, :]


def norm_crop(img: np.ndarray, landmark: np.ndarray,
              image_size: int = ARCFACE_SIZE) -> np.ndarray:
    """按 5 点做相似变换，裁出标准的 112×112 人脸（RGB uint8）。"""
    import cv2

    M = estimate_norm(landmark)
    return cv2.warpAffine(img, M, (image_size, image_size),
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


# ---------------------------------------------------------------- 引擎

class FaceEngine:
    """检测 + 识别一站式。会话在构造时建立，之后 analyze() 可反复调用。"""

    def __init__(self, model_dir: str | Path | None = None,
                 providers: list[str] | None = None,
                 det_thresh: float = _DET_THRESH,
                 nms_thresh: float = _NMS_THRESH,
                 embed_batch: int = 16):
        import onnxruntime as ort

        self.model_dir = Path(model_dir) if model_dir else find_model_dir()
        self.det_thresh = det_thresh
        self.nms_thresh = nms_thresh
        self.embed_batch = max(1, embed_batch)
        self.det_size = (DET_SIZE, DET_SIZE)

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3
        providers = providers or _providers()

        det_path = self.model_dir / DET_FILE
        rec_path = self.model_dir / REC_FILE
        self.det_sess = ort.InferenceSession(
            str(det_path), sess_options=opts, providers=providers)
        self.rec_sess = ort.InferenceSession(
            str(rec_path), sess_options=opts, providers=providers)

        det_in = self.det_sess.get_inputs()[0]
        self.det_input = det_in.name
        det_outs = self.det_sess.get_outputs()
        if len(det_outs) != 9:
            raise RuntimeError(
                f"det_10g 应有 9 个输出（3 stride × score/bbox/kps），实际 {len(det_outs)}")
        # 输出是 640×640 特化的静态形状，换成别的检测模型这里就会对不上，先炸在明处
        for idx, stride in enumerate(_STRIDES):
            want = (DET_SIZE // stride) ** 2 * _NUM_ANCHORS
            got = det_outs[idx].shape[0]
            if isinstance(got, int) and got != want:
                raise RuntimeError(
                    f"检测模型输出 {det_outs[idx].name} 有 {got} 个 anchor，"
                    f"按 {DET_SIZE}×{DET_SIZE} + stride {stride} 应为 {want}；"
                    "这个 ONNX 不是 det_10g@640")
        # 输出顺序：score(8,16,32) → bbox(8,16,32) → kps(8,16,32)，与官方 SCRFD 约定一致
        self.det_outputs = [o.name for o in det_outs]

        rec_in = self.rec_sess.get_inputs()[0]
        self.rec_input = rec_in.name
        self.rec_outputs = [o.name for o in self.rec_sess.get_outputs()]

        self._centers: dict[tuple[int, int, int], np.ndarray] = {}

    # -- 状态 ---------------------------------------------------------

    @property
    def providers(self) -> list[str]:
        return list(self.det_sess.get_providers())

    @property
    def on_gpu(self) -> bool:
        return "CUDAExecutionProvider" in self.providers

    def describe(self) -> str:
        return f"{FACE_MODEL_ID} det={DET_SIZE} providers={self.providers}"

    # -- 检测 ---------------------------------------------------------

    def _anchor_centers(self, stride: int, height: int, width: int) -> np.ndarray:
        key = (stride, height, width)
        centers = self._centers.get(key)
        if centers is None:
            ay, ax = np.mgrid[:height, :width]
            centers = np.stack((ax, ay), axis=-1).astype(np.float32)
            centers = (centers * stride).reshape(-1, 2)
            centers = np.stack([centers] * _NUM_ANCHORS, axis=1).reshape(-1, 2)
            if len(self._centers) < 32:
                self._centers[key] = centers
        return centers

    def _detect_blob(self, img: np.ndarray) -> np.ndarray:
        """letterbox 到 640×640 → (1,3,640,640) float32，RGB，mean/std 归一化。"""
        import cv2

        det_w, det_h = self.det_size
        height, width = img.shape[:2]
        im_ratio = height / width
        model_ratio = det_h / det_w
        if im_ratio > model_ratio:
            new_h = det_h
            new_w = max(1, int(new_h / im_ratio))
        else:
            new_w = det_w
            new_h = max(1, int(new_w * im_ratio))
        det_scale = new_h / height

        canvas = np.zeros((det_h, det_w, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = cv2.resize(img, (new_w, new_h))
        blob = (canvas.astype(np.float32) - _DET_MEAN) / _DET_STD
        blob = blob.transpose(2, 0, 1)[None]
        return np.ascontiguousarray(blob, dtype=np.float32), det_scale

    def detect(self, img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """返回 (dets[N,5], kpss[N,5,2])，坐标已还原到原图。"""
        blob, det_scale = self._detect_blob(img)
        outs = [np.asarray(o) for o in
                self.det_sess.run(self.det_outputs, {self.det_input: blob})]
        # 图里 batch 维被烘焙掉时输出是 (N, C)，保守起见统一摊平成 (N, C)
        outs = [o.reshape(-1, o.shape[-1]) for o in outs]

        dets_list, kpss_list = [], []
        for idx, stride in enumerate(_STRIDES):
            scores = outs[idx].reshape(-1)
            bbox_preds = outs[idx + 3] * stride
            kps_preds = outs[idx + 6] * stride
            pos = np.where(scores >= self.det_thresh)[0]
            if pos.size == 0:
                continue
            height = self.det_size[1] // stride
            width = self.det_size[0] // stride
            centers = self._anchor_centers(stride, height, width)
            bboxes = _distance2bbox(centers[pos], bbox_preds[pos])
            kpss = _distance2kps(centers[pos], kps_preds[pos]).reshape(-1, 5, 2)
            dets = np.hstack((bboxes, scores[pos][:, None])).astype(np.float32)
            dets_list.append(dets)
            kpss_list.append(kpss)

        if not dets_list:
            return (np.empty((0, 5), np.float32), np.empty((0, 5, 2), np.float32))

        dets = np.vstack(dets_list)
        dets[:, :4] /= det_scale
        kpss = np.vstack(kpss_list) / det_scale

        order = np.argsort(-dets[:, 4], kind="stable")
        dets, kpss = dets[order], kpss[order]
        keep = nms(dets, self.nms_thresh)
        return dets[keep], kpss[keep]

    # -- 识别 ---------------------------------------------------------

    def _embed(self, crops: list[np.ndarray]) -> np.ndarray:
        emb = np.empty((len(crops), 512), dtype=np.float32)
        for start in range(0, len(crops), self.embed_batch):
            chunk = crops[start:start + self.embed_batch]
            blob = np.empty((len(chunk), 3, ARCFACE_SIZE, ARCFACE_SIZE),
                            dtype=np.float32)
            for i, crop in enumerate(chunk):
                x = (crop.astype(np.float32) - 127.5) / 127.5
                blob[i] = x.transpose(2, 0, 1)
            out = self.rec_sess.run(self.rec_outputs, {self.rec_input: blob})[0]
            emb[start:start + len(chunk)] = out
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        return emb / np.maximum(norms, 1e-10)

    def analyze(self, img: np.ndarray) -> list[DetectedFace]:
        """一张 RGB 图 → 所有人脸（含 112×112 对齐裁剪）。"""
        height, width = img.shape[:2]
        dets, kpss = self.detect(img)
        if dets.shape[0] == 0:
            return []
        crops = [norm_crop(img, kps) for kps in kpss]
        embeddings = self._embed(crops)

        faces: list[DetectedFace] = []
        for (x1, y1, x2, y2, score), kps, crop, emb in zip(
                dets, kpss, crops, embeddings):
            # 贴边的人脸框会被裁到图外，先夹回图像范围再算质量
            cx1, cy1 = max(0.0, float(x1)), max(0.0, float(y1))
            cx2, cy2 = min(float(width), float(x2)), min(float(height), float(y2))
            if cx2 <= cx1 or cy2 <= cy1:
                continue
            faces.append(DetectedFace(
                bbox=(float(x1), float(y1), float(x2), float(y2)),
                det_score=float(score),
                kps=kps,
                embedding=emb,
                quality=(cx2 - cx1) * (cy2 - cy1) / (width * height),
                crop=crop,
            ))
        faces.sort(key=lambda f: (f.bbox[0], f.bbox[1]))
        return faces


def _providers() -> list[str]:
    """默认 CUDA → CPU；可用 FACE_PROVIDERS 覆盖（逗号分隔）。永不默认 TensorRT。"""
    import onnxruntime as ort

    available = set(ort.get_available_providers())
    override = os.environ.get("FACE_PROVIDERS")
    wanted = ([p.strip() for p in override.split(",") if p.strip()]
              if override else list(DEFAULT_PROVIDERS))
    chosen = [p for p in wanted if p in available]
    return chosen or ["CPUExecutionProvider"]


def save_crop(crop: np.ndarray, path: Path, size: int = 160) -> None:
    """对齐裁剪写盘（网页头像用）。已对齐的脸直接放大到 size，不裁剪。"""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.fromarray(crop)
    if im.size != (size, size):
        im = im.resize((size, size), Image.BICUBIC)
    im.save(path, "JPEG", quality=90)


# ---------------------------------------------------------------- 模型下载

def download_models(dest: Path | None = None, force: bool = False) -> Path:
    """下载 buffalo_l 包，只留 det_10g + w600k_r50（其余 143 MB 用不上）。"""
    import hashlib

    dest = (dest or MODELS_DIR / "insightface").expanduser()
    target = dest / "buffalo_l"
    if not force and (target / DET_FILE).exists() and (target / REC_FILE).exists():
        print(f"模型已存在：{target}")
        return target

    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / "buffalo_l.zip"

    def hook(blocks: int, block_size: int, total: int) -> None:
        done = blocks * block_size
        if total:
            pct = min(100, done * 100 // total)
            print(f"\r下载 {done/1024**2:7.1f} / {total/1024**2:.1f} MB  {pct:3d}%",
                  end="", flush=True)

    print(f"下载 {MODEL_ZIP_URL}")
    urllib.request.urlretrieve(MODEL_ZIP_URL, zip_path, reporthook=hook)
    print()

    digest = hashlib.sha256()
    with open(zip_path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != MODEL_ZIP_SHA256:
        raise RuntimeError(
            f"校验失败：sha256={actual}，期望 {MODEL_ZIP_SHA256}（文件可能不完整，重试 --download --force）")

    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for name in (DET_FILE, REC_FILE):
            with zf.open(name) as src, open(target / name, "wb") as dst:
                dst.write(src.read())
    zip_path.unlink()
    print(f"已就绪：{target}")
    for name in (DET_FILE, REC_FILE):
        print(f"  {name:14s} {(target / name).stat().st_size/1024**2:7.1f} MB")
    return target


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ml.faces",
                                 description="人脸引擎自检 / 模型下载")
    ap.add_argument("--download", action="store_true", help="下载 buffalo_l 模型（275 MB）")
    ap.add_argument("--force", action="store_true", help="配合 --download：重新下载")
    ap.add_argument("--images", nargs="*", default=[], help="对图片跑一遍检测，打印结果与耗时")
    args = ap.parse_args(argv)

    if args.download:
        download_models(force=args.force)
        if not args.images:
            return 0

    engine = FaceEngine()
    print(f"引擎就绪：{engine.describe()}", flush=True)
    if not engine.on_gpu:
        print("警告：CUDA 不可用，正在用 CPU。检查 onnxruntime-gpu 是否为 CUDA 12 构建"
              "（1.20–1.22）以及 CUDA/cuDNN 是否在 PATH 上。", file=sys.stderr)

    if args.images:
        for raw in args.images:
            path = Path(raw)
            img = load_rgb(path)
            started = time.time()
            faces = engine.analyze(img)
            dt = (time.time() - started) * 1000
            print(f"{path.name}: {img.shape[1]}×{img.shape[0]}  {len(faces)} 张脸  "
                  f"{dt:.0f} ms")
            for f in faces:
                x1, y1, x2, y2 = (round(v) for v in f.bbox)
                print(f"  [{x1},{y1},{x2},{y2}] score={f.det_score:.3f} "
                      f"quality={f.quality:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
