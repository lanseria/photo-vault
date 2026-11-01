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

  if (location.hash === '#upload') upload.openDialog(dropLibrary.value)
})

onBeforeUnmount(() => {
  window.removeEventListener('dragenter', onDragEnter)
  window.removeEventListener('dragover', onDragOver)
  window.removeEventListener('dragleave', onDragLeave)
  window.removeEventListener('drop', onDrop)
})
</script>

<template>
  <div class="min-h-screen bg-bg">
    <AppHeader />
    <NavChips />
    <FilterBar v-if="route.meta.filters" />
    <RouterView />
    <SelectionBar v-if="selection.active && route.meta.selectable" />
    <TagSheet />
    <UploadDialog />
    <DropOverlay :visible="dragging" :library="dropLibrary" />
    <ToastHost />
    <DialogHost />
  </div>
</template>
