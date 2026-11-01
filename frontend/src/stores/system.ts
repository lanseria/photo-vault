import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api'
import type { Stats } from '@/types'

const EMPTY_STATS: Stats = {
  count: 0,
  gallery: 0,
  material: 0,
  material_bytes: 0,
  material_duration_ms: 0,
  favorites: 0,
  trash: 0,
  persons: 0,
  cinema_titles: 0,
  cinema_episodes: 0,
  device_count: 0,
  unknown_device: 0,
  media_root: '',
  media_host_root: '',
  bytes: 0,
  first: null,
  last: null,
  devices: [],
}

const BRIDGE = 'http://127.0.0.1:8099'

export type BridgeState = 'unknown' | 'online' | 'offline'

/**
 * 系统级状态：顶部计数的统计数据 + 桌面桥（127.0.0.1:8099）的在线情况。
 * 桥只做两件事：在宿主机上打开资源管理器、用本机播放器打开视频。
 */
export const useSystemStore = defineStore('system', () => {
  const stats = ref<Stats>({ ...EMPTY_STATS })
  const loaded = ref(false)
  const bridge = ref<BridgeState>('unknown')

  async function refreshStats() {
    try {
      stats.value = await api.stats()
      loaded.value = true
    } catch {
      /* 统计失败不影响浏览 */
    }
    return stats.value
  }

  async function checkBridge(): Promise<boolean> {
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 700)
      const res = await fetch(`${BRIDGE}/health`, { signal: controller.signal })
      clearTimeout(timer)
      bridge.value = res.ok ? 'online' : 'offline'
    } catch {
      bridge.value = 'offline'
    }
    return bridge.value === 'online'
  }

  /** 出稿后让文件出现在资源管理器里。容器模式下只能靠桌面桥。 */
  async function revealOnHost(path: string): Promise<boolean> {
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 900)
      const res = await fetch(`${BRIDGE}/reveal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
        signal: controller.signal,
      })
      clearTimeout(timer)
      return res.ok
    } catch {
      return false
    }
  }

  /** 用宿主机的默认播放器打开文件。 */
  async function openOnHost(path: string): Promise<boolean> {
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 1500)
      const res = await fetch(`${BRIDGE}/open`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
        signal: controller.signal,
      })
      clearTimeout(timer)
      return res.ok
    } catch {
      return false
    }
  }

  return { stats, loaded, bridge, refreshStats, checkBridge, revealOnHost, openOnHost }
})
