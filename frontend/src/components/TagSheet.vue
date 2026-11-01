<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import type { Asset } from '@/types'
import { useAssetActions } from '@/composables/useAssetActions'
import { useFeedStore } from '@/stores/feed'
import { useTagSheetStore } from '@/stores/tagSheet'
import { useTagsStore } from '@/stores/tags'

const sheet = useTagSheetStore()
const tagsStore = useTagsStore()
const feed = useFeedStore()
const actions = useAssetActions()

const single = ref<Asset | null>(null)
const newTag = ref('')
const busy = ref(false)

const title = computed(() =>
  single.value ? '编辑标签' : `为 ${sheet.ids.length} 项添加标签`,
)

watch(
  () => [sheet.open, sheet.ids.join(',')],
  async () => {
    if (!sheet.open) return
    await tagsStore.load()
    const id = sheet.ids[0]
    if (sheet.ids.length === 1) {
      single.value =
        feed.items.find(a => a.id === id) ??
        (await api.asset(id).catch(() => null))
    } else {
      single.value = null
    }
  },
  { immediate: true },
)

function has(name: string) {
  return !!single.value?.tags.includes(name)
}

function allHave(name: string) {
  return sheet.ids.every(id => feed.items.find(a => a.id === id)?.tags?.includes(name))
}

async function toggle(name: string) {
  if (!single.value) {
    await actions.batchTag(sheet.ids, name, !allHave(name))
    await tagsStore.load(true)
    sheet.hide()
    return
  }
  busy.value = true
  try {
    const asset = single.value
    const res = has(name)
      ? await api.removeTag(asset.id, name)
      : await api.addTag(asset.id, name)
    asset.tags = res.tags
    feed.setTags(asset.id, res.tags)
  } finally {
    busy.value = false
  }
}

async function addNew() {
  const name = newTag.value.trim()
  if (!name) return
  newTag.value = ''
  if (!single.value) {
    await actions.batchTag(sheet.ids, name, true)
    await tagsStore.load(true)
    sheet.hide()
    return
  }
  busy.value = true
  try {
    const res = await api.addTag(single.value.id, name)
    single.value.tags = res.tags
    feed.setTags(single.value.id, res.tags)
    await tagsStore.load(true)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="sheet.open" class="fixed inset-0 z-[120]" @click.self="sheet.hide()">
    <div class="absolute inset-0" @click="sheet.hide()" />
    <div class="sheet z-[121]">
      <div class="sheet-grab" />
      <h3 class="sheet-title">{{ title }}</h3>
      <div class="no-scrollbar flex max-h-[200px] flex-wrap gap-2 overflow-auto pb-3">
        <button
          v-for="tag in tagsStore.tags"
          :key="tag.id"
          class="chip tap"
          :class="(single ? has(tag.name) : allHave(tag.name)) && 'chip-on'"
          :disabled="busy"
          @click="toggle(tag.name)"
        >
          {{ tag.name }}
          <span class="opacity-50">{{ tag.count }}</span>
        </button>
        <span v-if="!tagsStore.tags.length" class="text-mut">还没有标签，下面新建一个</span>
      </div>
      <div class="flex gap-2">
        <input
          v-model="newTag"
          placeholder="新建标签…"
          class="flex-1 rounded-[10px] bg-panel px-3 py-2.5 text-[15px] outline-none"
          @keydown.enter="addNew"
        />
        <button class="rounded-[10px] bg-acc px-[18px] font-semibold" @click="addNew">添加</button>
      </div>
    </div>
  </div>
</template>
