-- 018: 人物关联素材 —— 没检出人脸（背影 / 远景 / 画外音）但确实与人物有关的照片视频，手动挂到人物名下
--
-- 数据流：
--   灯箱「＋ 关联人物」→ POST /api/assets/{id}/persons → person_assets
--   人物页照片流（person_id 筛选）、人物计数（n_assets）、按人名搜索都把关联素材算进去
--   DELETE /api/assets/{id}/persons/{person_id} 取消关联
--
-- 设计取舍：
--   · 与 faces 解耦：脸是识别结果（重跑会删了重提、可忽略），关联是人工事实，
--     重跑识别 / 聚类都不动它。人物被解散或合并时关联随人物走（级联 / 合并搬运）。
--   · 纯多对多：一张素材可关联多个人，一个人可关联多张素材；(person_id, asset_id) 唯一。

create table if not exists person_assets (
    person_id  uuid not null references persons(id) on delete cascade,
    asset_id   uuid not null references assets(id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (person_id, asset_id)
);

-- 灯箱按资产取已关联人物、人物计数与按人名搜索走 asset_id 这一侧
create index if not exists person_assets_asset_idx on person_assets (asset_id);

insert into schema_migrations (version) values ('018_person_links')
  on conflict (version) do nothing;
