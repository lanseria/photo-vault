import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, errorText } from '@/api'
import type { MaterialFolder } from '@/types'
import { useToastStore } from './toast'

/**
 * 素材库文件夹。
 *
 * 文件夹是素材库里用户手动整理的维度：数量少（几十个量级）、无分页需求，
 * 所以一次性全量拉取；素材的增删只影响 count/cover，重新 load 一次就够。
 */
export const useFoldersStore = defineStore('folders', () => {
  const folders = ref<MaterialFolder[]>([])
  /** 未归入任何文件夹的素材数 */
  const unfiled = ref(0)
  const loading = ref(false)
  const loaded = ref(false)

  const byId = computed(() => new Map(folders.value.map(f => [f.id, f])))

  function nameOf(id: string | null | undefined): string {
    return (id && byId.value.get(id)?.name) || ''
  }

  async function load(force = false) {
    if (loading.value) return
    if (loaded.value && !force) return
    loading.value = true
    try {
      const res = await api.folders()
      folders.value = res.folders
      unfiled.value = res.unfiled
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  async function create(name: string): Promise<MaterialFolder | null> {
    try {
      const folder = await api.createFolder(name)
      folders.value = [folder, ...folders.value]
      return folder
    } catch (e) {
      useToastStore().show('创建失败：' + errorText(e).slice(0, 60))
      return null
    }
  }

  async function rename(id: string, name: string): Promise<boolean> {
    try {
      const next = await api.renameFolder(id, name)
      folders.value = folders.value.map(f => (f.id === id ? next : f))
      return true
    } catch (e) {
      useToastStore().show('改名失败：' + errorText(e).slice(0, 60))
      return false
    }
  }

  async function remove(id: string): Promise<boolean> {
    try {
      await api.deleteFolder(id)
    } catch (e) {
      useToastStore().show('删除失败：' + errorText(e).slice(0, 60))
      return false
    }
    folders.value = folders.value.filter(f => f.id !== id)
    return true
  }

  return { folders, unfiled, loading, loaded, byId, nameOf, load, create, rename, remove }
})
