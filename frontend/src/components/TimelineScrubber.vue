<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useFeedStore } from '@/stores/feed'
import DateJump from '@/components/DateJump.vue'

/**
 * 右侧时间轴（iOS 相册式的快速定位条）：
 *   · 每个「时间段」在轨道上有一个刻度，位置与它在页面里的高度成比例；
 *   · 分组少（按年 / 按月）时直接在刻度旁标出年月，一眼能点到想去的位置；
 *   · 当前看到的时间段高亮，拖动的气泡始终显示落点；
 *   · 按住拖动 → 页面立刻滚到对应位置，松手即停。
 */
const feed = useFeedStore()

const visible = computed(() => feed.ready && !feed.error && !feed.isEmpty && feed.sections.length >= 2)

interface Mark {
  key: string
  label: string
  /** 在整个文档里的纵向位置（px） */
  top: number
}

interface Tick {
  key: string
  label: string
  /** 0-1，轨道上的相对位置 */
  pct: number
}

const rail = ref<HTMLElement | null>(null)
const marks = ref<Mark[]>([])
const ticks = ref<Tick[]>([])
const activeKey = ref('')

const scrubbing = ref(false)
const thumbPct = ref(0)
const bubbleLabel = ref('')
const bubbleTop = ref(0)

let railTop = 0
let railHeight = 0
let measureFrame = 0
let scrollFrame = 0
let resizeTimer = 0

/** 标签只在分组不太多时显示（按年十来条、按月几条）；按日上千条只留刻度 */
const showLabels = computed(() => ticks.value.length > 0 && ticks.value.length <= 16)

/** 标签短一点：按月的年份由相邻标签自己体现，不重复占宽 */
function shortLabel(label: string): string {
  const mode = feed.config.group ?? 'day'
  if (mode === 'month' || mode === 'day' || mode === 'week') {
    return label.replace(/^\d{4}年/, '')
  }
  return label
}

function dockHeight(): number {
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--dock-h')
  return parseFloat(raw) || 0
}

function maxScroll(): number {
  return Math.max(1, document.documentElement.scrollHeight - window.innerHeight)
}

function measure() {
  measureFrame = 0
  if (!visible.value) {
    marks.value = []
    ticks.value = []
    return
  }
  const nodes = document.querySelectorAll<HTMLElement>('main [data-section]')
  if (!nodes.length) {
    marks.value = []
    ticks.value = []
    return
  }
  const offset = window.scrollY
  const dockH = dockHeight()
  const max = maxScroll()
  const nextMarks: Mark[] = []
  nodes.forEach((el, i) => {
    const section = feed.sections[i]
    if (!section) return
    nextMarks.push({
      key: section.key,
      label: section.label,
      top: el.getBoundingClientRect().top + offset,
    })
  })
  marks.value = nextMarks
  const pcts = nextMarks.map(m => Math.min(1, Math.max(0, (m.top - dockH) / max)))
  // 按日分组时可能有上千段：均匀抽样，轨道上最多留 ~140 个刻度
  const step = Math.ceil(pcts.length / 140)
  const nextTicks: Tick[] = []
  for (let i = 0; i < pcts.length; i += step) {
    nextTicks.push({ key: nextMarks[i].key, label: nextMarks[i].label, pct: pcts[i] })
  }
  ticks.value = nextTicks
  syncActive()
}

function scheduleMeasure() {
  if (measureFrame) return
  measureFrame = requestAnimationFrame(measure)
}

/** 当前视口顶部显示的是哪个时间段 */
function syncActive() {
  scrollFrame = 0
  if (!visible.value || !marks.value.length) {
    activeKey.value = ''
    return
  }
  const at = window.scrollY + dockHeight() + 4
  let key = marks.value[0].key
  for (const mark of marks.value) {
    if (mark.top <= at) key = mark.key
    else break
  }
  activeKey.value = key
}

function onScroll() {
  if (scrollFrame) return
  scrollFrame = requestAnimationFrame(syncActive)
}

function readRail() {
  const el = rail.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  railTop = rect.top
  railHeight = rect.height
}

