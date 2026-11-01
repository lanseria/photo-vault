<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { Device } from '@/types'
import { useDevicesStore } from '@/stores/devices'
import DeviceCreateDialog from './DeviceCreateDialog.vue'

const props = withDefaults(
  defineProps<{
    title?: string
    hint?: string
    /** 当前已选的设备 id，用来标「当前」 */
    currentId?: number | null
    /** 是否提供「未识别（清除设备）」选项 */
    allowNone?: boolean
    /** 列表里隐藏某台设备（合并时排除源设备自己） */
    excludeId?: number | null
    /** 是否提供「新建设备」入口 */
    allowCreate?: boolean
    /** 是否显示「管理设备」跳转 */
    allowManage?: boolean
  }>(),
  {
    title: '选择拍摄设备',
    hint: '',
    currentId: null,
    allowNone: false,
    excludeId: null,
    allowCreate: true,
    allowManage: true,
  },
)

const emit = defineEmits<{ pick: [number | null]; close: []; manage: [] }>()
const devices = useDevicesStore()

const creating = ref(false)

const list = computed(() =>
  devices.devices.filter(device => device.id !== props.excludeId),
)

const KIND_ICON: Record<string, string> = {
  phone: '📱',
  camera: '📷',
  drone: '🚁',
  actioncam: '🎥',
  other: '📦',
}

function icon(kind: string): string {
  return KIND_ICON[kind] ?? KIND_ICON.other
}

function manage() {
  emit('manage')
}

/** 新建完直接选中它：调用方（导入 / 灯箱 / 合并）拿到 id 就能接着用 */
function onCreated(device: Device) {
  creating.value = false
  void devices.load(true)
  emit('pick', device.id)
}

onMounted(() => void devices.load())
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[165] flex items-end justify-center bg-black/60 backdrop-blur-[2px]"
      @click.self="emit('close')"
    >
      <div
        class="flex max-h-[82vh] w-full max-w-[560px] flex-col rounded-t-2xl bg-panel2 px-4 pt-2.5 pb-safe-18"
      >
        <div class="sheet-grab" />
        <h3 class="m-0 text-center text-[16px] font-semibold">{{ title }}</h3>
        <p class="m-0 mb-3 mt-1 text-center text-[12px] leading-relaxed text-mut">
          {{ hint || 'EXIF 里没写机型时，可以按已有设备手工归类；之后在「设备」页还能改名。' }}
        </p>

        <div class="min-h-0 flex-1 overflow-auto">
          <button
            v-if="allowNone"
            class="mb-1 flex w-full items-center gap-2.5 rounded-[10px] px-2 py-2.5 text-left hover:bg-white/10"
            @click="emit('pick', null)"
          >
            <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-panel text-[16px] text-mut">?</span>
            <span class="min-w-0 flex-1">
              <span class="block text-[14px]">未识别设备</span>
              <span class="block text-[11.5px] text-mut">清除归类，回到「未识别」</span>
            </span>
            <span v-if="currentId == null" class="text-[12px] text-acc">当前</span>
          </button>

          <button
            v-for="device in list"
            :key="device.id"
            :data-device-id="device.id"
            class="mb-1 flex w-full items-center gap-2.5 rounded-[10px] px-2 py-2.5 text-left hover:bg-white/10"
            :class="currentId === device.id && 'bg-[rgba(10,132,255,.14)]'"
            @click="emit('pick', device.id)"
          >
            <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-panel text-[16px]">
              {{ icon(device.kind) }}
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-[14px]">{{ device.name }}</span>
              <span class="block truncate text-[11.5px] text-mut">
                {{ [device.make, device.model_raw].filter(Boolean).join(' ') || device.name }} ·
                {{ device.n_assets }} 张
              </span>
            </span>
            <span v-if="currentId === device.id" class="text-[12px] text-acc">当前</span>
          </button>

          <button
            v-if="allowCreate"
            class="mb-1 flex w-full items-center gap-2.5 rounded-[10px] px-2 py-2.5 text-left hover:bg-white/10"
            @click="creating = true"
          >
            <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-panel text-[18px] text-acc">＋</span>
            <span class="min-w-0 flex-1">
              <span class="block text-[14px] text-acc">新建设备…</span>
              <span class="block text-[11.5px] text-mut">EXIF 认不出来的机器，比如「大疆 Action 2」</span>
            </span>
          </button>

          <div v-if="!list.length && !devices.loading" class="py-8 text-center text-[13px] text-mut">
            还没有识别出任何设备<br />导入带 EXIF 的照片后，这里会出现机型列表
          </div>
          <div v-else-if="devices.loading" class="spinner" />
        </div>

        <div class="mt-3 flex gap-2.5">
          <button
            v-if="allowManage"
            class="flex-1 rounded-[10px] bg-panel py-2.5 text-[14px] text-acc"
            @click="manage"
          >
            管理设备
          </button>
          <button class="flex-1 rounded-[10px] bg-panel py-2.5 text-[14px]" @click="emit('close')">
            取消
          </button>
        </div>
      </div>

      <DeviceCreateDialog
        v-if="creating"
        @created="onCreated"
        @close="creating = false"
      />
    </div>
  </Teleport>
</template>
