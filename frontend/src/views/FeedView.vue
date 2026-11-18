<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api'
import type { Asset, Library } from '@/types'
import type { FeedConfig, FeedKind } from '@/stores/feed'
import { useFeedStore } from '@/stores/feed'
import { useSelectionStore } from '@/stores/selection'
import { useChromeStore } from '@/stores/chrome'
import { usePeopleStore } from '@/stores/people'
import { useFoldersStore } from '@/stores/folders'
import { useAssetActions } from '@/composables/useAssetActions'
import { usePhotoQuery } from '@/composables/usePhotoQuery'
import { isGroupMode } from '@/utils/date'
import { readScroll, restoreScroll, scrollKey } from '@/composables/scrollMemory'
import PhotoCell from '@/components/PhotoCell.vue'
import PhotoLightbox from '@/components/PhotoLightbox.vue'
import TimelineScrubber from '@/components/TimelineScrubber.vue'
import PersonCoverHeader from '@/components/PersonCoverHeader.vue'
import { setAssetDragData } from '@/utils/dnd'

/**
 * draggable：格子能否整格拖拽。只有素材库（有文件夹落点）传 true，
 * 拖起手时带上当前选中的资产 id（utils/dnd.ts 的站内协议）。
 * kind：覆盖路由 meta.feed 的流类型。图库 / 素材库的同一条路由要在
 * 「未整理」和「按时间（整个库）」两种流之间切换，见 LibraryView。
 */
const props = defineProps<{ draggable?: boolean; kind?: FeedKind }>()

const route = useRoute()
const feed = useFeedStore()
const selection = useSelectionStore()
const chrome = useChromeStore()
const people = usePeopleStore()
const folders = useFoldersStore()
const actions = useAssetActions()
const photo = usePhotoQuery()

const sentinel = ref<HTMLElement | null>(null)
/** 深链进来的资产可能不在当前分页里，单独取一份 */
const orphan = ref<Asset | null>(null)
let observer: IntersectionObserver | null = null
/** Shift 范围选择的起点（最后普通点过的那格） */
const anchorId = ref<string | null>(null)

const cfg = computed<FeedConfig>(() => ({
  kind: props.kind ?? (route.meta.feed as FeedKind) ?? 'gallery',
  library: (route.meta.library as Library) ?? 'gallery',
  tag: typeof route.params.tag === 'string' ? route.params.tag : null,
  personId: typeof route.params.id === 'string' && route.meta.feed === 'person'
    ? route.params.id
    : null,
  folderId: typeof route.params.id === 'string' && route.meta.feed === 'folder'
    ? route.params.id
    : null,
  q: typeof route.query.q === 'string' ? route.query.q : '',
  kindFilter: typeof route.query.kind === 'string' ? route.query.kind : '',
  dur: typeof route.query.dur === 'string' ? route.query.dur : '',
  px: typeof route.query.px === 'string' ? route.query.px : '',
  group: isGroupMode(route.query.group) ? route.query.group : 'day',
  device: typeof route.query.device === 'string' ? route.query.device : '',
}))

const cfgKey = computed(() => JSON.stringify(cfg.value))

const emptyText = computed(() => {
  const { kind, library } = cfg.value
  if (kind === 'unfiled') {
    // 空态文案按库区分：图库是"导入进来"，素材库是"从图库移进来"
    if (library === 'gallery') {
      return folders.folders.length
        ? '没有未整理的照片。\n所有照片都已归入文件夹，点上面的文件夹查看。'
        : '图库是空的。\n点右上角「导入」把照片加进来；「＋ 新建」可以先建文件夹归类。'
    }
    return folders.folders.length
      ? '没有未整理的素材。\n所有素材都已归入文件夹，点上面的文件夹查看。'
      : '素材库是空的。\n先建一个文件夹（右上「＋ 新建」），到「图库」勾选素材 → 底部「素材库」移进来，\n再选中素材点底部「文件夹」归档。'
  }
  if (kind === 'folder') {
    return '这个文件夹是空的。\n回到「素材库」选中素材，点底部「文件夹」移进来。'
  }
  if (kind === 'material') {
    return '素材库是空的。\n到「图库」点右上角「选择」→ 勾选素材 → 底部「素材库」，就能把资源移进来。\n移进来的素材保留 AI 标签、人脸和视频摘要。'
  }
  if (kind === 'trash') return '回收站是空的。'
  if (kind === 'favorites') return '还没有喜欢的照片。'
  if (kind === 'tag') return `「${cfg.value.tag}」下还没有照片。`
  if (kind === 'person') return '这个人名下还没有照片。'
  if (cfg.value.device === 'unknown') return '没有未识别设备的照片。'
  if (cfg.value.device) return '这台设备名下还没有照片。\n设备名可以在「📷 设备」页里重命名。'
  if (cfg.value.q) return `没有匹配「${cfg.value.q}」的内容。`
  return '图库是空的。点右上角「导入」，或者把图片 / 视频拖进窗口。'
})

