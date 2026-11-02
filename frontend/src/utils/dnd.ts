/**
 * 站内拖拽：照片格子 → 素材库文件夹。
 *
 * dataTransfer 里带一个自定义类型（JSON 数组的资产 id）。
 * 浏览器把「文件拖入」的 types 标成 'Files'，两者天然区分：
 * 文件夹卡片只认 ASSET_DRAG_TYPE，外部文件落进来一律不接。
 */
export const ASSET_DRAG_TYPE = 'application/x-pv-asset-ids'

export function isAssetDrag(event: DragEvent): boolean {
  return !!event.dataTransfer?.types.includes(ASSET_DRAG_TYPE)
}

export function setAssetDragData(event: DragEvent, ids: string[]) {
  // text/plain 兜底：Firefox 要求至少 setData 一次才肯起拖
  event.dataTransfer?.setData(ASSET_DRAG_TYPE, JSON.stringify(ids))
  event.dataTransfer?.setData('text/plain', ids.join(','))
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copyMove'
}

export function readAssetDragIds(event: DragEvent): string[] {
  const raw = event.dataTransfer?.getData(ASSET_DRAG_TYPE)
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}
