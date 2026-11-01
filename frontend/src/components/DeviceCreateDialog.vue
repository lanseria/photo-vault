<script setup lang="ts">
import { ref } from 'vue'
import { errorText } from '@/api'
import type { Device } from '@/types'
import { useDevicesStore } from '@/stores/devices'

const emit = defineEmits<{ created: [Device]; close: [] }>()
const devices = useDevicesStore()

const name = ref('')
const kind = ref('other')
const busy = ref(false)
const error = ref('')

const KINDS = [
  { value: 'phone', icon: '📱', label: '手机' },
  { value: 'camera', icon: '📷', label: '相机' },
  { value: 'drone', icon: '🚁', label: '无人机' },
  { value: 'actioncam', icon: '🎥', label: '运动相机' },
  { value: 'other', icon: '📦', label: '其他' },
]

async function submit() {
  const value = name.value.trim()
  if (!value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const device = await devices.create(value, kind.value)
    emit('created', device)
    emit('close')
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div
    class="fixed inset-0 z-[175] flex items-center justify-center bg-black/60 px-5"
    @click.self="emit('close')"
  >
    <form class="w-full max-w-[440px] rounded-2xl bg-panel2 px-5 pb-4 pt-5" @submit.prevent="submit">
      <h3 class="m-0 text-[16px] font-semibold">新建设备</h3>
      <p class="mb-0 mt-2 text-[12.5px] leading-relaxed text-mut">
        EXIF 认不出来的机器可以手动建一台，比如「大疆 Action 2」「办公室的 A7M4」。
        建好后在照片详情里把照片归过来。
      </p>

      <input
        v-model="name"
        autofocus
        maxlength="40"
        placeholder="例如：大疆 Action 2"
        class="mt-3 w-full rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[15px] outline-none"
        @keydown.esc="emit('close')"
      />

      <div class="mt-3 flex flex-wrap gap-1.5">
        <button
          v-for="option in KINDS"
          :key="option.value"
          type="button"
          class="chip"
          :class="kind === option.value && 'chip-on'"
          @click="kind = option.value"
        >
          {{ option.icon }} {{ option.label }}
        </button>
      </div>

      <p v-if="error" class="mb-0 mt-2.5 text-[12.5px] leading-relaxed text-heart">{{ error }}</p>

      <div class="mt-4 flex gap-2.5">
        <button
          type="button"
          class="flex-1 rounded-[10px] bg-panel py-2.5 text-[14.5px]"
          @click="emit('close')"
        >
          取消
        </button>
        <button
          type="submit"
          class="flex-1 rounded-[10px] bg-acc py-2.5 text-[14.5px] font-semibold disabled:opacity-50"
          :disabled="!name.trim() || busy"
        >
          {{ busy ? '创建中…' : '创建' }}
        </button>
      </div>
    </form>
  </div>
</template>
