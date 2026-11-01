import { api } from '@/api'
import type { ImportRun } from '@/types'

/** 轮询影视导入进度，直到 running 结束。 */
export function watchImportRun(runId: string, onTick?: (run: ImportRun) => void): Promise<ImportRun> {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const res = await api.cinemaImportStatus(runId)
        onTick?.(res.run)
        if (res.run.state === 'running') setTimeout(tick, 700)
        else resolve(res.run)
      } catch (e) {
        reject(e)
      }
    }
    void tick()
  })
}
