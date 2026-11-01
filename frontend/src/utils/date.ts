import type { Asset } from '@/types'

const dayFmt = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'short',
})

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
