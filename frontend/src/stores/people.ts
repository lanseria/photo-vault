import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api'
import type { Person } from '@/types'

/** 人物是跨素材库的实体：一张脸属于一个人，不关心照片在哪个库。 */
export const usePeopleStore = defineStore('people', () => {
  const persons = ref<Person[]>([])
  const unassigned = ref(0)
  const loading = ref(false)
  const loaded = ref(false)

  const byId = computed(() => new Map(persons.value.map(p => [p.id, p])))

  function nameOf(id: string | null | undefined): string {
    if (!id) return ''
    return byId.value.get(id)?.name || ''
  }

  async function load(force = false) {
    if (loading.value) return persons.value
    if (loaded.value && !force) return persons.value
    loading.value = true
    try {
      const res = await api.persons()
      persons.value = res.persons
      unassigned.value = res.unassigned_faces
      loaded.value = true
    } finally {
      loading.value = false
    }
    return persons.value
  }

  async function rename(id: string, name: string) {
    const person = await api.renamePerson(id, name)
    const hit = persons.value.find(p => p.id === id)
    if (hit) hit.name = person.name
    return person.name
  }

  async function merge(fromId: string, intoId: string) {
    await api.mergePerson(fromId, intoId)
    await load(true)
    return byId.value.get(intoId) ?? null
  }

  async function dissolve(id: string) {
    await api.dissolvePerson(id)
    persons.value = persons.value.filter(p => p.id !== id)
    loaded.value = false
  }

  /** 列表/详情共用的封面地址：自定义封面优先，没有就回退系统挑的人脸裁剪。
   *  slot=2 取备用封面（没传返回 ''）。bust 换个查询参数绕开旧缓存。 */
  function coverSrc(p: Person, bust = 0, slot: 1 | 2 = 1): string {
    const q = `slot=${slot}${bust ? `&v=${bust}` : ''}`
    if (slot === 2) return p.backup_cover ? `/covers/${p.id}.jpg?${q}` : ''
    if (p.custom_cover) return `/covers/${p.id}.jpg?${q}`
    return p.cover ? `/faces/${p.cover}.jpg` : ''
  }

  function applyPerson(person: Person) {
    const hit = persons.value.find(p => p.id === person.id)
    if (hit) Object.assign(hit, person)
    else persons.value.push(person)
  }

  async function setCover(id: string, file: File, slot: 1 | 2 = 1) {
    applyPerson(await api.uploadPersonCover(id, file, slot))
  }

  async function resetCover(id: string, slot: 1 | 2 = 1) {
    applyPerson(await api.resetPersonCover(id, slot))
  }

  return {
    persons, unassigned, loading, loaded, byId, nameOf,
    load, rename, merge, dissolve, coverSrc, setCover, resetCover,
  }
})
