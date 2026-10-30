from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import IO, Iterator

CHUNK = 1024 * 1024


def sha256_stream(fp: IO[bytes]) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    while True:
        buf = fp.read(CHUNK)
        if not buf:
            break
        h.update(buf)
        total += len(buf)
    return h.hexdigest(), total


def sha256_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as fp:
        return sha256_stream(fp)


def copy_and_hash(src: Path, dst: Path) -> tuple[str, int]:
    """单次读、单次写，同时算哈希。入库主路径。"""
    h = hashlib.sha256()
    total = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            buf = fin.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            fout.write(buf)
            total += len(buf)
        fout.flush()
    return h.hexdigest(), total


def vault_rel_path(sha_hex: str, ext: str) -> str:
    """内容寻址：ab/cd/abcdef....ext"""
    ext = ext.lower() if ext.startswith(".") else f".{ext.lower()}" if ext else ""
    return f"{sha_hex[0:2]}/{sha_hex[2:4]}/{sha_hex}{ext}"


def new_landing_path(landing: Path) -> Path:
    return landing / f".tmp-{uuid.uuid4().hex}"


def average_hash(path: Path, size: int = 8) -> int | None:
    """64bit 平均哈希。失败返回 None（RAW/损坏/未知格式）。"""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("L").resize(
                (size, size), Image.Resampling.LANCZOS
            )
            px = list(im.getdata())
    except Exception:
        return None

    avg = sum(px) / len(px)
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= 1 << i
    # 转成有符号 64 位，落 bigint
    if bits >= 1 << 63:
        bits -= 1 << 64
    return bits


def hamming(a: int, b: int) -> int:
    return bin((a & 0xFFFFFFFFFFFFFFFF) ^ (b & 0xFFFFFFFFFFFFFFFF)).count("1")