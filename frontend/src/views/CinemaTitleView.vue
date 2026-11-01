<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorText } from '@/api'
import type { CinemaEpisode } from '@/types'
import { useChromeStore } from '@/stores/chrome'
import { useCinemaStore } from '@/stores/cinema'
import { useDialogStore } from '@/stores/dialog'
import { useToastStore } from '@/stores/toast'
import { fmtBytes, fmtDur, fmtLong, pad2, sortSubtitles } from '@/utils/format'
import { watchImportRun } from '@/utils/importRun'
import CinemaFormDialog from '@/components/CinemaFormDialog.vue'
import CinemaPlayer from '@/components/CinemaPlayer.vue'

const route = useRoute()
const router = useRouter()
const cinema = useCinemaStore()
const chrome = useChromeStore()
const dialog = useDialogStore()
const toast = useToastStore()

const season = ref(0)
const playing = ref<CinemaEpisode | null>(null)
const editing = ref(false)
const rescanning = ref(false)

const detail = computed(() => cinema.detail)

const seasons = computed(() => detail.value?.seasons.filter(s => s.season > 0) ?? [])
const specials = computed(() => {
  const d = detail.value
  if (!d || d.kind === 'movie') return null
  return d.seasons.find(s => s.season === 0 && s.episodes.length) ?? null
})
const tabs = computed(() => (specials.value ? [...seasons.value, specials.value] : seasons.value))
const episodes = computed(
  () => detail.value?.seasons.find(s => s.season === season.value)?.episodes ?? [],
)

const seasonLabel = (value: number) => (value === 0 ? '特别篇' : `第 ${value} 季`)

