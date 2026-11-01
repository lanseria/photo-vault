# 前端（Vue 3 + Vite + UnoCSS）

```bash
pnpm install
pnpm dev      # http://127.0.0.1:5173，/api 与图片代理到 7878
pnpm build    # 产出 ../vault/static（FastAPI 直接托管）
pnpm typecheck
```

## 结构

```
src/
├── api.ts              # 所有 HTTP 调用 + 错误归一化
├── types.ts            # 与 vault/web.py 的 JSON 对齐的类型
├── router/index.ts     # 路由表；旧深链（?view=…）在这里重定向
├── stores/             # Pinia：feed / selection / bin / upload / cinema / people / …
├── composables/
│   ├── scrollMemory.ts # 按「路径 + 查询」记录与恢复滚动位置
│   ├── useAssetActions.ts # 收藏 / 删除 / 移库 / 加箱 / 出稿 / 打标签
│   └── usePhotoQuery.ts   # 灯箱的 ?photo= 状态
├── components/         # 外壳与可复用组件
├── views/              # 路由级视图
└── styles/main.css     # 全局重置 + 照片网格 + 动效
```

## 约定

- **URL 是唯一状态源**：视图类型、搜索、筛选、当前照片都在路由里，
  这样刷新 / 前进后退 / 分享链接都是同一套逻辑。
- **写操作只写在 `useAssetActions`**：组件只负责触发，接口调用 + 本地状态同步 + 撤销提示都在那一处。
- **样式用 UnoCSS 原子类**：主题色在 `uno.config.ts` 的 `theme.colors`（`bg` / `panel` / `fg` / `mut` / `acc` / `heart`）。
  只有照片网格、粘性日期头、动效这类拿原子类表达不划算的规则才写在 `styles/main.css`。
- 不要直接改 `vault/static/`：那是 `pnpm build` 的产物。

