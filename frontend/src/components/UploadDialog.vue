<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useUploadStore, type UploadJob } from '@/stores/upload'
import { filesFromDrop } from '@/utils/files'
import { fmtBytes } from '@/utils/format'
import DevicePicker from './DevicePicker.vue'

const router = useRouter()
const upload = useUploadStore()
const fileInput = ref<HTMLInputElement | null>(null)
const pickingDevice = ref(false)

/* ---- 拖入 ----
   只在对话框开着的时候接文件：这是「仅点击导入后才奏效」的拖入入口。
   App.vue 的全局护栏只拦浏览器默认行为，真正收文件的是这里。
   dragenter/dragleave 在子元素间穿梭会成对触发，用计数器判断是否真离开窗口。 */
const dragging = ref(false)
let dragDepth = 0

function hasFiles(event: DragEvent): boolean {
  return !!event.dataTransfer?.types.includes('Files')
}

function onDragEnter(event: DragEvent) {
  if (!hasFiles(event)) return
  dragDepth += 1
  dragging.value = true
}

function onDragOver(event: DragEvent) {
  if (!hasFiles(event)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}

function onDragLeave() {
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dragging.value = false
}

async function onDrop(event: DragEvent) {
  if (!hasFiles(event)) return
  event.preventDefault()
  dragDepth = 0
  dragging.value = false
  const files = await filesFromDrop(event.dataTransfer)
  if (files.length) upload.addFiles(files, upload.library)
}

watch(
  () => upload.open,
  open => {
    dragDepth = 0
    dragging.value = false
    if (open) {
      window.addEventListener('dragenter', onDragEnter)
      window.addEventListener('dragover', onDragOver)
      window.addEventListener('dragleave', onDragLeave)
      window.addEventListener('drop', onDrop)
    } else {
      window.removeEventListener('dragenter', onDragEnter)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('dragleave', onDragLeave)
      window.removeEventListener('drop', onDrop)
    }
  },
)

onBeforeUnmount(() => {
  window.removeEventListener('dragenter', onDragEnter)
  window.removeEventListener('dragover', onDragOver)
  window.removeEventListener('dragleave', onDragLeave)
  window.removeEventListener('drop', onDrop)
})

const ACCEPT =
  'image/*,video/*,.heic,.heif,.jfif,.jpe,.dng,.cr2,.cr3,.nef,.arw,.raf,.orf,.rw2,.pef,.srw,.mts,.m2ts,.insv,.360,.lrv'

const done = computed(
  () => upload.queue.filter(j => j.status === 'ok' || j.status === 'dup' || j.status === 'err').length,
)
const totalBytes = computed(() => upload.queue.reduce((sum, job) => sum + job.file.size, 0))
/** 已经传完、但有设备没认出来的才值得提示；上传途中不打扰 */
const unknownCount = computed(() =>
  upload.running ? 0 : upload.unknownIds.length,
)

function statusText(job: UploadJob): string {
  if (job.status === 'ok') return '已入库'
  if (job.status === 'dup') return '已在库中'
  if (job.status === 'err') return '失败'
  if (job.status === 'up') return job.pct >= 1 ? '入库中…' : `${Math.round(job.pct * 100)}%`
  return '等待'
}

function progressOf(job: UploadJob): number {
  if (job.status === 'ok' || job.status === 'dup' || job.status === 'err') return 1
  return job.pct || 0
}

function rowClass(job: UploadJob): string {
  return job.status === 'ok' ? 'ok' : job.status === 'dup' ? 'dup' : job.status === 'err' ? 'err' : ''
}

function pick() {
  fileInput.value?.click()
}

function onFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = [...(input.files ?? [])]
  input.value = ''
  if (files.length) upload.addFiles(files.map(file => ({ file, name: file.name })), upload.library)
}
</script>

