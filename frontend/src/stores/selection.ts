import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

/** 页面上的「选择」模式状态；真正的批量操作在 composables/useAssetActions.ts。 */
export const useSelectionStore = defineStore('selection', () => {
  const active = ref(false)
  const ids = ref(new Set<string>())
  /** 当前视图里所有可选 id，用于「全选」 */
  const scope = ref<string[]>([])

  const count = computed(() => ids.value.size)
  const allSelected = computed(
    () => scope.value.length > 0 && ids.value.size === scope.value.length,
  )
  const firstId = computed(() => ids.value.values().next().value as string | undefined)

  function start() {
    active.value = true
  }

  function stop() {
    active.value = false
    ids.value = new Set()
    scope.value = []
  }

  function isSelected(id: string) {
    return ids.value.has(id)
  }

  function toggle(id: string) {
    const next = new Set(ids.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    ids.value = next
  }

  function setScope(all: string[]) {
    scope.value = all
  }

  function selectAll() {
    ids.value = ids.value.size === scope.value.length ? new Set() : new Set(scope.value)
  }

  function list(): string[] {
    return [...ids.value]
  }

  return {
    active,
    ids,
    scope,
    count,
    allSelected,
    firstId,
    start,
    stop,
    isSelected,
    toggle,
    setScope,
    selectAll,
    list,
  }
})
