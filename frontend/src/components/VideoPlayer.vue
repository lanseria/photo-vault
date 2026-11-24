<script setup lang="ts">
/** xgplayer 播放器封装：优先直接播原件，浏览器放不了就回退到服务端转码。
 *
 * 原生兼容性差的典型场景：iPhone 的 HEVC 视频在没硬解的 Windows Chrome 上
 * 只出声音不出画面（<video> 不报错，视频轨静默解不出来）。两层兜底：
 *   · 提前探测：父组件把 ffprobe 出的编码（info.vcodec）传进来，浏览器
 *     canPlayType 明确不支持就直接播转码流，省得白放两秒音频；
 *   · 看门狗：起播后视频轨 videoWidth 停在 0，或者画面从来没渲染过一帧
 *     （requestVideoFrameCallback 超时），自动切转码。
 * 转码端点（/transcode/{id}）首次会同步等 ffmpeg 跑完，期间挂着 loading，
 * 转好之后 FileResponse 带 Range 直接回，进度条随便拖。 */
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import Player from 'xgplayer'
import 'xgplayer/dist/index.min.css'

const props = withDefaults(
  defineProps<{
    /** 原始文件地址（/file/{id}） */
    src: string
    /** 浏览器放不了时的转码地址（/transcode/{id}）；不传就只能提示错误 */
    transcodeSrc?: string | null
    /** ffprobe 出来的视频编码名（hevc / avc1 / vp9…），用来提前探测 */
    codec?: string | null
    /** 封面图，起播前显示 */
    poster?: string | null
    autoplay?: boolean
  }>(),
  { transcodeSrc: null, codec: null, poster: null, autoplay: true },
)

const emit = defineEmits<{ ended: []; error: [] }>()

const boxEl = ref<HTMLDivElement | null>(null)
/** native=直接播原件；transcode=浏览器放不了，播服务端转码；failed=转码也放不了 */
const mode = ref<'native' | 'transcode' | 'failed'>('native')
/** 转码流是否已经出元数据（转码完成后立刻就有）；没出之前挂提示条 */
const transcodeReady = ref(false)
const player = ref<Player | null>(null)

/** 原生失败最多回退一次，转码再失败就停在错误提示，不无限循环 */
let fallbackDone = false
let watchdogTimers: number[] = []

function later(fn: () => void, ms: number) {
  watchdogTimers.push(window.setTimeout(fn, ms))
}

function clearWatchdog() {
  watchdogTimers.forEach(t => window.clearTimeout(t))
  watchdogTimers = []
}

/** 浏览器自己报告支不支持这个编码（不认识的编码名探测不了，交给看门狗实测） */
function codecUnsupported(): boolean {
  const name = props.codec
  if (!name) return false
  const vcodec = /^(hevc|hev1|hvc1)/.test(name)
    ? 'hvc1.1.6.L93.B0'
    : /^(avc1|avc3|h264)/.test(name)
      ? 'avc1.4D401F'
      : /^(vp9|vp09)/.test(name)
        ? 'vp09.00.10.08'
        : /^(av01|av1)/.test(name)
          ? 'av01.0.04M.08'
          : null
  if (!vcodec) return false
  return document.createElement('video').canPlayType(`video/mp4; codecs="${vcodec}"`) === ''
}

function destroyPlayer() {
  clearWatchdog()
  const p = player.value
  player.value = null
  if (!p) return
  try {
    p.destroy()
  } catch {
    /* 已经挂掉的实例 destroy 也可能抛，忽略 */
  }
  if (boxEl.value) boxEl.value.innerHTML = ''
}

function fallbackFromNative() {
  if (fallbackDone) return
  fallbackDone = true
  if (!props.transcodeSrc) {
    mode.value = 'failed'
    emit('error')
    return
  }
  mode.value = 'transcode'
  mount()
}

function onPlayerError() {
  if (mode.value === 'native' && !fallbackDone) return fallbackFromNative()
  // 转码也放不了（ffmpeg 不可用 / 转码失败）
  mode.value = 'failed'
  emit('error')
}

