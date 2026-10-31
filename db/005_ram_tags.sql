-- 005: AI 打标收敛为 RAM++ 单轨
--   RAM++ ONNX INT8（4585 类，逐标签 sigmoid + 官方阈值）→ 中文标签 → asset_tags(source='ai')
--   语义嵌入路径（Chinese-CLIP 512 维向量）整体下线：drop embedding 列与 HNSW 索引
--   同时清空旧 AI 标签（RAM++ 词表与旧中文零样本词表不同）与旧 analyze 任务
-- 之后执行 `python -m ml.enqueue` 全量入队。

drop index if exists assets_emb_hnsw;
alter table assets drop column if exists embedding;

delete from asset_tags where source = 'ai';
delete from jobs where kind = 'analyze';

-- 记录当前打标模型代次：worker 启动时打印，排障时核对
create table if not exists ml_meta (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);

insert into ml_meta (key, value) values
    ('tagger_model', 'ram-plus-onnx-int8')
on conflict (key) do update set value = excluded.value, updated_at = now();

delete from ml_meta where key in ('embedding_model', 'embed_dim');

insert into schema_migrations (version) values ('005_ram_tags')
  on conflict (version) do nothing;
