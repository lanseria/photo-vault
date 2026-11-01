<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import type { Asset, Face, VideoInfo } from '@/types'
import { takenDateText } from '@/utils/date'
import { fmtBytes, fmtFrameTime } from '@/utils/format'
import { useAssetActions } from '@/composables/useAssetActions'
import { lockScroll, unlockScroll } from '@/composables/scrollMemory'
import { useDevicesStore } from '@/stores/devices'
import { useSystemStore } from '@/stores/system'
import { useTagSheetStore } from '@/stores/tagSheet'
import { useToastStore } from '@/stores/toast'
import AppIcon from './AppIcon.vue'
import DevicePicker from './DevicePicker.vue'

const props = defineProps<{
  asset: Asset
  /** 当前列表，用来算上一张/下一张 */
  items: Asset[]
  /** 回收站里只显示「恢复 / 彻底删除」 */
  trashView?: boolean
}>()

const emit = defineEmits<{
  close: []
  prev: []
  next: []
  trash: []
  restore: []
  purge: []
}>()

const router = useRouter()
const actions = useAssetActions()
const sheet = useTagSheetStore()
const devices = useDevicesStore()
const system = useSystemStore()
const toast = useToastStore()

const faces = ref<Face[]>([])
const video = ref<VideoInfo | null>(null)
const livePlaying = ref(false)
const videoEl = ref<HTMLVideoElement | null>(null)
const pickingDevice = ref(false)

const index = computed(() => props.items.findIndex(a => a.id === props.asset.id))
const hasPrev = computed(() => index.value > 0)
const hasNext = computed(() => index.value >= 0 && index.value < props.items.length - 1)
const isVideo = computed(() => props.asset.kind === 'video')
const canPlayLive = computed(() => props.asset.kind === 'live_photo')
const meta = computed(
  () => `${props.asset.width || '?'}×${props.asset.height || '?'} · ${fmtBytes(props.asset.bytes)}`,
)
const dateText = computed(() => takenDateText(props.asset))

async function loadExtras() {
  const id = props.asset.id
  faces.value = []
  video.value = null
  livePlaying.value = false

  api
    .faces(id)
    .then(res => {
      if (props.asset.id === id) faces.value = res.faces
    })
    .catch(() => undefined)

  if (props.asset.kind === 'video') {
    api
      .video(id)
      .then(res => {
        if (props.asset.id === id) video.value = res
      })
      .catch(() => undefined)
  }
}

watch(() => props.asset.id, loadExtras, { immediate: true })

async function favorite() {
  await actions.toggleFavorite(props.asset)
}

function addToBin() {
  void actions.addToBin([props.asset.id])
}

function editTags() {
  sheet.show([props.asset.id])
}

async function removeTag(name: string) {
  const res = await api.removeTag(props.asset.id, name)
  props.asset.tags = res.tags
}

function seek(ms: number) {
  const el = videoEl.value
  if (!el) return
  el.currentTime = ms / 1000
  void el.play().catch(() => undefined)
}

function openPerson(personId: string) {
  emit('close')
  void router.push(`/people/${personId}`)
}

/** 手工把这张照片归到某台设备（或清除归类）。 */
async function pickDevice(deviceId: number | null) {
  pickingDevice.value = false
  if ((props.asset.device_id ?? null) === deviceId) return
  try {
    const res = await api.assignDevice([props.asset.id], deviceId)
    if (!res.updated) return
    const device = deviceId == null ? null : devices.byId.get(deviceId) ?? null
    props.asset.device_id = deviceId
    props.asset.device_unknown = deviceId == null
    props.asset.model = device?.name ?? '未识别设备'
    toast.show(device ? `设备已设为「${device.name}」` : '已标记为未识别设备')
    void devices.load(true)
    void system.refreshStats()
  } catch {
    toast.show('设置设备失败')
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
  else if (event.key === 'ArrowLeft' && hasPrev.value) emit('prev')
  else if (event.key === 'ArrowRight' && hasNext.value) emit('next')
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  lockScroll()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  unlockScroll()
})
</script>