function syncTitle() {
  const { kind, tag, personId, library } = cfg.value
  let title =
    kind === 'favorites'
      ? '喜欢'
      : kind === 'trash'
        ? '最近删除'
        : kind === 'tag'
          ? tag || '标签'
          : kind === 'person'
            ? people.nameOf(personId) || '人物'
            : kind === 'folder'
              ? folders.nameOf(cfg.value.folderId) || '文件夹'
              : kind === 'material' || (kind === 'unfiled' && library === 'material')
                ? '素材库'
                : '图库'
  if ((kind === 'favorites' || kind === 'tag') && library === 'material') title += ' · 素材库'
  chrome.set({ title })
}

async function load() {
  anchorId.value = null // 换了视图，Shift 的范围起点作废
  syncTitle()
  if (cfg.value.kind === 'person' && cfg.value.personId) {
    if (!people.nameOf(cfg.value.personId)) {
      void people.load().then(() => syncTitle())
    }
  }
  if (cfg.value.kind === 'folder' && cfg.value.folderId) {
    // 文件夹名字是异步到的：先显示「文件夹」，列表到了再替换
    if (!folders.nameOf(cfg.value.folderId)) {
      void folders.load(cfg.value.library).then(() => syncTitle())
    }
  }
  if (cfg.value.kind === 'unfiled') {
    // 文件夹视图的空态文案依赖当前库的文件夹列表（图库 / 素材库各一套）
    void folders.load(cfg.value.library)
  }
  const target = readScroll(scrollKey(route))
  await feed.open(cfg.value)
  selection.setScope(feed.items.map(a => a.id))
  chrome.set({ count: feed.total ? `${feed.total} 项` : '' })
  if (target > 0) {
    // 刷新时列表只有第一页，先补齐到原来的高度，滚动恢复才落得下去
    await feed.expandToHeight(target)
    selection.setScope(feed.items.map(a => a.id))
  }
  await restoreScroll(scrollKey(route))
  await ensurePhoto()
}

watch(cfgKey, () => void load(), { immediate: true })
watch(
  () => feed.revision,
  () => void load(),
)
watch(
  () => feed.items.length,
  () => {
    selection.setScope(feed.items.map(a => a.id))
    // 每页加载完都补一次：页面还不到一屏就继续拉下一页
    fillViewport()
  },
)

/* ---------------- 灯箱 ---------------- */

const assetIndex = computed(() => feed.items.findIndex(a => a.id === photo.photoId.value))
const currentAsset = computed<Asset | null>(() =>
  assetIndex.value >= 0 ? feed.items[assetIndex.value] : orphan.value,
)

async function ensurePhoto() {
  const id = photo.photoId.value
  if (!id) {
    orphan.value = null
    return
  }
  if (feed.items.some(a => a.id === id)) {
    orphan.value = null
    return
  }
  if (!feed.ready) return
  try {
    orphan.value = await api.asset(id)
  } catch {
    orphan.value = null
    photo.close()
  }
}

watch(
  () => photo.photoId.value,
  () => void ensurePhoto(),
)

function onActivate(asset: Asset, event?: MouseEvent) {
  // 键盘多选：Ctrl/Cmd 点选加减，Shift 点选圈一段（桌面文件管理器的习惯）。
  // 触屏 / 普通点击走老路：选择模式里是切换，否则开灯箱。
  const items = feed.items
  const idx = items.findIndex(a => a.id === asset.id)
  if (event?.shiftKey && idx >= 0) {
    if (!selection.active) selection.start()
    const from = anchorId.value ? items.findIndex(a => a.id === anchorId.value) : -1
    const lo = from >= 0 ? Math.min(from, idx) : 0
    const hi = from >= 0 ? Math.max(from, idx) : idx
    for (let i = lo; i <= hi; i++) selection.select(items[i].id)
    anchorId.value = asset.id
    return
  }
  if ((event?.ctrlKey || event?.metaKey) && idx >= 0) {
    if (!selection.active) selection.start()
    selection.toggle(asset.id)
    anchorId.value = asset.id
    return
  }
  anchorId.value = asset.id
  if (selection.active) selection.toggle(asset.id)
  else photo.open(asset.id)
}

/** 拖拽起手：拖的是选中成员就带整个选中集，否则只带这一格 */
function onCellDragStart(event: DragEvent, asset: Asset) {
  if (!props.draggable) return
  const ids = selection.active && selection.isSelected(asset.id) ? selection.list() : [asset.id]
  setAssetDragData(event, ids)
}