function episodeMeta(episode: CinemaEpisode): string {
  return [
    episode.duration_ms ? fmtDur(episode.duration_ms) : '',
    episode.bytes ? fmtBytes(episode.bytes) : '',
    episode.width ? `${episode.width}×${episode.height}` : '',
    episode.missing ? '文件缺失' : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

async function load() {
  const id = String(route.params.id ?? '')
  if (!id) return
  try {
    const d = await cinema.open(id)
    chrome.set({ title: d.title, count: d.n_episodes ? `${d.n_episodes} 集` : '' })
    const real = d.seasons.filter(s => s.season > 0)
    season.value = real.length ? real[0].season : (d.seasons[0]?.season ?? 0)
  } catch (e) {
    toast.show('打不开这个条目：' + errorText(e).slice(0, 60))
    await router.replace('/cinema')
  }
}

watch(() => route.params.id, () => void load(), { immediate: true })

onBeforeUnmount(() => cinema.clearDetail())

function playFirst() {
  const episode = episodes.value.find(e => !e.missing)
  if (episode) playing.value = episode
  else toast.show('这一季没有可播放的剧集')
}

function play(episode: CinemaEpisode) {
  if (!episode.missing) playing.value = episode
}

async function removeTitle() {
  const d = detail.value
  if (!d) return
  const ok = await dialog.confirm({
    title: `从影视库删除「${d.title}」？`,
    message: '只删登记记录，磁盘上的视频文件不会动。',
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await cinema.remove(d.id)
    toast.show('已从影视库移除')
    await router.push('/cinema')
  } catch (e) {
    toast.show('删除失败：' + errorText(e).slice(0, 60))
  }
}

async function rescan() {
  const d = detail.value
  if (!d || rescanning.value) return
  rescanning.value = true
  try {
    const res = await api.cinemaRescan(d.id)
    const final = await watchImportRun(res.run.id)
    toast.show(final.failed ? `扫描完成，${final.failed} 个失败` : `扫描完成：更新 ${final.updated} 个`)
    await load()
    await cinema.load()
  } catch (e) {
    toast.show('扫描失败：' + errorText(e).slice(0, 60))
  } finally {
    rescanning.value = false
  }
}

function hideBrokenImage(event: Event) {
  ;(event.target as HTMLImageElement).style.display = 'none'
}
</script>

<template>
  <main class="px-4 pb-[120px]">
    <button class="px-0 pb-3.5 pt-0.5 text-[14.5px] text-acc" @click="router.push('/cinema')">
      ‹ 影视库
    </button>

    <div v-if="detail" class="flex gap-4">
      <div
        class="relative flex aspect-[2/3] w-[118px] shrink-0 items-center justify-center overflow-hidden rounded-[10px] bg-panel2"
      >
        <span class="text-[34px] font-semibold text-mut">{{ (detail.title || '?').slice(0, 1) }}</span>
        <img
          v-if="detail.poster"
          :src="detail.poster"
          alt=""
          class="absolute inset-0 h-full w-full object-cover"
          @error="hideBrokenImage"
        />
      </div>

      <div class="flex min-w-0 flex-1 flex-col gap-1.5">
        <h2 class="m-0 text-[20px] font-bold leading-tight">
          {{ detail.title }}
          <span v-if="detail.year" class="text-[16px] font-normal text-mut">({{ detail.year }})</span>
        </h2>
        <div class="text-[11.5px] leading-relaxed text-mut">
          {{ detail.kind === 'movie' ? '电影' : `剧集 · ${seasons.length} 季 · ${detail.n_episodes} 集` }}
          · {{ fmtLong(detail.duration_ms) || '时长未知' }} · {{ fmtBytes(detail.bytes) }}
        </div>
        <div class="mt-1.5 flex flex-wrap gap-2">
          <button
            class="rounded-lg border border-acc bg-acc px-3 py-1.5 text-[12.5px] font-semibold text-white"
            @click="playFirst"
          >
            ▶ 播放
          </button>
          <button
            v-if="detail.can_rescan"
            class="rounded-lg border border-line px-3 py-1.5 text-[12.5px] disabled:opacity-50"
            :disabled="rescanning"
            @click="rescan"
          >
            {{ rescanning ? '扫描中…' : '重新扫描' }}
          </button>
          <button class="rounded-lg border border-line px-3 py-1.5 text-[12.5px]" @click="editing = true">
            编辑
          </button>
          <button
            class="rounded-lg border border-[rgba(255,55,95,.4)] px-3 py-1.5 text-[12.5px] text-heart"
            @click="removeTitle"
          >
            删除
          </button>
        </div>
      </div>
    </div>

    <div v-if="detail" class="mt-3.5 whitespace-pre-wrap text-[13.5px] leading-relaxed text-[#c7c7cc]">
      {{ detail.overview }}
    </div>

    <div v-if="tabs.length > 1" class="no-scrollbar flex gap-1.5 overflow-x-auto pb-1.5 pt-3">
      <button
        v-for="tab in tabs"
        :key="tab.season"
        class="shrink-0 rounded-full bg-panel px-3.5 py-1.5 text-[13px] text-mut"
        :class="tab.season === season && '!bg-acc !text-white'"
        @click="season = tab.season"
      >
        {{ seasonLabel(tab.season) }}
      </button>
    </div>

    <div v-if="detail" class="mt-1 flex flex-col gap-0.5">
      <button
        v-for="episode in episodes"
        :key="episode.id"
        class="flex items-center gap-3 border-b border-line/60 px-1 py-[11px] text-left"
        :class="episode.missing && 'opacity-45'"
        @click="play(episode)"
      >
        <div class="w-[52px] shrink-0 text-[12px] tabular-nums text-mut">
          {{ detail.kind === 'series' ? 'E' + pad2(episode.episode) : '正片' }}
        </div>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[14px]">
            {{ episode.name || (detail.kind === 'series' ? `第 ${episode.episode} 集` : detail.title) }}
          </div>
          <div class="mt-0.5 truncate text-[11.5px] text-mut">{{ episodeMeta(episode) }}</div>
          <div v-if="episode.subtitles.length" class="mt-1 flex flex-wrap gap-1">
            <span
              v-for="sub in sortSubtitles(episode.subtitles)"
              :key="sub.id"
              class="rounded border border-[rgba(100,160,220,.35)] px-1 text-[10.5px] text-[#9ad]"
              >{{ sub.label }}</span
            >
          </div>
        </div>
        <div class="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-full bg-panel2 text-[12px]">
          ▶
        </div>
      </button>

      <div v-if="!episodes.length" class="py-12 text-center text-[14px] text-mut">
        这一季还没有登记剧集
      </div>
    </div>

    <div v-else-if="cinema.detailLoading" class="spinner" />
    <div v-else class="py-16 text-center text-[14px] text-mut">影视条目不存在</div>

    <CinemaPlayer
      v-if="playing && detail"
      :title="detail.title"
      :kind="detail.kind"
      :episodes="episodes"
      :start="playing"
      @close="playing = null"
    />

    <CinemaFormDialog
      v-if="editing && detail"
      mode="edit"
      :detail="detail"
      @close="editing = false"
      @saved="load"
    />
  </main>
</template>
