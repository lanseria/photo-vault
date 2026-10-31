-- 004: 语义层 —— 图像向量 + AI 标签来源
--   embedding      → Chinese-CLIP ViT-B/16 的 512 维图像向量，pgvector 余弦检索（"海边日落下的狗"）
--   asset_tags.source → 标签来源：user（人打的）/ ai（中文零样本模型打的），筛选时同等对待

alter table assets add column if not exists embedding vector(512);

create index if not exists assets_emb_hnsw on assets using hnsw (embedding vector_cosine_ops)
  where embedding is not null;

alter table asset_tags add column if not exists source text not null default 'user';

insert into schema_migrations (version) values ('004_semantic')
  on conflict (version) do nothing;
