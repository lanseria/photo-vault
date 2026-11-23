import { defineStore } from 'pinia'
import { computed, nextTick, ref } from 'vue'
import { api, errorText, type AssetQuery } from '@/api'
import type { Asset, Library } from '@/types'
import { groupHeader, groupKey, type GroupMode } from '@/utils/date'
import { useToastStore } from './toast'

export type FeedKind = 'gallery' | 'material' | 'unfiled' | 'folder' | 'favorites' | 'tag' | 'person' | 'trash'

export interface FeedConfig {
  kind: FeedKind
  library: Library
  tag?: string | null
  personId?: string | null
  /** kind='folder' 时的素材库文件夹 id */
  folderId?: string | null
  q?: string
  /** '' | 'video' | 'photo' */
  kindFilter?: string
  /** '' | '0-15' | '15-60' | '60-300' | '300-' */
  dur?: string
  /** '' | '1080' | '2160' */
  px?: string
  /** 时间轴分组粒度，默认按日 */
  group?: GroupMode
  /** 设备筛选：'' = 全部，'unknown' = 未识别，其余为设备 id 字符串 */
  device?: string
  /** 「展示隐藏」全局开关：true = 隐藏项混入显示（默认排除） */
  showHidden?: boolean
}

export interface TimeSection {
  key: string
  label: string
  items: Asset[]
}

const PAGE_SIZE = 120

/**
 * 照片流的状态机。
 *
 * 唯一的数据来源是 URL：视图类型来自路由，搜索/筛选来自 query。
 * 这样刷新、前进后退、分享链接都是同一套逻辑，不需要在 JS 里另存一份。
 */
