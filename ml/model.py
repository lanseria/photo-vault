"""RAM++ 离散打标引擎（唯一 ML 路径）。

模型: RAM++ ONNX INT8（anakhiu/ram-plus-onnx-int8，873 MB）
    models/ram_plus_int8.onnx
    输入 [B,3,384,384] fp32（squash 到 384×384 + ImageNet 归一化，与官方 transform 逐位一致）
    输出 [B,4585] 原始 logits → sigmoid → 官方逐标签阈值 → 中文标签（asset_tags source='ai'）

标签表/阈值表/中文对照表内置于 ml/assets/ram/，与 ONNX 权重的输出顺序一一对应。
"""
from __future__ import annotations

import os

try:                                    # HEIC/HEIF 原件解码（与 web.py 同款）
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
# 权重目录：默认 <repo>/models/，可用 VAULT_MODELS_DIR 指到别的盘
MODELS_DIR = Path(os.environ.get("VAULT_MODELS_DIR", str(_REPO / "models")))
RAM_ASSETS = Path(__file__).resolve().parent / "assets" / "ram"
RAM_ONNX_CANDIDATES = (
    MODELS_DIR / "ram_plus_int8.onnx",
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "photo-vault" / "ram_plus_int8.onnx",
    Path.home() / ".cache" / "photo-vault" / "ram_plus_int8.onnx",
)

TAGGER_MODEL_ID = "ram-plus-onnx-int8"
RAM_INPUT_SIZE = 384
RAM_N_TAGS = 4585
_RAM_MEAN = (0.485, 0.456, 0.406)
_RAM_STD = (0.229, 0.224, 0.225)


class RamTagger:
    """RAM++ 离散打标：4585 类逐标签 sigmoid + 官方阈值，输出中文标签。"""

    def __init__(self, onnx_path: str | None = None,
                 threshold_cap: float | None = None,
                 max_tags: int | None = None):
        import onnxruntime as ort

        path = Path(onnx_path) if onnx_path else self._find_onnx()
        self.tag_en = _read_lines(RAM_ASSETS / "ram_tag_list.txt")
        self.tag_zh = _read_lines(RAM_ASSETS / "ram_tag_list_chinese.txt")
        thresholds = [float(x) for x in
                      _read_lines(RAM_ASSETS / "ram_tag_list_threshold.txt")]
        if not (len(self.tag_en) == len(self.tag_zh) == len(thresholds) == RAM_N_TAGS):
            raise RuntimeError(
                f"RAM++ 标签表行数不为 {RAM_N_TAGS}（en={len(self.tag_en)} "
                f"zh={len(self.tag_zh)} thr={len(thresholds)}），与权重不配套")
        # 可选提高召回：官方阈值 74.5% 本来就是 0.65，把高于 cap 的压回 cap。
        # 阈值=1.0 的 10 个标签是官方故意关掉的（sigmoid 永远到不了 1.0），保持关闭。
        cap = threshold_cap if threshold_cap is not None \
            else (float(os.environ["RAM_THRESHOLD_CAP"])
                  if os.environ.get("RAM_THRESHOLD_CAP") else None)
        self.thresholds = [t if t >= 1.0 else min(t, cap) if cap else t
                           for t in thresholds]
        self.max_tags = max_tags if max_tags is not None \
            else int(os.environ.get("RAM_MAX_TAGS", "32"))

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = [p for p in os.environ.get(
            "RAM_PROVIDERS", "CPUExecutionProvider").split(",") if p]
        self.sess = ort.InferenceSession(str(path), sess_options=opts,
                                         providers=providers)
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        self.output_name = self.sess.get_outputs()[0].name
        self.dynamic_batch = not isinstance(inp.shape[0], int)

    @staticmethod
    def _find_onnx() -> Path:
        for c in RAM_ONNX_CANDIDATES:
            if c.exists():
                return c
        raise FileNotFoundError(
            "找不到 RAM++ ONNX 权重，找过:\n"
            + "\n".join(f"  {c}" for c in RAM_ONNX_CANDIDATES)
            + "\n  见 README「模型权重」章节（873 MB，下载后任选一处放置）")

    @staticmethod
    def _prep(img):
        """PIL.Image → (3,384,384) float32。必须与官方 transform 一致：
        Resize((384,384)) 是硬 squash（不是裁剪/留白），BILINEAR，ImageNet 归一化。"""
        import numpy as np
        from PIL import Image

        im = img.convert("RGB").resize((RAM_INPUT_SIZE, RAM_INPUT_SIZE), Image.BILINEAR)
        x = np.asarray(im, dtype=np.float32) / 255.0
        mean = np.asarray(_RAM_MEAN, dtype=np.float32)   # 显式 float32，避免被提升成 float64
        std = np.asarray(_RAM_STD, dtype=np.float32)
        x = (x - mean) / std
        return x.transpose(2, 0, 1)

    def tag_batch(self, images: list) -> list[list[str]]:
        """PIL.Image 列表 → 中文标签列表（按置信度降序，截到 max_tags）。"""
        import numpy as np

        step = len(images) if self.dynamic_batch else 1
        results: list[list[str]] = []
        for i in range(0, len(images), step):
            arr = np.stack([self._prep(im) for im in images[i:i + step]]).astype(np.float32)
            logits = self.sess.run([self.output_name], {self.input_name: arr})[0]
            probs = 1.0 / (1.0 + np.exp(-logits))
            for row in probs:
                hits = [j for j in range(RAM_N_TAGS) if row[j] > self.thresholds[j]]
                hits.sort(key=lambda j: row[j], reverse=True)
                results.append([self.tag_zh[j].strip() or self.tag_en[j]
                                for j in hits[:self.max_tags]])
        return results


def _read_lines(path: Path) -> list[str]:
    return [ln.strip() for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def load_image(path: Path):
    from PIL import Image

    with Image.open(path) as im:
        return im.convert("RGB")
