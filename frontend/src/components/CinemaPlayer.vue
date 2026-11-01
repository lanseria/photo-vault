<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, errorText } from '@/api'
import type { CinemaEpisode } from '@/types'
import { useSystemStore } from '@/stores/system'
import { useToastStore } from '@/stores/toast'
import { lockScroll, unlockScroll } from '@/composables/scrollMemory'
import { pad2, sortSubtitles } from '@/utils/format'

const props = defineProps<{
  title: string
  kind: 'movie' | 'series'
  episodes: CinemaEpisode[]
  start: CinemaEpisode
}>()

const emit = defineEmits<{ close: [] }>()

const system = useSystemStore()
const toast = useToastStore()

const current = ref<CinemaEpisode>(props.start)
const videoEl = ref<HTMLVideoElement | null>(null)
const errorVisible = ref(false)
const subtitleId = ref('')

const subtitles = computed(() => sortSubtitles(current.value.subtitles || []))
const index = computed(() => props.episodes.findIndex(e => e.id === current.value.id))
const canPrev = computed(() => index.value > 0)
const canNext = computed(() => index.value >= 0 && index.value < props.episodes.length - 1)
const heading = computed(
  () =>
    props.title +
    (props.kind === 'series'
      ? ` · S${pad2(current.value.season)}E${pad2(current.value.episode)}`
      : ''),
)

watch(
  () => props.start.id,
  () => {
    current.value = props.start
  },
)

function applySubtitle() {
  const video = videoEl.value
  if (!video) return
  video.querySelectorAll('track').forEach(track => track.remove())
  if (!subtitleId.value) return
  const sub = subtitles.value.find(s => s.id === subtitleId.value)
  const track = document.createElement('track')
  track.kind = 'subtitles'
  track.label = sub?.label ?? '字幕'
  track.srclang = 'zh'
  track.default = true
  track.src = `/api/cinema/subtitles/${subtitleId.value}`
  video.appendChild(track)
  window.setTimeout(() => {
    try {
      if (track.track) track.track.mode = 'showing'
    } catch {
      /* 某些浏览器还没加载完 track */
    }
  }, 200)
}

watch(
  () => current.value.id,
  async () => {
    errorVisible.value = false
    subtitleId.value = subtitles.value[0]?.id ?? ''
    await nextTick()
    const video = videoEl.value
    if (!video) return
    video.load()
    void video.play().catch(() => undefined)
    applySubtitle()
  },
  { immediate: true },
)

function step(delta: number) {
  const next = props.episodes[index.value + delta]
  if (next && !next.missing) current.value = next
}

async function openExternal() {
  try {
    const res = await api.cinemaOpenEpisode(current.value.id)
    if (res.opened) return toast.show('已用系统播放器打开')
    if (await system.openOnHost(res.host_path)) return toast.show('已用系统播放器打开')
    try {
      await navigator.clipboard.writeText(res.host_path)
    } catch {
      /* 剪贴板不可用 */
    }
    toast.show('桌面桥未启动，路径已复制；也可在资源管理器里打开')
  } catch (e) {
    toast.show('打不开：' + errorText(e).slice(0, 60))
  }
}

function close() {
  emit('close')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  lockScroll()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  unlockScroll()
  const video = videoEl.value
  if (video) {
    video.pause()
    video.removeAttribute('src')
    try {
      video.load()
    } catch {
      /* ignore */
    }
  }
})
</script>

<template>
  <div class="fixed inset-0 z-[130] flex flex-col bg-black">
    <div class="flex items-center gap-2.5 px-3.5 py-3">
      <button class="w-[30px] text-[18px]" aria-label="关闭" @click="close">✕</button>
      <div class="flex-1 truncate text-[14px] font-semibold">{{ heading }}</div>
      <button
        class="rounded-lg border border-[rgba(10,132,255,.4)] px-2.5 py-1.5 text-[12.5px] text-acc"
        @click="openExternal"
      >
        系统播放器
      </button>
    </div>

    <div class="relative flex min-h-0 flex-1 items-center justify-center">
      <video
        ref="videoEl"
        :src="`/api/cinema/episodes/${current.id}/stream`"
        controls
        playsinline
        preload="metadata"
        class="h-full w-full bg-black"
        @error="errorVisible = true"
      />
      <div
        v-if="errorVisible"
        class="absolute inset-x-5 bottom-5 rounded-xl bg-[rgba(44,44,46,.96)] px-4 py-3.5 text-[13px] leading-relaxed text-[#e5e5ea]"
      >
        浏览器放不了这个容器（MKV / AVI / TS 很常见）。点右上角「系统播放器」用本机播放器打开，同目录的字幕会跟着走。
      </div>
    </div>

    <div class="flex items-center gap-2.5 px-3.5 pt-2.5 pb-safe-14">
      <select
        v-model="subtitleId"
        class="max-w-[48%] rounded-lg bg-panel2 px-2.5 py-2 text-[13px]"
        @change="applySubtitle"
      >
        <option value="">字幕：关</option>
        <option v-for="sub in subtitles" :key="sub.id" :value="sub.id">
          {{ sub.label }}{{ sub.format === 'ass' ? ' · ASS' : '' }}
        </option>
      </select>
      <span class="flex-1" />
      <button
        class="px-1.5 py-2 text-[13.5px] text-acc disabled:text-mut"
        :disabled="!canPrev"
        @click="step(-1)"
      >
        上一集
      </button>
      <button
        class="px-1.5 py-2 text-[13.5px] text-acc disabled:text-mut"
        :disabled="!canNext"
        @click="step(1)"
      >
        下一集
      </button>
    </div>
  </div>
</template>
