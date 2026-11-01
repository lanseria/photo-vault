import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * 页面外壳（顶栏）的可变部分。
 * 谁渲染内容谁负责设置标题/计数，顶栏只负责显示 —— 避免顶栏去猜路由。
 */
export const useChromeStore = defineStore('chrome', () => {
  const title = ref('')
  const count = ref('')
  const placeholder = ref('搜索')

  function set(patch: { title?: string; count?: string; placeholder?: string }) {
    if (patch.title !== undefined) title.value = patch.title
    if (patch.count !== undefined) count.value = patch.count
    if (patch.placeholder !== undefined) placeholder.value = patch.placeholder
  }

  return { title, count, placeholder, set }
})
