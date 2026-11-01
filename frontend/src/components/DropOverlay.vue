<script setup lang="ts">
defineProps<{ visible: boolean; library: 'gallery' | 'material' }>()
</script>

<template>
  <div
    v-if="visible"
    class="pointer-events-none fixed inset-0 z-[170] flex flex-col items-center justify-center gap-2 border-2 border-dashed border-[rgba(10,132,255,.85)] bg-black/60 backdrop-blur-[3px]"
  >
    <div class="text-[22px] font-bold">
      松开，导入到 <b class="text-acc">{{ library === 'material' ? '素材库' : '图库' }}</b>
    </div>
    <div class="text-[13px] text-mut">图片 / 视频直接进库，重复内容按哈希跳过</div>
  </div>
</template>
