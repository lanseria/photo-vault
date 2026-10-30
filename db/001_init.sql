-- Photo Vault · P0 schema (PostgreSQL 16 + pgvector)
-- 设计要点：
--   1. assets 是逻辑资产，files 是物理文件（一张 Live Photo = 1 asset + 2 files）
--   2. 时间三件套：taken_at_local / tz_offset_min / taken_at_utc
--   3. 幂等键 = (sha256, role)，重复导入只追加 provenance

create extension if not exists pgcrypto;
create extension if not exists vector;

create table if not exists schema_migrations (
  version    text primary key,
  applied_at timestamptz not null default now()
);

-- 设备字典：把 "Apple iPhone15,2" 与 "iPhone 15 Pro" 归一
create table if not exists devices (
  id              bigserial primary key,
  make            text,
  model_raw       text not null,
  model_canonical text,
  kind            text,           -- phone | drone | actioncam | camera | other
  unique (make, model_raw)
);

create table if not exists sources (
  id         bigserial primary key,
  kind       text not null,        -- folder | sd | ptp | photokit | smb | dji
  label      text,
  root_path  text,
  serial     text,
  first_seen timestamptz not null default now(),
  last_seen  timestamptz not null default now()
);

create table if not exists assets (
  id            uuid primary key default gen_random_uuid(),
  kind          text not null,     -- photo | video | live_photo | raw | other
  taken_at_local  timestamp,
  tz_offset_min   int,
  tz_source       text,            -- exif | gps | default | user
  taken_at_utc    timestamptz,
  device_id     bigint references devices(id),
  lens          text,
  width         int,
  height        int,
  duration_ms   int,
  gps           point,             -- 未来换 geography(Point,4326) + PostGIS
  rating        smallint not null default 0,
  flag          smallint not null default 0,   -- 1=pick, -1=reject
  is_deleted    boolean not null default false,
  created_at    timestamptz not null default now()
);

create index if not exists assets_taken_idx  on assets (taken_at_utc desc) where not is_deleted;
create index if not exists assets_device_idx on assets (device_id, taken_at_utc desc);
create index if not exists assets_kind_idx   on assets (kind, taken_at_utc desc);

create table if not exists files (
  id          uuid primary key default gen_random_uuid(),
  asset_id    uuid not null references assets(id) on delete cascade,
  role        text not null,      -- original | live_motion
  sha256      bytea not null,
  bytes       bigint not null,
  mime        text,
  ext         text,
  rel_path    text not null,
  width       int,
  height      int,
  phash       bigint,             -- 64bit 平均哈希，用于后续近似去重
  source_id   bigint references sources(id),
  src_path    text,
  imported_at timestamptz not null default now(),
  unique (sha256, role)
);

create index if not exists files_asset_idx on files (asset_id);
create index if not exists files_phash_idx on files (phash);

create table if not exists exif (
  asset_id   uuid primary key references assets(id) on delete cascade,
  iso        int,
  aperture   numeric(4,2),
  shutter    numeric(12,6),
  focal_mm   numeric(7,2),
  orientation int,
  raw        jsonb
);

-- 入库审计：每次 ingest 一行，失败清单落在这里
create table if not exists ingest_runs (
  id         bigserial primary key,
  root       text,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  stats      jsonb not null default '{}'::jsonb,
  errors     jsonb not null default '[]'::jsonb
);

-- 任务队列（P0 未启用，为 ML worker 预留）
create table if not exists jobs (
  id         bigserial primary key,
  kind       text not null,
  payload    jsonb not null default '{}'::jsonb,
  state      text not null default 'pending',  -- pending|running|ok|failed|dead
  priority   int  not null default 100,
  attempts   int  not null default 0,
  run_after  timestamptz not null default now(),
  locked_by  text,
  locked_at  timestamptz,
  last_error text
);
create index if not exists jobs_ready_idx on jobs (run_after) where state = 'pending';

insert into schema_migrations (version) values ('001_init')
  on conflict (version) do nothing;