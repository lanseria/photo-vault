import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * 「展示隐藏」全局开关：打开后所有资产列表把隐藏项混入显示（带角标）。
 * 顶栏按钮切换，localStorage 持久化——切页面、刷新、重启浏览器都保持，
 * 所以不走 URL query（导航胶囊跳转不携带 query，全局状态放 URL 会丢）。
 */
const KEY = 'show_hidden'

export const useRevealStore = defineStore('reveal', () => {
  const showHidden = ref(localStorage.getItem(KEY) === '1')

  function toggle() {
    showHidden.value = !showHidden.value
    localStorage.setItem(KEY, showHidden.value ? '1' : '0')
  }

  return { showHidden, toggle }
})
