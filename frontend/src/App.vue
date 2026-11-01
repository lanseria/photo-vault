<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useScrollMemory } from '@/composables/scrollMemory'
import { useChromeStore } from '@/stores/chrome'
import { useBinStore } from '@/stores/bin'
import { useSelectionStore } from '@/stores/selection'
import { useSystemStore } from '@/stores/system'
import { useToastStore } from '@/stores/toast'
import { useUploadStore } from '@/stores/upload'
import { filesFromDrop } from '@/utils/files'
import AppHeader from '@/components/AppHeader.vue'
import NavChips from '@/components/NavChips.vue'
import FilterBar from '@/components/FilterBar.vue'
import SelectionBar from '@/components/SelectionBar.vue'
import TagSheet from '@/components/TagSheet.vue'
import UploadDialog from '@/components/UploadDialog.vue'
import DropOverlay from '@/components/DropOverlay.vue'
import ToastHost from '@/components/ToastHost.vue'
import DialogHost from '@/components/DialogHost.vue'

const route = useRoute()
const system = useSystemStore()
const bin = useBinStore()
const selection = useSelectionStore()
const upload = useUploadStore()
const toast = useToastStore()
const chrome = useChromeStore()

useScrollMemory()

const dropLibrary = computed<'gallery' | 'material'>(() =>
  route.meta.library === 'material' ? 'material' : 'gallery',
)

const dragging = ref(false)

// 浏览器标签页标题跟着视图走，历史记录里也看得出是哪一页
watch(
  () => chrome.title || (route.meta.title as string) || '',
  title => {
    document.title = title ? `${title} · Photo Vault` : 'Photo Vault'
  },
  { immediate: true },
)

let dragDepth = 0

function hasFiles(event: DragEvent): boolean {
  const types = event.dataTransfer?.types
  return !!types && Array.from(types).includes('Files')
}

function onDragEnter(event: DragEvent) {
  if (!hasFiles(event)) return
  event.preventDefault()
  dragDepth += 1
  dragging.value = true
}

function onDragOver(event: DragEvent) {
  if (!hasFiles(event)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}

function onDragLeave() {
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dragging.value = false
}

async function onDrop(event: DragEvent) {
  event.preventDefault()
  dragDepth = 0
  dragging.value = false
  if (!hasFiles(event)) return
  const files = await filesFromDrop(event.dataTransfer)
  if (files.length) upload.addFiles(files, dropLibrary.value)
  else toast.show('没有可导入的图片或视频')
}

/* ---- 顶部固定区：标题 + 标签页 + 筛选器，滚动时只有时间轴动 ---- */

const dock = ref<HTMLElement | null>(null)
const scrolled = ref(false)

let resizeObserver: ResizeObserver | null = null

function syncScrolled() {
  scrolled.value = window.scrollY > 4
}

onMounted(() => {
  void system.refreshStats()
  // 箱子是懒创建的：第一次打开页面就把默认剪辑箱准备好，点「剪辑箱」不用等
  void bin
    .ensure()
    .then(() => bin.refreshSummary())
    .catch(() => undefined)

  window.addEventListener('dragenter', onDragEnter)
  window.addEventListener('dragover', onDragOver)
  window.addEventListener('dragleave', onDragLeave)
  window.addEventListener('drop', onDrop)
  window.addEventListener('scroll', syncScrolled, { passive: true })
  syncScrolled()

  // 日期头条要停在固定区下方，把它的高度同步成 CSS 变量（搜索框出现/消失也会跟着变）
  const el = dock.value
  if (el) {
    const applyDockHeight = () =>
      document.documentElement.style.setProperty('--dock-h', `${Math.round(el.offsetHeight)}px`)
    applyDockHeight()
    resizeObserver = new ResizeObserver(applyDockHeight)
    resizeObserver.observe(el)
  }

  if (location.hash === '#upload') upload.openDialog(dropLibrary.value)
})

onBeforeUnmount(() => {
  window.removeEventListener('dragenter', onDragEnter)
  window.removeEventListener('dragover', onDragOver)
  window.removeEventListener('dragleave', onDragLeave)
  window.removeEventListener('drop', onDrop)
  window.removeEventListener('scroll', syncScrolled)
  resizeObserver?.disconnect()
  resizeObserver = null
  document.documentElement.style.removeProperty('--dock-h')
})
</script>

<template>
  <div class="min-h-screen bg-bg">
    <div
      ref="dock"
      class="sticky top-0 z-40 border-b bg-[rgba(0,0,0,.82)] backdrop-blur-[14px]"
      :class="scrolled ? 'border-line/70' : 'border-transparent'"
    >
      <AppHeader />
      <NavChips />
      <FilterBar v-if="route.meta.filters" />
    </div>
    <RouterView />
    <SelectionBar v-if="selection.active && route.meta.selectable" />
    <TagSheet />
    <UploadDialog />
    <DropOverlay :visible="dragging" :library="dropLibrary" />
    <ToastHost />
    <DialogHost />
  </div>
</template>
