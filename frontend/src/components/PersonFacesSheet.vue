<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, errorText } from '@/api'
import type { Person, PersonFace } from '@/types'
import { useToastStore } from '@/stores/toast'
import { usePeopleStore } from '@/stores/people'
import { fmtStamp } from '@/utils/format'
import PersonPicker from '@/components/PersonPicker.vue'

/**
 * 「人脸」抽屉：列出人物的全部脸，点选后忽略 / 恢复 / 移动到别的人物。
 * 忽略 = 标记 + 记住 embedding 规则，重跑识别再检出同一张脸自动忽略。
 * 移动 = 聚类纠错：把并错的脸挂到正确的人物名下。
 */
const props = defineProps<{ personId: string; personName: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const toast = useToastStore()
const people = usePeopleStore()

const faces = ref<PersonFace[]>([])
const loading = ref(false)
const showIgnored = ref(false)
/** 选中的脸 id（忽略和恢复共用一个选择集，按脸自身的 ignored 归边） */
const selected = ref(new Set<string>())
const working = ref(false)
const pickerOpen = ref(false)

const active = computed(() => faces.value.filter(f => !f.ignored))
const ignored = computed(() => faces.value.filter(f => f.ignored))
const visible = computed(() => (showIgnored.value ? faces.value : active.value))

const selActive = computed(() =>
  [...selected.value].filter(id => active.value.some(f => f.id === id)).length,
)
const selIgnored = computed(() =>
  [...selected.value].filter(id => ignored.value.some(f => f.id === id)).length,
)

/** 移动目标候选：除当前人物外的所有人 */
const others = computed(() => people.persons.filter(p => p.id !== props.personId))

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await api.personFaces(props.personId)
    faces.value = res.faces
    selected.value.clear()
  } catch (e) {
    toast.show('人脸加载失败：' + errorText(e).slice(0, 40))
  } finally {
    loading.value = false
  }
}

