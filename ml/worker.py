r"""ML worker：jobs 队列消费（RAM++ 打标 + InsightFace 人脸 + Qwen3-VL 视频打标）。

    # 宿主机运行（onnxruntime-gpu / opencv / llama.cpp 在 ml 依赖组和 models/ 里）：
    $env:DATABASE_URL='postgresql://vault:vault@localhost:5433/vault'
    $env:VAULT_ROOT='E:\photo\vault'
    uv run --group ml python -m ml.worker                 # 三类任务都接
    uv run --group ml python -m ml.worker --task video    # 只跑视频打标

- 队列：jobs 表 kind='analyze'（打标）/ 'faces'（人脸）/ 'video'（视频），
  FOR UPDATE SKIP LOCKED 批量认领
- 写回：打标 → asset_tags(source='ai')；人脸 → faces（先删后插）+ derived/faces 裁剪；
  视频 → video_frames + video_summaries + asset_tags(source='vlm') + derived/keyframes
- 失败重试：attempts>=3 → dead
- 人脸暂停：ml_meta.faces_paused='1'（网页「暂停」按钮设置）时跳过 faces 认领，
  正在跑的批次收尾，pending 原样保留；网页「继续」清标记后接着跑
- 人脸聚类：faces 队列清空且未暂停时自动跑 cluster，网页点「开始识别」即可长出人物，
  不再需要手动 `python -m ml.cluster`
- 视频任务需要 models/qwen3-vl-4b/（python -m ml.vlm --download），缺了就跳过 video，
  不影响另外两类
"""
from __future__ import annotations

import os
import time
import uuid as uuidlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from vault.config import Config

from . import cluster as cluster_lib
from . import keyframes, vlm as vlmlib
from .faces import FACE_MODEL_ID, FaceEngine, load_rgb, save_crop
from .model import TAGGER_MODEL_ID, RamTagger, load_image
from .pgvector import to_vector

CLAIM_SQL = """
with claimed as (
    select id from jobs
    where kind = any(%s::text[]) and state = 'pending' and run_after <= now()
    order by priority, id
    limit %s
    for update skip locked
)
update jobs j
   set state = 'running', locked_by = %s, locked_at = now(), attempts = j.attempts + 1
  from claimed c
 where j.id = c.id
returning j.id, j.kind, j.payload
"""

ASSET_SQL = """
select a.id, a.kind, a.library, a.duration_ms, f.rel_path
from assets a join files f on f.asset_id = a.id and f.role = 'original'
where a.id = %s::uuid
"""

TAG_UPSERT_SQL = """
with t as (
    insert into tags (name) values (%s)
    on conflict (name) do update set name = excluded.name
    returning id
)
insert into asset_tags (asset_id, tag_id, source)
select %s::uuid, t.id, %s from t
on conflict (asset_id, tag_id) do nothing
"""

FACE_INSERT_SQL = """
insert into faces (asset_id, x1, y1, x2, y2, det_score, quality, source, embedding, model)
values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
returning id
"""


def finish(conn, job_id: int, ok: bool, err: str | None = None):
    conn.execute(
        """update jobs
              set state = case when %s then 'ok'
                               when attempts >= 3 then 'dead' else 'pending' end,
                  last_error = %s,
                  run_after = case when %s then run_after else now() + interval '5 minutes' end
            where id = %s""",
        (ok, err, ok, job_id),
    )
    conn.commit()


def thumb_for(cfg: Config, rel_path: str) -> Path:
    sha = Path(rel_path).stem
    return cfg.derived_root / "thumbs" / sha[0:2] / sha[2:4] / f"{sha}.webp"


def analyze_batch(conn, tagger: RamTagger, cfg, jobs: list[dict]) -> None:
    """批量认领的打标任务：读图 → RAM++ 标签 → 写库。

    静态图直接读原件；视频/HEIC 等打不开的格式回退到入库时生成的缩略图海报
    （RAM++ 会把输入压到 384px，用 512px 海报信息足够）。
    """
    ready: list[tuple[dict, object, str]] = []
    for job in jobs:
        asset_id = job["payload"]["asset_id"]
        row = conn.execute(ASSET_SQL, (asset_id,)).fetchone()
        if not row:
            finish(conn, job["id"], False, "asset 或 original file 不存在")
            continue
        path = cfg.vault_root / row["rel_path"]
        try:
            img = load_image(path)
        except Exception:
            try:
                img = load_image(thumb_for(cfg, row["rel_path"]))
            except Exception as exc:
                finish(conn, job["id"], False,
                       f"原件与缩略图都无法解码: {type(exc).__name__}")
                continue
        ready.append((job, img, asset_id))

    if not ready:
        return
    tags_list = tagger.tag_batch([r[1] for r in ready])

    for (job, _img, asset_id), tags in zip(ready, tags_list):
        try:
            with conn.transaction():
                for t in tags:
                    conn.execute(TAG_UPSERT_SQL, (t, asset_id, "ai"))
            finish(conn, job["id"], True)
        except Exception as exc:
            conn.rollback()
            finish(conn, job["id"], False, f"{type(exc).__name__}: {exc}")


