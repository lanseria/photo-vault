<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, errorText } from '@/api'
import { useDialogStore } from '@/stores/dialog'
import { useToastStore } from '@/stores/toast'
import { fmtBytes, fmtDur, fmtStamp } from '@/utils/format'
import { smartPop, type PeekAsset, type PeekRect } from '@/utils/peek'

/**
 * 照片/视频 hover 快速预览框（人脸抽屉和图库/素材库等照片流共用）。
 *
 * 位置锚在触发的格子上：默认哪边空间大放哪边（prefer='above' 强制上方），
 * 水平跟锚点中心对齐并夹在视口内。右击格子由父组件置 pinned=true 固定，
 * 固定后不随 hover 消失、按住盒子可拖动（▶ / 视频控件不作为拖动起手）、✕ 关闭。
 * 盒子定宽，媒体高度按锚点附近的可用空间自适应，保证不出视口。
 * 照片带「压缩」按钮：质量 80 重编码覆盖原件，成功后 emit compressed 让父组件刷新数据。
 */
const props = withDefaults(
  defineProps<{
    asset: PeekAsset
    anchor: PeekRect
    pinned: boolean
    /** 人脸抽屉明确要"上面"；照片流哪边空间大放哪边 */
    prefer?: 'above' | 'auto'
  }>(),
  { prefer: 'auto' },
)

const emit = defineEmits<{
  close: []
  keep: []
  leave: []
  /** 压缩副本已生成（或已存在），父组件据此刷新列表 */
  compressed: [{ assetId: string }]
}>()

const dialog = useDialogStore()
const toast = useToastStore()

const BOX_W = 280
const FOOTER_H = 48
const BAR_H = 27

const el = ref<HTMLElement | null>(null)
const playing = ref(false)
/** 固定态拖动后的落点；null = 停在锚点旁的默认位置 */
const dragPos = ref<{ x: number; y: number } | null>(null)

// 换了目标就复位：视频停播、拖动位置作废
watch(
  () => props.asset.id,
  () => {
    playing.value = false
    dragPos.value = null
  },
)

/** 落位 + 媒体高度：围绕锚点算出上下哪边放得下 */
const layout = computed(() => {
  const a = props.anchor
  const vh = window.innerHeight
  const bar = props.pinned ? BAR_H : 0
  const availAbove = a.top - 8 - 8 - FOOTER_H - bar
  const availBelow = vh - a.bottom - 8 - 8 - FOOTER_H - bar
  const above =
    props.prefer === 'above' ? availAbove >= 140 : availAbove >= availBelow
  const mediaH = Math.max(120, Math.min(260, above ? availAbove : availBelow))
  const boxH = mediaH + FOOTER_H + bar
  const pos = smartPop(a, BOX_W, boxH, props.prefer)
  return { left: Math.round(pos.left), top: Math.round(pos.top), mediaH }
})

const posStyle = computed(() => {
  if (props.pinned && dragPos.value) {
    return { left: `${dragPos.value.x}px`, top: `${dragPos.value.y}px` }
  }
  return { left: `${layout.value.left}px`, top: `${layout.value.top}px` }
})

/* 固定态拖动：Pointer Capture 跟手 */
const dragOffset = { dx: 0, dy: 0 }

function onPointerDown(event: PointerEvent) {
  if (!props.pinned || !el.value) return
  const target = event.target as HTMLElement
  if (target.closest('button, video, input')) return
  const rect = el.value.getBoundingClientRect()
  dragOffset.dx = event.clientX - rect.left
  dragOffset.dy = event.clientY - rect.top
  dragPos.value = { x: rect.left, y: rect.top }
  el.value.setPointerCapture(event.pointerId)
}

function onDrag(event: PointerEvent) {
  const box = el.value
  if (!box || !props.pinned || !box.hasPointerCapture?.(event.pointerId)) return
  dragPos.value = {
    x: Math.max(8, Math.min(event.clientX - dragOffset.dx, window.innerWidth - box.offsetWidth - 8)),
    y: Math.max(8, Math.min(event.clientY - dragOffset.dy, window.innerHeight - box.offsetHeight - 8)),
  }
}

function onDragEnd(event: PointerEvent) {
  const box = el.value
  if (box?.hasPointerCapture?.(event.pointerId)) box.releasePointerCapture(event.pointerId)
}

function close() {
  emit('close')
}

/** 缩略图挂了（没生成过）退到 /preview 原件代理 */
function thumbError(event: Event) {
  const img = event.target as HTMLImageElement
  if (!img.dataset.fallback) {
    img.dataset.fallback = '1'
    img.src = `/preview/${props.asset.id}`
  } else {
    img.style.display = 'none'
  }
}

/* ---- 简要信息行 ---- */

