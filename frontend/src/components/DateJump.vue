<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useFeedStore } from '@/stores/feed'
import { useSelectionStore } from '@/stores/selection'
import { dateHeader, dayGroupKey } from '@/utils/date'
import AppIcon from '@/components/AppIcon.vue'

/**
 * 按日期跳转：左下角的入口按钮 + 年/月/日选择面板。
 *
 * 照片流是分页懒加载的，跳到很久以前不可能先翻完所有分页：
 * /api/assets/dates 一次拿到「哪天有几张」，倒序流里目标日期的偏移
 * = 比它新的张数之和，按页补齐到那个位置再滚到对应分组即可。
 * 回收站按删除时间排序，日期对不上，入口在回收站隐藏。
 */
const props = defineProps<{ activeKey: string }>()

const feed = useFeedStore()
const selection = useSelectionStore()

const sheetOpen = ref(false)
const days = ref<{ day: string; count: number }[]>([])
const loadingDays = ref(false)
const selYear = ref('')
const selMonth = ref('')

const jumping = ref(false)
const jumpText = ref('')

const visible = computed(
  () =>
    feed.ready &&
    !feed.error &&
    !feed.isEmpty &&
    feed.sections.length >= 2 &&
    feed.config.kind !== 'trash' &&
    !selection.active,
)

/* 入口按钮上的当前年月，跟可视位置走（activeKey 由时间轴算好传进来） */
const pillLabel = computed(() => {
  const key = props.activeKey
  if (!key || key === 'unknown') return '未知'
  const year = key.slice(0, 4)
  const month = Number(key.slice(5, 7))
  return month ? `${year}年${month}月` : `${year}年`
})

/* 换了视图/筛选，分布作废，下次打开重拉 */
watch(
  () => feed.config,
  () => {
    days.value = []
    selYear.value = ''
    selMonth.value = ''
  },
)

async function ensureDays() {
  if (loadingDays.value || days.value.length) return
  loadingDays.value = true
  try {
    days.value = (await feed.loadDates()).days
    // 默认落在最新的一年/一月
    selYear.value = days.value[0]?.day.slice(0, 4) ?? ''
    selMonth.value = days.value[0]?.day.slice(5, 7) ?? ''
  } catch {
    days.value = []
  } finally {
    loadingDays.value = false
  }
}

watch(sheetOpen, value => {
  if (value) void ensureDays()
})

/** 年份聚合（分布倒序 → 年份倒序） */
const yearList = computed(() => {
  const counts = new Map<string, number>()
  for (const d of days.value) {
    const y = d.day.slice(0, 4)
    counts.set(y, (counts.get(y) ?? 0) + d.count)
  }
  return [...counts].map(([year, count]) => ({ year, count }))
})

/** 选中年份里有照片的月份；last = 该月最新的一天（倒序流里这个月分组的开头） */
const monthList = computed(() => {
  const agg = new Map<string, { count: number; last: string }>()
  for (const d of days.value) {
    if (!d.day.startsWith(selYear.value + '-')) continue
    const key = d.day.slice(0, 7)
    const cur = agg.get(key)
    if (cur) cur.count += d.count
    else agg.set(key, { count: d.count, last: d.day })
  }
  return [...agg].map(([key, v]) => ({ key, ...v }))
})

watch(selYear, year => {
  // 切年份后，日历落到该年最近的有照片的月份
  const first = days.value.find(d => d.day.startsWith(year + '-'))
  if (first) selMonth.value = first.day.slice(5, 7)
})

/** 选中月份的日历格子：lead 是 1 号前面的空白占位数（周一起算） */
const dayCells = computed(() => {
  const y = Number(selYear.value)
  const m = Number(selMonth.value)
  if (!y || !m) return { lead: 0, cells: [] as DayCell[] }
  const prefix = `${selYear.value}-${selMonth.value}-`
  const counts = new Map<string, number>()
  for (const d of days.value) if (d.day.startsWith(prefix)) counts.set(d.day, d.count)
  const today = new Date()
  const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
  const cells: DayCell[] = []
  for (let n = 1; n <= new Date(y, m, 0).getDate(); n++) {
    const day = `${prefix}${String(n).padStart(2, '0')}`
    cells.push({ n, day, count: counts.get(day) ?? 0, today: day === todayKey })
  }
  return { lead: (new Date(y, m - 1, 1).getDay() + 6) % 7, cells }
})

interface DayCell {
  n: number
  day: string
  count: number
  today: boolean
}

/** 排在目标日期之前的条数就是它在倒序流里的偏移；无拍摄时间的条目垫底，不影响 */
function offsetOf(day: string): number {
  let n = 0
  for (const d of days.value) if (d.day > day) n += d.count
  return n
}

