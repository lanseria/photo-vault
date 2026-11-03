-- 014: 文件夹从素材库推广到图库 —— 两个库各有自己的整理维度
--
-- 图库和素材库一样需要「项目 / 事件」维度：表泛化为 folders，加 library 列，
-- 每个库一套文件夹；assets.folder_id 单归属不变，一个资产只会在它所属库的
-- 某个文件夹里。已有文件夹默认归入 material，语义与迁移前完全一致。
--
-- 顺带修一个旧版遗留：move 换库时原本不清 folder_id，material 资产移回
-- gallery 后可能还挂着 material 文件夹。迁移时把这种跨库脏引用统一清掉，
-- 回到「未整理」；此后 move 会主动清 folder_id，不会再产生。

alter table material_folders rename to folders;

alter table folders add column if not exists library text not null default 'material';

alter table folders drop constraint if exists folders_library_chk;
alter table folders add constraint folders_library_chk
    check (library in ('gallery', 'material'));

update assets a
   set folder_id = null
  from folders f
 where a.folder_id = f.id
   and a.library <> f.library;

insert into schema_migrations (version) values ('014_gallery_folders')
  on conflict (version) do nothing;
