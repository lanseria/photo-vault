-- 007: 视频打标 —— 关键帧 + Qwen3-VL 4B（Q4_K_M，GGUF/llama.cpp）
--
-- 数据流：
--   jobs(kind='video') → ml.worker
--     → ml.keyframes 抽 4 张差异最大的帧（derived/keyframes/<sha>/f00.jpg…）
--     → ml.vlm 一帧一图提问，输出 JSON（caption / labels / ocr）
--     → 帧级结果进 video_frames，视频级聚合进 video_summaries + asset_tags(source='vlm')
--
-- 设计取舍：
--   · 帧不存库里、只存相对路径：图在 derived/，可删可重建，跟 thumbs/faces 一致
--   · 每帧一行（含 t_ms）：将来能按时间轴展示/说"第 3 分钟有烟花"
--   · video_summaries.labels 存 [{"name":"…","frames":3}]：保留票数，
--     方便回头调聚合阈值而不用重跑模型
--   · asset_tags 里另存一份（source='vlm'）：筛选、搜索、标签云都复用现有通路
--   · 重跑先删后插（worker 保证），天然幂等

create table if not exists video_frames (
    id         bigserial primary key,
    asset_id   uuid not null references assets(id) on delete cascade,
    ord        int not null,                    -- 0..n-1，时间顺序
    t_ms       int not null,                    -- 在视频里的时间点
    rel_path   text not null,                   -- 相对 derived_root，不带 derived 前缀
    width      int,
    height     int,
    diff       real,                            -- 与已选帧的最小画面差异（选帧依据）
    caption    text,                            -- 这一帧的一句话描述
    labels     jsonb not null default '[]'::jsonb,
    ocr        text,
    created_at timestamptz not null default now(),
    unique (asset_id, ord)
);

create index if not exists video_frames_asset_idx on video_frames (asset_id);

create table if not exists video_summaries (
    asset_id   uuid primary key references assets(id) on delete cascade,
    n_frames   int not null default 0,
    caption    text,                            -- 视频级一句话（取票数最高的帧描述）
    labels     jsonb not null default '[]'::jsonb,   -- [{"name":"海滩","frames":3}]
    ocr        text,
    model      text not null,
    ms         int,                             -- 抽帧 + 推理耗时，排障用
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

insert into ml_meta (key, value) values
    ('video_model', 'qwen3-vl-4b-instruct-q4_k_m'),
    ('video_frames', '4'),
    ('video_frames_max', '5')
on conflict (key) do update set value = excluded.value, updated_at = now();

insert into schema_migrations (version) values ('007_video')
  on conflict (version) do nothing;
