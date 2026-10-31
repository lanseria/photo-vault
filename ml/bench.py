"""RAM++ 打标基准：8 张图跑 3 轮，看吞吐与平均标签数。

前置：$env:TEMP\\bench_paths.txt（每行一个 vault 内相对路径，.jpg/.jpeg/.png）。
"""
import os
import time
from pathlib import Path

from ml.model import RamTagger, load_image

paths = [Path(p.strip()) for p in open(os.environ["TEMP"] + r"\bench_paths.txt", encoding="utf-8-sig") if p.strip()]
paths = [Path(r"E:\photo\vault") / p for p in paths]

t0 = time.time()
tagger = RamTagger()
print(f"ram++ load: {time.time()-t0:.1f}s  dynamic_batch={tagger.dynamic_batch}", flush=True)

imgs = [load_image(p) for p in paths]
t0 = time.time()
tags_all = tagger.tag_batch(imgs[:1])
print(f"first call: {time.time()-t0:.2f}s -> {tags_all[0][:8]}", flush=True)
for r in range(3):
    t = time.time()
    tags_all = tagger.tag_batch(imgs)
    dt = time.time() - t
    print(f"round{r}: {len(imgs)} imgs in {dt:.1f}s -> {len(imgs)/dt*1000:.0f} ms/img  "
          f"平均标签数 {sum(len(t) for t in tags_all)/len(tags_all):.1f}", flush=True)
