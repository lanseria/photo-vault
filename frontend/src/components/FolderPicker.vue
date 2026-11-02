<script setup lang="ts">
import { onMounted } from 'vue'
import { useDialogStore } from '@/stores/dialog'
import { useFoldersStore } from '@/stores/folders'

const props = withDefaults(
  defineProps<{
    /** 当前所在的文件夹 id，用来标「当前」；null = 不在任何文件夹里 */
    currentFolderId?: string | null
  }>(),
  { currentFolderId: null },
)

const emit = defineEmits<{ pick: [string | null]; close: [] }>()

const folders = useFoldersStore()
const dialog = useDialogStore()

/** 新建完直接把选中的素材移进去，一步到位 */
async function createAndPick() {
  const name = await dialog.prompt({
    title: '新建文件夹',
    message: `把选中的素材移进新文件夹。`,
    placeholder: '文件夹名字',
    required: true,
  })
  if (!name) return
  const folder = await folders.create(name)
  if (folder) emit('pick', folder.id)
}

onMounted(() => void folders.load(true))
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
        <h3 class="m-0 text-center text-[16px] font-semibold">移到文件夹</h3>
        <p class="m-0 mb-3 mt-1 text-center text-[12px] leading-relaxed text-mut">
          文件夹只改变素材在素材库里的位置，不复制、不移动磁盘文件。
        </p>

        <div class="min-h-0 flex-1 overflow-auto">
          <button
            v-if="props.currentFolderId"
            class="mb-1 flex w-full items-center gap-2.5 rounded-[10px] px-2 py-2.5 text-left hover:bg-white/10"
            @click="emit('pick', null)"
          >
            <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-panel text-[16px] text-mut">
              ↩
            </span>
            <span class="min-w-0 flex-1">
              <span class="block text-[14px]">移出文件夹</span>
              <span class="block text-[11.5px] text-mut">回到素材库的「未整理」</span>
            </span>
          </button>

          <button
            v-for="folder in folders.folders"
            :key="folder.id"
            class="mb-1 flex w-full items-center gap-2.5 rounded-[10px] px-2 py-2.5 text-left hover:bg-white/10"
            :class="currentFolderId === folder.id && 'bg-[rgba(10,132,255,.14)]'"
            @click="emit('pick', folder.id)"
          >
            <span
              class="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-panel text-[15px]"
            >
              <img
                v-if="folder.cover"
                :src="folder.cover"
                alt=""
                loading="lazy"
                class="h-full w-full object-cover"
              />
              <span v-else>🗂</span>
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-[14px]">{{ folder.name }}</span>
              <span class="block text-[11.5px] text-mut">{{ folder.count }} 项</span>
            </span>
            <span v-if="currentFolderId === folder.id" class="text-[12px] text-acc">当前</span>
          </button>

          <button
            class="mb-1 flex w-full items-center gap-2.5 rounded-[10px] px-2 py-2.5 text-left hover:bg-white/10"
            @click="createAndPick"
          >
            <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-panel text-[18px] text-acc">＋</span>
            <span class="min-w-0 flex-1">
              <span class="block text-[14px] text-acc">新建文件夹…</span>
              <span class="block text-[11.5px] text-mut">建好直接把选中的素材移进去</span>
            </span>
          </button>

          <div v-if="!folders.folders.length && !folders.loading" class="py-8 text-center text-[13px] text-mut">
            还没有文件夹<br />选上面「新建文件夹」，给素材归归类
          </div>
          <div v-else-if="folders.loading && !folders.folders.length" class="spinner" />
        </div>

        <div class="mt-3 flex gap-2.5">
          <button class="flex-1 rounded-[10px] bg-panel py-2.5 text-[14px]" @click="emit('close')">
            取消
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
