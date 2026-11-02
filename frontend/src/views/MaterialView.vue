<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import type { MaterialFolder } from '@/types'
import { useAssetActions } from '@/composables/useAssetActions'
import { useDialogStore } from '@/stores/dialog'
import { useFoldersStore } from '@/stores/folders'
import { useFeedStore } from '@/stores/feed'
import { useSystemStore } from '@/stores/system'
import { isAssetDrag, readAssetDragIds } from '@/utils/dnd'
import FeedView from './FeedView.vue'

/**
 * 素材库的文件夹视图。
 *
 * 页面分两段：上面是用户自建的文件夹（网格，点进某个文件夹看里面的素材，
 * 也可以把下面的照片直接拖进来），下面是「未整理」的照片流 —— 直接复用
 * FeedView（路由 meta.feed = 'unfiled'），分页、多选、灯箱逻辑全部白拿。
 */
const router = useRouter()
const folders = useFoldersStore()
const feed = useFeedStore()
const dialog = useDialogStore()
const system = useSystemStore()
const actions = useAssetActions()

async function createFolder() {
  const name = await dialog.prompt({
    title: '新建文件夹',
    message: '用项目、主题给素材归类，比如「毕业旅拍」「客户 A」。',
    placeholder: '文件夹名字',
    required: true,
  })
  if (!name) return
  const folder = await folders.create(name)
  if (folder) void system.refreshStats()
}

async function renameFolder(folder: MaterialFolder) {
  const name = await dialog.prompt({
    title: '重命名文件夹',
    placeholder: '文件夹名字',
    value: folder.name,
    required: true,
  })
  if (!name || name === folder.name) return
  await folders.rename(folder.id, name)
}

async function deleteFolder(folder: MaterialFolder) {
  const ok = await dialog.confirm({
    title: `删除文件夹「${folder.name}」？`,
    message: `里面的 ${folder.count} 项素材会回到「未整理」，不会被删除。`,
    confirmText: '删除文件夹',
    danger: true,
  })
  if (!ok) return
  if (await folders.remove(folder.id)) {
    void system.refreshStats()
    void feed.requestReload() // 未整理的列表和计数都变了
  }
}

/* ---------------- 拖照片进文件夹 ----------------
   拖的是站内资产（utils/dnd.ts 的自定义类型），外部文件不接。
   整个卡片是落点：松手后整批 set_folder，setFolder 里会刷列表和计数。 */

const dropHover = ref<string | null>(null) // 正被拖拽悬停的文件夹 id；'__new__' = 新建卡片

