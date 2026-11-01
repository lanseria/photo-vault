import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api'
import type { Asset, BinSummary } from '@/types'

const BIN_KEY = 'vault.binId'
const DIR_KEY = 'vault.lastDir'

/**
 * 剪辑箱。它是「跨多次搜索攒素材」的持久容器，所以存在后端的 bins 表里，
 * 本地只缓存箱子 id —— 换浏览器不丢，重开页面还是同一个。
 */
export const useBinStore = defineStore('bin', () => {
  const id = ref<string | null>(localStorage.getItem(BIN_KEY))
  const summary = ref<BinSummary | null>(null)
  const items = ref<Asset[]>([])
  const loading = ref(false)
  const lastDir = ref(localStorage.getItem(DIR_KEY) || '')

  const count = computed(() => summary.value?.items ?? items.value.length)
  const name = computed(() => summary.value?.name ?? '剪辑箱')

  function adopt(bin: BinSummary) {
    id.value = bin.id
    summary.value = bin
    localStorage.setItem(BIN_KEY, bin.id)
  }

  /** 确保有一个箱子；本地缓存的 id 没了（比如被删）就重新挑一个或建一个。 */
  async function ensure(): Promise<string> {
    const cached = localStorage.getItem(BIN_KEY)
    const { bins } = await api.bins()
    const hit = cached ? bins.find(b => b.id === cached) : undefined
    if (hit) {
      adopt(hit)
      return hit.id
    }
    if (bins.length) {
      adopt(bins[0])
      return bins[0].id
    }
    const created = await api.createBin('剪辑箱')
    id.value = created.id
    summary.value = { id: created.id, name: created.name, note: null, items: 0, dur_ms: 0, bytes: 0 }
    localStorage.setItem(BIN_KEY, created.id)
    return created.id
  }

  async function refreshSummary() {
    try {
      const { bins } = await api.bins()
      const hit = bins.find(b => b.id === id.value) ?? bins[0]
      if (!hit) {
        await ensure()
        return summary.value
      }
      if (hit.id !== id.value) id.value = hit.id
      summary.value = hit
      return hit
    } catch {
      return null
    }
  }

  async function loadItems() {
    if (!id.value) await ensure()
    loading.value = true
    try {
      const res = await api.binItems(id.value!)
      items.value = res.items
    } finally {
      loading.value = false
    }
    return items.value
  }

  async function add(ids: string[]) {
    if (!ids.length) return { added: 0, moved: 0 }
    if (!id.value) await ensure()
    const res = await api.addToBin(id.value!, ids)
    await refreshSummary()
    return res
  }

  async function remove(assetId: string) {
    if (!id.value) return
    await api.removeFromBin(id.value, assetId)
    items.value = items.value.filter(a => a.id !== assetId)
    await refreshSummary()
  }

  async function clear() {
    if (!id.value) return
    await api.clearBin(id.value)
    items.value = []
    await refreshSummary()
  }

  async function rename(next: string) {
    if (!id.value) await ensure()
    const res = await api.renameBin(id.value!, next)
    if (summary.value) summary.value.name = res.name
    return res.name
  }

  async function deliver() {
    if (!id.value) await ensure()
    const result = await api.deliverBin(id.value!, 'link')
    lastDir.value = result.host_dir
    localStorage.setItem(DIR_KEY, result.host_dir)
    return result
  }

  return {
    id,
    summary,
    items,
    loading,
    lastDir,
    count,
    name,
    ensure,
    refreshSummary,
    loadItems,
    add,
    remove,
    clear,
    rename,
    deliver,
  }
})
