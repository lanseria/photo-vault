-- 010: 影视库 —— 电影 / 剧集 / 字幕（只登记，不搬家，不跑 AI）
--
-- 和 assets 完全分开的三张表，不是"另一种 kind"：
--   · assets 是内容寻址库，原件进 vault 后不可变；影视动辄 2TB，硬链接只能同卷，
--     拷贝一份等于把用户的硬盘复制一遍 —— 所以影视**只登记路径**，文件留在原地。
--   · 影视没有拍摄时间、没有 EXIF、不需要人脸/RAM++，塞进 assets 会污染相册的
--     时间轴、统计、AI 队列和剪辑搜索。
--   · 播放/字幕/季集结构是三张表的自然形状，用 assets 表达得靠 payload 硬塞。
--
-- 路径存 (root_key, rel_path)，不存绝对路径：
--   root_key='media' → 容器里的 MEDIA_ROOT（默认 /media，对应宿主机 MEDIA_HOST_ROOT）
--   root_key='host'  → HOST_ROOT（容器里的 /host）
-- 换挂载点只需要改 env，库里的数据不用动。
--
-- 字幕先建表并自动识别同目录 sidecar（.srt/.ass/.ssa/.vtt），播放时 SRT 转 VTT。
-- 完整的字幕入库/匹配（opensubtitles 之类）留到以后，表结构不用再改。

create table if not exists cinema_titles (
    id          uuid primary key default gen_random_uuid(),
    kind        text not null default 'movie',      -- movie | series
    title       text not null,
    title_key   text not null,                      -- 去空格/标点/大小写，用于归并同名条目
    year        int,
    overview    text,
    poster_rel  text,                               -- 相对 derived_root，如 media/<id>.jpg
    source_rel  text,                               -- 剧集根目录（相对 root_key），用于「重新扫描」
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint cinema_titles_kind_chk check (kind in ('movie', 'series'))
);

-- coalesce(year,0)：同一年份的同一部片子只留一行；两部同名不同年的翻拍各留一行
create unique index if not exists cinema_titles_key_uniq
    on cinema_titles (kind, title_key, coalesce(year, 0));
create index if not exists cinema_titles_sort_idx on cinema_titles (kind, title_key);

create table if not exists cinema_episodes (
    id          uuid primary key default gen_random_uuid(),
    title_id    uuid not null references cinema_titles(id) on delete cascade,
    season      int not null default 0,             -- 电影固定 0；剧集从 1 起，特别篇 0
    episode     int not null default 0,             -- 电影固定 0
    name        text,                               -- 集名（从文件名解析，可能为空）
    root_key    text not null default 'media',
    rel_path    text not null,
    source_path text,                               -- 导入时用户给的原路径，排障用
    bytes       bigint,
    duration_ms int,
    width       int,
    height      int,
    codec       text,
    bit_rate    bigint,
    added_at    timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (title_id, season, episode)
);

-- 同一物理文件只能登记一次：改名/移动后重新扫描会更新这一行，而不是多出一条
create unique index if not exists cinema_episodes_path_uniq
    on cinema_episodes (root_key, rel_path);
create index if not exists cinema_episodes_title_idx
    on cinema_episodes (title_id, season, episode);

create table if not exists cinema_subtitles (
    id         bigserial primary key,
    episode_id uuid not null references cinema_episodes(id) on delete cascade,
    root_key   text not null default 'media',
    rel_path   text not null,
    format     text not null,                       -- srt | ass | ssa | vtt | sub
    lang       text,                                -- BCP-47：zh-CN / zh-TW / en / ja …
    label      text,                                -- 给人看的：简体中文 / English
    forced     boolean not null default false,
    created_at timestamptz not null default now(),
    unique (episode_id, rel_path)
);

create index if not exists cinema_subtitles_ep_idx on cinema_subtitles (episode_id);

insert into schema_migrations (version) values ('010_cinema')
  on conflict (version) do nothing;