function onFolderDragOver(event: DragEvent, id: string) {
  if (!isAssetDrag(event)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dropHover.value = id
}

function onFolderDragLeave(event: DragEvent, id: string) {
  // 划过卡片内部子元素也会触发 dragleave，只有真离开卡片才熄掉高亮
  if (dropHover.value !== id) return
  const related = event.relatedTarget as Node | null
  const card = event.currentTarget as HTMLElement
  if (related && card.contains(related)) return
  dropHover.value = null
}

async function onFolderDrop(event: DragEvent, folder: MaterialFolder) {
  if (!isAssetDrag(event)) return
  event.preventDefault()
  dropHover.value = null
  const ids = readAssetDragIds(event)
  if (!ids.length) return
  await actions.setFolder(ids, folder.id)
}

/** 拖到「新建文件夹」卡片：先起名，建好直接把拖来的素材移进去 */
async function onNewDrop(event: DragEvent) {
  if (!isAssetDrag(event)) return
  event.preventDefault()
  dropHover.value = null
  const ids = readAssetDragIds(event)
  if (!ids.length) return
  const name = await dialog.prompt({
    title: `新建文件夹（${ids.length} 项）`,
    message: '建好立即把拖来的素材移进去。',
    placeholder: '文件夹名字',
    required: true,
  })
  if (!name) return
  const folder = await folders.create(name)
  if (folder) {
    await actions.setFolder(ids, folder.id)
    void system.refreshStats()
  }
}

/* ---------------- 文件夹卡片的 ⋯ 菜单（重命名 / 删除） ---------------- */

const menuId = ref<string | null>(null)
const menuLeft = ref(16)
const menuTop = ref(120)
const menuEl = ref<HTMLElement | null>(null)

function toggleMenu(folder: MaterialFolder, event: MouseEvent) {
  if (menuId.value === folder.id) {
    menuId.value = null
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menuLeft.value = Math.max(8, Math.min(rect.left, window.innerWidth - 190))
  menuTop.value = rect.bottom + 6
  menuId.value = folder.id
}

function onMenuAction(folder: MaterialFolder, action: 'rename' | 'delete') {
  menuId.value = null
  if (action === 'rename') void renameFolder(folder)
  else void deleteFolder(folder)
}

// 菜单渲染出来才知道尺寸，超右/超底就往回拉
function clampMenu() {
  const el = menuEl.value
  if (!el) return
  const overflowX = menuLeft.value + el.offsetWidth - (window.innerWidth - 8)
  if (overflowX > 0) menuLeft.value = Math.max(8, menuLeft.value - overflowX)
  const overflowY = menuTop.value + el.offsetHeight - (window.innerHeight - 8)
  if (overflowY > 0) menuTop.value = Math.max(8, menuTop.value - overflowY)
}

watch(menuId, async id => {
  if (!id) return
  await nextTick()
  clampMenu()
})

function onDocumentClick(event: MouseEvent) {
  const target = event.target as Element | null
  if (!target?.closest('[data-folder-menu]')) menuId.value = null
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  window.addEventListener('resize', clampMenu)
  // 每次进页面都刷一遍：文件夹的 count/cover 变化不经过 revision
  void folders.load(true)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  window.removeEventListener('resize', clampMenu)
})

// 上传等外部写入会 bump revision，未整理的素材变了，文件夹封面/计数也跟着刷
watch(
  () => feed.revision,
  () => void folders.load(true),
)
</script>

<template>
  <div>
    <section class="px-4 pt-2">
      <div class="flex items-baseline justify-between">
        <h2 class="m-0 text-[21px] font-bold leading-tight">文件夹</h2>
        <button class="txt-btn" @click="createFolder">＋ 新建</button>
      </div>

      <div
        class="grid grid-cols-[repeat(auto-fill,minmax(108px,1fr))] gap-x-2.5 gap-y-3.5 py-3"
        data-folder-menu
      >
        <div
          v-for="folder in folders.folders"
          :key="folder.id"
          class="group relative"
          @dragover="onFolderDragOver($event, folder.id)"
          @dragleave="onFolderDragLeave($event, folder.id)"
          @drop="onFolderDrop($event, folder)"
        >
          <button
            class="tap flex w-full flex-col items-stretch gap-1 text-left"
            @click="router.push(`/material/folders/${folder.id}`)"
          >
            <span
              class="flex aspect-[4/3] items-center justify-center overflow-hidden rounded-[12px] bg-panel2 text-[26px]"
              :class="dropHover === folder.id && 'ring-2 ring-acc'"
            >
              <img
                v-if="folder.cover"
                :src="folder.cover"
                alt=""
                loading="lazy"
                class="h-full w-full object-cover"
              />
              <span v-else class="opacity-60">🗂</span>
            </span>
            <span class="truncate text-[13px] leading-tight">{{ folder.name }}</span>
            <span class="-mt-0.5 text-[11.5px] text-mut">{{ folder.count }} 项</span>
          </button>
          <button
            class="absolute right-0.5 top-0.5 flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-[14px] leading-none text-fg opacity-55 hover:opacity-100"
            :class="menuId === folder.id && '!opacity-100'"
            title="重命名 / 删除"
            @click.stop="toggleMenu(folder, $event)"
          >
            ⋯
          </button>
        </div>

        <button
          class="tap flex aspect-[4/3] flex-col items-center justify-center gap-1 rounded-[12px] border border-dashed border-line text-mut hover:border-acc hover:text-acc max-sm:hidden"
          :class="dropHover === '__new__' && 'ring-2 ring-acc'"
          @click="createFolder"
          @dragover="onFolderDragOver($event, '__new__')"
          @dragleave="onFolderDragLeave($event, '__new__')"
          @drop="onNewDrop"
        >
          <span class="text-[22px] leading-none">＋</span>
          <span class="text-[12px]">新建文件夹</span>
        </button>
      </div>

      <div v-if="!folders.folders.length && folders.loading" class="spinner !my-3" />
    </section>

    <section class="px-4 pb-1 pt-4">
      <h2 class="m-0 text-[21px] font-bold leading-tight">
        未整理
        <span v-if="folders.unfiled" class="ml-1.5 text-[13px] font-normal text-mut">
          {{ folders.unfiled }} 项
        </span>
      </h2>
    </section>

    <!-- 未整理素材的照片流：路由 meta.feed = 'unfiled'，FeedView 自己读；
         draggable = 格子可以拖到上面的文件夹卡片上 -->
    <FeedView draggable />

    <Teleport to="body">
      <div
        v-if="menuId"
        ref="menuEl"
        data-folder-menu
        class="fixed z-[80] min-w-[168px] rounded-xl bg-panel2 p-1.5 shadow-[0_8px_30px_rgba(0,0,0,.6)]"
        :style="{ left: menuLeft + 'px', top: menuTop + 'px' }"
      >
        <template v-for="folder in folders.folders" :key="folder.id">
          <template v-if="folder.id === menuId">
            <button
              class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-[14px] hover:bg-white/10"
              @click="onMenuAction(folder, 'rename')"
            >
              ✏️ 重命名
            </button>
            <button
              class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-[14px] text-heart hover:bg-white/10"
              @click="onMenuAction(folder, 'delete')"
            >
              🗑 删除文件夹
            </button>
          </template>
        </template>
      </div>
    </Teleport>
  </div>
</template>
