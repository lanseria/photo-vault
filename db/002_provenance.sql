-- 002: 存储方式与来源路径
--   storage_mode = copy  → 拷贝进 vault（独立副本，最安全）
--                = link  → 硬链接进 vault（同一文件系统，零额外空间，秒级）
--   origin_rel  → 相对来源根目录的路径，用于"按原文件夹浏览"
--   origin_path → 完整来源路径，用于溯源与反向删除

alter table files add column if not exists storage_mode text not null default 'copy';
alter table files add column if not exists origin_path text;
alter table files add column if not exists origin_rel text;

create index if not exists files_origin_rel_idx on files (origin_rel text_pattern_ops);
create index if not exists files_storage_idx on files (storage_mode);

insert into schema_migrations (version) values ('002_provenance')
  on conflict (version) do nothing;