import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api'
import type { TagCount } from '@/types'

/** 标签是 AI 打标 + 手工整理共用的分类轴。只缓存最近用到的 400 个。 */
export const useTagsStore = defineStore('tags', () => {
  const tags = ref<TagCount[]>([])
  const loading = ref(false)

  async function load(force = false) {
    if (loading.value) return tags.value
    if (tags.value.length && !force) return tags.value
    loading.value = true
    try {
      tags.value = (await api.tags(400)).tags
    } finally {
      loading.value = false
    }
    return tags.value
  }

  return { tags, loading, load }
})