function toggle(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function clearSelection() {
  selected.value = new Set()
}

async function run(fn: (ids: string[]) => Promise<unknown>, okText: string) {
  const ids = [...selected.value]
  if (!ids.length || working.value) return
  working.value = true
  try {
    await fn(ids)
    await load()
    toast.show(okText)
    emit('changed')
  } catch (e) {
    toast.show('操作失败：' + errorText(e).slice(0, 40))
    // 部分脸可能已被别的操作处理过，刷新一次让状态对齐
    await load()
  } finally {
    working.value = false
  }
}

function ignoreSelected() {
  void run(ids => api.ignorePersonFaces(props.personId, ids),
    `已忽略 ${selActive.value} 张脸`)
}

function restoreSelected() {
  void run(ids => api.restorePersonFaces(props.personId, ids),
    `已恢复 ${selIgnored.value} 张脸`)
}

/** 选中的未忽略脸 id（移动只作用于未忽略的脸） */
function activeSelectionIds(): string[] {
  return [...selected.value].filter(id => active.value.some(f => f.id === id))
}

async function openPicker() {
  if (!selActive.value || working.value) return
  await people.load()
  pickerOpen.value = true
}

async function onPickTarget(target: Person) {
  pickerOpen.value = false
  const ids = activeSelectionIds()
  if (!ids.length) return
  working.value = true
  try {
    const res = await api.movePersonFaces(props.personId, ids, target.id)
    await load()
    toast.show(`已移动 ${res.moved} 张到「${target.name || '未命名'}」`)
    emit('changed')
  } catch (e) {
    toast.show('移动失败：' + errorText(e).slice(0, 40))
    await load()
  } finally {
    working.value = false
  }
}

/* ---------------- hover 预览：悬停人脸看原图 / 播视频，方便判断是谁 ---------------- */

const canHover =
  typeof window !== 'undefined' && !!window.matchMedia?.('(hover: hover)').matches

const preview = ref<{ face: PersonFace; x: number; y: number } | null>(null)
/** 视频海报上的 ▶ 点了之后挂 video 元素流播原件 */
const playing = ref(false)
let showTimer: ReturnType<typeof setTimeout> | undefined
let hideTimer: ReturnType<typeof setTimeout> | undefined

function onFaceEnter(face: PersonFace, event: MouseEvent) {
  if (!canHover) return
  const x = event.clientX
  const y = event.clientY
  clearTimeout(hideTimer)
  clearTimeout(showTimer)
  showTimer = setTimeout(() => {
    playing.value = false
    preview.value = { face, x, y }
  }, 220)
}

function onFaceLeave() {
  clearTimeout(showTimer)
  hideTimer = setTimeout(() => {
    preview.value = null
    playing.value = false
  }, 150)
}

/** 鼠标从脸移进浮层（看视频时）不关浮层 */
function keepPreview() {
  clearTimeout(hideTimer)
}

function hidePreviewNow() {
  clearTimeout(showTimer)
  clearTimeout(hideTimer)
  preview.value = null
  playing.value = false
}

/** 贴着鼠标弹出，但别伸出屏幕右边 / 下边 */
const popStyle = computed(() => {
  const p = preview.value
  if (!p) return {}
  const w = p.face.kind === 'video' ? 420 : 280
  const left = Math.max(8, Math.min(p.x + 16, window.innerWidth - w - 12))
  const top = Math.max(8, Math.min(p.y + 16, window.innerHeight - 280))
  return { left: `${left}px`, top: `${top}px` }
})

/** 缩略图挂了（没生成过）退到 /preview 原件代理 */
function thumbError(event: Event) {
  const img = event.target as HTMLImageElement
  const face = preview.value?.face
  if (face && !img.dataset.fallback) {
    img.dataset.fallback = '1'
    img.src = `/preview/${face.asset_id}`
  } else {
    img.style.display = 'none'
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[140] flex items-end justify-center bg-black/55"
      @click.self="emit('close')"
    >
      <div class="w-full max-w-[560px] rounded-t-2xl bg-panel2 px-4 pb-safe-16 pt-3.5">
        <div class="mb-1 flex items-center justify-center gap-2">
          <h3 class="m-0 text-center text-[16px] font-semibold">
            {{ personName || '未命名' }} 的人脸
          </h3>
          <span class="text-[12px] text-mut">{{ active.length }} 张</span>
        </div>
        <p class="mb-2.5 px-1 text-center text-[11.5px] leading-relaxed text-mut">
          点选要移除的脸 →「忽略所选」。忽略的脸不再计入人物，
          重跑识别检出同一张脸也会自动忽略。
        </p>

        <div v-if="ignored.length" class="mb-2 flex items-center justify-between px-1">
          <button
            class="text-[12.5px] text-mut tap"
            @click="showIgnored = !showIgnored"
          >
            {{ showIgnored ? '隐藏已忽略' : `显示已忽略（${ignored.length}）` }}
          </button>
          <button
            v-if="selected.size"
            class="text-[12.5px] text-mut tap"
            @click="clearSelection"
          >
            清除选择
          </button>
        </div>

        <div class="max-h-[52vh] overflow-auto">
          <div v-if="loading" class="spinner" />
          <div
            v-else-if="!visible.length"
            class="px-2 py-8 text-center text-[13px] text-mut"
          >
            {{ showIgnored ? '没有已忽略的脸' : '这个人物还没有脸' }}
          </div>
          <div
            v-else
            class="grid grid-cols-[repeat(auto-fill,minmax(78px,1fr))] gap-x-2 gap-y-3 p-1"
            @scroll="hidePreviewNow"
          >
            <button
              v-for="face in visible"
              :key="face.id"
              class="flex flex-col items-center gap-1"
              @click="toggle(face.id)"
              @mouseenter="onFaceEnter(face, $event)"
              @mouseleave="onFaceLeave"
            >
              <span
                class="relative block h-[72px] w-[72px] overflow-hidden rounded-full bg-panel"
                :class="[
                  selected.has(face.id) ? 'ring-2 ring-acc' : '',
                  face.ignored && 'opacity-40',
                ]"
              >
                <img
                  :src="`/faces/${face.id}.jpg`"
                  alt=""
                  loading="lazy"
                  class="h-full w-full object-cover"
                />
                <span
                  v-if="face.ignored"
                  class="absolute bottom-0 left-0 right-0 bg-black/60 py-[1px] text-center text-[9.5px] text-white"
                >
                  已忽略
                </span>
              </span>
              <span class="text-[10px] leading-none text-mut">
                {{ face.taken ? fmtStamp(face.taken) : '日期未知' }}
              </span>
            </button>
          </div>
        </div>

        <div class="mt-2.5 flex gap-2">
          <button
            v-if="selActive"
            class="flex-1 rounded-[10px] py-[11px] text-[13.5px] font-semibold tap disabled:opacity-50"
            :class="selIgnored ? 'bg-panel text-warn' : 'bg-warn text-black'"
            :disabled="working"
            @click="ignoreSelected"
          >
            {{ working ? '处理中…' : `忽略所选（${selActive}）` }}
          </button>
          <button
            v-if="selActive"
            class="flex-1 rounded-[10px] bg-panel py-[11px] text-[13.5px] font-semibold text-acc tap disabled:opacity-50"
            :disabled="working"
            @click="openPicker"
          >
            移动到…
          </button>
          <button
            v-if="selIgnored"
            class="flex-1 rounded-[10px] bg-panel py-[11px] text-[13.5px] font-semibold text-acc tap disabled:opacity-50"
            :disabled="working"
            @click="restoreSelected"
          >
            {{ working ? '处理中…' : `恢复所选（${selIgnored}）` }}
          </button>
        </div>
        <button
          class="mt-1.5 w-full rounded-[10px] bg-panel py-[11px] font-semibold text-acc"
          @click="emit('close')"
        >
          {{ selected.size ? '完成' : '关闭' }}
        </button>
      </div>
    </div>

    <PersonPicker
      v-if="pickerOpen"
      :people="others"
      :title="`把 ${selActive} 张脸移动到…`"
      @pick="onPickTarget"
      @close="pickerOpen = false"
    />

    <!-- hover 预览浮层：照片直接看原图，视频先看海报帧、点 ▶ 流播 -->
    <div
      v-if="preview"
      class="fixed z-[150] overflow-hidden rounded-xl border border-line bg-panel shadow-2xl"
      :style="popStyle"
      @mouseenter="keepPreview"
      @mouseleave="onFaceLeave"
    >
      <div class="relative bg-black">
        <template v-if="preview.face.kind === 'video' && playing">
          <video
            :src="`/file/${preview.face.asset_id}`"
            controls
            autoplay
            playsinline
            class="max-h-[300px] w-full"
            @ended="playing = false"
          />
        </template>
        <template v-else>
          <img
            v-if="preview.face.sha"
            :src="`/thumbs/${preview.face.sha}.webp`"
            alt=""
            class="max-h-[300px] w-full object-contain"
            @error="thumbError"
          />
          <div v-else class="flex h-[160px] items-center justify-center text-[13px] text-mut">
            没有可预览的原图
          </div>
          <button
            v-if="preview.face.kind === 'video'"
            class="absolute inset-0 flex items-center justify-center bg-black/30 text-[34px] text-white tap"
            title="播放视频"
            @click="playing = true"
          >
            ▶
          </button>
        </template>
      </div>
      <div class="flex items-center justify-between px-3 py-1.5 text-[11.5px] text-mut">
        <span>{{ preview.face.taken ? fmtStamp(preview.face.taken) : '日期未知' }}</span>
        <span>{{ preview.face.kind === 'video' ? '视频' : '照片' }}</span>
      </div>
    </div>
  </Teleport>
</template>
