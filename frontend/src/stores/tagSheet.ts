import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 标签面板是全局的单例：多选工具条、灯箱、标签菜单都往这里丢 ids。 */
export const useTagSheetStore = defineStore('tagSheet', () => {
  const open = ref(false)
  const ids = ref<string[]>([])

  function show(list: string[]) {
    if (!list.length) return
    ids.value = [...list]
    open.value = true
  }

  function hide() {
    open.value = false
    ids.value = []
  }

  return { open, ids, show, hide }
})
