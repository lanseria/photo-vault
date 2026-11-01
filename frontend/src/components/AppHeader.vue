<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { Library } from '@/types'
import { useChromeStore } from '@/stores/chrome'
import { useSelectionStore } from '@/stores/selection'
import { useUploadStore } from '@/stores/upload'
import PersonActions from './PersonActions.vue'

const route = useRoute()
const router = useRouter()
const chrome = useChromeStore()
const selection = useSelectionStore()
const upload = useUploadStore()

const title = computed(() => chrome.title || (route.meta.title as string) || '图库')
const showSearch = computed(() => route.meta.search === 'assets' || route.meta.search === 'cinema')
const isPerson = computed(() => route.name === 'person')
const canSelect = computed(() => route.meta.selectable === true)

const text = ref(typeof route.query.q === 'string' ? route.query.q : '')
let timer: ReturnType<typeof setTimeout> | undefined

watch(
  () => route.query.q,
  value => {
    const next = typeof value === 'string' ? value : ''
    if (next !== text.value) text.value = next
  },
)

function applySearch() {
  const q = text.value.trim()
  const current = typeof route.query.q === 'string' ? route.query.q : ''
  if (q === current) return
  const query = { ...route.query }
  if (q) query.q = q
  else delete query.q
  void router.replace({ path: route.path, query })
}

function onInput() {
  clearTimeout(timer)
  timer = setTimeout(applySearch, 250)
}

function onEnter() {
  clearTimeout(timer)
  applySearch()
  ;(document.activeElement as HTMLElement | null)?.blur()
}

onBeforeUnmount(() => clearTimeout(timer))

function defaultLibrary(): Library {
  return route.meta.library === 'material' ? 'material' : 'gallery'
}

function toggleSelect() {
  if (selection.active) selection.stop()
  else selection.start()
}
</script>

<template>
  <header class="flex items-center gap-3 px-4 pt-3.5 pb-1.5 max-sm:gap-2 max-sm:px-3 max-sm:pt-3">
    <h1 class="flex-1 min-w-0 m-0 text-[26px] font-bold tracking-[.2px] truncate max-sm:text-[22px]">
      {{ title }}
    </h1>

    <input
      v-if="showSearch"
      v-model="text"
      type="search"
      :placeholder="chrome.placeholder"
      class="w-[210px] max-sm:w-[104px] bg-panel rounded-[10px] text-fg px-3 py-2 text-[14px] outline-none max-sm:px-2.5 max-sm:py-[7px]"
      @input="onInput"
      @keydown.enter="onEnter"
    />

    <PersonActions v-if="isPerson" />

    <button v-if="selection.active && canSelect" class="txt-btn" @click="selection.selectAll()">
      {{ selection.scope.length && selection.ids.size === selection.scope.length ? '取消全选' : '全选' }}
    </button>
    <button class="txt-btn" @click="upload.openDialog(defaultLibrary())">导入</button>
    <button v-if="canSelect" class="txt-btn" @click="toggleSelect">
      {{ selection.active ? '取消' : '选择' }}
    </button>
  </header>
</template>
