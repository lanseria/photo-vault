-- 017: 人脸忽略 —— 误检/杂物脸移入"忽略"，重跑识别不再回来
--
-- 数据流：
--   网页人物详情 →「人脸」抽屉 → 选中若干脸 → POST ignore
--     → faces.ignored = true + ignored_faces 记一条 embedding 规则
--   worker 重跑同一资产：先删后插 faces（face_id 变了），
--     插入时拿新 embedding 跟 ignored_faces 比，余弦相似度 >= 0.8 的新脸自动 ignored
--   聚类 / 人物计数 / 封面挑选 / 人物照片流 一律排除 ignored
--
-- 设计取舍：
--   · ignored 标记在 faces 上，查询简单；规则另存 ignored_faces（embedding），
--     因为重跑会删掉旧 face 行，只靠 faces.ignored 记不住"下次别再收进来"
--   · 恢复 = ignored=false + 删掉对应 ignored_faces 规则，否则下次重检又被压下去

alter table faces add column if not exists ignored boolean not null default false;

create table if not exists ignored_faces (
    id         uuid primary key default gen_random_uuid(),
    embedding  vector(512) not null,      -- 被忽略脸的特征，识别"同一张脸"用
    model      text not null,             -- 提特征的模型代次，换模型后规则按需重录
    face_id    uuid,                      -- 来源脸（可能已被重跑删掉），仅溯源
    asset_id   uuid,                      -- 来源资产，溯源 + 展示
    created_at timestamptz not null default now()
);

-- 逐脸查最近邻用；量小（误检不会太多），HNSW 足够
create index if not exists ignored_faces_emb_idx
    on ignored_faces using hnsw (embedding vector_cosine_ops);

insert into schema_migrations (version) values ('017_ignore_faces')
  on conflict (version) do nothing;
