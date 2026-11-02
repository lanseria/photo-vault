<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useScrollMemory } from '@/composables/scrollMemory'
import { useChromeStore } from '@/stores/chrome'
import { useBinStore } from '@/stores/bin'
import { useSelectionStore } from '@/stores/selection'
import { useSystemStore } from '@/stores/system'
import { useUploadStore } from '@/stores/upload'
import AppHeader from '@/components/AppHeader.vue'
import NavChips from '@/components/NavChips.vue'
import FilterBar from '@/components/FilterBar.vue'
import SelectionBar from '@/components/SelectionBar.vue'
import TagSheet from '@/components/TagSheet.vue'
import UploadDialog from '@/components/UploadDialog.vue'
import ToastHost from '@/components/ToastHost.vue'
import DialogHost from '@/components/DialogHost.vue'

const route = useRoute()
const system = useSystemStore()
const bin = useBinStore()
const selection = useSelectionStore()
const upload = useUploadStore()
const chrome = useChromeStore()

useScrollMemory()

const dropLibrary = computed<'gallery' | 'material'>(() =>
  route.meta.library === 'material' ? 'material' : 'gallery',
)

/* ---- 拖拽 ----
   拖文件进窗口直接导入的功能已取消：导入一律走右上角「导入」按钮。
   这里只拦住浏览器默认行为 —— 不拦的话，文件落进窗口会整页跳到那个文件。
   站内的「照片拖进素材库文件夹」是另一套 dataTransfer 类型（utils/dnd.ts），不受影响。 */
function guardFileDrop(event: DragEvent) {
  if (event.dataTransfer?.types.includes('Files')) event.preventDefault()
}

// 浏览器标签页标题跟着视图走，历史记录里也看得出是哪一页
watch(
  () => chrome.title || (route.meta.title as string) || '',
  title => {
    document.title = title ? `${title} · Photo Vault` : 'Photo Vault'
  },
  { immediate: true },
)

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

  window.addEventListener('dragover', guardFileDrop)
  window.addEventListener('drop', guardFileDrop)
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
  window.removeEventListener('dragover', guardFileDrop)
  window.removeEventListener('drop', guardFileDrop)
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
    <ToastHost />
    <DialogHost />
  </div>
</template>
