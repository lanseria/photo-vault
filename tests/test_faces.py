"""
人脸链路的回归测试：不连数据库、不读模型，只锁纯逻辑。

    uv run --group ml python tests/test_faces.py

覆盖：
  · 人物匿名命名（Person A..Z, AA..）与"跳过已占用名字"
  · 并查集连通分量（聚类的核心）
  · 512 维向量的 pgvector 文本往返（精度不掉）
  · 5 点对齐矩阵：把检测到的关键点变换回模板位置（误差 < 0.01px）
  · NMS 抑制重叠框、保留不同位置的脸
  · 对齐裁剪的形状/类型
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml.cluster import UnionFind, next_labels, person_label       # noqa: E402
from ml.faces import (ARCFACE_DST, ARCFACE_SIZE, estimate_norm,    # noqa: E402
                      nms, norm_crop)
from ml.pgvector import from_vector, to_vector                     # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(f"{name}  {detail}")
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------- 命名
print("\n[命名]")
check("Person A", person_label(0) == "Person A", person_label(0))
check("Person Z", person_label(25) == "Person Z", person_label(25))
check("进位 Person AA", person_label(26) == "Person AA", person_label(26))
check("Person AZ", person_label(51) == "Person AZ", person_label(51))
check("Person BA", person_label(52) == "Person BA", person_label(52))
labels = next_labels(["Person A", "Person B"], 3)
check("跳过已占用", labels == ["Person C", "Person D", "Person E"], str(labels))
labels = next_labels(["妈妈", "person b"], 2)
check("改名后 A 可复用", labels == ["Person A", "Person C"], str(labels))
check("改名后大小写不敏感", next_labels(["PERSON A"], 1) == ["Person B"])


# ---------------------------------------------------------------- 并查集
print("\n[并查集]")
uf = UnionFind()
for i in range(5):
    uf.add(chr(ord("a") + i))
uf.union("a", "b")
uf.union("b", "c")
uf.union("d", "e")
comps = sorted((sorted(c) for c in uf.components()), key=len, reverse=True)
check("连通分量 2 组", comps == [["a", "b", "c"], ["d", "e"]], str(comps))
uf.union("c", "d")
check("连边后合并", len(uf.components()) == 1, str(uf.components()))
check("重复 add 不重置", (uf.add("a"), uf.find("a") == uf.find("e"))[1])


# ---------------------------------------------------------------- 向量往返
print("\n[pgvector 文本往返]")
rng = np.random.default_rng(0)
vec = rng.standard_normal(512).astype(np.float32)
vec /= np.linalg.norm(vec)
back = from_vector(to_vector(vec))
check("形状", back.shape == (512,), str(back.shape))
check("最大误差 < 1e-6", float(np.max(np.abs(back - vec))) < 1e-6,
      str(float(np.max(np.abs(back - vec)))))
check("余弦相似度 ≈ 1", abs(float(back @ (vec / np.linalg.norm(vec))) - 1) < 1e-5)
check("空/异常输入不炸", from_vector("[]").shape == (0,))


# ---------------------------------------------------------------- 对齐
print("\n[5 点对齐]")
M = estimate_norm(ARCFACE_DST)
mapped = (ARCFACE_DST @ M[:, :2].T) + M[:, 2]
err = float(np.max(np.abs(mapped - ARCFACE_DST)))
check("模板自映射误差 < 0.01px", err < 0.01, f"{err:.5f}")

# 平移 + 缩放 2 倍的人脸，应该被拉回模板位置
scaled = ARCFACE_DST * 2.0 + 100.0
M2 = estimate_norm(scaled)
mapped2 = (scaled @ M2[:, :2].T) + M2[:, 2]
err2 = float(np.max(np.abs(mapped2 - ARCFACE_DST)))
check("缩放平移后对齐", err2 < 0.1, f"{err2:.5f}")

img = np.zeros((400, 400, 3), dtype=np.uint8)
img[100:300, 100:300] = 200
crop = norm_crop(img, scaled)
check("裁剪尺寸 112×112", crop.shape == (ARCFACE_SIZE, ARCFACE_SIZE, 3), str(crop.shape))
check("裁剪 dtype uint8", crop.dtype == np.uint8, str(crop.dtype))
check("裁剪非全黑", int(crop.max()) > 0)


# ---------------------------------------------------------------- NMS
print("\n[NMS]")
dets = np.array([
    [10, 10, 110, 110, 0.9],
    [12, 12, 112, 112, 0.8],      # 与第一张几乎重合 → 抑制
    [300, 300, 400, 400, 0.7],    # 另一张脸 → 保留
], dtype=np.float32)
keep = nms(dets, 0.4)
check("抑制重叠框", keep == [0, 2], str(keep))
keep = nms(np.empty((0, 5), dtype=np.float32), 0.4)
check("空输入", keep == [], str(keep))


print()
if failures:
    print(f"{len(failures)} 个用例失败：")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("全部通过")
