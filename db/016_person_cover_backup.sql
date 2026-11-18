-- 016: 人物备用封面 —— 第二张自定义封面，主封面缺失时顶上
--
-- 数据流：
--   POST /api/persons/{id}/cover?slot=2 → derived/person_covers/{id}_2.jpg，backup_cover = true
--   DELETE /api/persons/{id}/cover?slot=2 → 删文件，backup_cover = false
--   （slot=1 仍是主封面 {id}.jpg / custom_cover，015 引入）
--
-- 展示优先级：主封面 → 备用封面 → 系统挑的人脸裁剪 → 首字母头像。

alter table persons add column if not exists backup_cover boolean not null default false;

insert into schema_migrations (version) values ('016_person_cover_backup')
  on conflict (version) do nothing;
