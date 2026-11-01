<script setup lang="ts">
import { useToastStore } from '@/stores/toast'

const toast = useToastStore()
</script>

<template>
  <div
    class="fixed bottom-[96px] left-1/2 z-[200] flex -translate-x-1/2 items-center gap-3.5 rounded-full bg-[rgba(50,50,54,.96)] px-[18px] py-2.5 text-[14px] transition-opacity duration-200"
    :class="toast.visible ? 'opacity-100' : 'pointer-events-none opacity-0'"
    role="status"
  >
    <span>{{ toast.message }}</span>
    <button v-if="toast.actionLabel" class="font-semibold text-acc" @click="toast.run()">
      {{ toast.actionLabel }}
    </button>
  </div>
</template>