const extLabel = computed(() => (props.asset.ext || '').replace(/^\./, '').toUpperCase())
const sizeLabel = computed(() => (props.asset.bytes ? fmtBytes(props.asset.bytes) : ''))
const dimsLabel = computed(() => {
  const { width, height } = props.asset
  return width && height ? `${width}×${height}` : ''
})
const durLabel = computed(() =>
  props.asset.kind === 'video' ? fmtDur(props.asset.durationMs) : '',
)
const infoLine = computed(() =>
  [extLabel.value, sizeLabel.value, dimsLabel.value, durLabel.value, props.asset.rawExt && '+RAW']
    .filter(Boolean)
    .join(' · '),
)
const kindLabel = computed(() =>
  props.asset.kind === 'video'
    ? '视频'
    : props.asset.kind === 'live_photo'
      ? 'LIVE'
      : props.asset.kind === 'raw'
        ? 'RAW'
        : '照片',
)

/* ---- 照片压缩：质量 80 重编码，另存为压缩副本（原件保留） ---- */

const canCompress = computed(() => props.asset.kind === 'photo' || props.asset.kind === 'live_photo')
const compressing = ref(false)

async function compress() {
  if (compressing.value) return
  const ok = await dialog.confirm({
    title: '生成压缩副本？',
    message: `按质量 80 重新编码，另存为一张新照片（当前 ${sizeLabel.value || '未知大小'}），落在同一日期分组旁边。原图保留不变，确认副本没问题后可自行删除。`,
    confirmText: '生成压缩副本',
  })
  if (!ok) return
  compressing.value = true
  try {
    const res = await api.compress(props.asset.id)
    if (res.skipped) {
      toast.show('这张压不动了：重编码省不出空间，未生成副本')
      return
    }
    if (res.duplicate) {
      toast.show('压缩副本已经存在，不重复生成')
    } else {
      toast.show(`已生成压缩副本：${fmtBytes(res.before)} → ${fmtBytes(res.after)}，省 ${fmtBytes(res.saved)}`)
    }
    console.log('[peek] emit compressed', res)
    emit('compressed', { assetId: res.asset_id! })
  } catch (e) {
    toast.show('压缩失败：' + errorText(e).slice(0, 60))
  } finally {
    compressing.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="asset"
      ref="el"
      class="fixed z-[150] w-[280px] overflow-hidden rounded-xl border border-line bg-panel shadow-2xl"
      :class="pinned && 'cursor-grab touch-none'"
      :style="posStyle"
      @pointerdown="onPointerDown"
      @pointermove="onDrag"
      @pointerup="onDragEnd"
      @pointercancel="onDragEnd"
      @mouseenter="emit('keep')"
      @mouseleave="emit('leave')"
    >
      <div v-if="pinned" class="flex items-center justify-between bg-panel2 px-2.5 py-1">
        <span class="select-none text-[11px] text-mut">⠿ 按住拖动</span>
        <button class="text-[13px] leading-none text-mut tap" title="关闭预览" @click="close">
          ✕
        </button>
      </div>
      <div class="relative bg-black">
        <video
          v-if="asset.kind === 'video' && playing"
          :src="`/file/${asset.id}`"
          controls
          autoplay
          playsinline
          class="w-full"
          :style="{ maxHeight: `${layout.mediaH}px` }"
          @ended="playing = false"
        />
        <template v-else>
          <img
            v-if="asset.sha"
            :src="`/thumbs/${asset.sha}.webp`"
            alt=""
            class="w-full object-contain"
            :style="{ maxHeight: `${layout.mediaH}px` }"
            @error="thumbError"
          />
          <div
            v-else
            class="flex h-[140px] items-center justify-center text-[13px] text-mut"
          >
            没有可预览的原图
          </div>
          <button
            v-if="asset.kind === 'video'"
            class="absolute inset-0 flex items-center justify-center bg-black/30 text-[34px] text-white tap"
            title="播放视频"
            @click="playing = true"
          >
            ▶
          </button>
        </template>
      </div>
      <div class="border-t border-line px-3 py-1 text-[11px] leading-snug text-mut">
        <div class="flex items-center gap-2">
          <span class="min-w-0 flex-1 truncate">{{ infoLine || ' ' }}</span>
          <button
            v-if="canCompress"
            class="shrink-0 rounded-[7px] bg-panel2 px-2 py-[3px] text-[10.5px] font-medium text-acc tap disabled:opacity-50"
            :disabled="compressing"
            title="质量 80 重编码，覆盖原件"
            @click="compress"
          >
            {{ compressing ? '压缩中…' : '压缩' }}
          </button>
        </div>
        <div class="flex items-center justify-between">
          <span>{{ asset.taken ? fmtStamp(asset.taken) : '日期未知' }}</span>
          <span>{{ kindLabel }}</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>
