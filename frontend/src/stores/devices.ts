import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api'
import type { Device } from '@/types'

/**
 * 拍摄设备字典。
 *
 * 展示名的优先级在后端算好了（label > model_canonical > model_raw），
 * 这里只负责缓存列表、改名、以及把「未识别」的数量带出来。
 */
export const useDevicesStore = defineStore('devices', () => {
  const devices = ref<Device[]>([])
  const unknown = ref(0)
  const loading = ref(false)
  const loaded = ref(false)

  const byId = computed(() => new Map(devices.value.map(d => [d.id, d])))

  async function load(force = false) {
    if (loading.value) return devices.value
    if (loaded.value && !force) return devices.value
    loading.value = true
    try {
      const res = await api.devices()
      devices.value = res.devices
      unknown.value = res.unknown
      loaded.value = true
    } finally {
      loading.value = false
    }
    return devices.value
  }

  function nameOf(id: number | null | undefined): string {
    if (id == null) return '未识别设备'
    return byId.value.get(id)?.name ?? '未识别设备'
  }

  /** 改名；label 传空串 = 恢复默认展示名。 */
  async function rename(id: number, label: string) {
    const updated = await api.renameDevice(id, label)
    const index = devices.value.findIndex(d => d.id === id)
    if (index >= 0) devices.value[index] = updated
    return updated
  }

  /** 新建一台 EXIF 认不出来的设备。 */
  async function create(name: string, kind: string) {
    const device = await api.createDevice(name, kind)
    await load(true)
    return device
  }

  /** 把一台设备的照片并到另一台，源设备随之删除。 */
  async function merge(id: number, into: number) {
    const res = await api.mergeDevice(id, into)
    await load(true)
    return res
  }

  async function assign(ids: string[], deviceId: number | null) {
    const res = await api.assignDevice(ids, deviceId)
    await load(true)
    return res.updated
  }

  return { devices, unknown, loading, loaded, byId, load, nameOf, rename, create, merge, assign }
})
