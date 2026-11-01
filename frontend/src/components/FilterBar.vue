<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

type FilterKey = 'kind' | 'dur' | 'px'

const groups: { key: FilterKey; label: string; all: string; options: { value: string; label: string }[] }[] = [
  {
    key: 'kind',
    label: '类型',
    all: '全部',
    options: [
      { value: 'video', label: '🎬 视频' },
      { value: 'photo', label: '照片' },
    ],
  },
  {
    key: 'dur',
    label: '时长',
    all: '任意',
    options: [
      { value: '0-15', label: '<15s' },
      { value: '15-60', label: '15–60s' },
      { value: '60-300', label: '1–5min' },
      { value: '300-', label: '5min+' },
    ],
  },
  {
    key: 'px',
    label: '清晰度',
    all: '任意',
    options: [
      { value: '1080', label: '≥1080p' },
      { value: '2160', label: '≥4K' },
    ],
  },
]

function current(key: FilterKey): string {
  const raw = route.query[key]
  return typeof raw === 'string' ? raw : ''
}

function setFilter(key: FilterKey, value: string) {
  if (current(key) === value) return
  const query = { ...route.query }
  if (value) query[key] = value
  else delete query[key]
  void router.replace({ path: route.path, query })
}
</script>

<template>
  <div class="flex items-center gap-2.5 px-4 pb-2.5 overflow-x-auto no-scrollbar whitespace-nowrap">
    <template v-for="group in groups" :key="group.key">
      <span class="text-[12px] text-mut shrink-0">{{ group.label }}</span>
      <div class="inline-flex shrink-0 items-center rounded-[9px] bg-panel p-0.5">
        <button
          class="seg-btn"
          :class="!current(group.key) && 'seg-on'"
          @click="setFilter(group.key, '')"
        >
          {{ group.all }}
        </button>
        <button
          v-for="option in group.options"
          :key="option.value"
          class="seg-btn"
          :class="current(group.key) === option.value && 'seg-on'"
          @click="setFilter(group.key, option.value)"
        >
          {{ option.label }}
        </button>
      </div>
    </template>
  </div>
</template>
