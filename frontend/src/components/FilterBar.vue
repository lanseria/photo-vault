<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { isGroupMode, type GroupMode } from '@/utils/date'
import { useDevicesStore } from '@/stores/devices'
import { useTagsStore } from '@/stores/tags'

const route = useRoute()
const router = useRouter()
const devices = useDevicesStore()
const tags = useTagsStore()

type FilterKey = 'kind' | 'dur' | 'px'

const groups: { key: FilterKey; label: string; all: string; options: { value: string; label: string }[] }[] = [
  {
    key: 'kind',
    label: '类型',
    all: '全部',
    options: [
      { value: 'video', label: '🎬 视频' },
      { value: 'photo', label: '照片' },
    ],
  },
  {
    key: 'dur',
    label: '时长',
    all: '任意',
    options: [
      { value: '0-15', label: '<15s' },
      { value: '15-60', label: '15–60s' },
      { value: '60-300', label: '1–5min' },
      { value: '300-', label: '5min+' },
    ],
  },
  {
    key: 'px',
    label: '清晰度',
    all: '任意',
    options: [
      { value: '1080', label: '≥1080p' },
      { value: '2160', label: '≥4K' },
    ],
  },
]

const groupOptions: { value: GroupMode; label: string }[] = [
  { value: 'day', label: '日' },
  { value: 'week', label: '周' },
  { value: 'month', label: '月' },
  { value: 'year', label: '年' },
]

const currentGroup = computed<GroupMode>(() => (isGroupMode(route.query.group) ? route.query.group : 'day'))

function setGroup(value: GroupMode) {
  if (currentGroup.value === value) return
  const query = { ...route.query }
  if (value === 'day') delete query.group
  else query.group = value
  void router.replace({ path: route.path, query })
}

function current(key: FilterKey): string {
  const raw = route.query[key]
  return typeof raw === 'string' ? raw : ''
}

function setFilter(key: FilterKey, value: string) {
  if (current(key) === value) return
  const query = { ...route.query }
  if (value) query[key] = value
  else delete query[key]
  void router.replace({ path: route.path, query })
}

/* ---------------- 设备筛选（下拉菜单） ---------------- */

const deviceParam = computed(() => {
  const raw = route.query.device
  return typeof raw === 'string' && raw ? raw : ''
})

const deviceLabel = computed(() => {
  if (!deviceParam.value) return '设备'
  if (deviceParam.value === 'unknown') return '未识别设备'
  const id = Number(deviceParam.value)
  return devices.byId.get(id)?.name ?? `设备 #${deviceParam.value}`
})

/** 设备多的排前面，找起来顺手 */
const sortedDevices = computed(() => [...devices.devices].sort((a, b) => b.n_assets - a.n_assets))

const menuOpen = ref(false)
const menuLeft = ref(16)
const menuTop = ref(120)
const menuEl = ref<HTMLElement | null>(null)

