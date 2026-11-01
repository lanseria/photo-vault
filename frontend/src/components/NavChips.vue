<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBinStore } from '@/stores/bin'
import { useChromeStore } from '@/stores/chrome'
import { useSystemStore } from '@/stores/system'
import { useTagsStore } from '@/stores/tags'

const route = useRoute()
const router = useRouter()
const system = useSystemStore()
const bin = useBinStore()
const tags = useTagsStore()
const chrome = useChromeStore()

const menuOpen = ref(false)
const menuLeft = ref(16)
const menuTop = ref(120)

const library = computed<'gallery' | 'material'>(() =>
  route.meta.library === 'material' ? 'material' : 'gallery',
)
const activeChip = computed(() => route.meta.chip as string | undefined)

function isActive(id: string) {
  if (activeChip.value === id) return true
  // 喜欢 / 标签是「库内的筛选」，同时点亮所在的库
  if ((activeChip.value === 'favorites' || activeChip.value === 'tag') && id === library.value) {
    return true
  }
  return false
}

const chips = computed(() => [
  { id: 'gallery', label: '图库', to: '/gallery', count: system.stats.gallery },
  { id: 'material', label: '🗂 素材库', to: '/material', count: system.stats.material },
  {
    id: 'favorites',
    label: '❤️',
    to: `/${library.value}/favorites`,
    count: system.stats.favorites,
  },
  { id: 'bin', label: '✂️ 剪辑箱', to: '/bin', count: bin.count },
  { id: 'people', label: '人物', to: '/people', count: system.stats.persons },
  { id: 'tag', label: '标签', to: null, count: null },
  { id: 'devices', label: '📷 设备', to: '/devices', count: system.stats.device_count },
  { id: 'cinema', label: '🎞 影视', to: '/cinema', count: system.stats.cinema_titles },
  { id: 'trash', label: '🗑', to: '/trash', count: system.stats.trash },
])

function onTagChip(event: MouseEvent) {
  if (activeChip.value === 'tag') {
    void router.push(`/${library.value}`)
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menuLeft.value = Math.max(8, Math.min(rect.left, window.innerWidth - 216))
  menuTop.value = rect.bottom + 6
  void tags.load()
  menuOpen.value = !menuOpen.value
}

function goTag(name: string) {
  menuOpen.value = false
  void router.push(`/${library.value}/tags/${encodeURIComponent(name)}`)
}

function onDocumentClick(event: MouseEvent) {
  const target = event.target as Element | null
  if (!target?.closest('[data-tag-nav]')) menuOpen.value = false
}

function closeMenu() {
  menuOpen.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  window.addEventListener('resize', closeMenu)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  window.removeEventListener('resize', closeMenu)
})
</script>

<template>
  <div class="flex items-center gap-2 px-4 pb-2.5 max-sm:px-3">
    <div class="no-scrollbar flex min-w-0 flex-1 items-center gap-2 overflow-x-auto">
      <template v-for="chip in chips" :key="chip.id">
        <button
          v-if="chip.to"
          class="chip tap shrink-0"
          :class="isActive(chip.id) && 'chip-on'"
          @click="router.push(chip.to)"
        >
          {{ chip.label }}
          <span v-if="chip.count" class="chip-count">{{ chip.count }}</span>
        </button>

        <div v-else data-tag-nav class="relative shrink-0">
          <button class="chip tap" :class="isActive('tag') && 'chip-on'" @click="onTagChip">
            <template v-if="activeChip === 'tag'">
              🏷 {{ route.params.tag }} <span class="opacity-60">✕</span>
            </template>
            <template v-else>标签</template>
          </button>
        </div>
      </template>
    </div>

    <span class="shrink-0 text-[12px] text-mut">{{ chrome.count }}</span>

    <Teleport to="body">
      <div
        v-if="menuOpen"
        data-tag-nav
        class="fixed z-[80] max-h-[300px] min-w-[200px] max-w-[calc(100vw-16px)] overflow-auto rounded-xl bg-panel2 p-1.5 shadow-[0_8px_30px_rgba(0,0,0,.6)]"
        :style="{ left: menuLeft + 'px', top: menuTop + 'px' }"
      >
        <button
          v-for="tag in tags.tags"
          :key="tag.id"
          class="flex w-full items-center justify-between gap-3.5 rounded-lg px-3 py-2 text-left hover:bg-white/10"
          @click="goTag(tag.name)"
        >
          <span class="truncate">{{ tag.name }}</span>
          <span class="text-[12.5px] text-mut">{{ tag.count }}</span>
        </button>
        <div v-if="!tags.tags.length" class="px-3 py-2 text-[13px] text-mut">还没有标签</div>
      </div>
    </Teleport>
  </div>
</template>