def faces_batch(conn, engine: FaceEngine, cfg, jobs: list[dict]) -> None:
    """人脸任务：读图 → 检测/对齐/提特征 → 写 faces + derived/faces 头像。

    视频、HEIC 之类解不出静态帧的，退回用入库时的海报帧（source='poster'），
    质量标记照常参与聚类过滤。
    """
    faces_dir = cfg.derived_root / "faces"
    for job in jobs:
        asset_id = job["payload"]["asset_id"]
        row = conn.execute(ASSET_SQL, (asset_id,)).fetchone()
        if not row:
            finish(conn, job["id"], False, "asset 或 original file 不存在")
            continue
        # 素材库不做人脸识别：历史队列里可能还有素材库的任务，直接收掉
        if row["library"] != "gallery":
            finish(conn, job["id"], True, None)
            continue

        source = "photo"
        try:
            img = load_rgb(cfg.vault_root / row["rel_path"])
        except Exception:
            try:
                img = load_rgb(thumb_for(cfg, row["rel_path"]))
                source = "poster"
            except Exception as exc:
                finish(conn, job["id"], False,
                       f"原件与缩略图都无法解码: {type(exc).__name__}")
                continue

        try:
            detected = engine.analyze(img)
        except Exception as exc:
            finish(conn, job["id"], False, f"推理失败: {type(exc).__name__}: {exc}")
            continue

        try:
            with conn.transaction():
                stale = [r["id"] for r in conn.execute(
                    "delete from faces where asset_id = %s::uuid returning id",
                    (asset_id,))]
                created: list[tuple[str, object]] = []
                for face in detected:
                    x1, y1, x2, y2 = face.bbox
                    fid = conn.execute(FACE_INSERT_SQL, (
                        asset_id, x1, y1, x2, y2, face.det_score, face.quality,
                        source, to_vector(face.embedding), FACE_MODEL_ID,
                    )).fetchone()["id"]
                    created.append((str(fid), face.crop))
            finish(conn, job["id"], True)
        except Exception as exc:
            conn.rollback()
            finish(conn, job["id"], False, f"{type(exc).__name__}: {exc}")
            continue

        # 文件写在事务外：写盘失败不该让整条记录回滚（头像可以下次重生成）
        for fid, crop in created:
            try:
                save_crop(crop, faces_dir / f"{fid}.jpg")
            except Exception as exc:
                print(f"[worker] 头像写入失败 {fid}: {type(exc).__name__}", flush=True)
        for old in stale:
            try:
                (faces_dir / f"{old}.jpg").unlink(missing_ok=True)
            except OSError:
                pass


FRAME_INSERT_SQL = """
insert into video_frames (asset_id, ord, t_ms, rel_path, width, height, diff,
                          caption, labels, ocr)
values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
"""

SUMMARY_UPSERT_SQL = """
insert into video_summaries (asset_id, n_frames, caption, labels, ocr, model, ms, updated_at)
values (%s::uuid, %s, %s, %s::jsonb, %s, %s, %s, now())
on conflict (asset_id) do update
   set n_frames = excluded.n_frames, caption = excluded.caption,
       labels = excluded.labels, ocr = excluded.ocr,
       model = excluded.model, ms = excluded.ms, updated_at = now()
"""


