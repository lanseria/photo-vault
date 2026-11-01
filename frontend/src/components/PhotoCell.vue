<script setup lang="ts">
import { computed } from 'vue'
import type { Asset } from '@/types'
import { fmtDur } from '@/utils/format'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  asset: Asset
  selecting?: boolean
  selected?: boolean
  /** 剪辑箱里的顺序号（1 开始），其他视图不传 */
  order?: number
  removable?: boolean
}>()

const emit = defineEmits<{ activate: []; remove: [] }>()

const thumb = computed(() => `/thumbs/${props.asset.sha}.webp`)
const duration = computed(() => (props.asset.kind === 'video' ? fmtDur(props.asset.duration_ms) : ''))
const placeholder = computed(() => (props.asset.kind === 'video' ? '▶' : 'RAW'))
</script>

<template>
  <div
    class="relative aspect-square bg-panel overflow-hidden cursor-pointer select-none"
    :data-id="asset.id"
    :title="asset.model"
    @click="emit('activate')"
  >
    <img
      v-if="asset.has_thumb"
      :src="thumb"
      alt=""
      loading="lazy"
      decoding="async"
      class="block h-full w-full object-cover"
      :class="[selected ? 'opacity-75' : '', asset.deleted ? 'opacity-45' : '']"
    />
    <div v-else class="flex h-full items-center justify-center text-[11px] text-mut">
      {{ placeholder }}
    </div>

    <span v-if="duration" class="card-badge bottom-[5px] left-1.5">▶ {{ duration }}</span>
    <span
      v-if="asset.kind === 'live_photo'"
      class="card-badge left-1.5 top-[5px] rounded-[5px] bg-[rgba(60,60,67,.55)] px-[5px] py-px text-[10px] tracking-[.5px] [text-shadow:none]"
      >LIVE</span
    >
    <AppIcon
      v-if="asset.favorite"
      name="heart"
      :size="15"
      filled
      class="absolute bottom-[5px] right-1.5 text-white drop-shadow-[0_1px_2px_rgba(0,0,0,.8)]"
    />

    <!-- 没识别出拍摄设备的照片：右上角一个小问号，点了进灯箱可以手工归到某台设备 -->
    <span
      v-if="asset.device_unknown"
      class="absolute top-1.5 z-[2] flex h-[18px] w-[18px] items-center justify-center rounded-full bg-warn text-[11px] font-bold text-black shadow-[0_1px_3px_rgba(0,0,0,.6)]"
      :class="removable ? 'right-[30px]' : 'right-1.5'"
      title="未识别拍摄设备"
      >?</span
    >

    <span
      v-if="order !== undefined"
      class="card-badge left-1.5 top-[5px] rounded-[5px] bg-black/60 px-[5px] py-px text-[10px] tracking-[.5px] [text-shadow:none]"
      >{{ String(order).padStart(3, '0') }}</span
    >

    <span
      v-if="selecting"
      class="absolute left-1.5 top-1.5 flex h-[23px] w-[23px] items-center justify-center rounded-full border-[1.6px] border-white/85 bg-black/25"
      :class="selected && '!border-acc !bg-acc'"
    >
      <AppIcon v-if="selected" name="check" :size="14" :stroke="3.2" />
    </span>

    <button
      v-if="removable"
      class="absolute right-1.5 top-1.5 z-[2] flex h-[22px] w-[22px] items-center justify-center rounded-full bg-black/60 text-[12px] text-white"
      title="移出剪辑箱"
      @click.stop="emit('remove')"
    >
      ✕
    </button>
  </div>
</template>
