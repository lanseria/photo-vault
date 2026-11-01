import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { errorText } from '@/api'
import type { Library, UploadResult } from '@/types'
import { newBatchId, UPLOAD_EXT, type DroppedFile } from '@/utils/files'
import { useDevicesStore } from './devices'
import { useFeedStore } from './feed'
import { useSystemStore } from './system'
import { useToastStore } from './toast'

export type UploadStatus = 'wait' | 'up' | 'ok' | 'dup' | 'err'

export interface UploadJob {
  id: number
  file: File
  name: string
  status: UploadStatus
  /** 0..1 */
  pct: number
  note: string
}

const LIB_KEY = 'vault.upLib'

function uploadOne(job: UploadJob, batch: string, library: Library, onProgress: (pct: number) => void) {
  return new Promise<UploadResult>((resolve, reject) => {
    const search = new URLSearchParams({ name: job.name, library, batch })
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/upload?${search.toString()}`)
    xhr.upload.onprogress = ev => {
      if (ev.lengthComputable) onProgress(ev.loaded / ev.total)
    }
    xhr.upload.onload = () => onProgress(1) // 字节传完，服务器还在探测/做缩略图
    xhr.onload = () => {
      let body: (UploadResult & { detail?: string }) | null = null
      try {
        body = JSON.parse(xhr.responseText) as UploadResult
      } catch {
        /* 非 JSON 错误页 */
      }
      if (xhr.status >= 200 && xhr.status < 300 && body) resolve(body)
      else reject(new Error(body?.detail || `HTTP ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error('网络中断'))
    xhr.ontimeout = () => reject(new Error('上传超时'))
    xhr.send(job.file)
  })
}

/**
 * 网页导入：文件直接推进来，后端走和 CLI 同一条入库流水线
 * （sha256 去重、EXIF/ffprobe、Live Photo 配对、缩略图）。
 * 一次拖拽共用一个 batch，后端据此把同目录的 Live Photo 配成一对。
 */
export const useUploadStore = defineStore('upload', () => {
  const open = ref(false)
  const queue = ref<UploadJob[]>([])
  const running = ref(false)
  const library = ref<Library>((localStorage.getItem(LIB_KEY) as Library) || 'gallery')
  const skipped = ref(0)
  const added = ref(0)
  const dup = ref(0)
  const fail = ref(0)
  /** 这批里 EXIF 没给出机型、需要人工指派的资产 */
  const unknownIds = ref<string[]>([])
  const assigning = ref(false)

  let batch = ''
  let seq = 0

  const finished = computed(() => queue.value.filter(j => j.status !== 'wait' && j.status !== 'up').length)

  function setLibrary(lib: Library) {
    if (running.value) return
    library.value = lib
    localStorage.setItem(LIB_KEY, lib)
  }

  function reset() {
    queue.value = []
    added.value = 0
    dup.value = 0
    fail.value = 0
    skipped.value = 0
    unknownIds.value = []
  }

  function openDialog(defaultLib: Library) {
    if (!running.value) {
      // 记住上次选的库；没有记录就用当前视图所在的库
      library.value = (localStorage.getItem(LIB_KEY) as Library) || defaultLib
    }
    open.value = true
  }

  function closeDialog() {
    open.value = false
    if (!running.value) reset() // 跑到一半关掉时保留队列，重新打开能接着看
  }

  function addFiles(files: DroppedFile[], defaultLib: Library) {
    const toast = useToastStore()
    const picks: UploadJob[] = []
    let rejected = 0
    for (const entry of files) {
      const file = entry.file
      const name = entry.name || file?.name || ''
      if (!file || !file.size || !UPLOAD_EXT.test(name)) {
        rejected += 1
        continue
      }
      picks.push({ id: ++seq, file, name, status: 'wait', pct: 0, note: '' })
    }
    skipped.value += rejected
    if (!picks.length) {
      if (rejected) toast.show(`已跳过 ${rejected} 个非图片/视频文件`)
      return
    }
    if (!running.value) batch = newBatchId()
    queue.value.push(...picks)
    openDialog(defaultLib)
    void pump()
  }

  async function pump() {
    if (running.value) return
    running.value = true
    const toast = useToastStore()
    const system = useSystemStore()
    const feed = useFeedStore()
    const round = { added: 0, dup: 0, fail: 0 }

    try {
      for (;;) {
        const job = queue.value.find(j => j.status === 'wait')
        if (!job) break
        job.status = 'up'
        job.pct = 0
        try {
          const res = await uploadOne(job, batch, library.value, pct => {
            job.pct = pct
          })
          if (res.status === 'duplicate') {
            job.status = 'dup'
            job.note = res.asset?.library === 'material' ? '已在素材库' : '已在图库'
            dup.value += 1
            round.dup += 1
          } else {
            job.status = 'ok'
            added.value += 1
            round.added += 1
          }
          const asset = res.asset
          if (asset?.device_unknown && !unknownIds.value.includes(asset.id)) {
            unknownIds.value.push(asset.id)
          }
        } catch (e) {
          job.status = 'err'
          job.note = errorText(e)
          fail.value += 1
          round.fail += 1
        }
        job.pct = 1
      }
    } finally {
      running.value = false
    }

    const parts = [`新增 ${round.added}`, `重复 ${round.dup}`]
    if (round.fail) parts.push(`失败 ${round.fail}`)
    if (skipped.value) parts.push(`跳过 ${skipped.value}`)
    toast.show(`导入完成：${parts.join('，')}`)
    await system.refreshStats()
    feed.requestReload()
  }

  /** 把本批「没识别出设备」的照片指到某台设备上。 */
  async function assignUnknown(deviceId: number) {
    const ids = unknownIds.value.slice()
    if (!ids.length) return 0
    assigning.value = true
    try {
      const updated = await useDevicesStore().assign(ids, deviceId)
      unknownIds.value = []
      const device = useDevicesStore().byId.get(deviceId)
      useToastStore().show(`已把 ${updated} 项归到「${device?.name ?? '设备'}」`)
      await useSystemStore().refreshStats()
      useFeedStore().requestReload()
      return updated
    } finally {
      assigning.value = false
    }
  }

  function dismissUnknown() {
    unknownIds.value = []
  }

  return {
    open,
    queue,
    running,
    library,
    skipped,
    added,
    dup,
    fail,
    finished,
    unknownIds,
    assigning,
    setLibrary,
    openDialog,
    closeDialog,
    addFiles,
    reset,
    assignUnknown,
    dismissUnknown,
  }
})
