<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useChromeStore } from '@/stores/chrome'
import { usePeopleStore } from '@/stores/people'
import { initials } from '@/utils/format'
import FaceQueueControl from '@/components/FaceQueueControl.vue'

const router = useRouter()
const people = usePeopleStore()
const chrome = useChromeStore()

async function refreshPeople() {
  await people.load(true)
  chrome.set({ count: people.persons.length ? `${people.persons.length} 人` : '' })
}

onMounted(refreshPeople)

// 「开始识别」的队列跑完（worker 已自动聚类）→ 重载人物列表
function onQueueDrained() {
  void refreshPeople()
}

function hideBrokenImage(event: Event) {
  ;(event.target as HTMLImageElement).style.display = 'none'
}

function open(id: string) {
  void router.push(`/people/${id}`)
}
</script>

<template>
  <main class="pb-[120px]">
    <FaceQueueControl @drained="onQueueDrained" />

    <div v-if="people.persons.length" class="grid grid-cols-[repeat(auto-fill,minmax(112px,1fr))] gap-x-2 gap-y-4 px-4 py-4">
      <button
        v-for="person in people.persons"
        :key="person.id"
        class="flex flex-col items-center gap-[7px]"
        @click="open(person.id)"
      >
        <div
          class="relative flex h-[86px] w-[86px] items-center justify-center overflow-hidden rounded-full bg-panel2 text-[30px] font-semibold text-mut"
        >
          <span>{{ initials(person.name) }}</span>
          <img
            v-if="person.cover"
            :src="`/faces/${person.cover}.jpg`"
            alt=""
            loading="lazy"
            class="absolute inset-0 h-full w-full object-cover"
            @error="hideBrokenImage"
          />
        </div>
        <div class="max-w-[112px] truncate text-[13.5px]">{{ person.name || '未命名' }}</div>
        <div class="-mt-1 text-[11.5px] text-mut">{{ person.n_assets }} 张</div>
      </button>
    </div>

    <div
      v-else-if="!people.loading"
      class="px-6 py-16 text-center text-[14px] leading-loose text-mut"
    >
      还没有识别出人物。{{ '\n' }}点上方「开始识别」全量入队，
      {{ '\n' }}宿主机 worker（<code class="rounded bg-panel px-1.5 py-0.5">ml.worker</code>）
      提取完会自动聚类成人物。
      <template v-if="people.unassigned">
        {{ '\n\n' }}当前有 {{ people.unassigned }} 张未归类的人脸，
        {{ '\n' }}补跑聚类可把它们归进人物。
      </template>
    </div>

    <div v-else class="spinner" />
  </main>
</template>
