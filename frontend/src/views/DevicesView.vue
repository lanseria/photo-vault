<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { errorText } from '@/api'
import type { Device } from '@/types'
import { useChromeStore } from '@/stores/chrome'
import { useDevicesStore } from '@/stores/devices'
import { useDialogStore } from '@/stores/dialog'
import { useFeedStore } from '@/stores/feed'
import { useSystemStore } from '@/stores/system'
import { useToastStore } from '@/stores/toast'
import DeviceCreateDialog from '@/components/DeviceCreateDialog.vue'
import DevicePicker from '@/components/DevicePicker.vue'

const router = useRouter()
const devices = useDevicesStore()
const chrome = useChromeStore()
const dialog = useDialogStore()
const feed = useFeedStore()
const system = useSystemStore()
const toast = useToastStore()

const creating = ref(false)
const mergeSource = ref<Device | null>(null)

const KIND: Record<string, { icon: string; label: string }> = {
  phone: { icon: '📱', label: '手机' },
  camera: { icon: '📷', label: '相机' },
  drone: { icon: '🚁', label: '无人机' },
  actioncam: { icon: '🎥', label: '运动相机' },
  other: { icon: '📦', label: '其他' },
}

const kindOf = (kind: string) => KIND[kind] ?? KIND.other

/** 跳到图库并按这台设备过滤（'unknown' = 未识别） */
function viewPhotos(device: number | 'unknown') {
  void router.push(device === 'unknown' ? '/gallery?device=unknown' : `/gallery?device=${device}`)
}

function syncChrome() {
  chrome.set({
    title: '设备',
    count: devices.devices.length ? `${devices.devices.length} 台` : '',
  })
}

async function load() {
  syncChrome()
  await devices.load(true)
  syncChrome()
}

onMounted(load)

async function rename(device: Device) {
  const name = await dialog.prompt({
    title: '给这台设备起个名字',
    message: `EXIF 里写的是「${device.model_raw || '未知'}」。留空则恢复默认名称。`,
    value: device.label || '',
    placeholder: device.model_canonical || device.model_raw,
    confirmText: '保存',
  })
  if (name === null) return
  try {
    const updated = await devices.rename(device.id, name.trim())
    toast.show(name.trim() ? `已改名为「${updated.name}」` : '已恢复默认名称')
    // 列表/灯箱里的设备名是随资产一起返回的，刷一遍
    feed.requestReload()
    await system.refreshStats()
  } catch (e) {
    toast.show('改名失败：' + errorText(e).slice(0, 40))
  }
}

function startMerge(device: Device) {
  mergeSource.value = device
}

async function onMergePick(into: number | null) {
  const source = mergeSource.value
  mergeSource.value = null
  if (!source || into == null) return
  const target = devices.byId.get(into)
  if (!target) return
  const ok = await dialog.confirm({
    title: `把「${source.name}」合并到「${target.name}」？`,
    message:
      `${source.n_assets} 张照片会改挂到「${target.name}」，源设备随之删除。\n` +
      'EXIF 里的机型号不动，所以以后导入同型号机器时，会重新建出一台源设备。',
    confirmText: '合并',
    danger: true,
  })
  if (!ok) return
  try {
    const res = await devices.merge(source.id, target.id)
    syncChrome()
    toast.show(`已合并：${res.moved} 张照片归到「${res.device.name}」`)
    feed.requestReload()
    await system.refreshStats()
  } catch (e) {
    toast.show('合并失败：' + errorText(e).slice(0, 40))
  }
}

async function onCreated(device: Device) {
  creating.value = false
  syncChrome()
  toast.show(`已新建设备「${device.name}」`)
  await system.refreshStats()
}
</script>

<template>
  <main class="pb-[120px]">
    <div
      class="mx-4 mb-3 rounded-xl border border-[rgba(255,159,10,.35)] bg-[rgba(255,159,10,.1)] px-3.5 py-3"
    >
      <div class="flex items-baseline gap-2">
        <span class="text-[13.5px] font-medium text-warn">未识别拍摄设备</span>
        <span class="text-[12.5px] text-mut">{{ devices.unknown }} 张</span>
        <span class="flex-1" />
        <button class="shrink-0 text-[12.5px] text-acc" @click="viewPhotos('unknown')">看照片 →</button>
      </div>
      <p class="m-0 mt-1 text-[12px] leading-relaxed text-mut">
        导入面板里可以给整批选设备；单张照片打开后点设备名也能改。
        照片列表右上角带
        <span class="inline-flex h-[15px] w-[15px] items-center justify-center rounded-full bg-warn align-text-bottom text-[10px] font-bold text-black">?</span>
        角标的就是这些没认出设备的。
      </p>
    </div>

    <div class="flex items-center gap-2 px-4 pb-2">
      <span class="text-[12.5px] text-mut">
        {{ devices.devices.length }} 台设备 · 同名机型可以合并成一台
      </span>
      <span class="flex-1" />
      <button
        class="shrink-0 rounded-[9px] bg-acc px-3.5 py-2 text-[13px] font-semibold"
        @click="creating = true"
      >
        ＋ 新建设备
      </button>
    </div>

    <div v-if="devices.devices.length" class="px-4">
      <div
        v-for="device in devices.devices"
        :key="device.id"
        :data-device-id="device.id"
        class="flex w-full items-center gap-3 border-b border-line/60 py-3"
      >
        <span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-panel2 text-[18px]">
          {{ kindOf(device.kind).icon }}
        </span>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1.5">
            <span class="truncate text-[14.5px]">{{ device.name }}</span>
            <span
              v-if="device.label"
              class="shrink-0 rounded bg-[rgba(10,132,255,.18)] px-1 py-px text-[10.5px] text-acc"
              >已命名</span
            >
          </div>
          <div class="truncate text-[11.5px] text-mut">
            {{ kindOf(device.kind).label }} ·
            {{ [device.make, device.model_raw].filter(Boolean).join(' ') || '未知型号' }} ·
            {{ device.n_assets }} 张
          </div>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <button class="px-0.5 py-1.5 text-[12.5px] text-acc" @click="viewPhotos(device.id)">
            看照片
          </button>
          <button class="px-0.5 py-1.5 text-[12.5px] text-acc" @click="rename(device)">
            重命名
          </button>
          <button
            class="px-0.5 py-1.5 text-[12.5px] text-acc disabled:opacity-40"
            :disabled="devices.devices.length < 2"
            @click="startMerge(device)"
          >
            合并
          </button>
        </div>
      </div>
    </div>

    <div v-else-if="devices.loading" class="spinner" />
    <div v-else class="px-6 py-16 text-center text-[14px] leading-relaxed text-mut">
      还没有识别出任何设备。<br />
      导入带 EXIF 的照片后，这里会出现机型列表；也可以点「＋ 新建设备」手动建一台。
    </div>

    <DeviceCreateDialog v-if="creating" @created="onCreated" @close="creating = false" />

    <DevicePicker
      v-if="mergeSource"
      title="合并到哪台设备？"
      :hint="`「${mergeSource.name}」的 ${mergeSource.n_assets} 张照片会全部改挂到目标设备，源设备随之删除。`"
      :exclude-id="mergeSource.id"
      :allow-manage="false"
      @pick="onMergePick"
      @close="mergeSource = null"
    />
  </main>
</template>
