-- 设备标签
--
-- EXIF 里的机型号（model_raw）常常很难看（"iPhone15,2"）或者干脆没有
-- （截图、微信保存的图、剪辑软件导出的图）。label 是用户自己起的名字，
-- 展示时优先级：label > model_canonical > model_raw，都为空就是「未识别设备」。
--
-- 只加列、不改数据：已有设备的 label 为空，展示行为与之前完全一致。

alter table devices add column if not exists label text;

comment on column devices.label is '用户起的设备名（"我的手机"），空则用 model_canonical / model_raw';

insert into schema_migrations (version) values ('011_devices')
on conflict (version) do nothing;
