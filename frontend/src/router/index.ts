import {
  createRouter,
  createWebHistory,
  type LocationQuery,
  type RouteMeta,
  type RouteRecordRaw,
} from 'vue-router'
import type { FeedKind } from '@/stores/feed'
import FeedView from '@/views/FeedView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** 这个路由展示哪一种照片流 */
    feed?: FeedKind
    /** 收藏 / 标签跟在哪个库下 */
    library?: 'gallery' | 'material'
    title?: string
    /** 顶栏搜索框：搜资产，还是搜影视 */
    search?: 'assets' | 'cinema'
    /** 显示类型 / 时长 / 清晰度筛选条 */
    filters?: boolean
    /** 允许「选择」多选 */
    selectable?: boolean
    /** 高亮哪个导航 chip */
    chip?: 'gallery' | 'material' | 'favorites' | 'bin' | 'people' | 'cinema' | 'trash' | 'tag' | 'devices'
  }
}

const feed = (
  path: string,
  name: string,
  kind: FeedKind,
  chip: RouteMeta['chip'],
  extra: Partial<RouteMeta> = {},
): RouteRecordRaw => ({
  path,
  name,
  component: FeedView,
  meta: { feed: kind, title: '图库', search: 'assets', filters: true, selectable: true, chip, ...extra },
})

/** 旧版深链：/?view=fav、/?view=tag&tag=x、/?person=id、/?title=id… */
export function legacyTarget(query: LocationQuery) {
  const str = (key: string) => (typeof query[key] === 'string' ? (query[key] as string) : '')
  const view = str('view')
  const tag = str('tag')
  const person = str('person')
  const title = str('title')
  const keep: LocationQuery = {}
  if (str('q')) keep.q = str('q')

  if (view === 'person' && person) return { path: `/people/${person}`, query: keep }
  if (view === 'tag' && tag) return { path: `/gallery/tags/${encodeURIComponent(tag)}`, query: keep }
  if (view === 'trash') return { path: '/trash', query: keep }
  if (view === 'people') return { path: '/people', query: keep }
  if (view === 'bin') return { path: '/bin', query: keep }
  if (view === 'cinema') return { path: title ? `/cinema/${title}` : '/cinema', query: keep }
  if (view === 'material') return { path: '/material', query: keep }
  if (view === 'fav') return { path: '/gallery/favorites', query: keep }
  return { path: '/gallery', query: keep }
}

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: to => legacyTarget(to.query) },

  feed('/gallery', 'gallery', 'gallery', 'gallery', {
    library: 'gallery',
    title: '图库',
  }),
  feed('/gallery/favorites', 'gallery-favorites', 'favorites', 'favorites', {
    library: 'gallery',
    title: '喜欢',
  }),
  feed('/gallery/tags/:tag', 'gallery-tag', 'tag', 'tag', {
    library: 'gallery',
    title: '标签',
  }),

  // 素材库的默认视图是文件夹：文件夹网格 + 未整理素材（MaterialView 内嵌 FeedView）。
  // 不放筛选条 —— 那是照片流（文件夹内 / 喜欢 / 标签）的工具。
  {
    path: '/material',
    name: 'material',
    component: () => import('@/views/MaterialView.vue'),
    meta: {
      feed: 'unfiled',
      library: 'material',
      title: '素材库',
      chip: 'material',
      search: 'assets',
      selectable: true,
    },
  },
  feed('/material/folders/:id', 'material-folder', 'folder', 'material', {
    library: 'material',
    title: '文件夹',
  }),
  feed('/material/favorites', 'material-favorites', 'favorites', 'favorites', {
    library: 'material',
    title: '喜欢 · 素材库',
  }),
  feed('/material/tags/:tag', 'material-tag', 'tag', 'tag', {
    library: 'material',
    title: '标签 · 素材库',
  }),

  feed('/people/:id', 'person', 'person', 'people', { title: '人物' }),

  { path: '/people', name: 'people', component: () => import('@/views/PeopleView.vue'), meta: { title: '人物', chip: 'people' } },
  { path: '/bin', name: 'bin', component: () => import('@/views/BinView.vue'), meta: { title: '剪辑箱', chip: 'bin' } },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('@/views/DevicesView.vue'),
    meta: { title: '设备', chip: 'devices' },
  },
  feed('/trash', 'trash', 'trash', 'trash', { title: '最近删除' }),

  {
    path: '/cinema',
    name: 'cinema',
    component: () => import('@/views/CinemaView.vue'),
    meta: { title: '影视', chip: 'cinema', search: 'cinema' },
  },
  {
    path: '/cinema/:id',
    name: 'cinema-title',
    component: () => import('@/views/CinemaTitleView.vue'),
    meta: { title: '影视', chip: 'cinema', search: 'cinema' },
  },

  { path: '/:pathMatch(.*)*', redirect: '/gallery' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  // 滚动位置由 composables/scrollMemory.ts 统一管理（需要等分页数据补齐）
  scrollBehavior: () => false,
})
