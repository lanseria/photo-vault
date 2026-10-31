-- 008: 剪辑交付层 —— 剪辑箱（bins）+ 搜索加速
--
-- 为什么需要 bins：搜索结果随查询变化，但剪辑是"跨多次搜索把素材攒起来"的动作。
-- 没有持久化的收纳，用户只能在一次查询里选完，或者拿笔记下文件名——都不成立。
--
-- bin_items.ord 不是摆设：出稿时它决定文件名前缀 001/002…，
-- 也就是素材库里的顺序。in_ms/out_ms 先留位（将来存"只要第 2–7 秒"）。

create extension if not exists pg_trgm;

-- 搜索走 ilike '%x%'，没有 trigram 索引就是全表扫。3 千行看不出差别，
-- 但 10 万行、或者几个并发搜索时，差别是几毫秒 vs 几百毫秒。
create index if not exists files_src_trgm    on files using gin (src_path gin_trgm_ops);
create index if not exists files_origin_trgm on files using gin (origin_rel gin_trgm_ops);

-- 剪辑最常用的筛选轴：时长。partial index 只覆盖视频，不把照片混进来。
create index if not exists assets_clip_dur_idx on assets (duration_ms)
  where kind in ('video', 'live_photo') and not is_deleted;

create table if not exists bins (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  note       text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists bin_items (
  bin_id   uuid not null references bins(id) on delete cascade,
  asset_id uuid not null references assets(id) on delete cascade,
  ord      int  not null default 0,
  in_ms    int,
  out_ms   int,
  added_at timestamptz not null default now(),
  primary key (bin_id, asset_id)
);

create index if not exists bin_items_ord_idx on bin_items (bin_id, ord);

insert into schema_migrations (version) values ('008_clip')
  on conflict (version) do nothing;
