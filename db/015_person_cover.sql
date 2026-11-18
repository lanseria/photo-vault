-- 015: 人物自定义封面 —— 支持上传图片替代系统自动挑的人脸裁剪
--
-- 数据流：
--   POST /api/persons/{id}/cover → derived/person_covers/{id}.jpg，custom_cover = true
--   DELETE /api/persons/{id}/cover → 删文件，custom_cover = false，回退到 cover_face_id
--
-- 设计取舍：
--   文件落 derived（可再生的衍生物），库里只存一个开关：
--   文件丢了 / 手动删了，前端回退到人脸裁剪，行为和没传过一样。

alter table persons add column if not exists custom_cover boolean not null default false;

insert into schema_migrations (version) values ('015_person_cover')
  on conflict (version) do nothing;
