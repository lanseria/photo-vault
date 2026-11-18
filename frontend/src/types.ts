/** 后端 /api 的响应类型。字段名与 vault/web.py 的 JSON 逐一对应。 */

export type AssetKind = 'photo' | 'video' | 'live_photo' | 'raw' | (string & {})
export type Library = 'gallery' | 'material'

export interface Asset {
  id: string
  kind: AssetKind
  library: Library
  sha: string
  favorite: boolean
  /** 本地拍摄时间（不带时区），如 2024-05-01T18:22:31 */
  taken: string | null
  tz_source?: string | null
  model: string
  /** devices.id；null = EXIF 里没有机型，未识别 */
  device_id?: number | null
  device_unknown?: boolean
  device_kind?: string | null
  /** EXIF 里的原始机型号，用于设备管理页对照 */
  device_raw?: string
  width: number | null
  height: number | null
  duration_ms: number | null
  bytes: number
  has_thumb: boolean
  origin: string
  /** RAW+JPEG：有 RAW 原件时非空（如 ".dng"），出稿会连同 JPEG 一起给 */
  raw_ext: string
  raw_bytes: number
  tags: string[]
  /** 素材库文件夹；null = 未整理 */
  folder_id?: string | null
  deleted?: boolean
  /** 剪辑箱里的序号 */
  ord?: number
}

export interface AssetPage {
  total: number
  items: Asset[]
}

export interface Stats {
  count: number
  gallery: number
  material: number
  material_bytes: number
  material_duration_ms: number
  favorites: number
  trash: number
  persons: number
  cinema_titles: number
  cinema_episodes: number
  device_count: number
  unknown_device: number
  media_root: string
  media_host_root: string
  bytes: number
  first: string | null
  last: string | null
  devices: { model: string; count: number }[]
}

export interface Person {
  id: string
  name: string
  cover: string | null
  /** true = 用了上传的自定义封面（/covers/{id}.jpg），false = 系统挑的人脸裁剪 */
  custom_cover?: boolean
  /** true = 备用封面（/covers/{id}.jpg?slot=2）已上传；主封面缺失时顶上 */
  backup_cover?: boolean
  n_faces: number
  n_assets: number
}

export interface PersonList {
  persons: Person[]
  unassigned_faces: number
}

/** 人脸任务队列（/api/faces/queue）：worker 在宿主机跑，网页只管启停和看进度 */
export interface FaceQueue {
  paused: boolean
  pending: number
  running: number
  ok: number
  failed: number
  assets_total: number
  assets_done: number
}

export interface TagCount {
  id: number | string
  name: string
  count: number
}

export interface BinSummary {
  id: string
  name: string
  note: string | null
  items: number
  dur_ms: number
  bytes: number
}

export interface BinList {
  bins: BinSummary[]
}

export interface BinItems {
  items: Asset[]
}

/** 文件夹：图库和素材库各有自己的一套（library 区分） */
export interface Folder {
  id: string
  name: string
  library: Library
  count: number
  /** 封面缩略图 URL（文件夹里最新一条素材）；空文件夹为 null */
  cover: string | null
  created_at: string | null
  updated_at: string | null
}

export interface FolderList {
  folders: Folder[]
  /** 本库里未归入任何文件夹的素材数 */
  unfiled: number
}

/** 灯箱「详细信息」面板：/api/assets/{id}/info 的响应 */
export interface AssetInfoSource {
  kind: string | null
  label: string | null
  root: string | null
}

export interface AssetInfoDevice {
  make: string | null
  model_raw: string
  model_canonical: string | null
  kind: string | null
}

export interface AssetInfoExif {
  iso: number | null
  aperture: number | null
  /** 快门秒数，如 0.004 = 1/250s */
  shutter: number | null
  focal_mm: number | null
  orientation: number | null
}

