export const pad2 = (n: number | null | undefined) => String(n || 0).padStart(2, '0')

export function fmtBytes(bytes: number | null | undefined): string {
  const b = bytes || 0
  if (b >= 1 << 30) return (b / 1073741824).toFixed(1) + ' GB'
  if (b >= 1 << 20) return (b / 1048576).toFixed(0) + ' MB'
  return (b / 1024).toFixed(0) + ' KB'
}

/** 0:07 / 1:32:05，用于角标 */
export function fmtDur(ms: number | null | undefined): string {
  if (!ms) return ''
  const s = Math.round(ms / 1000)
  if (s < 3600) {
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
  }
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

/** 8 分钟 / 1 小时 12 分，用于统计行 */
export function fmtLong(ms: number | null | undefined): string {
  if (!ms) return ''
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s} 秒`
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return h ? `${h} 小时 ${m} 分` : `${m} 分钟`
}

/** 1.5s / 0:12 — 视频关键帧时间点 */
export function fmtFrameTime(ms: number): string {
  return ms >= 60000 ? fmtDur(ms) : (ms / 1000).toFixed(1) + 's'
}

/** EXIF 快门：0.004 → 1/250s；2 → 2s */
export function fmtShutter(sec: number): string {
  if (sec >= 1) return `${+sec.toFixed(1)}s`
  return `1/${Math.round(1 / sec)}s`
}

/** ISO 时间戳 → 本地 yyyy-MM-dd HH:mm:ss（同步/入库时间给人看） */
export function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

export function initials(name: string | null | undefined): string {
  return (name || '?').replace(/^Person\s+/i, '').slice(0, 1) || '?'
}

/** 中文字幕优先、非强制优先；和旧前端排序规则一致。 */
export function sortSubtitles<T extends { forced?: boolean; lang?: string | null; label?: string }>(
  subs: T[],
): T[] {
  const score = (s: T) =>
    (s.forced ? 4 : 0) +
    (s.lang && s.lang.indexOf('zh') === 0 ? 0 : 1) +
    (s.label === '字幕' ? 2 : 0)
  return subs.slice().sort((a, b) => score(a) - score(b))
}

export function fileName(path: string): string {
  return String(path).split(/[\\/]/).pop() || path
}
