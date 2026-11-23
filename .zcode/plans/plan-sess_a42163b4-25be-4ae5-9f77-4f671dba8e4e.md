# 隐藏媒体 + 全局「展示隐藏」开关

## Context

给照片库添加「隐藏」功能：单张/批量隐藏媒体后，默认在所有视图中不可见；顶栏有一个全局开关，一键打开后隐藏项**混入**各列表显示（带角标），可随时关掉恢复默认。隐藏 ≠ 删除：回收站不受影响，AI 队列照常处理。

用户已确认两个 UX 决策：
1. **混入显示**：开关打开 = 图库/收藏/标签/人物等所有列表混入隐藏项 + 角标；关闭 = 全部视图不含隐藏项。
2. **顶栏全局开关**：导航胶囊行（NavChips）右侧固定按钮，localStorage 持久化，切页/刷新保持。

## 改动清单

### 1. 数据库 — 新建 `db/019_hidden_assets.sql`

仿照 `db/003_engagement.sql:6-10` 的布尔标记模式：

```sql
alter table assets add column if not exists hidden boolean not null default false;
create index if not exists assets_visible_idx on assets (taken_at_utc desc)
  where not hidden and not is_deleted;
insert into schema_migrations (version) values ('019_hidden_assets') on conflict (version) do nothing;
```

迁移由 `vault/db.py:15-35` 的 `init_db()` 在启动时自动执行，无需手工步骤。

### 2. 后端 — `vault/web.py`

- **`_asset_filters()`** (web.py:220)：新增 `show_hidden: bool = False` 参数；`not trash and not show_hidden` 时追加 `where.append("not a.hidden")`。回收站视图跳过此条件（保证被隐藏又删除的项仍能恢复）。
- **`api_assets`** (web.py:325)：新增 `show_hidden: bool = False` 查询参数并传入 `_asset_filters`；SELECT（web.py:361）加 `a.hidden`。
- **`api_asset_dates`** (web.py:381)：同样加 `show_hidden` 参数传入——日期面板张数与列表自动保持一致。
- **`_ITEM_COLS`** (web.py:447)：加 `a.hidden,`，让 `api_asset` 深链（web.py:496）和剪辑箱列表（web.py:799）都带上该字段。
- **`_item_json`** (web.py:460)：加 `"hidden": bool(r.get("hidden"))`。
- **单资产接口**（仿 `api_favorite` web.py:1029-1036）：新增 `POST /api/assets/{asset_id}/hidden`，body `{"hidden": bool}`，`update assets set hidden=%s where id=%s and not is_deleted returning id`，无行返回 404。
- **`api_batch`** (web.py:1320)：action 分支加 `hide`/`unhide`（仿 favorite/unfavorite），更新 `BatchBody` 注释（web.py:1241）。

### 3. 前端

- **`types.ts`**：`Asset` 加 `hidden?: boolean`（`deleted` 旁，~line 36）。
- **`api.ts`**：`AssetQuery` 加 `show_hidden?: boolean`；batch action 联合类型加 `'hide' | 'unhide'`；新增 `hidden(id, hidden)` 方法（仿 `favorite`，~line 135）。
- **新建 `stores/reveal.ts`**：全局开关状态 `showHidden`（ref），初始值读 `localStorage('show_hidden')`，`toggle()` 写回。
- **`stores/feed.ts`**：`FeedConfig` 加 `showHidden?: boolean`；`buildParams`（line 76）加 `if (cfg.showHidden) params.show_hidden = true`。
- **`views/FeedView.vue`**：`cfg` computed（line 48）从 reveal store 注入 `showHidden` → `cfgKey` 变化自动触发 `watch(cfgKey, load)`（line 148）重新拉取，无需额外逻辑。新增 `onHidden()` 处理灯箱事件（完全仿 `onTrash` line 317：步进到相邻/关闭）。
- **`composables/useAssetActions.ts`**：新增 `setHidden(ids, value)`：
  - `api.batch({ ids, action: value ? 'hide' : 'unhide' })`；
  - `feed.patch(id, { hidden: value })` 更新角标；
  - 隐藏且开关关闭时 `feed.removeMany(ids)`（项从当前列表消失）；
  - toast「已隐藏 N 项」带**撤销**（撤销 = unhide + `feed.requestReload()`）；
  - `selection.stop()`；加入 return 导出。
- **`components/NavChips.vue`**：chips 行右侧（count span 前）加切换按钮：chip 样式 + eyeOff 图标 + 文案「展示隐藏」，激活态 `chip-on`；仅 `route.meta.feed` 存在且非 trash 的路由显示；点击 `reveal.toggle()`。
- **`components/PhotoCell.vue`**：`asset.hidden` 时右上角 eyeOff 角标（仿喜欢心形 line 66-72 的定位模式），图片加轻度变暗 `opacity-60`（区别于已删除的 `opacity-45`）；与右上角「?」未识别设备角标共存时按 removable 模式（line 89）错位。
- **`components/SelectionBar.vue`**：非回收站分支（line 89-137）在「删除」前加「隐藏/取消隐藏」按钮（按 `one?.hidden` 切换文案），调 `actions setHidden(selection.list(), ...)`。
- **`components/PhotoLightbox.vue`**：操作行（line 682-742）「删除」前加「隐藏/取消隐藏」按钮（回收站不显示），向上 emit `hidden`，由 FeedView 的 `onHidden` 处理。
- **`components/AppIcon.vue`**：注册新图标 `eyeOff` / `eye`（现有图标集无眼睛类图标）。

### 明确不改

- **回收站**：始终显示全部已删除项（`_asset_filters` 中 trash 跳过 hidden 条件）。
- **ML**：`ml/worker.py`（按 id 处理）与 `ml/cluster.py`（只排除 `is_deleted`）不动——隐藏的资产照常参与已排队的识别。
- **统计/胶囊计数**（`/api/stats`）不变：图库计数仍为库内总数（含隐藏项），仅列表过滤。
- **剪辑箱、影视（Cinema）**：独立域，不加入隐藏过滤。

## 验证

1. 重启后端（端口 7878）→ 启动日志确认 `019_hidden_assets` 迁移已应用。
2. `cd frontend && pnpm dev`（5173，代理到 7878）。
3. 手动流程：
   - 「选择」勾选几张 → 底部「隐藏」→ 项从列表消失，toast「撤销」可恢复；
   - 顶栏「展示隐藏」打开 → 隐藏项混入显示并带角标；图库/收藏/标签/人物/搜索各视图均生效；关闭后消失；
   - 灯箱内隐藏 → 自动步进到下一张；取消隐藏正常；
   - 刷新页面开关状态保持；回收站不受影响（隐藏过的项也能恢复）；
   - 日期跳转面板张数与列表一致。
4. `cd frontend && pnpm build` 通过（含 vue-tsc 类型检查），产出刷新 7878 亦可验证。