/** 拖动/点击轨道：比例 → 目标滚动位置，同时更新气泡里的时间段名 */
function onPointerDown(event: PointerEvent) {
  if (!visible.value) return
  readRail()
  scrubbing.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  moveTo(event)
}

function moveTo(event: PointerEvent): void {
  if (!scrubbing.value) return
  const ratio = Math.min(1, Math.max(0, (event.clientY - railTop) / Math.max(1, railHeight)))
  applyRatio(ratio)
  const limitTop = dockHeight() + 16
  bubbleTop.value = Math.min(window.innerHeight - 26, Math.max(limitTop, event.clientY))
}

/** 按比例定位。拖到底部会触发无限加载，页尾变远时靠这里重新贴住拇指 */
function applyRatio(ratio: number): void {
  thumbPct.value = ratio
  const target = ratio * maxScroll()
  window.scrollTo(0, target)
  bubbleLabel.value = sectionAt(target)
}

function onPointerMove(event: PointerEvent) {
  if (scrubbing.value) moveTo(event)
}

function onPointerUp(event: PointerEvent) {
  if (!scrubbing.value) return
  scrubbing.value = false
  const el = event.currentTarget as HTMLElement | null
  if (el?.hasPointerCapture?.(event.pointerId)) el.releasePointerCapture(event.pointerId)
}

/** 当前滚动位置顶部显示的是哪个时间段 */
function sectionAt(scrollY: number): string {
  const at = scrollY + dockHeight() + 4
  let label = marks.value[0]?.label ?? ''
  for (const mark of marks.value) {
    if (mark.top <= at) label = mark.label
    else break
  }
  return label
}

watch(
  () => feed.sections.length,
  () => {
    // 等新分组的 DOM 真正挂出来再量高度
    requestAnimationFrame(scheduleMeasure)
  },
)
watch(
  // 同一分组里追加新一页时，段数不变但内容变高，刻度位置也要跟着重算
  () => feed.items.length,
  () => {
    scheduleMeasure()
    // 拖动过程中页面还在变长（拖到底部会继续加载）：保持拇指位置，重新落位
    if (scrubbing.value) requestAnimationFrame(() => applyRatio(thumbPct.value))
  },
)
watch(visible, value => {
  if (value) scheduleMeasure()
})

function onResize() {
  clearTimeout(resizeTimer)
  resizeTimer = window.setTimeout(measure, 120)
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  window.addEventListener('scroll', onScroll, { passive: true })
  scheduleMeasure()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('scroll', onScroll)
  clearTimeout(resizeTimer)
  if (measureFrame) cancelAnimationFrame(measureFrame)
  if (scrollFrame) cancelAnimationFrame(scrollFrame)
})
</script>

<template>
  <div
    v-if="visible"
    ref="rail"
    data-tl-rail
    class="tl-rail"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
  >
    <span
      v-for="(tick, i) in ticks"
      :key="`${tick.key}-${i}`"
      class="tl-tick"
      :class="tick.key === activeKey && 'tl-tick-on'"
      :style="{ top: (tick.pct * 100).toFixed(2) + '%' }"
    />
    <template v-if="showLabels">
      <span
        v-for="(tick, i) in ticks"
        :key="`label-${tick.key}-${i}`"
        class="tl-label"
        :class="tick.key === activeKey && 'tl-label-on'"
        :style="{ top: (tick.pct * 100).toFixed(2) + '%' }"
      >
        {{ shortLabel(tick.label) }}
      </span>
    </template>
    <span v-if="scrubbing" class="tl-thumb" :style="{ top: (thumbPct * 100).toFixed(2) + '%' }" />
  </div>

  <!-- 左下角的按日期跳转入口：activeKey 就是当前看到的时间段 -->
  <DateJump :active-key="activeKey" />

  <Teleport to="body">
    <div
      v-if="scrubbing"
      data-tl-bubble
      class="tl-bubble"
      :style="{ top: bubbleTop.toFixed(0) + 'px' }"
    >
      {{ bubbleLabel }}
    </div>
  </Teleport>
</template>