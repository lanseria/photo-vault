import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api'
import type { CinemaTitleDetail, CinemaTitleSummary } from '@/types'

/** 影视库只登记路径：不搬文件、不算哈希、不跑 AI。 */
export const useCinemaStore = defineStore('cinema', () => {
  const titles = ref<CinemaTitleSummary[]>([])
  const loading = ref(false)
  const loaded = ref(false)
  const mediaHost = ref('')
  const mediaRoot = ref('')
  const detail = ref<CinemaTitleDetail | null>(null)
  const detailLoading = ref(false)

  const movieCount = computed(() => titles.value.filter(t => t.kind === 'movie').length)
  const seriesCount = computed(() => titles.value.length - movieCount.value)
  const mediaDir = computed(() => mediaHost.value || mediaRoot.value)

  async function load(q = '') {
    loading.value = true
    try {
      const res = await api.cinema(q)
      titles.value = res.titles
      mediaHost.value = res.media_host_root || ''
      mediaRoot.value = res.media_root || ''
      loaded.value = true
    } finally {
      loading.value = false
    }
    return titles.value
  }

  async function open(id: string) {
    detailLoading.value = true
    try {
      detail.value = await api.cinemaTitle(id)
      return detail.value
    } finally {
      detailLoading.value = false
    }
  }

  function clearDetail() {
    detail.value = null
  }

  async function remove(id: string) {
    await api.cinemaDelete(id)
    titles.value = titles.value.filter(t => t.id !== id)
    if (detail.value?.id === id) detail.value = null
  }

  async function saveEdit(id: string, patch: { title?: string; year?: number | null; overview?: string }) {
    await api.cinemaEdit(id, patch)
    await open(id)
    await load()
  }

  return {
    titles,
    loading,
    loaded,
    mediaHost,
    mediaRoot,
    mediaDir,
    detail,
    detailLoading,
    movieCount,
    seriesCount,
    load,
    open,
    clearDetail,
    remove,
    saveEdit,
  }
})
