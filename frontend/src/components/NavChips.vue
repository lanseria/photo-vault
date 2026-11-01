<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBinStore } from '@/stores/bin'
import { useChromeStore } from '@/stores/chrome'
import { useSystemStore } from '@/stores/system'

const route = useRoute()
const router = useRouter()
const system = useSystemStore()
const bin = useBinStore()
const chrome = useChromeStore()

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
  { id: 'devices', label: '📷 设备', to: '/devices', count: system.stats.device_count },
  { id: 'cinema', label: '🎞 影视', to: '/cinema', count: system.stats.cinema_titles },
  { id: 'trash', label: '🗑', to: '/trash', count: system.stats.trash },
])
</script>

<template>
  <div class="flex items-center gap-2 px-4 pb-2.5 max-sm:px-3">
    <div class="no-scrollbar flex min-w-0 flex-1 items-center gap-2 overflow-x-auto">
      <button
        v-for="chip in chips"
        :key="chip.id"
        class="chip tap shrink-0"
        :class="isActive(chip.id) && 'chip-on'"
        @click="router.push(chip.to)"
      >
        {{ chip.label }}
        <span v-if="chip.count" class="chip-count">{{ chip.count }}</span>
      </button>
    </div>

    <span class="shrink-0 text-[12px] text-mut">{{ chrome.count }}</span>
  </div>
</template>
