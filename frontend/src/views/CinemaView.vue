<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { CinemaTitleSummary } from '@/types'
import { useChromeStore } from '@/stores/chrome'
import { useCinemaStore } from '@/stores/cinema'
import { fmtDur } from '@/utils/format'
import CinemaFormDialog from '@/components/CinemaFormDialog.vue'

const route = useRoute()
const router = useRouter()
const cinema = useCinemaStore()
const chrome = useChromeStore()

const adding = ref(false)
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))

const groups = computed(() =>
  [
    { kind: 'series' as const, label: '剧集', items: cinema.titles.filter(t => t.kind === 'series') },
    { kind: 'movie' as const, label: '电影', items: cinema.titles.filter(t => t.kind === 'movie') },
  ].filter(group => group.items.length),
)

function meta(title: CinemaTitleSummary): string {
  return title.kind === 'series'
    ? [title.year, `${title.n_seasons || 1} 季 ${title.n_episodes} 集`].filter(Boolean).join(' · ')
    : [title.year, title.duration_ms ? fmtDur(title.duration_ms) : ''].filter(Boolean).join(' · ')
}

async function load() {
  chrome.set({ title: '影视', placeholder: '搜索影视' })
  await cinema.load(q.value)
  chrome.set({ count: cinema.titles.length ? `${cinema.titles.length} 个条目` : '' })
}

onMounted(load)
watch(q, () => void load())

function hideBrokenImage(event: Event) {
  ;(event.target as HTMLImageElement).style.display = 'none'
}
</script>

<template>
  <main class="px-3 pb-[120px]">
    <div class="flex items-center gap-3 px-1 pt-1 pb-3">
      <button
        class="rounded-[9px] bg-acc px-3.5 py-2 text-[13.5px] font-semibold"
        @click="adding = true"
      >
        ＋ 添加影视
      </button>
      <span class="text-[12.5px] text-mut">
        {{ cinema.titles.length ? `${cinema.seriesCount} 部剧集 · ${cinema.movieCount} 部电影` : '' }}
      </span>
      <span class="flex-1" />
      <button class="px-0.5 py-1.5 text-[13.5px] text-acc" @click="load">刷新</button>
    </div>

    <div
      v-if="!cinema.titles.length && cinema.loaded"
      class="mx-auto max-w-[560px] px-6 py-16 text-center text-[14px] leading-loose text-mut"
    >
      <p class="m-0">影视库还是空的。</p>
      <p class="m-0 mt-3">
        把电影 / 剧集放进影视目录
        <b v-if="cinema.mediaDir" class="font-medium text-fg">{{ cinema.mediaDir }}</b>
        <b v-else class="font-medium text-fg">MEDIA_ROOT</b>
        ，<br />
        点「＋ 添加影视」填目录，或者命令行跑
        <code class="rounded bg-panel px-1.5 py-0.5">photo-vault cinema-import E:\Movies</code>。
      </p>
      <p class="m-0 mt-3">只登记路径：不搬文件、不算哈希、不跑 AI。</p>
    </div>

    <div v-else-if="cinema.loading && !cinema.titles.length" class="spinner" />

    <section v-for="group in groups" :key="group.kind">
      <h2 class="mb-2.5 mt-4 px-1 text-[17px] font-bold">{{ group.label }} · {{ group.items.length }}</h2>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(118px,1fr))] gap-x-2.5 gap-y-4">
        <button
          v-for="title in group.items"
          :key="title.id"
          class="text-left tap"
          @click="router.push(`/cinema/${title.id}`)"
        >
          <div
            class="relative flex aspect-[2/3] items-center justify-center overflow-hidden rounded-[10px] bg-panel2"
          >
            <span class="text-[32px] font-semibold text-mut">{{ (title.title || '?').slice(0, 1) }}</span>
            <img
              v-if="title.poster"
              :src="title.poster"
              alt=""
              loading="lazy"
              class="absolute inset-0 h-full w-full object-cover"
              @error="hideBrokenImage"
            />
            <span
              class="absolute left-1.5 top-1.5 rounded-[5px] bg-black/60 px-1.5 py-px text-[10.5px] tracking-[.3px]"
              >{{ title.kind === 'series' ? '剧集' : '电影' }}</span
            >
            <span
              v-if="title.kind === 'series'"
              class="absolute bottom-1.5 right-1.5 rounded-[5px] bg-black/60 px-1.5 py-px text-[10.5px]"
              >{{ title.n_episodes }} 集</span
            >
          </div>
          <div class="mt-1.5 truncate text-[13px]">{{ title.title }}</div>
          <div class="truncate text-[11.5px] text-mut">{{ meta(title) }}</div>
        </button>
      </div>
    </section>

    <CinemaFormDialog v-if="adding" mode="add" @close="adding = false" @saved="load" />
  </main>
</template>