export interface AssetInfo {
  id: string
  kind: AssetKind
  library: Library
  /** 拍摄时间（本地时区不带偏移） */
  taken: string | null
  tz_offset_min: number | null
  /** exif | gps | default | user */
  tz_source: string | null
  /** 同步时间：这份文件进库的时间 */
  synced_at: string | null
  /** 入库时间：资产首次登记的时间 */
  added_at: string | null
  /** 原始地址：导入时文件在来源里的路径（已映射成宿主机路径给人看） */
  src_path: string | null
  /** 库内位置：内容寻址的相对路径 */
  origin_rel: string
  source: AssetInfoSource | null
  ext: string | null
  mime: string | null
  bytes: number
  raw_ext: string
  raw_bytes: number
  width: number | null
  height: number | null
  duration_ms: number | null
  sha: string
  device: AssetInfoDevice | null
  lens: string | null
  exif: AssetInfoExif | null
  gps: { lon: number; lat: number } | null
}

export interface Face {
  id: string
  person_id: string | null
  person: string
  bbox: [number, number, number, number]
  det_score: number
  /** true = 用户已忽略（误检/杂物），UI 里不再展示 */
  ignored?: boolean
}

/** /api/persons/{id}/faces：「人脸」抽屉里的一张脸 */
export interface PersonFace {
  id: string
  asset_id: string
  ignored: boolean
  det_score: number
  quality: number | null
  taken: string | null
  /** 照片 / 视频 / raw…（hover 预览区分播放方式） */
  kind?: string
  /** 原件内容 sha → /thumbs/{sha}.webp 海报 */
  sha?: string | null
}

/** 忽略/恢复人脸的响应：person 为人物最新状态（person_removed=true 时人物已不存在） */
export interface FaceMutateResult {
  ignored?: number
  restored?: number
  person?: Person
  person_removed?: boolean
  id?: string
}

/** 移动人脸的响应：from / into 为两边人物的最新状态 */
export interface FaceMoveResult {
  moved: number
  from: Person
  into: Person
}

export interface VideoFrame {
  ord: number
  t_ms: number
  width: number | null
  height: number | null
  diff: number | null
  caption: string | null
  labels: unknown
  ocr: string | null
  url: string | null
}

export interface VideoSummary {
  n_frames: number
  caption: string | null
  labels: unknown
  ocr: string | null
  model: string | null
  ms: number | null
}

export interface VideoInfo {
  summary: VideoSummary | null
  frames: VideoFrame[]
}

export interface CinemaTitleSummary {
  id: string
  kind: 'movie' | 'series'
  title: string
  year: number | null
  overview: string | null
  poster: string | null
  n_episodes: number
  n_seasons: number
  first_season: number
  duration_ms: number
  bytes: number
  subtitles: number
  updated_at: string | null
}

export interface CinemaSubtitle {
  id: string
  lang: string | null
  label: string
  format: string | null
  forced: boolean
}

export interface CinemaEpisode {
  id: string
  season: number
  episode: number
  name: string | null
  bytes: number
  duration_ms: number | null
  width: number | null
  height: number | null
  codec: string | null
  subtitles: CinemaSubtitle[]
  missing: boolean
  host_path: string
}

export interface CinemaTitleDetail {
  id: string
  kind: 'movie' | 'series'
  title: string
  year: number | null
  overview: string | null
  poster: string | null
  can_rescan: boolean
  seasons: { season: number; episodes: CinemaEpisode[] }[]
  n_episodes: number
  duration_ms: number
  bytes: number
}

export interface CinemaList {
  titles: CinemaTitleSummary[]
  media_root: string
  media_host_root: string
}

export interface ImportRun {
  id: string
  state: 'running' | 'done' | 'error'
  kind: string
  path: string
  host_path: string
  total: number
  done: number
  scanned: number
  imported: number
  updated: number
  failed: number
  titles: number
  subtitles: number
  current: string
  errors: { path: string; error: string }[]
  error: string | null
}

export interface UploadResult {
  status: 'imported' | 'duplicate'
  asset?: Asset
  queued?: number
}

export interface DeliverResult {
  items: number
  bytes: number
  missing: number
  host_dir: string
  opened: boolean
}

export interface Device {
  id: number
  /** 展示名：label > model_canonical > model_raw > 未识别设备 */
  name: string
  label: string
  make: string
  model_raw: string
  model_canonical: string
  kind: string
  n_assets: number
  n_gallery: number
  first: string | null
  last: string | null
}

export interface DeviceList {
  devices: Device[]
  /** 没有 device_id 的资产数 */
  unknown: number
}