def video_batch(conn, vlm: vlmlib.Vlm, cfg, jobs: list[dict]) -> None:
    """视频任务：抽关键帧 → 逐帧问 Qwen3-VL → 票数聚合成视频级标签。

    帧图落在 derived/keyframes/，库里的 video_frames.rel_path 只存相对路径。
    没有可分析画面（纯黑/损坏/0 秒）不算失败：写一条 n_frames=0 的摘要，
    免得队列反复重试同一个坏文件。
    """
    import json

    n_frames = int(os.environ.get("VLM_FRAMES", "4"))
    for job in jobs:
        asset_id = job["payload"]["asset_id"]
        row = conn.execute(ASSET_SQL, (asset_id,)).fetchone()
        if not row:
            finish(conn, job["id"], False, "asset 或 original file 不存在")
            continue
        if row["kind"] != "video":
            finish(conn, job["id"], False, f"不是视频（kind={row['kind']}）")
            continue
        src = cfg.vault_root / row["rel_path"]
        if not src.exists():
            finish(conn, job["id"], False, "原件不在磁盘上")
            continue

        sha = Path(row["rel_path"]).stem
        t0 = time.time()
        try:
            frames = keyframes.extract(
                sha, src, cfg.derived_root,
                duration_s=(row["duration_ms"] or 0) / 1000 or None,
                n=n_frames)
        except Exception as exc:
            finish(conn, job["id"], False, f"抽帧失败: {type(exc).__name__}: {exc}")
            continue

        result = None
        if frames:
            try:
                result = vlm.tag_frames(
                    [cfg.derived_root / f.rel_path for f in frames])
            except Exception as exc:
                finish(conn, job["id"], False,
                       f"VLM 推理失败: {type(exc).__name__}: {exc}")
                continue

        labels = result.labels if result else []
        try:
            with conn.transaction():
                conn.execute("delete from video_frames where asset_id = %s::uuid",
                             (asset_id,))
                for i, f in enumerate(frames):
                    fr = result.frames[i] if result and i < len(result.frames) else None
                    conn.execute(FRAME_INSERT_SQL, (
                        asset_id, i, f.t_ms, f.rel_path, f.width, f.height, f.diff,
                        fr.caption if fr else None,
                        json.dumps(fr.labels if fr else [], ensure_ascii=False),
                        fr.ocr if fr else None,
                    ))
                conn.execute(SUMMARY_UPSERT_SQL, (
                    asset_id, len(frames),
                    result.caption if result else None,
                    json.dumps(labels, ensure_ascii=False),
                    result.ocr if result else None,
                    vlmlib.VIDEO_MODEL_ID, int((time.time() - t0) * 1000),
                ))
                conn.execute("delete from asset_tags where asset_id = %s::uuid "
                             "and source = 'vlm'", (asset_id,))
                for lab in labels:
                    conn.execute(TAG_UPSERT_SQL, (lab["name"], asset_id, "vlm"))
            finish(conn, job["id"], True)
        except Exception as exc:
            conn.rollback()
            finish(conn, job["id"], False, f"{type(exc).__name__}: {exc}")
            continue

        print(f"[worker] 视频 {sha[:8]} {len(frames)} 帧 "
              f"{len(labels)} 标签 {time.time()-t0:.1f}s "
              f"{(result.caption if result else '')[:30]}", flush=True)


class Models:
    """按需加载：只跑打标就不必把 166 MB 的 ArcFace 读进内存，反之亦然。"""

    def __init__(self):
        self._tagger: RamTagger | None = None
        self.face: FaceEngine | None = None
        self.vlm: vlmlib.Vlm | None = None

    def tagger(self) -> RamTagger:
        if self._tagger is None:
            t0 = time.time()
            self._tagger = RamTagger()
            print(f"[worker] RAM++ 就绪 {TAGGER_MODEL_ID} "
                  f"{self._tagger.sess.get_providers()} "
                  f"threshold_cap={os.environ.get('RAM_THRESHOLD_CAP', '官方默认')} "
                  f"({time.time()-t0:.1f}s)", flush=True)
        return self._tagger

    def face_engine(self) -> FaceEngine:
        if self.face is None:
            t0 = time.time()
            self.face = FaceEngine()
            print(f"[worker] 人脸引擎就绪 {self.face.describe()} "
                  f"({time.time()-t0:.1f}s)", flush=True)
            if not self.face.on_gpu:
                print("[worker] 警告：CUDA 未启用，人脸任务在 CPU 上会慢 10-30 倍",
                      flush=True)
        return self.face

    def vlm_engine(self) -> vlmlib.Vlm:
        if self.vlm is not None and not self.vlm.alive:
            print("[worker] llama-server 已退出，重启", flush=True)
            self.vlm.stop()
            self.vlm = None
        if self.vlm is None:
            t0 = time.time()
            self.vlm = vlmlib.Vlm().start()
            print(f"[worker] 视频打标就绪 {vlmlib.VIDEO_MODEL_ID} "
                  f"llama.cpp:{vlmlib.LLAMA_CPP_TAG} port={self.vlm._server.port} "
                  f"({time.time()-t0:.1f}s)", flush=True)
        return self.vlm

    def close(self) -> None:
        if self.vlm is not None:
            self.vlm.stop()
            self.vlm = None


def _faces_paused(conn) -> bool:
    """网页「暂停人脸」标记（ml_meta.faces_paused），每次认领前检查。"""
    row = conn.execute("select value from ml_meta where key = 'faces_paused'").fetchone()
    return bool(row and row["value"] == "1")