<template>
  <div class="fixed inset-0 z-[100] flex flex-col bg-black">
    <div class="z-[3] flex items-center gap-2.5 px-3.5 py-3">
      <button class="icon-btn" aria-label="返回" @click="emit('close')">
        <AppIcon name="chevron-left" :size="26" :stroke="2.2" />
      </button>
      <div class="flex-1 text-center text-[14px] font-semibold">{{ dateText }}</div>
      <span class="w-9" />
    </div>

    <div class="relative flex min-h-0 flex-1 items-center justify-center">
      <img
        v-if="!isVideo && !livePlaying"
        :src="`/preview/${asset.id}`"
        alt=""
        class="max-h-full max-w-full object-contain"
      />
      <video
        v-else-if="isVideo"
        ref="videoEl"
        :src="`/file/${asset.id}`"
        controls
        autoplay
        playsinline
        class="max-h-full max-w-full"
      />
      <video
        v-else
        :src="`/file/${asset.id}?role=live_motion`"
        controls
        autoplay
        playsinline
        class="max-h-full max-w-full"
      />

      <button
        v-if="hasPrev"
        class="absolute left-2.5 top-1/2 flex h-[70px] w-[46px] -translate-y-1/2 items-center justify-center rounded-xl bg-[rgba(60,60,67,.4)] text-white opacity-75 hover:opacity-100"
        aria-label="上一张"
        @click="emit('prev')"
      >
        <AppIcon name="chevron-left" :size="24" :stroke="2.4" />
      </button>
      <button
        v-if="hasNext"
        class="absolute right-2.5 top-1/2 flex h-[70px] w-[46px] -translate-y-1/2 items-center justify-center rounded-xl bg-[rgba(60,60,67,.4)] text-white opacity-75 hover:opacity-100"
        aria-label="下一张"
        @click="emit('next')"
      >
        <AppIcon name="chevron-right" :size="24" :stroke="2.4" />
      </button>
    </div>

    <div class="flex flex-col gap-2.5 px-4 pb-safe-16">
      <div class="flex flex-wrap items-center justify-center gap-x-2 gap-y-1.5 text-[12.5px] text-mut">
        <button
          class="inline-flex items-center gap-1.5 rounded-full bg-panel2 px-2.5 py-1 text-[12.5px] tap"
          :class="asset.device_unknown && '!text-warn'"
          :title="asset.device_raw || 'EXIF 里没有机型'"
          @click="pickingDevice = true"
        >
          <span>📷 {{ asset.device_unknown ? '未识别设备' : asset.model }}</span>
          <span class="text-[11px] opacity-60">更换</span>
        </button>
        <span>{{ meta }}</span>
      </div>

      <div v-if="faces.length" class="flex flex-wrap items-center gap-2">
        <button
          v-for="face in faces"
          :key="face.id"
          class="inline-flex items-center gap-1.5 rounded-full bg-panel2 py-[3px] pl-[3px] pr-3 text-[13px]"
          :class="!face.person_id && 'opacity-55'"
          :disabled="!face.person_id"
          @click="face.person_id && openPerson(face.person_id)"
        >
          <img :src="`/faces/${face.id}.jpg`" alt="" loading="lazy" class="h-[30px] w-[30px] rounded-full bg-panel object-cover" />
          <span>{{ face.person || '未归类' }}</span>
        </button>
      </div>

      <div v-if="isVideo && video?.summary" class="flex flex-col gap-1.5">
        <div v-if="video.summary.caption" class="text-[13px] leading-[1.45] opacity-90">
          AI 描述：{{ video.summary.caption }}
        </div>
        <div v-if="video.frames.length" class="no-scrollbar flex gap-1.5 overflow-x-auto pb-0.5">
          <button
            v-for="frame in video.frames"
            :key="frame.ord"
            class="relative shrink-0 rounded-lg border-2 border-transparent p-0 leading-none"
            @click="seek(frame.t_ms)"
          >
            <img
              v-if="frame.url"
              :src="frame.url"
              alt=""
              loading="lazy"
              class="block h-[52px] w-[92px] bg-panel object-cover"
            />
            <span
              class="absolute bottom-[3px] right-[3px] rounded bg-black/60 px-[3px] text-[10px] leading-[1.4] text-white"
              >{{ fmtFrameTime(frame.t_ms) }}</span
            >
          </button>
        </div>
        <div v-if="video.summary.ocr" class="max-h-[46px] overflow-auto whitespace-pre-wrap text-[12px] text-mut">
          画面文字：{{ video.summary.ocr }}
        </div>
      </div>

      <div class="flex min-h-7 flex-wrap items-center gap-1.5">
        <span
          v-for="tag in asset.tags"
          :key="tag"
          class="inline-flex items-center gap-1.5 rounded-full bg-panel2 px-3 py-1 text-[13px]"
        >
          {{ tag }}
          <button class="text-mut hover:text-heart" @click="removeTag(tag)">✕</button>
        </span>
        <button class="rounded-full bg-panel2 px-3 py-1 text-[13px] tap" @click="editTags">
          ＋ 标签
        </button>
      </div>

      <div class="flex items-center justify-around">
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          :class="asset.favorite && 'text-heart'"
          @click="favorite"
        >
          <AppIcon name="heart" :size="24" :filled="asset.favorite" />
          {{ asset.favorite ? '取消喜欢' : '喜欢' }}
        </button>
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          @click="addToBin"
        >
          <AppIcon name="bin" :size="24" />
          剪辑箱
        </button>
        <button
          v-if="canPlayLive"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          @click="livePlaying = !livePlaying"
        >
          <AppIcon name="play" :size="24" />
          {{ livePlaying ? '照片' : '播放实况' }}
        </button>
        <button
          v-if="!trashView"
          class="flex flex-col items-center gap-[3px] text-[11px] text-heart"
          @click="emit('trash')"
        >
          <AppIcon name="trash" :size="24" />
          删除
        </button>
        <button
          v-if="trashView"
          class="flex flex-col items-center gap-[3px] text-[11px]"
          @click="emit('restore')"
        >
          <AppIcon name="restore" :size="24" />
          恢复
        </button>
        <button
          v-if="trashView"
          class="flex flex-col items-center gap-[3px] text-[11px] text-heart"
          @click="emit('purge')"
        >
          <AppIcon name="trash" :size="24" />
          彻底删除
        </button>
      </div>
    </div>

    <DevicePicker
      v-if="pickingDevice"
      title="这张照片的拍摄设备"
      :hint="`EXIF：${asset.device_raw || '没有机型信息'}。选一个已有设备，或者清回「未识别」。`"
      :current-id="asset.device_id ?? null"
      allow-none
      @pick="pickDevice"
      @close="pickingDevice = false"
      @manage="pickingDevice = false; emit('close'); router.push('/devices')"
    />
  </div>
</template>
