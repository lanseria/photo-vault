import type { Asset, PersonFace } from '@/types'

/** hover 快速预览的目标资产：Asset / PersonFace 归一化后的最小字段集 */
export interface PeekAsset {
  /** 资产 id：视频播放 /file/{id}，缩略图挂了退 /preview/{id} */
  id: string
  /** 原件内容 sha → /thumbs/{sha}.webp */
  sha?: string | null
  kind?: string
  taken?: string | null
  /** 简要信息行：格式 / 大小 / 尺寸 / 时长（人脸接口补齐了这些字段） */
  ext?: string | null
  bytes?: number | null
  width?: number | null
  height?: number | null
  durationMs?: number | null
  /** RAW+JPEG 有 RAW 原件时标注 */
  rawExt?: string | null
}

export function toPeekAsset(a: Asset | PersonFace): PeekAsset {
  if ('origin' in a) {
    // Asset：格式从原始路径取后缀
    const m = /\.([a-z0-9]+)$/i.exec(a.origin || '')
    return {
      id: a.id,
      sha: a.sha,
      kind: a.kind,
      taken: a.taken,
      ext: m?.[1],
      bytes: a.bytes,
      width: a.width,
      height: a.height,
      durationMs: a.duration_ms,
      rawExt: a.raw_ext || null,
    }
  }
  return {
    id: a.asset_id,
    sha: a.sha,
    kind: a.kind,
    taken: a.taken,
    ext: a.ext,
    bytes: a.bytes,
    width: a.width,
    height: a.height,
    durationMs: a.duration_ms,
  }
}

/** 预览框定位用的锚点矩形（拿的时候从 getBoundingClientRect() 拷贝成普通对象） */
export interface PeekRect {
  left: number
  top: number
  right: number
  bottom: number
  width: number
  height: number
}

export function rectOf(el: Element): PeekRect {
  const r = el.getBoundingClientRect()
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }
}

/**
 * 智能落位：默认哪边空间大放哪边（上方优先平手），prefer='above' 强制上方；
 * 水平以锚点中心对齐，夹在视口内。
 */
export function smartPop(
  anchor: PeekRect,
  boxW: number,
  boxH: number,
  prefer: 'above' | 'auto' = 'auto',
): { left: number; top: number; above: boolean } {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const gap = 8
  const availAbove = anchor.top - gap - 8
  const availBelow = vh - anchor.bottom - gap - 8
  const above =
    prefer === 'above' ? availAbove >= Math.min(boxH, 160) : availAbove >= availBelow
  const rawTop = above ? anchor.top - gap - boxH : anchor.bottom + gap
  const cx = anchor.left + anchor.width / 2
  return {
    left: Math.max(8, Math.min(cx - boxW / 2, vw - boxW - 8)),
    top: Math.max(8, Math.min(rawTop, vh - boxH - 8)),
    above,
  }
}
