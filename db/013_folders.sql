-- 013: 素材库文件夹 —— 用户手动整理的「项目」维度
--
-- 时间轴（拍摄时间）回答的是"什么时候存的"；剪辑时找素材真正用的是
-- "这批素材属于哪个项目 / 哪一期"。文件夹就是这个维度：用户自己建、自己归。
--
-- 为什么单归属（assets.folder_id 一列），不是多对多：
--   和 009 的 library 轴同一个哲学。文件夹的语义是"整理"而不是"属性"，
--   同一份素材出现在两个文件夹里，出稿时就要回答"以哪份为准"；单归属让
--   「未整理」永远有明确边界。要交叉检索用标签，两个维度正交。
--
-- 删文件夹 = on delete set null：文件夹只是视图，删掉它素材必须原地不动，
--   回到「未整理」，原件更是碰都不碰。

create table if not exists material_folders (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table assets add column if not exists folder_id uuid
    references material_folders(id) on delete set null;

-- 文件夹内素材的默认排序轴（拍摄时间倒序），partial index 不把回收站算进去
create index if not exists assets_folder_idx
    on assets (folder_id, taken_at_utc desc) where not is_deleted;

insert into schema_migrations (version) values ('013_folders')
  on conflict (version) do nothing;
