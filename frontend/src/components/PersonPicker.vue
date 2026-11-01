<script setup lang="ts">
import { computed } from 'vue'
import type { Person } from '@/types'
import { initials } from '@/utils/format'

const props = defineProps<{
  people: Person[]
  title: string
  dissolveText?: string
}>()

const emit = defineEmits<{ pick: [Person]; dissolve: []; close: [] }>()
const imageUrl = (faceId: string) => `/faces/${faceId}.jpg`

const list = computed(() => props.people)
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[140] bg-black/55 flex items-end justify-center"
      @click.self="emit('close')"
    >
      <div class="w-full max-w-[520px] bg-panel2 rounded-t-2xl px-4 pt-3.5 pb-safe-16">
        <h3 class="m-0 mb-2.5 text-[16px] text-center font-semibold">{{ title }}</h3>
        <div class="max-h-[50vh] overflow-auto">
          <button
            v-for="person in list"
            :key="person.id"
            class="flex w-full items-center gap-2.5 rounded-[10px] px-1.5 py-2.5 text-left hover:bg-white/10"
            @click="emit('pick', person)"
          >
            <img
              v-if="person.cover"
              :src="imageUrl(person.cover)"
              alt=""
              class="w-9 h-9 rounded-full object-cover bg-panel"
            />
            <span
              v-else
              class="w-9 h-9 rounded-full bg-panel flex items-center justify-center text-mut"
              >{{ initials(person.name) }}</span
            >
            <span class="flex-1">{{ person.name || '未命名' }}</span>
            <span class="text-mut text-[12.5px]">{{ person.n_assets }} 张</span>
          </button>
          <div v-if="!list.length" class="px-1.5 py-2.5 text-mut">没有其他人物</div>
        </div>
        <button
          v-if="dissolveText"
          class="w-full mt-2.5 rounded-[10px] py-2.5 text-heart text-[13.5px] bg-panel"
          @click="emit('dissolve')"
        >
          {{ dissolveText }}
        </button>
        <button
          class="w-full mt-1.5 rounded-[10px] py-[11px] bg-panel text-acc font-semibold"
          @click="emit('close')"
        >
          取消
        </button>
      </div>
    </div>
  </Teleport>
</template>
