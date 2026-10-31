-- 009: 素材库 —— 资产的两套归属（相册 gallery / 素材库 material）
--
-- 为什么是单值列，不是多对多：
--   一件素材要么是"回忆"，要么是"可用的素材"。从相册移进素材库 = 换归属，
--   不是再打一个标签。单值列让两个默认视图天然互斥，也不会出现
--   "同一张图两边都能看到、删了才知道删的是哪份"的歧义。
--
-- 标签 / 人脸 / 视频摘要一概不动：换归属只改筛选轴，AI 结果原样复用，
--   所以素材库天然带 AI 识别能力，不需要第二套模型或第二份数据。
--
-- 默认 gallery：迁移已有数据时不会把相册清空。

alter table assets add column if not exists library text not null default 'gallery';

alter table assets drop constraint if exists assets_library_chk;
alter table assets add constraint assets_library_chk
    check (library in ('gallery', 'material'));

-- 两个视图各自的默认排序轴（按拍摄时间倒序），partial index 不把回收站算进去
create index if not exists assets_library_idx
    on assets (library, taken_at_utc desc) where not is_deleted;

insert into schema_migrations (version) values ('009_library')
  on conflict (version) do nothing;
