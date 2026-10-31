"""pgvector 文本格式的读写（不引入 `pgvector` Python 包）。

psycopg 没有注册 vector 类型适配器时，参数按 text 传、`column::text` 读出；
格式就是 Postgres 数组字面量：`[0.1,0.2,...]`。
"""
from __future__ import annotations

import numpy as np


def to_vector(embedding) -> str:
    """numpy 向量 → `'[...]'::vector` 用的字面量。"""
    return "[" + ",".join(f"{float(v):.6f}" for v in embedding) + "]"


def from_vector(raw) -> np.ndarray:
    """vector / vector 的文本表示 → float32 数组。"""
    if isinstance(raw, np.ndarray):
        return raw.astype(np.float32, copy=False)
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return np.array([part for part in text.split(",") if part], dtype=np.float32)