function stepPhoto(delta: number) {
  const list = feed.items
  const i = list.findIndex(a => a.id === photo.photoId.value)
  const next = list[i + delta]
  if (next) photo.step(next.id)
}

function neighbour(direction: 1 | -1): Asset | null {
  const id = photo.photoId.value
  const list = feed.items
  const i = list.findIndex(a => a.id === id)
  if (i < 0) return null
  return list[i + direction] ?? null
}

async function onTrash() {
  const id = photo.photoId.value
  if (!id) return
  const next = neighbour(1) ?? neighbour(-1)
  await actions.trash([id])
  if (next) photo.step(next.id)
  else photo.close()
}

async function onRestore() {
  const id = photo.photoId.value
  if (!id) return
  const next = neighbour(1) ?? neighbour(-1)
  await actions.restore([id])
  if (next) photo.step(next.id)
  else photo.close()
}

async function onPurge() {
  const id = photo.photoId.value
  if (!id) return
  const next = neighbour(1) ?? neighbour(-1)
  await actions.purge([id])
  if (feed.items.some(a => a.id === id)) return // 用户在确认框里取消了
  if (next) photo.step(next.id)
  else photo.close()
}

/* ---------------- 无限滚动 ---------------- */

/**
 * 兜底：分页太靠后时，哨兵可能一进页面就落在 rootMargin 里，
 * IntersectionObserver 没有"从外面进入"的状态变化，就永远不会触发 loadMore。
 * 按年/月分组时页面矮，最容易撞上：只加载一屏就再也滚不出新内容。
 * 所以每页合并后都主动检查一次，哨兵还在附近就继续拉，直到填满视口。
 * 用 rAF 等这一帧的 DOM 真的长出来再量，否则量到的是旧高度、会多拉一页。
 */
let fillFrame = 0

function fillViewport() {
  if (fillFrame) return
  fillFrame = requestAnimationFrame(() => {
    fillFrame = 0
    const el = sentinel.value
    if (!el || feed.loading || feed.done || feed.error) return
    if (el.getBoundingClientRect().top > window.innerHeight + 900) return
    void feed.loadMore()
  })
}

onMounted(() => {
  observer = new IntersectionObserver(
    entries => {
      if (entries[0]?.isIntersecting) void feed.loadMore()
    },
    { rootMargin: '900px' },
  )
  if (sentinel.value) observer.observe(sentinel.value)
  document.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', fillViewport)
  fillViewport()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  selection.stop()
  document.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', fillViewport)
  if (fillFrame) cancelAnimationFrame(fillFrame)
})

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && selection.active) selection.stop()
}
</script>

<template>
  <main class="pb-[120px]">
    <PersonCoverHeader v-if="cfg.kind === 'person' && cfg.personId" :person-id="cfg.personId" />

    <div
      v-for="section in feed.sections"
      :key="section.key"
      class="pb-1"
      data-section
      :data-key="section.key"
    >
      <h2 class="day-head">{{ section.label }}</h2>
      <div class="photo-grid">
        <PhotoCell
          v-for="asset in section.items"
          :key="asset.id"
          :asset="asset"
          :selecting="selection.active"
          :selected="selection.isSelected(asset.id)"
          :draggable="draggable"
          @activate="onActivate(asset, $event)"
          @dragstart="onCellDragStart($event, asset)"
        />
      </div>
    </div>

    <div v-if="feed.isEmpty" class="whitespace-pre-line px-5 py-20 text-center text-[15px] leading-relaxed text-mut">
      {{ emptyText }}
    </div>

    <div
      v-if="feed.error && !feed.items.length"
      class="px-5 py-20 text-center text-[14px] leading-relaxed text-mut"
    >
      加载失败：{{ feed.error.slice(0, 120) }}
      <div class="mt-4">
        <button class="rounded-lg bg-panel px-4 py-2 text-acc" @click="load()">重试</button>
      </div>
    </div>

    <div v-if="feed.loading" class="spinner" />
    <div v-else-if="feed.done && feed.items.length" class="py-6 text-center text-[12px] text-mut">
      共 {{ feed.total }} 项
    </div>

    <div ref="sentinel" class="h-2" />

    <TimelineScrubber />

    <PhotoLightbox
      v-if="currentAsset"
      :asset="currentAsset"
      :items="feed.items"
      :trash-view="cfg.kind === 'trash'"
      @close="photo.close()"
      @prev="stepPhoto(-1)"
      @next="stepPhoto(1)"
      @trash="onTrash"
      @restore="onRestore"
      @purge="onPurge"
    />
  </main>
</template>
