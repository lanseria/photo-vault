<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePeopleStore } from '@/stores/people'
import { useChromeStore } from '@/stores/chrome'
import { useDialogStore } from '@/stores/dialog'
import { useToastStore } from '@/stores/toast'
import { errorText } from '@/api'
import type { Person } from '@/types'
import PersonPicker from './PersonPicker.vue'

const route = useRoute()
const router = useRouter()
const people = usePeopleStore()
const chrome = useChromeStore()
const dialog = useDialogStore()
const toast = useToastStore()

const personId = computed(() => String(route.params.id ?? ''))
const personName = computed(() => people.nameOf(personId.value))
const picking = ref(false)
const others = computed(() => people.persons.filter(p => p.id !== personId.value))

async function rename() {
  const name = await dialog.prompt({
    title: '给这个人物起个名字',
    value: personName.value,
    placeholder: '例如：妈妈',
    confirmText: '保存',
    required: true,
  })
  if (!name) return
  try {
    const next = await people.rename(personId.value, name)
    chrome.set({ title: next })
    toast.show('已改名')
  } catch (e) {
    toast.show('改名失败：' + errorText(e).slice(0, 40))
  }
}

async function openPicker() {
  await people.load()
  picking.value = true
}

async function onPick(into: Person) {
  picking.value = false
  try {
    await people.merge(personId.value, into.id)
    toast.show(`已合并到「${into.name || '未命名'}」`)
    await router.replace(`/people/${into.id}`)
  } catch {
    toast.show('合并失败')
  }
}

async function onDissolve() {
  picking.value = false
  const label = personName.value || '这个人物'
  const ok = await dialog.confirm({
    title: `取消「${label}」？`,
    message: '它的脸会回到「未归类」，下次重新聚类时可能被重新分组。',
    confirmText: '取消这个人物',
    danger: true,
  })
  if (!ok) return
  try {
    await people.dissolve(personId.value)
    await router.replace('/people')
    toast.show('已取消这个人物')
  } catch {
    toast.show('操作失败')
  }
}
</script>

<template>
  <button class="txt-btn" @click="rename">重命名</button>
  <button class="txt-btn" @click="openPicker">合并</button>
  <PersonPicker
    v-if="picking"
    :people="others"
    :title="`把「${personName || '未命名'}」合并到…`"
    dissolve-text="取消这个人物（脸回到未归类）"
    @pick="onPick"
    @dissolve="onDissolve"
    @close="picking = false"
  />
</template>