export const useFeedStore = defineStore('feed', () => {
  const config = ref<FeedConfig>({ kind: 'gallery', library: 'gallery', q: '', kindFilter: '', dur: '', px: '', group: 'day' })
  const items = ref<Asset[]>([])
  const total = ref(0)
  const loading = ref(false)
  const ready = ref(false)
  const error = ref('')
  /** 上传等外部写入后 +1，视图看到就把列表重新拉一遍 */
  const revision = ref(0)

  let seq = 0
  let pending = 0

  /** 按当前分组粒度（日 / 周 / 月 / 年）把已加载的资产切段；顺序即渲染顺序 */
  const sections = computed<TimeSection[]>(() => {
    const mode = config.value.group ?? 'day'
    const out: TimeSection[] = []
    let current: TimeSection | null = null
    for (const asset of items.value) {
      const key = groupKey(asset, mode)
      if (!current || current.key !== key) {
        current = { key, label: groupHeader(key, mode), items: [] }
        out.push(current)
      }
      current.items.push(asset)
    }
    return out
  })

  const done = computed(() => total.value > 0 && items.value.length >= total.value)
  const isEmpty = computed(() => ready.value && !loading.value && items.value.length === 0)

  function buildParams(cfg: FeedConfig, offset: number): AssetQuery {
    const params: AssetQuery = { limit: PAGE_SIZE, offset }
    if (cfg.kind === 'favorites') params.favorite = true
    if (cfg.kind === 'trash') params.trash = true
    if (cfg.kind === 'tag' && cfg.tag) params.tag = cfg.tag
    if (cfg.kind === 'person' && cfg.personId) params.person_id = cfg.personId
    // 文件夹视图：'unfiled' = 素材库里未整理的，'folder' = 某个文件夹内的
    if (cfg.kind === 'unfiled') params.folder = 'none'
    if (cfg.kind === 'folder' && cfg.folderId) params.folder = cfg.folderId
    // 素材库是 assets 表上的另一个 library 轴；收藏/标签跟着当前所在的库走，
    // 人物和回收站是跨库视图，不加 library 条件。
    if (!['trash', 'person'].includes(cfg.kind)) params.library = cfg.library
    if (cfg.q) params.q = cfg.q
    if (cfg.kindFilter) {
      // 剪辑视角的「视频」含 Live Photo：有视频流，能剪
      params.kind = cfg.kindFilter === 'video' ? 'video,live_photo' : 'photo,raw'
    }
    if (cfg.dur) {
      const [min, max] = cfg.dur.split('-')
      if (min) params.min_sec = Number(min)
      if (max) params.max_sec = Number(max)
    }
    if (cfg.px) params.min_px = Number(cfg.px)
    if (cfg.device) params.device = cfg.device
    if (cfg.showHidden) params.show_hidden = true
    return params
  }

  function merge(itemsToAdd: Asset[], count: number) {
    const seen = new Set(items.value.map(a => a.id))
    for (const asset of itemsToAdd) {
      if (seen.has(asset.id)) continue
      seen.add(asset.id)
      items.value.push(asset)
    }
    total.value = count
  }

  async function loadPage(reset: boolean, pageSize = PAGE_SIZE): Promise<void> {
    if (!reset && pending > 0) return
    if (!reset && ready.value && total.value === 0) return
    if (!reset && total.value > 0 && items.value.length >= total.value) return

    const mySeq = ++seq
    if (reset) {
      items.value = []
      total.value = 0
      ready.value = false
      error.value = ''
    }
    pending += 1
    loading.value = true
    try {
      const params = buildParams(config.value, reset ? 0 : items.value.length)
      params.limit = pageSize
      const page = await api.assets(params)
      if (mySeq !== seq) return
      merge(page.items, page.total)
      ready.value = true
    } catch (e) {
      if (mySeq !== seq) return
      error.value = errorText(e)
      useToastStore().show('加载失败：' + errorText(e).slice(0, 60))
    } finally {
      pending = Math.max(0, pending - 1)
      loading.value = pending > 0
    }
  }

  /** 打开（或切换）一个视图；相同配置重复调用也会重新拉取。 */
  async function open(cfg: FeedConfig) {
    config.value = { ...cfg }
    await loadPage(true)
  }

  function loadMore() {
    return loadPage(false)
  }

  /**
   * 刷新后恢复滚动位置用：内容还不够高时按页补齐。
   * 网格里每格是定高的（aspect-square），所以高度可以提前算出来。
   */
  async function expandToHeight(targetY: number, maxPages = 12) {
    for (let i = 0; i < maxPages; i++) {
      if (typeof window === 'undefined') return
      if (done.value) return
      if (document.documentElement.scrollHeight >= targetY + window.innerHeight) return
      const before = items.value.length
      await loadPage(false)
      await nextTick()
      if (items.value.length === before) return
    }
  }

  /**
   * 日期跳转用：补齐到第 index 项可见为止（index 来自日期分布的偏移计算）。
   * 跳转可能要一次补很多页，用 300/页（接口上限）减少往返。
   * loadPage 对并发会让路（哨兵的 IntersectionObserver 可能同时触发 loadMore），
   * 所以空转一圈时先等在途的页落地再判断，连续几轮真没长出来才放弃。
   */
  async function expandToIndex(index: number, maxPages = 200) {
    let stalled = 0
    for (let i = 0; i < maxPages; i++) {
      if (done.value || items.value.length > index) return
      const before = items.value.length
      await loadPage(false, 300)
      while (loading.value) await new Promise(r => setTimeout(r, 80))
      await nextTick()
      if (items.value.length === before) {
        stalled += 1
        if (stalled >= 3) return
        continue
      }
      stalled = 0
    }
  }

  /** 当前筛选下「哪天有几张」的日期分布，日期跳转面板用。 */
  function loadDates() {
    return api.dates(buildParams(config.value, 0))
  }

  /** 按 id 替换字段；灯箱和网格共享同一批响应式对象。 */
  function patch(id: string, fields: Partial<Asset>) {
    const asset = items.value.find(a => a.id === id)
    if (!asset) return
    Object.assign(asset, fields)
  }

  function toggleFavorite(id: string, favorite: boolean) {
    const asset = items.value.find(a => a.id === id)
    if (asset) asset.favorite = favorite
  }

  function removeMany(ids: string[]) {
    const gone = new Set(ids)
    const before = items.value.length
    items.value = items.value.filter(a => !gone.has(a.id))
    total.value = Math.max(0, total.value - (before - items.value.length))
  }

  function setTags(id: string, tags: string[]) {
    const asset = items.value.find(a => a.id === id)
    if (asset) asset.tags = tags
  }

  function requestReload() {
    revision.value += 1
  }

  return {
    config,
    items,
    total,
    loading,
    ready,
    error,
    revision,
    sections,
    done,
    isEmpty,
    open,
    loadMore,
    expandToHeight,
    expandToIndex,
    loadDates,
    patch,
    toggleFavorite,
    removeMany,
    setTags,
    requestReload,
  }
})
