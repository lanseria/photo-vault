-- 003: 用户参与度 —— 喜欢 / 回收站 / 标签
--   favorite   → iOS 相册式"喜欢"，网格右下角小红心，可筛选
--   deleted_at → 移入"最近删除"的时间（软删仍用 is_deleted；两者配合可做 30 天自动清理）
--   tags       → 自由标签。系统替用户归类：先人工打标，后续 ML worker 经 jobs 表自动打

alter table assets add column if not exists favorite boolean not null default false;
alter table assets add column if not exists deleted_at timestamptz;

create index if not exists assets_fav_idx   on assets (taken_at_utc desc) where favorite and not is_deleted;
create index if not exists assets_trash_idx on assets (deleted_at desc) where is_deleted;

create table if not exists tags (
  id         bigserial primary key,
  name       text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists asset_tags (
  asset_id   uuid not null references assets(id) on delete cascade,
  tag_id     bigint not null references tags(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (asset_id, tag_id)
);

create index if not exists asset_tags_tag_idx on asset_tags (tag_id);

insert into schema_migrations (version) values ('003_engagement')
  on conflict (version) do nothing;
