<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import type { Asset } from '@/types'
import { errorText } from '@/api'
import { useBinStore } from '@/stores/bin'
import { useChromeStore } from '@/stores/chrome'
import { useDialogStore } from '@/stores/dialog'
import { useSystemStore } from '@/stores/system'
import { useToastStore } from '@/stores/toast'
import { useAssetActions } from '@/composables/useAssetActions'
import { usePhotoQuery } from '@/composables/usePhotoQuery'
import { fmtBytes, fmtDur } from '@/utils/format'
import PhotoCell from '@/components/PhotoCell.vue'
import PhotoLightbox from '@/components/PhotoLightbox.vue'

const bin = useBinStore()
const chrome = useChromeStore()
const dialog = useDialogStore()
const system = useSystemStore()
const toast = useToastStore()
const actions = useAssetActions()
const photo = usePhotoQuery()

const totals = computed(() => ({
  duration: bin.items.reduce((sum, a) => sum + (a.duration_ms || 0), 0),
  bytes: bin.items.reduce((sum, a) => sum + (a.bytes || 0), 0),
}))

const bridgeState = computed(() => {
  if (system.bridge === 'online') {
    return {
      text: '桥 · 在线',
      cls: 'border-[rgba(48,209,88,.45)] text-ok',
      title: '「出稿」会自动打开资源管理器',
    }
  }
  if (system.bridge === 'offline') {
    return {
      text: '桥 · 未启动',
      cls: 'border-[rgba(255,159,10,.45)] text-warn',
      title: '桌面桥没跑：出稿后只会复制路径。启动一次即开机自启：uv run photo-vault bridge --install',
    }
  }
  return { text: '桥 · 检测中', cls: '', title: '' }
})

function syncChrome() {
  chrome.set({
    title: '剪辑箱',
    count: bin.items.length ? `${bin.items.length} 项` : '',
  })
}

async function reload() {
  try {
    await bin.ensure()
    await bin.loadItems()
  } catch (e) {
    toast.show('剪辑箱加载失败：' + errorText(e).slice(0, 60))
  }
  syncChrome()
  void system.checkBridge()
}

onMounted(() => {
  void reload()
})

watch(() => bin.items.length, syncChrome)

async function remove(asset: Asset) {
  await actions.removeFromBin(asset.id)
  await bin.loadItems()
}

async function rename() {
  const name = await dialog.prompt({
    title: '剪辑箱名字',
    message: '同时决定出稿目录名。',
    value: bin.name,
    confirmText: '保存',
    required: true,
  })
  if (!name) return
  try {
    await bin.rename(name)
    toast.show('已改名')
  } catch {
    toast.show('改名失败')
  }
}

async function openDir() {
  if (!bin.lastDir) return toast.show('还没出稿过 — 先点「出稿」')
  if (await system.revealOnHost(bin.lastDir)) return
  toast.show('桌面桥未启动', '复制命令', () =>
    navigator.clipboard.writeText('uv run photo-vault bridge --install'),
  )
}

/* ---------------- 灯箱 ---------------- */

const assetIndex = computed(() => bin.items.findIndex(a => a.id === photo.photoId.value))
const currentAsset = computed<Asset | null>(() =>
  assetIndex.value >= 0 ? bin.items[assetIndex.value] : null,
)

watch(
  () => [photo.photoId.value, bin.items.length] as const,
  () => {
    if (photo.photoId.value && bin.items.length && assetIndex.value < 0) photo.close()
  },
)

function onActivate(asset: Asset) {
  photo.open(asset.id)
}

function stepPhoto(delta: number) {
  const i = assetIndex.value
  const next = bin.items[i + delta]
  if (next) photo.step(next.id)
}

async function onTrash() {
  const id = photo.photoId.value
  if (!id) return
  const next = bin.items[assetIndex.value + 1] ?? bin.items[assetIndex.value - 1]
  await actions.trash([id])
  await bin.loadItems()
  if (next) photo.step(next.id)
  else photo.close()
}

async function onPurge() {
  const id = photo.photoId.value
  if (!id) return
  const next = bin.items[assetIndex.value + 1] ?? bin.items[assetIndex.value - 1]
  await actions.purge([id])
  if (bin.items.some(a => a.id === id)) return
  await bin.loadItems()
  if (next) photo.step(next.id)
  else photo.close()
}
</script>

<template>
  <main class="pb-[120px]">
    <div class="flex flex-wrap items-center gap-2 px-4 pb-3">
      <span class="text-[13px] text-mut">
        「{{ bin.name }}」{{ bin.items.length }} 项
        <template v-if="bin.items.length">
          · {{ fmtDur(totals.duration) || '0:00' }} · {{ fmtBytes(totals.bytes) }}
        </template>
      </span>
      <span class="stat-pill" :class="bridgeState.cls" :title="bridgeState.title">
        {{ bridgeState.text }}
      </span>
      <span class="flex-1" />
      <button class="txt-btn !text-[14px]" @click="openDir">打开目录</button>
      <button class="txt-btn !text-[14px]" @click="rename">重命名</button>
      <button class="txt-btn !text-[14px]" @click="actions.clearBin()">清空</button>
      <button
        class="rounded-[9px] bg-acc px-4 py-2 text-[14px] font-semibold disabled:opacity-50"
        :disabled="!bin.items.length"
        @click="actions.deliverBin()"
      >
        出稿
      </button>
    </div>

    <div v-if="bin.items.length" class="photo-grid">
      <PhotoCell
        v-for="(asset, i) in bin.items"
        :key="asset.id"
        :asset="asset"
        :order="i + 1"
        removable
        @activate="onActivate(asset)"
        @remove="remove(asset)"
      />
    </div>

    <div
      v-else-if="!bin.loading"
      class="whitespace-pre-line px-5 py-20 text-center text-[14px] leading-relaxed text-mut"
    >
      剪辑箱是空的。{{ '\n' }}
      在任意视图里点右上角「选择」→ 勾选素材 → 底部「剪辑箱」，或者灯箱里点「剪辑箱」。{{ '\n' }}
      攒够了一批再回来点「出稿」。
    </div>

    <div v-else class="spinner" />

    <PhotoLightbox
      v-if="currentAsset"
      :asset="currentAsset"
      :items="bin.items"
      @close="photo.close()"
      @prev="stepPhoto(-1)"
      @next="stepPhoto(1)"
      @trash="onTrash"
      @purge="onPurge"
      @restore="photo.close()"
    />
  </main>
</template>
