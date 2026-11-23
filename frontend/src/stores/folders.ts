import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, errorText } from '@/api'
import type { Folder, Library } from '@/types'
import { useRevealStore } from './reveal'
import { useToastStore } from './toast'

/**
 * 文件夹（按库区分：图库 / 素材库各一套）。
 *
 * 文件夹是用户手动整理的维度：数量少（几十个量级）、无分页需求，
 * 所以一次性全量拉取；素材的增删只影响 count/cover，重新 load 一次就够。
 * store 保存「当前库」的一份列表：load / create 都带 library，换库自动重拉。
 *
 * 「展示隐藏」开关（reveal store）影响计数 / 封面 / 全隐藏文件夹的显隐，
 * 所以开关翻转后必须重拉：load 的去重条件里带上开关状态。
 */
export const useFoldersStore = defineStore('folders', () => {
  const library = ref<Library>('material')
  const folders = ref<Folder[]>([])
  /** 当前库里未归入任何文件夹的素材数（开关关闭时不含隐藏素材） */
  const unfiled = ref(0)
  const loading = ref(false)
  /** 已完成过加载的库；换了库再 load 会自动重拉 */
  const loaded = ref<Library | null>(null)
  /** 拉取当前列表时的开关状态；翻转后再 load 视为过期 */
  const loadedReveal = ref(false)

  const byId = computed(() => new Map(folders.value.map(f => [f.id, f])))

  function nameOf(id: string | null | undefined): string {
    return (id && byId.value.get(id)?.name) || ''
  }

  async function load(lib: Library, force = false) {
    if (loading.value) return
    const revealOn = useRevealStore().showHidden
    if (!force && loaded.value === lib && library.value === lib && loadedReveal.value === revealOn)
      return
    library.value = lib
    loading.value = true
    try {
      const res = await api.folders(lib, revealOn)
      folders.value = res.folders
      unfiled.value = res.unfiled
      loaded.value = lib
      loadedReveal.value = revealOn
    } finally {
      loading.value = false
    }
  }

  /**
   * 「展示隐藏」关闭时，内容全被隐藏的文件夹不出现在网格里。
   * create / rename 的单条返回也会带上这种文件夹（后端单条接口不过滤），
   * 合并进列表前在这里摘掉。
   */
  function allHidden(folder: Folder): boolean {
    return !useRevealStore().showHidden && folder.count === 0 && folder.total > 0
  }

  async function create(name: string, lib: Library): Promise<Folder | null> {
    if (library.value !== lib) {
      // 换库后旧列表不能留：先把视图切干净，新文件夹才有地方放
      library.value = lib
      folders.value = []
    }
    try {
      const folder = await api.createFolder(name, lib, useRevealStore().showHidden)
      if (!allHidden(folder)) folders.value = [folder, ...folders.value]
      loaded.value = lib
      return folder
    } catch (e) {
      useToastStore().show('创建失败：' + errorText(e).slice(0, 60))
      return null
    }
  }

  async function rename(id: string, name: string): Promise<boolean> {
    try {
      const next = await api.renameFolder(id, name, useRevealStore().showHidden)
      folders.value = allHidden(next)
        ? folders.value.filter(f => f.id !== id)
        : folders.value.map(f => (f.id === id ? next : f))
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

  return {
    library,
    folders,
    unfiled,
    loading,
    loaded,
    loadedReveal,
    byId,
    nameOf,
    load,
    create,
    rename,
    remove,
  }
})
