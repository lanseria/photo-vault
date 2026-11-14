<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, errorText } from '@/api'
import { useToastStore } from '@/stores/toast'
import type { FaceQueue } from '@/types'

/**
 * 人脸识别任务控制：开始（全量入队）/ 暂停 / 继续 + 进度条。
 * 提取在宿主机 worker 里跑，网页只写 jobs 队列和 ml_meta 暂停标记；
 * 队列清空后 worker 自动聚类，所以这里在「从有到无」时发 drained 让人物页刷新。
 */
const emit = defineEmits<{ drained: [] }>()
const toast = useToastStore()

const queue = ref<FaceQueue | null>(null)
const busy = ref(false)

let timer: ReturnType<typeof setInterval> | undefined

const active = computed(() => !!queue.value && queue.value.pending + queue.value.running > 0)
const pct = computed(() => {
  const q = queue.value
  if (!q || !q.assets_total) return 0
  return Math.round((q.assets_done / q.assets_total) * 100)
})

async function refresh() {
  try {
    queue.value = await api.facesQueue()
  } catch {
    /* 状态拿不到就不打扰，下一轮再试 */
  }
}

// 队列从有到无 = 本轮提取（含自动聚类）结束，人物页可以重新加载了
watch(active, (now, before) => {
  if (before && !now) emit('drained')
})

async function start() {
  busy.value = true
  try {
    const res = await api.runFaces()
    toast.show(res.queued ? `已入队 ${res.queued} 张待识别` : '所有照片都已识别过或已在队列里')
    await refresh()
  } catch (e) {
    toast.show('入队失败：' + errorText(e).slice(0, 40))
  } finally {
    busy.value = false
  }
}

async function setPaused(paused: boolean) {
  busy.value = true
  try {
    await api.pauseFaces(paused)
    toast.show(paused ? '已暂停，正在识别的照片会收尾' : '已继续')
    await refresh()
  } catch (e) {
    toast.show((paused ? '暂停失败：' : '继续失败：') + errorText(e).slice(0, 40))
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), 3000)
})

onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div v-if="queue && queue.assets_total" class="px-4 pt-1 pb-1">
    <div class="flex items-center gap-3 rounded-xl bg-panel px-3.5 py-2.5">
      <div class="min-w-0 flex-1">
        <div class="text-[13px] truncate">
          <template v-if="queue.paused">
            已暂停 · 已识别 {{ queue.assets_done }}/{{ queue.assets_total }}
            <span v-if="active" class="text-mut"> · 待处理 {{ queue.pending + queue.running }}</span>
          </template>
          <template v-else-if="active">
            识别中 {{ queue.assets_done }}/{{ queue.assets_total }}
            <span class="text-mut"> · 待处理 {{ queue.pending + queue.running }}</span>
          </template>
          <template v-else>
            已识别 {{ queue.assets_done }}/{{ queue.assets_total }}
          </template>
          <span v-if="queue.failed" class="text-warn"> · 失败 {{ queue.failed }}</span>
        </div>
        <div
          v-if="active && !queue.paused"
          class="mt-1.5 h-[3px] overflow-hidden rounded-full bg-panel2"
        >
          <div
            class="h-full rounded-full bg-acc transition-all duration-500"
            :style="{ width: pct + '%' }"
          />
        </div>
      </div>

      <button
        v-if="queue.paused"
        class="txt-btn"
        :disabled="busy"
        @click="setPaused(false)"
      >
        继续
      </button>
      <button
        v-else-if="active"
        class="txt-btn"
        :disabled="busy"
        @click="setPaused(true)"
      >
        暂停
      </button>
      <button
        v-else
        class="rounded-full bg-acc px-4 py-1.5 text-[14px] font-medium text-white tap disabled:opacity-50"
        :disabled="busy"
        @click="start"
      >
        开始识别
      </button>
    </div>
  </div>
</template>