/** 起播后视频轨解不出来（HEVC 无硬解：声音走、画面黑、不报错）→ 自动切转码 */
function armWatchdog(p: Player) {
  const video = () => p.media as HTMLVideoElement | null
  /** 音频在走却从没渲染过一帧 → 认定浏览器解不了 → 转码 */
  const watchFrames = () => {
    const v = video()
    if (!v || mode.value !== 'native') return
    if (!v.videoWidth) return fallbackFromNative()
    // videoWidth 有了也不保险：有的浏览器元数据照报、帧一个都解不出来，
    // 用 requestVideoFrameCallback 盯 2.5s，真渲染过帧才放行
    if (!('requestVideoFrameCallback' in v)) return
    if (v.paused) return p.once('play', watchFrames) // 等真的开始播再盯帧
    let got = false
    type RVFC = HTMLVideoElement & { requestVideoFrameCallback: (cb: () => void) => number }
    ;(v as RVFC).requestVideoFrameCallback(() => {
      got = true
    })
    later(() => {
      if (!got && mode.value === 'native') fallbackFromNative()
    }, 2500)
  }
  // canplay（音频就绪）后稍等一下再查：不支持的视频轨 videoWidth 停在 0
  p.once('canplay', () => later(watchFrames, 1200))
}

function mount() {
  destroyPlayer()
  const el = boxEl.value
  if (!el) return
  // 挂载前先问浏览器：明确放不了的编码直接转码，免得先白放两秒音频再回退
  if (mode.value === 'native' && !fallbackDone && codecUnsupported() && props.transcodeSrc) {
    fallbackDone = true
    mode.value = 'transcode'
  }
  transcodeReady.value = false
  const url = mode.value === 'transcode' && props.transcodeSrc ? props.transcodeSrc : props.src
  const p = new Player({
    el,
    url,
    autoplay: props.autoplay,
    playsinline: true,
    width: '100%',
    height: '100%',
    fitVideoSize: 'fixed',
    poster: props.poster ?? undefined,
    pip: true,
    cssFullscreen: false,
    // 灯箱自己监听 ←/→ 切上一张下一张，别让播放器的快捷键抢
    ignores: ['keyboard'],
  })
  player.value = p
  p.on('ended', () => emit('ended'))
  p.on('loadedmetadata', () => {
    transcodeReady.value = true
  })
  p.on('error', onPlayerError)
  if (mode.value === 'native') armWatchdog(p)
}

watch(boxEl, el => {
  if (el) void nextTick(mount)
})

watch(
  () => [props.src, props.transcodeSrc, props.codec, props.poster],
  () => {
    // 换素材了：重新从原生开始试
    mode.value = 'native'
    fallbackDone = false
    nextTick(mount)
  },
)

/** 供灯箱的关键帧缩略图 seek 用 */
function seek(ms: number) {
  const v = player.value?.media as HTMLVideoElement | undefined
  if (!v) return
  v.currentTime = ms / 1000
  void v.play().catch(() => undefined)
}

defineExpose({ seek })

onBeforeUnmount(destroyPlayer)
</script>

<template>
  <div class="vp-root relative h-full w-full">
    <div ref="boxEl" class="h-full w-full bg-black" />
    <div
      v-if="mode === 'transcode' && !transcodeReady"
      class="pointer-events-none absolute inset-x-4 top-4 rounded-lg bg-black/70 px-3 py-2 text-center text-[12.5px] leading-relaxed text-white"
    >
      浏览器不支持这个视频编码，正在服务器转码…首次可能要几十秒，转好有缓存就快了
    </div>
    <div
      v-else-if="mode === 'failed'"
      class="absolute inset-x-4 top-4 rounded-lg bg-[rgba(44,44,46,.96)] px-3 py-2.5 text-center text-[12.5px] leading-relaxed text-[#e5e5ea]"
    >
      浏览器放不了这个视频，服务器转码也没成功。稍后重试，或通过「系统播放器 / 下载」打开原文件。
    </div>
  </div>
</template>

<style>
/* xgplayer 的样式是全局注入的；这里限定在本组件根下补容器尺寸：
   视频按 contain 完整显示，控制条压在底部，深色界面正好贴合默认皮肤 */
.vp-root .xgplayer {
  width: 100% !important;
  height: 100% !important;
  background: transparent;
}
.vp-root .xgplayer video {
  width: 100%;
  height: 100%;
  object-fit: contain;
}
</style>
