<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { api, errorText } from '@/api'
import type { CinemaTitleDetail, ImportRun } from '@/types'
import { useCinemaStore } from '@/stores/cinema'
import { useToastStore } from '@/stores/toast'
import { watchImportRun } from '@/utils/importRun'
import { fileName } from '@/utils/format'

const props = defineProps<{ mode: 'add' | 'edit'; detail?: CinemaTitleDetail | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const cinema = useCinemaStore()
const toast = useToastStore()

const path = ref(localStorage.getItem('vault.cinePath') || '')
const kind = ref('auto')
const posters = ref(true)
const busy = ref(false)
const run = ref<ImportRun | null>(null)
const error = ref('')

const title = ref(props.detail?.title ?? '')
const year = ref(props.detail?.year != null ? String(props.detail.year) : '')
const overview = ref(props.detail?.overview ?? '')

/** 弹层关掉后轮询还在跑，别再往已卸载的组件里写状态 */
let disposed = false

const progressText = computed(() => {
  const r = run.value
  if (!r) return ''
  const pct = r.total ? Math.round((r.done * 100) / r.total) : 0
  const label = r.state === 'running' ? '导入中' : r.state === 'done' ? '完成' : '失败'
  return `${label} · ${r.done}/${r.total}（${pct}%）`
})

const progressDetail = computed(() => {
  const r = run.value
  if (!r) return ''
  return `新增 ${r.imported} · 更新 ${r.updated} · 条目 ${r.titles} · 字幕 ${r.subtitles} · 失败 ${r.failed}`
})

async function startImport() {
  const target = path.value.trim()
  if (!target) return toast.show('请填路径')
  localStorage.setItem('vault.cinePath', target)
  busy.value = true
  error.value = ''
  run.value = null
  try {
    const res = await api.cinemaImport({ path: target, kind: kind.value, posters: posters.value })
    run.value = res.run
    const final = await watchImportRun(res.run.id, tick => {
      if (!disposed) run.value = tick
    })
    if (disposed) return
    busy.value = false
    if (final.state === 'error') {
      error.value = final.error || '导入失败'
      return
    }
    if (final.errors?.length) {
      error.value = `${final.errors[0].path}：${final.errors[0].error}`
    }
    toast.show(`导入完成：新增 ${final.imported} · 更新 ${final.updated}`)
    emit('saved')
    window.setTimeout(() => emit('close'), 700)
  } catch (e) {
    busy.value = false
    error.value = errorText(e).slice(0, 400)
  }
}

async function saveEdit() {
  const detail = props.detail
  if (!detail) return
  busy.value = true
  try {
    await cinema.saveEdit(detail.id, {
      title: title.value.trim(),
      year: year.value.trim() ? Number(year.value) : null,
      overview: overview.value,
    })
    toast.show('已保存')
    emit('saved')
    emit('close')
  } catch (e) {
    toast.show('保存失败：' + errorText(e).slice(0, 80))
  } finally {
    busy.value = false
  }
}

function submit() {
  if (busy.value) return
  if (props.mode === 'edit') void saveEdit()
  else void startImport()
}

onBeforeUnmount(() => {
  disposed = true
  busy.value = false
})
</script>

<template>
  <div
    class="fixed inset-0 z-[150] flex items-end justify-center bg-black/60"
    @click.self="emit('close')"
  >
    <div class="w-full max-w-[560px] rounded-t-2xl bg-panel2 px-[18px] pt-4 pb-safe-18">
      <h3 class="m-0 mb-1.5 text-center text-[17px]">
        {{ mode === 'edit' ? '编辑影视信息' : '添加影视' }}
      </h3>

      <template v-if="mode === 'add'">
        <p class="m-0 mb-3 text-[12.5px] leading-relaxed text-mut">
          填宿主机上的文件或目录。影视文件留在原地，只登记路径，不搬动、不跑 AI。<br />
          目录里可以混放电影和剧集（S01E02 / 第01集 / Season 1 都能识别）。
        </p>

        <label class="mb-1.5 block text-[12.5px] text-mut">路径</label>
        <input
          v-model="path"
          type="text"
          placeholder="E:\Movies 或 E:\Movies\电影.mp4"
          class="w-full rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[14px] outline-none"
          @keydown.enter="submit"
        />

        <label class="mb-1.5 mt-2.5 block text-[12.5px] text-mut">类型</label>
        <select
          v-model="kind"
          class="w-full rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[14px] outline-none"
        >
          <option value="auto">自动识别</option>
          <option value="movie">全部当电影</option>
          <option value="series">全部当剧集</option>
        </select>

        <label class="mt-3.5 flex items-center gap-2 text-[13px]">
          <input v-model="posters" type="checkbox" class="h-4 w-4" />
          抽封面帧（每部片 / 剧一次，稍慢）
        </label>

        <div v-if="run" class="mt-3.5 rounded-[10px] bg-panel px-3 py-2.5 text-[12.5px] leading-relaxed text-mut">
          <div class="mb-1 text-[13px] text-fg">{{ progressText }}</div>
          {{ progressDetail }}
          <div v-if="run.current" class="truncate">{{ fileName(run.current) }}</div>
        </div>
        <div v-if="error" class="mt-2 break-all text-[12.5px] text-heart">{{ error }}</div>
      </template>

      <template v-else>
        <label class="mb-1.5 block text-[12.5px] text-mut">片名</label>
        <input
          v-model="title"
          type="text"
          class="w-full rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[14px] outline-none"
        />
        <label class="mb-1.5 mt-2.5 block text-[12.5px] text-mut">年份</label>
        <input
          v-model="year"
          type="number"
          min="1888"
          max="2100"
          class="w-full rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[14px] outline-none"
        />
        <label class="mb-1.5 mt-2.5 block text-[12.5px] text-mut">简介</label>
        <textarea
          v-model="overview"
          placeholder="可不填"
          class="min-h-[82px] w-full resize-y rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[14px] outline-none"
        />
      </template>

      <div class="mt-4 flex gap-2.5">
        <button class="flex-1 rounded-[10px] bg-panel py-2.5 text-[14.5px]" @click="emit('close')">
          取消
        </button>
        <button
          class="flex-1 rounded-[10px] bg-acc py-2.5 text-[14.5px] font-semibold disabled:opacity-50"
          :disabled="busy"
          @click="submit"
        >
          {{ busy ? '处理中…' : mode === 'edit' ? '保存' : '开始导入' }}
        </button>
      </div>
    </div>
  </div>
</template>
