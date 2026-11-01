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
import { useAssetActions } from '@/composables/useAssetActions'
import { usePhotoQuery } from '@/composables/usePhotoQuery'
import { readScroll, restoreScroll, scrollKey } from '@/composables/scrollMemory'
import PhotoCell from '@/components/PhotoCell.vue'
import PhotoLightbox from '@/components/PhotoLightbox.vue'

const route = useRoute()
const feed = useFeedStore()
const selection = useSelectionStore()
const chrome = useChromeStore()
const people = usePeopleStore()
const actions = useAssetActions()
const photo = usePhotoQuery()

const sentinel = ref<HTMLElement | null>(null)
/** 深链进来的资产可能不在当前分页里，单独取一份 */
const orphan = ref<Asset | null>(null)
let observer: IntersectionObserver | null = null

const cfg = computed<FeedConfig>(() => ({
  kind: (route.meta.feed as FeedKind) ?? 'gallery',
  library: (route.meta.library as Library) ?? 'gallery',
  tag: typeof route.params.tag === 'string' ? route.params.tag : null,
  personId: typeof route.params.id === 'string' ? route.params.id : null,
  q: typeof route.query.q === 'string' ? route.query.q : '',
  kindFilter: typeof route.query.kind === 'string' ? route.query.kind : '',
  dur: typeof route.query.dur === 'string' ? route.query.dur : '',
  px: typeof route.query.px === 'string' ? route.query.px : '',
}))

const cfgKey = computed(() => JSON.stringify(cfg.value))

const emptyText = computed(() => {
  const kind = cfg.value.kind
  if (kind === 'material') {
    return '素材库是空的。\n到「图库」点右上角「选择」→ 勾选素材 → 底部「素材库」，就能把资源移进来。\n移进来的素材保留 AI 标签、人脸和视频摘要。'
  }
  if (kind === 'trash') return '回收站是空的。'
  if (kind === 'favorites') return '还没有喜欢的照片。'
  if (kind === 'tag') return `「${cfg.value.tag}」下还没有照片。`
  if (kind === 'person') return '这个人名下还没有照片。'
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
            : kind === 'material'
              ? '素材库'
              : '图库'
  if ((kind === 'favorites' || kind === 'tag') && library === 'material') title += ' · 素材库'
  chrome.set({ title })
}

async function load() {
  syncTitle()
  if (cfg.value.kind === 'person' && cfg.value.personId) {
    if (!people.nameOf(cfg.value.personId)) {
      void people.load().then(() => syncTitle())
    }
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
  () => selection.setScope(feed.items.map(a => a.id)),
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

function onActivate(asset: Asset) {
  if (selection.active) selection.toggle(asset.id)
  else photo.open(asset.id)
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

onMounted(() => {
  observer = new IntersectionObserver(
    entries => {
      if (entries[0]?.isIntersecting) void feed.loadMore()
    },
    { rootMargin: '900px' },
  )
  if (sentinel.value) observer.observe(sentinel.value)
  document.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  selection.stop()
  document.removeEventListener('keydown', onKeydown)
})

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && selection.active) selection.stop()
}
</script>

<template>
  <main class="pb-[120px]">
    <div
      v-for="section in feed.sections"
      :key="section.key"
      class="pb-1"
    >
      <h2 class="day-head">{{ section.label }}</h2>
      <div class="photo-grid">
        <PhotoCell
          v-for="asset in section.items"
          :key="asset.id"
          :asset="asset"
          :selecting="selection.active"
          :selected="selection.isSelected(asset.id)"
          @activate="onActivate(asset)"
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
