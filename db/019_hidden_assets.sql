-- 019: 隐藏媒体 —— 软隐藏，不进回收站
--   hidden → 默认所有资产列表排除隐藏项；前端顶栏全局开关打开后混入显示（带角标）。
--   隐藏 ≠ 删除：回收站不受影响（保证隐藏过的项也能恢复），AI 队列照常处理已排队的任务。
--   默认浏览（未隐藏 + 未删除）是最常见的查询：部分索引加速，仿 003 的 favorite 模式。

alter table assets add column if not exists hidden boolean not null default false;

create index if not exists assets_visible_idx on assets (taken_at_utc desc)
  where not hidden and not is_deleted;

insert into schema_migrations (version) values ('019_hidden_assets')
  on conflict (version) do nothing;
