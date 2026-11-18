<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { usePeopleStore } from '@/stores/people'
import { useFeedStore } from '@/stores/feed'
import { useToastStore } from '@/stores/toast'
import { errorText } from '@/api'
import { initials } from '@/utils/format'
import PersonFacesSheet from '@/components/PersonFacesSheet.vue'

/**
 * 人物详情页顶部的封面行：主封面 + 备用封面两张头像 + 名字 + 上传 / 恢复默认。
 * 主封面缺失（没传或文件丢了）时头像自动回退到备用封面。
 * 「人脸」按钮打开人脸管理抽屉（忽略误检脸）。
 */
const props = defineProps<{ personId: string }>()

const people = usePeopleStore()
const feed = useFeedStore()
const toast = useToastStore()

const person = computed(() => people.byId.get(props.personId) ?? null)
const facesOpen = ref(false)

/** 上传 / 恢复后换一下查询参数，绕过浏览器里旧的 /covers 缓存 */
const bust = ref(Date.now())
const fileInput = ref<HTMLInputElement | null>(null)
/** 正在上传的槽位：0 = 空闲，1 = 主封面，2 = 备用封面 */
const uploading = ref<0 | 1 | 2>(0)
/** 待上传的槽位（点按钮 / 点头像时记下，change 事件里用） */
const pendingSlot = ref<1 | 2>(1)

/** 主头像：主封面 → 备用封面 → 系统人脸裁剪 → 首字母 */
const mainSrc = computed(() => {
  const p = person.value
  if (!p) return ''
  if (p.custom_cover) return people.coverSrc(p, bust.value, 1)
  if (p.backup_cover) return people.coverSrc(p, bust.value, 2)
  return people.coverSrc(p, bust.value, 1)
})
const backupSrc = computed(() => (person.value ? people.coverSrc(person.value, bust.value, 2) : ''))

/** 标记着主头像当前那张图 404 了（文件被手动清掉之类）→ 下一优先级顶上 */
const mainBroken = ref(false)
watch(mainSrc, () => {
  mainBroken.value = false
})
const displaySrc = computed(() => (mainBroken.value ? backupSrc.value : mainSrc.value))

const coverLabel = computed(() => {
  const p = person.value
  if (!p) return ''
  return p.custom_cover ? '自定义封面' : p.backup_cover ? '备用封面' : '默认封面'
})

function pick(slot: 1 | 2) {
  if (uploading.value) return
  pendingSlot.value = slot
  fileInput.value?.click()
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 清掉，同一张图还能再选一次
  const slot = pendingSlot.value
  if (!file) return
  uploading.value = slot
  try {
    await people.setCover(props.personId, file, slot)
    bust.value = Date.now()
    toast.show(slot === 1 ? '主封面已更新' : '备用封面已更新')
  } catch (e) {
    toast.show('封面更新失败：' + errorText(e).slice(0, 40))
  } finally {
    uploading.value = 0
  }
}

/** 恢复默认 = 两张都清掉，回到系统挑的人脸裁剪 */
async function resetAll() {
  uploading.value = 1
  try {
    await Promise.all([people.resetCover(props.personId, 1), people.resetCover(props.personId, 2)])
    bust.value = Date.now()
    toast.show('已恢复默认封面')
  } catch (e) {
    toast.show('操作失败：' + errorText(e).slice(0, 40))
  } finally {
    uploading.value = 0
  }
}

/** 人脸抽屉里忽略/恢复了脸 → 人物计数变了，照片流也可能少照片 */
function onFacesChanged() {
  void people.load(true)
  feed.requestReload()
}

/** 封面文件不在了（迁移 / 手动清理）就退回字母头像 */
function hideBrokenImage(event: Event) {
  ;(event.target as HTMLImageElement).style.display = 'none'
}
</script>

<template>
  <div v-if="person" class="flex items-center gap-4 px-4 pb-3 pt-1.5 max-sm:gap-3 max-sm:px-3">
    <!-- 主封面：点击可上传 / 更换；主封面缺失（没传或文件挂了）时头像显示备用图 -->
    <div class="flex shrink-0 items-center">
      <button
        class="relative flex h-[76px] w-[76px] cursor-pointer items-center justify-center overflow-hidden rounded-full bg-panel2 text-[28px] font-semibold text-mut tap"
        :title="person.custom_cover ? '更换主封面' : '上传主封面'"
        @click="pick(1)"
      >
        <span>{{ initials(person.name) }}</span>
        <img
          v-if="displaySrc"
          :key="displaySrc"
          :src="displaySrc"
          alt=""
          class="absolute inset-0 h-full w-full object-cover"
          @error="mainBroken = true"
        />
        <span
          v-if="uploading === 1"
          class="absolute inset-0 flex items-center justify-center bg-black/55 text-[12px] font-normal text-white"
        >
          上传中…
        </span>
      </button>

      <!-- 备用封面：小一号叠在主头像右侧；没传时虚线 + 号占位 -->
      <button
        class="relative -ml-4 flex h-[60px] w-[60px] cursor-pointer items-center justify-center overflow-hidden rounded-full bg-panel2 ring-2 ring-black tap"
        :class="backupSrc ? '' : 'border border-dashed border-line'"
        title="上传备用封面（主封面缺失时顶上）"
        @click="pick(2)"
      >
        <span v-if="!backupSrc" class="text-[18px] leading-none text-mut">＋</span>
        <img
          v-else
          :key="backupSrc"
          :src="backupSrc"
          alt=""
          class="absolute inset-0 h-full w-full object-cover"
          @error="hideBrokenImage"
        />
        <span
          v-if="uploading === 2"
          class="absolute inset-0 flex items-center justify-center bg-black/55 text-[11px] font-normal text-white"
        >
          上传中…
        </span>
      </button>
    </div>

    <div class="min-w-0 flex-1">
      <div class="truncate text-[21px] font-bold leading-tight max-sm:text-[19px]">
        {{ person.name || '未命名' }}
      </div>
      <div class="mt-1 text-[12.5px] text-mut">
        {{ person.n_assets }} 张 · {{ coverLabel }}
      </div>
      <button
        v-if="person.n_faces"
        class="mt-1 text-[12.5px] text-acc tap"
        @click="facesOpen = true"
      >
        人脸 {{ person.n_faces }} ›
      </button>
    </div>

    <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onFile" />

    <div class="flex shrink-0 flex-col gap-1.5 max-sm:flex-row max-sm:flex-wrap max-sm:justify-end">
      <button
        class="rounded-lg bg-acc px-3 py-1.5 text-[12.5px] font-semibold text-white tap disabled:opacity-50"
        :disabled="uploading !== 0"
        @click="pick(1)"
      >
        {{ person.custom_cover ? '换主封面' : '上传主封面' }}
      </button>
      <button
        class="rounded-lg border border-line px-3 py-1.5 text-[12.5px] tap disabled:opacity-50"
        :disabled="uploading !== 0"
        @click="pick(2)"
      >
        {{ person.backup_cover ? '换备用' : '上传备用' }}
      </button>
      <button
        v-if="person.custom_cover || person.backup_cover"
        class="rounded-lg border border-line px-3 py-1.5 text-[12.5px] tap disabled:opacity-50"
        :disabled="uploading !== 0"
        @click="resetAll"
      >
        恢复默认
      </button>
    </div>
  </div>

  <PersonFacesSheet
    v-if="facesOpen && person"
    :person-id="props.personId"
    :person-name="person.name"
    @close="facesOpen = false"
    @changed="onFacesChanged"
  />
</template>
