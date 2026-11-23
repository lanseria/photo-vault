<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { Library } from '@/types'
import { useAssetActions } from '@/composables/useAssetActions'
import { useFeedStore } from '@/stores/feed'
import { useSelectionStore } from '@/stores/selection'
import { useTagSheetStore } from '@/stores/tagSheet'
import AppIcon from './AppIcon.vue'
import DevicePicker from './DevicePicker.vue'
import FolderPicker from './FolderPicker.vue'

const route = useRoute()
const router = useRouter()
const feed = useFeedStore()
const selection = useSelectionStore()
const sheet = useTagSheetStore()
const actions = useAssetActions()

const devicePickerOpen = ref(false)
const folderPickerOpen = ref(false)

const trashView = computed(() => route.meta.feed === 'trash')
const movable = computed(() => !!route.meta.feed && !trashView.value)
const inMaterial = computed(() => route.meta.library === 'material')
/** 文件夹是每个库都有的整理维度；人物 / 回收站没有 meta.library，不显示 */
const hasFolders = computed(() => !!route.meta.library)
/** 当前库：文件夹按钮要按库拿对应的文件夹列表 */
const lib = computed<Library>(() => (route.meta.library === 'material' ? 'material' : 'gallery'))
/** 当前正在浏览的素材库文件夹；在里面多选时「移出」才有意义 */
const currentFolderId = computed(() =>
  route.meta.feed === 'folder' && typeof route.params.id === 'string' ? route.params.id : null,
)
const one = computed(() =>
  selection.count === 1 ? (feed.items.find(a => a.id === selection.firstId) ?? null) : null,
)
const favoriteLabel = computed(() => (one.value?.favorite ? '取消喜欢' : '喜欢'))
const picked = computed(() => selection.list())

function toggleFavorite() {
  void actions.setFavorite(selection.list(), !(one.value?.favorite ?? false))
}

function addToBin() {
  void actions.addToBin(selection.list())
}

function moveLibrary() {
  void actions.moveTo(inMaterial.value ? 'gallery' : 'material')
}

/** 移入 / 移出素材库文件夹；点「当前」的文件夹等于没动，直接关掉 */
async function pickFolder(folderId: string | null) {
  folderPickerOpen.value = false
  if (folderId && folderId === currentFolderId.value) return
  await actions.setFolder(selection.list(), folderId)
}

function openTags() {
  sheet.show(selection.list())
}

async function pickDevice(deviceId: number | null) {
  devicePickerOpen.value = false
  await actions.setDevice(picked.value, deviceId)
}

function toggleHidden() {
  void actions.setHidden(selection.list(), !(one.value?.hidden ?? false))
}

function remove() {
  void actions.trash(selection.list())
}

function restore() {
  void actions.restore(selection.list())
}

function purge() {
  void actions.purge(selection.list())
}
</script>

<template>
  <div
    class="fixed inset-x-0 bottom-0 z-[60] flex items-center justify-between gap-1 border-t border-line bg-[rgba(22,22,24,.94)] px-2 pt-2.5 pb-safe-12 backdrop-blur-lg"
  >
    <span class="whitespace-nowrap px-1 text-[12px] text-mut">
      {{ selection.count ? `已选 ${selection.count} 项` : '' }}
    </span>

    <template v-if="!trashView">
      <button class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]" @click="toggleFavorite">
        <AppIcon name="heart" :size="22" :filled="!!one?.favorite" />
        {{ favoriteLabel }}
      </button>
      <button class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]" @click="addToBin">
        <AppIcon name="bin" :size="22" />
        剪辑箱
      </button>
      <button
        v-if="movable"
        class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]"
        @click="moveLibrary"
      >
        <AppIcon name="folder" :size="22" />
        {{ inMaterial ? '图库' : '素材库' }}
      </button>
      <button
        v-if="movable && hasFolders"
        class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]"
        @click="folderPickerOpen = true"
      >
        <AppIcon name="folderOpen" :size="22" />
        文件夹
      </button>
      <button
        v-if="movable && inMaterial"
        class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]"
        @click="actions.enqueueAI(selection.list())"
      >
        <AppIcon name="sparkles" :size="22" />
        AI 识别
      </button>
      <button class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]" @click="openTags">
        <AppIcon name="tag" :size="22" />
        标签
      </button>
      <button
        class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]"
        @click="devicePickerOpen = true"
      >
        <AppIcon name="camera" :size="22" />
        设备
      </button>
      <button
        class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]"
        title="隐藏后默认所有列表不显示；顶栏「展示隐藏」开关可以重新看到"
        @click="toggleHidden"
      >
        <AppIcon :name="one?.hidden ? 'eye' : 'eyeOff'" :size="22" />
        {{ one?.hidden ? '取消隐藏' : '隐藏' }}
      </button>
      <button class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px] text-heart" @click="remove">
        <AppIcon name="trash" :size="22" />
        删除
      </button>
    </template>

    <template v-else>
      <button class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px]" @click="restore">
        <AppIcon name="restore" :size="22" />
        恢复
      </button>
      <button class="flex flex-col items-center gap-[3px] px-1.5 text-[10.5px] text-heart" @click="purge">
        <AppIcon name="trash" :size="22" />
        彻底删除
      </button>
    </template>

    <DevicePicker
      v-if="devicePickerOpen"
      :title="`给选中的 ${selection.count} 项设置设备`"
      hint="选中的照片会统一挂到这台设备；也可以选「未识别设备」清除原来的归类。"
      allow-none
      @pick="pickDevice"
      @close="devicePickerOpen = false"
      @manage="devicePickerOpen = false; router.push('/devices')"
    />

    <FolderPicker
      v-if="folderPickerOpen"
      :library="lib"
      :current-folder-id="currentFolderId"
      @pick="pickFolder"
      @close="folderPickerOpen = false"
    />
  </div>
</template>
