import { defineStore } from 'pinia'
import { computed, nextTick, ref } from 'vue'
import { api, errorText, type AssetQuery } from '@/api'
import type { Asset, Library } from '@/types'
import { dateHeader, dateKey } from '@/utils/date'
import { useToastStore } from './toast'

export type FeedKind = 'gallery' | 'material' | 'favorites' | 'tag' | 'person' | 'trash'

export interface FeedConfig {
  kind: FeedKind
  library: Library
  tag?: string | null
  personId?: string | null
  q?: string
  /** '' | 'video' | 'photo' */
  kindFilter?: string
  /** '' | '0-15' | '15-60' | '60-300' | '300-' */
  dur?: string
  /** '' | '1080' | '2160' */
  px?: string
}

export interface DaySection {
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
  const config = ref<FeedConfig>({ kind: 'gallery', library: 'gallery', q: '', kindFilter: '', dur: '', px: '' })
  const items = ref<Asset[]>([])
  const total = ref(0)
  const loading = ref(false)
  const ready = ref(false)
  const error = ref('')
  /** 上传等外部写入后 +1，视图看到就把列表重新拉一遍 */
  const revision = ref(0)

  let seq = 0
  let pending = 0

  const sections = computed<DaySection[]>(() => {
    const out: DaySection[] = []
    let current: DaySection | null = null
    for (const asset of items.value) {
      const key = dateKey(asset)
      if (!current || current.key !== key) {
        current = { key, label: dateHeader(key), items: [] }
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

  async function loadPage(reset: boolean): Promise<void> {
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
      const page = await api.assets(buildParams(config.value, reset ? 0 : items.value.length))
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
    patch,
    toggleFavorite,
    removeMany,
    setTags,
    requestReload,
  }
})
