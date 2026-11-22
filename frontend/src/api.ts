import type {
  Asset,
  AssetInfo,
  AssetPage,
  BinList,
  BinItems,
  CinemaList,
  CinemaTitleDetail,
  DateHistogram,
  DeliverResult,
  Device,
  DeviceList,
  Face,
  FaceQueue,
  FaceMutateResult,
  FaceMoveResult,
  Folder,
  FolderList,
  ImportRun,
  Library,
  Person,
  PersonFace,
  PersonList,
  Stats,
  TagCount,
  VideoInfo,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** 把 FastAPI 的 {"detail": "..."} 或纯文本变成一句能直接给用户看的话。 */
export function errorText(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e ?? '')
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const detail = (parsed as { detail: unknown }).detail
      return typeof detail === 'string' ? detail : JSON.stringify(detail)
    }
  } catch {
    /* 不是 JSON，原样返回 */
  }
  return raw
}

export type QueryValue = string | number | boolean | null | undefined

/** 空字符串 / false / null 一律不发，避免后端把 "" 当成筛选值。 */
export function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) throw new ApiError(res.status, await res.text())
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function jsonBody(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export interface AssetQuery {
  limit?: number
  offset?: number
  kind?: string
  q?: string
  favorite?: boolean
  trash?: boolean
  tag?: string
  person_id?: string
  library?: string
  min_sec?: number
  max_sec?: number
  min_px?: number
  /** 设备 id 字符串，或 'unknown' = 未识别设备 */
  device?: string
  /** 文件夹 id（任意库），或 'none' = 未整理 */
  folder?: string
}

export const api = {
  stats: () => request<Stats>('/api/stats'),

  assets: (params: AssetQuery) => request<AssetPage>(`/api/assets${queryString({ ...params })}`),
  /** 日期分布（同一套筛选）：日期跳转面板用它算出任意日期在流里的偏移 */
  dates: (params: AssetQuery) => request<DateHistogram>(`/api/assets/dates${queryString({ ...params })}`),
  asset: (id: string) => request<Asset>(`/api/assets/${id}`),
  /** 灯箱详细信息面板：拍摄/同步/来源/EXIF/GPS 一次给全 */
  info: (id: string) => request<AssetInfo>(`/api/assets/${id}/info`),

  analyze: (ids: string[]) =>
    request<{ queued: number; by_kind: Record<string, number> }>(
      '/api/assets/analyze',
      jsonBody('POST', { ids }),
    ),

  batch: (body: {
    ids: string[]
    action:
      | 'favorite'
      | 'unfavorite'
      | 'trash'
      | 'restore'
      | 'move'
      | 'tag'
      | 'untag'
      | 'set_folder'
      | 'clear_folder'
    tag?: string
    library?: string
    folder_id?: string | null
  }) => request<{ updated: number }>('/api/assets/batch', jsonBody('POST', body)),

  favorite: (id: string, favorite: boolean) =>
    request<{ favorite: boolean }>(
      `/api/assets/${id}/favorite`,
      jsonBody('POST', { favorite }),
    ),
  trash: (id: string) => request<{ ok: boolean }>(`/api/assets/${id}/trash`, { method: 'POST' }),
  restore: (id: string) =>
    request<{ ok: boolean }>(`/api/assets/${id}/restore`, { method: 'POST' }),
  /** 照片压缩：质量 80 重编码另存为副本（原件保留）；duplicate=库里已有相同内容 */
  compress: (id: string) =>
    request<{
      created: boolean
      skipped?: boolean
      duplicate?: boolean
      asset_id?: string
      before: number
      after: number
      saved: number
    }>(`/api/assets/${id}/compress`, { method: 'POST' }),
  purge: (id: string) => request<{ ok: boolean }>(`/api/assets/${id}`, { method: 'DELETE' }),

  tags: (limit = 400) => request<{ tags: TagCount[] }>(`/api/tags?limit=${limit}`),
  addTag: (id: string, name: string) =>
    request<{ id: string; tags: string[] }>(
      `/api/assets/${id}/tags`,
      jsonBody('POST', { name }),
    ),
  removeTag: (id: string, name: string) =>
    request<{ id: string; tags: string[] }>(
      `/api/assets/${id}/tags/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    ),

  faces: (id: string) => request<{ faces: Face[] }>(`/api/assets/${id}/faces`),
  video: (id: string) => request<VideoInfo>(`/api/assets/${id}/video`),

  devices: () => request<DeviceList>('/api/devices'),
  createDevice: (name: string, kind: string) =>
    request<Device>('/api/devices', jsonBody('POST', { name, kind })),
  renameDevice: (id: number, label: string) =>
    request<Device>(`/api/devices/${id}`, jsonBody('PATCH', { label })),
  mergeDevice: (id: number, into: number) =>
    request<{ into: number; moved: number; device: Device }>(
      `/api/devices/${id}/merge`,
      jsonBody('POST', { into }),
    ),
  assignDevice: (ids: string[], deviceId: number | null) =>
    request<{ updated: number; device_id: number | null }>(
      '/api/devices/assign',
      jsonBody('POST', { ids, device_id: deviceId }),
    ),

  /** 人脸任务队列：网页只管入队 / 启停 / 看进度，提取在宿主机 worker 里跑 */
  facesQueue: () => request<FaceQueue>('/api/faces/queue'),
  runFaces: () =>
    request<{ queued: number }>('/api/faces/queue/run', jsonBody('POST', {})),
  pauseFaces: (paused: boolean) =>
    request<{ paused: boolean }>(
      '/api/faces/queue/pause',
      jsonBody('POST', { paused }),
    ),

  persons: () => request<PersonList>('/api/persons'),
  renamePerson: (id: string, name: string) =>
    request<Person>(`/api/persons/${id}`, jsonBody('PATCH', { name })),
  mergePerson: (id: string, into: string) =>
    request<{ into: string; faces: number }>(
      `/api/persons/${id}/merge`,
      jsonBody('POST', { into }),
    ),
  dissolvePerson: (id: string) =>
    request<{ id: string; faces: number }>(`/api/persons/${id}`, { method: 'DELETE' }),
  /** 上传人物封面：原始字节流（和 /api/upload 一个路数），服务端裁方压缩。
   *  slot=1 主封面、slot=2 备用封面 */
  uploadPersonCover: (id: string, file: File, slot: 1 | 2 = 1) =>
    request<Person>(`/api/persons/${id}/cover${slot === 2 ? '?slot=2' : ''}`, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    }),
  /** 删掉某一张自定义封面，回退到系统挑的人脸裁剪 */
  resetPersonCover: (id: string, slot: 1 | 2 = 1) =>
    request<Person>(`/api/persons/${id}/cover${slot === 2 ? '?slot=2' : ''}`, {
      method: 'DELETE',
    }),

  /** 人物的全部脸（「人脸」抽屉）：已忽略的排后面 */
  personFaces: (id: string) => request<{ faces: PersonFace[] }>(`/api/persons/${id}/faces`),
  /** 忽略人脸：标记 + 记住 embedding 规则，重跑识别不再收回 */
  ignorePersonFaces: (id: string, ids: string[]) =>
    request<FaceMutateResult>(`/api/persons/${id}/faces/ignore`, jsonBody('POST', { ids })),
  /** 恢复被忽略的人脸（同时删掉忽略规则） */
  restorePersonFaces: (id: string, ids: string[]) =>
    request<FaceMutateResult>(`/api/persons/${id}/faces/restore`, jsonBody('POST', { ids })),
  /** 把若干脸移到另一个人物（聚类纠错） */
  movePersonFaces: (id: string, ids: string[], into: string) =>
    request<FaceMoveResult>(`/api/persons/${id}/faces/move`, jsonBody('POST', { ids, into })),

  bins: () => request<BinList>('/api/bins'),
  createBin: (name: string) =>
    request<{ id: string; name: string }>('/api/bins', jsonBody('POST', { name })),
  renameBin: (id: string, name: string) =>
    request<{ id: string; name: string }>(`/api/bins/${id}`, jsonBody('PATCH', { name })),
  binItems: (id: string) => request<BinItems>(`/api/bins/${id}/items`),
  addToBin: (id: string, ids: string[]) =>
    request<{ added: number; moved: number; total: number }>(
      `/api/bins/${id}/items`,
      jsonBody('POST', { ids }),
    ),
  removeFromBin: (id: string, assetId: string) =>
    request<{ ok: boolean }>(`/api/bins/${id}/items/${assetId}`, { method: 'DELETE' }),
  clearBin: (id: string) =>
    request<{ cleared: number }>(`/api/bins/${id}/items`, { method: 'DELETE' }),
  deliverBin: (id: string, mode: 'link' | 'copy' = 'link') =>
    request<DeliverResult>(`/api/bins/${id}/deliver`, jsonBody('POST', { mode })),

  folders: (library: Library) => request<FolderList>(`/api/folders?library=${library}`),
  createFolder: (name: string, library: Library) =>
    request<Folder>('/api/folders', jsonBody('POST', { name, library })),
  renameFolder: (id: string, name: string) =>
    request<Folder>(`/api/folders/${id}`, jsonBody('PATCH', { name })),
  deleteFolder: (id: string) =>
    request<{ deleted: number }>(`/api/folders/${id}`, { method: 'DELETE' }),

  cinema: (q = '') =>
    request<CinemaList>(`/api/cinema${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  cinemaTitle: (id: string) => request<CinemaTitleDetail>(`/api/cinema/${id}`),
  cinemaEdit: (id: string, body: { title?: string; year?: number | null; overview?: string }) =>
    request<{ id: string }>(`/api/cinema/${id}`, jsonBody('PATCH', body)),
  cinemaDelete: (id: string) => request<{ ok: boolean }>(`/api/cinema/${id}`, { method: 'DELETE' }),
  cinemaImport: (body: { path: string; kind: string; posters: boolean }) =>
    request<{ run: ImportRun }>('/api/cinema/import', jsonBody('POST', body)),
  cinemaImportStatus: (runId: string) =>
    request<{ run: ImportRun }>(`/api/cinema/import/${runId}`),
  cinemaRescan: (id: string) =>
    request<{ run: ImportRun }>(`/api/cinema/${id}/rescan`, { method: 'POST' }),
  cinemaOpenEpisode: (id: string) =>
    request<{ opened: boolean; host_path: string }>(
      `/api/cinema/episodes/${id}/open`,
      { method: 'POST' },
    ),
}