function openMenu(event: MouseEvent) {
  if (menuOpen.value) {
    menuOpen.value = false
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menuLeft.value = Math.max(8, rect.left)
  menuTop.value = rect.bottom + 6
  void devices.load()
  menuOpen.value = true
}

// 菜单宽度要等渲染出来才知道：超右/超底就往回拉。
// 设备列表是异步到的，列表填进来后菜单会变宽，所以要跟着再夹一次。
function clampMenu() {
  const el = menuEl.value
  if (!el) return
  const overflowX = menuLeft.value + el.offsetWidth - (window.innerWidth - 8)
  if (overflowX > 0) menuLeft.value = Math.max(8, menuLeft.value - overflowX)
  const overflowY = menuTop.value + el.offsetHeight - (window.innerHeight - 8)
  if (overflowY > 0) menuTop.value = Math.max(8, menuTop.value - overflowY)
}

watch(menuOpen, async open => {
  if (!open) return
  await nextTick()
  clampMenu()
})

watch(
  () => devices.devices.length,
  async () => {
    if (!menuOpen.value) return
    await nextTick()
    clampMenu()
  },
)

function setDevice(value: string) {
  menuOpen.value = false
  if (deviceParam.value === value) return
  const query = { ...route.query }
  if (value) query.device = value
  else delete query.device
  void router.replace({ path: route.path, query })
}

/* ---------------- 标签（从导航栏挪到筛选栏） ---------------- */

const library = computed(() => (route.meta.library === 'material' ? 'material' : 'gallery'))
const tagActive = computed(() => route.meta.chip === 'tag')

const tagMenu = ref(false)
const tagLeft = ref(16)
const tagTop = ref(120)
const tagEl = ref<HTMLElement | null>(null)

function onTagClick(event: MouseEvent) {
  // 在标签视图里点它 = 回库；其它时候打开标签菜单
  if (tagActive.value) {
    void router.push(`/${library.value}`)
    return
  }
  if (tagMenu.value) {
    tagMenu.value = false
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  tagLeft.value = Math.max(8, rect.left)
  tagTop.value = rect.bottom + 6
  void tags.load()
  tagMenu.value = true
}

function goTag(name: string) {
  tagMenu.value = false
  void router.push(`/${library.value}/tags/${encodeURIComponent(name)}`)
}

function clampTagMenu() {
  const el = tagEl.value
  if (!el) return
  const overflowX = tagLeft.value + el.offsetWidth - (window.innerWidth - 8)
  if (overflowX > 0) tagLeft.value = Math.max(8, tagLeft.value - overflowX)
  const overflowY = tagTop.value + el.offsetHeight - (window.innerHeight - 8)
  if (overflowY > 0) tagTop.value = Math.max(8, tagTop.value - overflowY)
}

watch(tagMenu, async open => {
  if (!open) return
  await nextTick()
  clampTagMenu()
})

watch(
  () => tags.tags.length,
  async () => {
    if (!tagMenu.value) return
    await nextTick()
    clampTagMenu()
  },
)

function onDocumentClick(event: MouseEvent) {
  const target = event.target as Element | null
  if (!target?.closest('[data-device-filter]')) menuOpen.value = false
  if (!target?.closest('[data-tag-nav]')) tagMenu.value = false
}

function closeMenu() {
  menuOpen.value = false
  tagMenu.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  window.addEventListener('resize', closeMenu)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  window.removeEventListener('resize', closeMenu)
})
</script>

<template>
  <div class="flex items-center gap-2.5 px-4 pb-2.5 overflow-x-auto no-scrollbar whitespace-nowrap">
    <template v-for="group in groups" :key="group.key">
    <span class="text-[12px] text-mut shrink-0">{{ group.label }}</span>
    <div class="inline-flex shrink-0 items-center rounded-[9px] bg-panel p-0.5">
      <button
        class="seg-btn"
        :class="!current(group.key) && 'seg-on'"
        @click="setFilter(group.key, '')"
      >
        {{ group.all }}
      </button>
      <button
        v-for="option in group.options"
        :key="option.value"
        class="seg-btn"
        :class="current(group.key) === option.value && 'seg-on'"
        @click="setFilter(group.key, option.value)"
      >
        {{ option.label }}
      </button>
    </div>
  </template>

  <span class="text-[12px] text-mut shrink-0">设备</span>
  <div data-device-filter class="shrink-0">
    <button
      class="seg-btn inline-flex items-center gap-1"
      :class="deviceParam && 'seg-on'"
      data-device-filter-btn
      @click="openMenu"
    >
      <span class="max-w-[132px] truncate">{{ deviceLabel }}</span>
      <span class="text-[9px] opacity-70">▼</span>
    </button>
  </div>

  <span class="text-[12px] text-mut shrink-0">标签</span>
  <div data-tag-nav class="relative shrink-0">
    <button
      class="seg-btn inline-flex items-center gap-1"
      :class="tagActive && 'seg-on'"
      :title="tagActive ? '回到当前库' : '按标签筛选'"
      @click="onTagClick"
    >
      <template v-if="tagActive">
        🏷 {{ route.params.tag }} <span class="opacity-60">✕</span>
      </template>
      <template v-else>全部 <span class="text-[9px] opacity-70">▼</span></template>
    </button>
  </div>

  <span class="text-[12px] text-mut shrink-0">分组</span>
  <div class="inline-flex shrink-0 items-center rounded-[9px] bg-panel p-0.5">
    <button
      v-for="option in groupOptions"
      :key="option.value"
      class="seg-btn"
      :class="currentGroup === option.value && 'seg-on'"
      @click="setGroup(option.value)"
    >
      {{ option.label }}
    </button>
  </div>
  </div>

  <Teleport to="body">
    <div
      v-if="menuOpen"
      ref="menuEl"
      data-device-filter
      data-device-menu
      class="fixed z-[80] max-h-[320px] min-w-[220px] max-w-[calc(100vw-16px)] overflow-auto rounded-xl bg-panel2 p-1.5 shadow-[0_8px_30px_rgba(0,0,0,.6)]"
      :style="{ left: menuLeft + 'px', top: menuTop + 'px' }"
    >
      <button
        class="flex w-full items-center justify-between gap-3.5 rounded-lg px-3 py-2 text-left text-[14px] hover:bg-white/10"
        :class="!deviceParam && 'bg-white/5 text-acc'"
        @click="setDevice('')"
      >
        <span>全部设备</span>
      </button>
      <button
        class="flex w-full items-center justify-between gap-3.5 rounded-lg px-3 py-2 text-left text-[14px] hover:bg-white/10"
        :class="deviceParam === 'unknown' && 'bg-white/5 text-acc'"
        @click="setDevice('unknown')"
      >
        <span>未识别设备</span>
        <span class="text-[12.5px] text-mut">{{ devices.unknown || '' }}</span>
      </button>
      <button
        v-for="d in sortedDevices"
        :key="d.id"
        class="flex w-full items-center justify-between gap-3.5 rounded-lg px-3 py-2 text-left text-[14px] hover:bg-white/10"
        :class="deviceParam === String(d.id) && 'bg-white/5 text-acc'"
        :data-device-option="d.id"
        @click="setDevice(String(d.id))"
      >
        <span class="truncate">{{ d.name }}</span>
        <span class="shrink-0 text-[12.5px] text-mut">{{ d.n_assets }}</span>
      </button>
      <div v-if="!devices.devices.length && !devices.loading" class="px-3 py-2 text-[13px] text-mut">
        还没有设备记录
      </div>
    </div>
  </Teleport>

  <Teleport to="body">
    <div
      v-if="tagMenu"
      ref="tagEl"
      data-tag-nav
      class="fixed z-[80] max-h-[300px] min-w-[200px] max-w-[calc(100vw-16px)] overflow-auto rounded-xl bg-panel2 p-1.5 shadow-[0_8px_30px_rgba(0,0,0,.6)]"
      :style="{ left: tagLeft + 'px', top: tagTop + 'px' }"
    >
      <button
        v-for="tag in tags.tags"
        :key="tag.id"
        class="flex w-full items-center justify-between gap-3.5 rounded-lg px-3 py-2 text-left hover:bg-white/10"
        @click="goTag(tag.name)"
      >
        <span class="truncate">{{ tag.name }}</span>
        <span class="text-[12.5px] text-mut">{{ tag.count }}</span>
      </button>
      <div v-if="!tags.tags.length" class="px-3 py-2 text-[13px] text-mut">还没有标签</div>
    </div>
  </Teleport>
</template>