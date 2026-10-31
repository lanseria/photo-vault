-- 006: 人脸 —— SCRFD 检测 + ArcFace 512 维向量 → 匿名人物聚类
--
-- 数据流：
--   jobs(kind='faces') → ml.worker 检测 / 对齐 / 提特征 → faces
--   ml.cluster 按余弦相似度聚类 → persons（Person A/B/C，用户手动改名）
--
-- 设计取舍：
--   · 存 L2 归一化后的向量：余弦相似度 = 点积，pgvector 的 <=> 直接可用
--   · quality = 人脸面积 / 图像面积：选封面、过滤海报帧里的小脸
--   · 同一资产重跑先删后插（worker 保证），所以不需要复杂的幂等键
--   · persons.name 可空但一旦命名就唯一（大小写不敏感），避免两个"妈妈"

create table if not exists persons (
    id            uuid primary key default gen_random_uuid(),
    name          text,                       -- 'Person A'（自动）→ 用户改成真实姓名
    cover_face_id uuid,                       -- 封面脸；FK 在 faces 建好后补
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create table if not exists faces (
    id         uuid primary key default gen_random_uuid(),
    asset_id   uuid not null references assets(id) on delete cascade,
    person_id  uuid references persons(id) on delete set null,
    x1         real not null,
    y1         real not null,
    x2         real not null,
    y2         real not null,
    det_score  real not null,
    quality    real,                          -- 人脸面积占比，聚类/封面用
    source     text not null default 'photo', -- photo | poster（视频用海报帧）
    embedding  vector(512) not null,
    model      text not null,                 -- 模型代次，换模型后据此重算
    created_at timestamptz not null default now()
);

create index if not exists faces_asset_idx  on faces (asset_id);
create index if not exists faces_person_idx on faces (person_id) where person_id is not null;
-- 聚类时逐脸查最近邻；建表即建索引，插入时增量维护
create index if not exists faces_emb_idx    on faces using hnsw (embedding vector_cosine_ops);

alter table persons drop constraint if exists persons_cover_face_fk;
alter table persons add constraint persons_cover_face_fk
    foreign key (cover_face_id) references faces(id) on delete set null;

create unique index if not exists persons_name_uniq
    on persons (lower(name)) where name is not null;

insert into ml_meta (key, value) values
    ('face_model', 'buffalo_l:det_10g+w600k_r50'),
    ('face_det_size', '640')
on conflict (key) do update set value = excluded.value, updated_at = now();

insert into schema_migrations (version) values ('006_faces')
  on conflict (version) do nothing;
