import type { Asset } from '@/types'

const dayFmt = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'short',
})

const monthFmt = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long' })
const monthDayFmt = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric' })

export type GroupMode = 'day' | 'week' | 'month' | 'year'

export function isGroupMode(v: unknown): v is GroupMode {
  return v === 'day' || v === 'week' || v === 'month' || v === 'year'
}

function parseDay(day: string): Date {
  return new Date(day + 'T00:00:00')
}

function isoDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 日期分组键：taken 的日 / 周（周一起算）/ 月 / 年；没有拍摄时间的一律归到 unknown。 */
export function groupKey(asset: Asset, mode: GroupMode): string {
  const day = asset.taken ? asset.taken.slice(0, 10) : 'unknown'
  if (day === 'unknown' || mode === 'day') return day
  if (mode === 'month') return day.slice(0, 7)
  if (mode === 'year') return day.slice(0, 4)
  const start = parseDay(day)
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7)) // 回到这周的周一
  return isoDay(start)
}

export function groupHeader(key: string, mode: GroupMode): string {
  if (key === 'unknown') return '时间未知'
  if (mode === 'day') return dateHeader(key)
  if (mode === 'month') return monthFmt.format(parseDay(key + '-01'))
  if (mode === 'year') return `${key}年`
  // 周：周一 → 周日的日期范围；跨年时两端都带年份
  const start = parseDay(key)
  const end = new Date(start)
  end.setDate(end.getDate() + 6)
  const startYear = start.getFullYear()
  const endText =
    end.getFullYear() === startYear ? monthDayFmt.format(end) : `${end.getFullYear()}年${monthDayFmt.format(end)}`
  return `${startYear}年${monthDayFmt.format(start)} – ${endText}`
}

/** 日期分组键：taken 的 YYYY-MM-DD；没有拍摄时间的一律归到 unknown。 */
export function dateKey(asset: Asset): string {
  return asset.taken ? asset.taken.slice(0, 10) : 'unknown'
}

export function dateHeader(key: string): string {
  if (key === 'unknown') return '时间未知'
  const day = new Date(key + 'T00:00:00')
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const diff = Math.round((today.getTime() - day.getTime()) / 86_400_000)
  if (diff === 0) return '今天'
  if (diff === 1) return '昨天'
  return dayFmt.format(day)
}

export function takenDateText(asset: Asset): string {
  if (!asset.taken) return '时间未知'
  const d = new Date(asset.taken)
  return `${dayFmt.format(d)} ${d.toTimeString().slice(0, 5)}`
}

/** 剪辑箱分组标题用的时间范围（最早 → 最晚）。 */
export function rangeText(assets: Asset[]): string {
  const dates = assets.map(a => a.taken).filter((x): x is string => !!x).sort()
  if (!dates.length) return ''
  const first = dates[0].slice(0, 10)
  const last = dates[dates.length - 1].slice(0, 10)
  return first === last ? dateHeader(first) : `${first} → ${last}`
}