function dockHeight(): number {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dock-h')) || 0
}

/** 滚到目标日期的分组；分组键没落上（跨时区等边缘）就滚到该日期第一条所在分组 */
function scrollToDay(day: string, offset: number) {
  const mode = feed.config.group ?? 'day'
  const find = (key: string) => document.querySelector<HTMLElement>(`main [data-key="${key}"]`)
  const asset = feed.items[Math.min(offset, feed.items.length - 1)]
  const fallback = asset ? feed.sections.find(s => s.items.includes(asset))?.key : undefined
  const el = find(dayGroupKey(day, mode)) ?? (fallback ? find(fallback) : null)
  if (el) {
    window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - dockHeight() - 2))
  }
}

async function jumpTo(day: string | null) {
  sheetOpen.value = false
  if (day === null) {
    // 「最新」：流的顶部
    window.scrollTo({ top: 0, behavior: 'smooth' })
    return
  }
  if (jumping.value) return
  const offset = offsetOf(day)
  jumping.value = true
  jumpText.value = `正在跳转到 ${dateHeader(day)}`
  try {
    await feed.expandToIndex(offset)
    await nextTick()
    scrollToDay(day, offset)
  } finally {
    jumping.value = false
  }
}
</script>

<template>
  <button v-if="visible" class="dj-pill tap" data-date-jump @click="sheetOpen = true">
    <AppIcon name="calendar" :size="14" :stroke="2" />
    {{ pillLabel }}
  </button>

  <Teleport to="body">
    <div v-if="sheetOpen" class="fixed inset-0 z-[120]" @click.self="sheetOpen = false">
      <div class="absolute inset-0" @click="sheetOpen = false" />
      <div class="sheet z-[121] max-h-[76vh] overflow-auto">
        <div class="sheet-grab" />
        <h3 class="sheet-title">按日期跳转</h3>

        <div v-if="loadingDays" class="spinner !my-8" />
        <template v-else-if="days.length">
          <div class="no-scrollbar -mx-1 mb-3 flex gap-2 overflow-x-auto px-1 pb-1">
            <button class="chip tap shrink-0" @click="jumpTo(null)">最新</button>
            <button
              v-for="y in yearList"
              :key="y.year"
              class="chip tap shrink-0"
              :class="y.year === selYear && 'chip-on'"
              @click="selYear = y.year"
            >
              {{ y.year }}年
              <span class="chip-count">{{ y.count }}</span>
            </button>
          </div>

          <div class="mb-3 grid grid-cols-4 gap-2">
            <button
              v-for="m in monthList"
              :key="m.key"
              class="rounded-xl bg-panel py-2 text-center tap"
              @click="jumpTo(m.last)"
            >
              <div class="text-[14px] font-semibold leading-tight">{{ Number(m.key.slice(5)) }}月</div>
              <div class="text-[11px] leading-tight text-mut">{{ m.count }} 张</div>
            </button>
          </div>

          <div class="pb-2">
            <div class="mb-1 text-center text-[13px] font-semibold text-mut">
              {{ selYear }}年{{ Number(selMonth) }}月
            </div>
            <div class="grid grid-cols-7 gap-y-1 text-center">
              <span
                v-for="w in ['一', '二', '三', '四', '五', '六', '日']"
                :key="w"
                class="text-[11px] text-mut"
              >
                {{ w }}
              </span>
              <span v-for="b in dayCells.lead" :key="b" />
              <button
                v-for="c in dayCells.cells"
                :key="c.day"
                :disabled="!c.count"
                class="rounded-lg py-1 tap disabled:opacity-25"
                @click="jumpTo(c.day)"
              >
                <div class="text-[13px] leading-tight" :class="c.today && 'font-semibold text-acc'">
                  {{ c.n }}
                </div>
                <div class="text-[10px] leading-tight text-mut">{{ c.count || ' ' }}</div>
              </button>
            </div>
          </div>
        </template>
        <p v-else class="pb-6 text-center text-[14px] text-mut">这个视图里没有带拍摄时间的照片</p>
      </div>
    </div>

    <!-- 跳转进行中：按页补齐可能要一会儿，给个明确的提示 -->
    <div
      v-if="jumping"
      class="pointer-events-none fixed inset-x-0 top-1/2 z-[130] flex -translate-y-1/2 justify-center"
    >
      <div
        class="flex items-center gap-3 rounded-2xl bg-[rgba(44,44,46,.97)] px-5 py-3.5 text-[14px] font-medium shadow-[0_6px_24px_rgba(0,0,0,.5)]"
      >
        <span class="spinner !m-0 !h-5 !w-5 !border-2" />
        {{ jumpText }}
      </div>
    </div>
  </Teleport>
</template>