def _cluster_if_drained(conn) -> None:
    """faces 队列清空且未暂停时自动聚类——「开始识别」按钮的最后一环。"""
    if _faces_paused(conn):
        return
    left = conn.execute(
        "select count(*) c from jobs where kind = 'faces' and state = 'pending'"
    ).fetchone()["c"]
    if left:
        return
    t0 = time.time()
    try:
        cluster_lib.run(conn)
        conn.commit()
        print(f"[worker] 人脸聚类完成 ({time.time() - t0:.1f}s)", flush=True)
    except Exception as exc:
        conn.rollback()
        print(f"[worker] 人脸聚类失败: {type(exc).__name__}: {exc}", flush=True)


def run_worker(kinds: list[str], once: bool = False,
               max_jobs: int | None = None) -> None:
    cfg = Config.load()
    models = Models()
    try:
        _loop(models, cfg, kinds, once, max_jobs)
    finally:
        models.close()


def _loop(models: Models, cfg: Config, kinds: list[str], once: bool,
          max_jobs: int | None = None) -> None:
    worker = f"worker-{uuidlib.uuid4().hex[:8]}"
    batch = int(os.environ.get("ML_BATCH", "8"))
    # 回收上次崩溃遗留在 running 态的任务（单 worker 假设；多机部署需加租约时间判断）
    with psycopg.connect(cfg.database_url, row_factory=dict_row) as conn:
        n = conn.execute(
            "update jobs set state='pending', locked_by=null, locked_at=null "
            "where state='running' and kind = any(%s::text[])", (kinds,)
        ).rowcount
        conn.commit()
        if n:
            print(f"[worker] 回收中断任务 {n} 个", flush=True)
    print(f"[worker] 接任务 {kinds}，批次 {batch}", flush=True)

    idle = 0
    done = 0
    pause_logged = False
    while True:
        if max_jobs and done >= max_jobs:
            print(f"[worker] 已处理 {done} 个，--max 退出", flush=True)
            return
        with psycopg.connect(cfg.database_url, row_factory=dict_row) as conn:
            # 网页点了「暂停」就不再认领 faces；打标 / 视频不受影响
            active = kinds
            if "faces" in active and _faces_paused(conn):
                active = [k for k in kinds if k != "faces"]
                if not active and once:
                    print("[worker] faces 已暂停，--once 退出", flush=True)
                    return
            rows = conn.execute(CLAIM_SQL, (active, batch, worker)).fetchall()
            conn.commit()
            if not rows:
                conn.close()
                if once:
                    print("[worker] 队列已清空，--once 退出", flush=True)
                    return
                if not active and not pause_logged:
                    print("[worker] faces 已暂停（网页可继续），待命中…", flush=True)
                    pause_logged = True
                idle += 1
                time.sleep(min(2 + idle, 8))
                continue
            pause_logged = False
            idle = 0
            groups: dict[str, list[dict]] = {}
            for r in rows:
                groups.setdefault(r["kind"], []).append(r)
            for kind, group in groups.items():
                try:
                    if kind == "analyze":
                        analyze_batch(conn, models.tagger(), cfg, group)
                    elif kind == "faces":
                        faces_batch(conn, models.face_engine(), cfg, group)
                    elif kind == "video":
                        video_batch(conn, models.vlm_engine(), cfg, group)
                    else:
                        for r in group:
                            finish(conn, r["id"], False, f"未知任务类型 {kind}")
                except Exception as exc:
                    conn.rollback()
                    for r in group:
                        finish(conn, r["id"], False,
                               f"batch: {type(exc).__name__}: {exc}")
            print(f"[worker] 处理 {len(rows)} 个 " +
                  ", ".join(f"{k}×{len(v)}" for k, v in groups.items()), flush=True)
            done += len(rows)
            if "faces" in groups:
                _cluster_if_drained(conn)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m ml.worker", description="ML worker")
    ap.add_argument("--task", choices=["all", "tags", "faces", "video"],
                    default=os.environ.get("ML_TASK", "all"),
                    help="只接某一类任务；默认全都接")
    ap.add_argument("--once", action="store_true",
                    help="把队列清空后退出（适合定时任务/测试）")
    ap.add_argument("--max", type=int, default=None, dest="max_jobs",
                    help="处理 N 个任务后退出（试跑/分批用）")
    args = ap.parse_args(argv)
    kinds = {"all": ["faces", "analyze", "video"], "tags": ["analyze"],
             "faces": ["faces"], "video": ["video"]}[args.task]

    ok, why = vlmlib.ready()
    if "video" in kinds and not ok:
        if args.task == "video":
            print(f"[worker] 视频打标不可用：{why}", flush=True)
            return 1
        print(f"[worker] 视频打标不可用，本次跳过 video：{why}", flush=True)
        kinds = [k for k in kinds if k != "video"]
    if not kinds:
        print("[worker] 没有可接的任务类型，退出", flush=True)
        return 0
    run_worker(kinds, once=args.once, max_jobs=args.max_jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
