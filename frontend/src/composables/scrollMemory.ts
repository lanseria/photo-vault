import type { RouteLocationNormalizedLoaded } from 'vue-router'
import { useRoute } from 'vue-router'
import { onBeforeUnmount, onMounted } from 'vue'
import { watch } from 'vue'
import { sleep } from '@/utils/format'

const PREFIX = 'pv:scroll:'
const MAX_WAIT_MS = 6000
const STEP_MS = 50

/**
 * 滚动位置。按「路径 + 查询参数（不含 photo）」分桶存在 sessionStorage：
 *   · 刷新 → 同一个 key，恢复
 *   · 浏览器前进/后退 → 同一个 key，恢复
 *   · 开灯箱（?photo=xxx）不算换位置，不触发恢复
 * 浏览器自带的 scrollRestoration 在 main.ts 里关掉了，避免两边打架。
 */
export function scrollKey(route: Pick<RouteLocationNormalizedLoaded, 'path' | 'query'>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(route.query)) {
    if (key === 'photo') continue
    if (typeof value === 'string') search.set(key, value)
  }
  const qs = search.toString()
  return route.path + (qs ? `?${qs}` : '')
}

export function readScroll(key: string): number {
  try {
    const raw = sessionStorage.getItem(PREFIX + key)
    return raw ? Number(raw) || 0 : 0
  } catch {
    return 0
  }
}

function writeScroll(key: string, y: number) {
  try {
    sessionStorage.setItem(PREFIX + key, String(Math.max(0, Math.round(y))))
  } catch {
    /* 隐私模式 / 配额满：记不住就算了 */
  }
}

/**
 * 等页面长到够高再把滚动位置摆过去。
 * 照片流是分页 + 懒加载的，刷新时先只有第一页，直接 scrollTo 会被浏览器夹到底部。
 */
export async function restoreScroll(
  key: string,
  token?: { cancelled: boolean },
): Promise<void> {
  const target = readScroll(key)
  if (!target) {
    window.scrollTo(0, 0)
    return
  }
  const deadline = Date.now() + MAX_WAIT_MS
  while (Date.now() < deadline) {
    if (token?.cancelled) return
    if (document.documentElement.scrollHeight - window.innerHeight >= target) {
      window.scrollTo(0, target)
      if (Math.abs(window.scrollY - target) < 2) return
    }
    await sleep(STEP_MS)
  }
}

/**
 * 灯箱 / 播放器这种全屏浮层会把 body 设成 overflow:hidden，
 * Chrome 在这种情况下会把滚动位置归零。所以浮层打开时：
 *   · 先记住当前位置
 *   · 暂停滚动记录（否则归零的 0 会覆盖掉记忆）
 *   · 关闭时再摆回去
 */
let locked = false
let saveActive: (() => void) | null = null
let restoreActive: (() => Promise<void>) | null = null

export function lockScroll() {
  if (locked) return
  saveActive?.()
  locked = true
  document.body.classList.add('overlay-open')
}

export function unlockScroll() {
  if (!locked) return
  locked = false
  document.body.classList.remove('overlay-open')
  void restoreActive?.()
}

/**
 * 在 App.vue 里调用一次：滚动时记录，路由变化时恢复。
 * 只靠「路径 + 查询」判断，所以同一页面里打开/关闭灯箱不会让页面跳动。
 */
export function useScrollMemory() {
  const route = useRoute()
  let key = scrollKey(route)
  let frame = 0
  let token = { cancelled: false }

  const save = () => writeScroll(key, window.scrollY)

  const restore = () => restoreScroll(key, token)

  const onScroll = () => {
    if (frame || locked) return
    frame = requestAnimationFrame(() => {
      frame = 0
      if (!locked) save()
    })
  }

  const onHide = () => {
    if (!locked) save()
  }

  saveActive = save
  restoreActive = restore

  watch(
    () => route.fullPath,
    async () => {
      const next = scrollKey(route)
      if (next === key) return
      if (!locked) save() // 先记住离开时的旧位置，再切换 key（浮层里位置是 0，不能覆盖）
      key = next
      token.cancelled = true
      token = { cancelled: false }
      await restoreScroll(key, token)
    },
  )

  onMounted(() => {
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('pagehide', onHide)
    document.addEventListener('visibilitychange', onHide)
    void restoreScroll(key, token)
  })

  onBeforeUnmount(() => {
    window.removeEventListener('scroll', onScroll)
    window.removeEventListener('pagehide', onHide)
    document.removeEventListener('visibilitychange', onHide)
    if (saveActive === save) saveActive = null
    if (restoreActive === restore) restoreActive = null
    save()
  })

  return { save, restore }
}