<template>
  <div
    v-if="upload.open"
    class="fixed inset-0 z-[160] flex items-end justify-center bg-black/60"
    @click.self="upload.closeDialog()"
  >
    <div
      class="relative flex max-h-[84vh] w-full max-w-[640px] flex-col overflow-hidden rounded-t-2xl bg-panel"
      :class="dragging && 'ring-2 ring-inset ring-acc'"
    >
      <div
        v-if="dragging"
        class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-[rgba(10,132,255,.14)] text-[15px] font-semibold text-acc"
      >
        松手把文件加入队列
      </div>
      <div class="flex items-center px-[18px] pb-2 pt-4">
        <h3 class="m-0 flex-1 text-[17px]">导入</h3>
        <button class="w-7 text-right text-[18px] text-mut" @click="upload.closeDialog()">✕</button>
      </div>

      <div class="flex gap-2 px-[18px] pb-2.5">
        <button
          class="flex-1 rounded-[10px] bg-panel2 px-1.5 py-2.5 text-[13.5px] text-mut disabled:opacity-50"
          :class="upload.library === 'gallery' && '!bg-[rgba(10,132,255,.18)] !text-acc font-semibold'"
          :disabled="upload.running"
          @click="upload.setLibrary('gallery')"
        >
          📷 图库
        </button>
        <button
          class="flex-1 rounded-[10px] bg-panel2 px-1.5 py-2.5 text-[13.5px] text-mut disabled:opacity-50"
          :class="upload.library === 'material' && '!bg-[rgba(10,132,255,.18)] !text-acc font-semibold'"
          :disabled="upload.running"
          @click="upload.setLibrary('material')"
        >
          🗂 素材库
        </button>
      </div>

      <p class="m-0 px-[18px] pb-3 text-[12.5px] leading-relaxed text-mut">
        把文件或文件夹拖到窗口任意位置，也可以选择文件。
        <b class="font-medium text-[#c7c7cc]">HEIC / RAW / 视频 / Live Photo</b>
        都支持；已经导入过的会自动跳过。
      </p>

      <div class="min-h-[110px] flex-1 overflow-auto px-[18px]">
        <div v-if="!upload.queue.length" class="px-0 py-8 text-center text-[13px] leading-loose text-mut">
          还没有文件。<br />把图片 / 视频拖进这个窗口，或点下面「选择文件」。
        </div>
        <div
          v-for="job in upload.queue.slice(0, 400)"
          :key="job.id"
          class="flex items-center gap-3 border-b border-line py-[9px] last:border-b-0"
          :class="'urow ' + rowClass(job)"
        >
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px]">{{ job.name }}</div>
            <div class="mt-1.5 h-[3px] overflow-hidden rounded-[2px] bg-panel2">
              <i
                class="block h-full rounded-[2px] bg-acc transition-[width] duration-150"
                :class="{
                  '!bg-ok': job.status === 'ok',
                  '!bg-[#636366]': job.status === 'dup',
                  '!bg-heart': job.status === 'err',
                }"
                :style="{ width: progressOf(job) * 100 + '%' }"
              />
            </div>
          </div>
          <div
            class="w-[78px] shrink-0 text-right text-[12px] text-mut"
            :class="{
              '!text-ok': job.status === 'ok',
              '!text-heart': job.status === 'err',
            }"
            :title="job.status === 'err' ? job.note : ''"
          >
            {{ statusText(job) }}
          </div>
        </div>
        <div
          v-if="upload.queue.length > 400"
          class="py-6 text-center text-[13px] text-mut"
        >
          还有 {{ upload.queue.length - 400 }} 个文件未显示（仍会继续上传）
        </div>
      </div>

      <div
        v-if="unknownCount"
        class="mx-[18px] mb-1 mt-2 flex items-center gap-2 rounded-[10px] border border-[rgba(255,159,10,.35)] bg-[rgba(255,159,10,.12)] px-3 py-2.5"
      >
        <span class="min-w-0 flex-1 text-[12.5px] leading-snug text-warn">
          {{ unknownCount }} 项没识别出拍摄设备
        </span>
        <button class="shrink-0 text-[12.5px] font-medium text-acc" @click="pickingDevice = true">
          选择设备
        </button>
        <button class="shrink-0 text-[12.5px] text-mut" @click="upload.dismissUnknown()">忽略</button>
      </div>

      <div class="flex items-center gap-2.5 px-[18px] pt-3 pb-safe-16">
        <span class="flex-1 text-[12.5px] leading-[1.45] text-mut">
          <template v-if="upload.queue.length">
            {{ done }}/{{ upload.queue.length }}
            <template v-if="upload.added"> · 新增 {{ upload.added }}</template>
            <template v-if="upload.dup"> · 重复 {{ upload.dup }}</template>
            <template v-if="upload.fail"> · 失败 {{ upload.fail }}</template>
            <template v-if="!upload.running && done === upload.queue.length">
              · {{ fmtBytes(totalBytes) }}
            </template>
          </template>
        </span>
        <button class="rounded-[10px] bg-panel2 px-4 py-2.5 text-[14px]" @click="pick">
          选择文件
        </button>
        <button
          class="rounded-[10px] bg-acc px-4 py-2.5 text-[14px] font-semibold"
          @click="upload.closeDialog()"
        >
          完成
        </button>
      </div>

      <input
        ref="fileInput"
        type="file"
        multiple
        class="hidden"
        :accept="ACCEPT"
        @change="onFiles"
      />
    </div>

    <DevicePicker
      v-if="pickingDevice && upload.open"
      title="这批照片用的是什么设备？"
      :hint="`有 ${unknownCount} 项 EXIF 里没有机型。选一台已有设备，全部归过去；也可以先忽略。`"
      @pick="deviceId => { pickingDevice = false; if (deviceId != null) void upload.assignUnknown(deviceId) }"
      @close="pickingDevice = false"
      @manage="pickingDevice = false; upload.closeDialog(); router.push('/devices')"
    />
  </div>
</template>
