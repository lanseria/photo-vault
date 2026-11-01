-- 012: RAW+JPEG 配对
--
-- 一个逻辑资产 = 1 张 JPEG（role='original'）+ 1 个 RAW（role='raw'）。
-- 列表里只出现一条（缩略图 / 预览 / 尺寸都取自 JPEG），出稿时两个文件一起物化，
-- 剪辑软件拿到的仍然是一对 RAW+JPEG。
--
-- 配对规则（vault/ingest.py 导入时自动做，vault/pairs.py 补历史数据）：
--   同库 + 同目录 + 同文件名（忽略大小写）+ 拍摄时间相差 ≤ 5 分钟。
-- 物理文件都在 vault 里，权限/路径不变；合并只动 assets.device_id 之类的元数据。

create index if not exists files_role_raw_idx on files (asset_id) where role = 'raw';

insert into schema_migrations (version) values ('012_raw_pairs')
  on conflict (version) do nothing;
