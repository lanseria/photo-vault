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
  /** 允许整格拖拽（素材库拖进文件夹用），其他视图不传 */
  draggable?: boolean
}>()

const emit = defineEmits<{
  activate: [event?: MouseEvent]
  dragstart: [event: DragEvent]
  remove: []
}>()

const thumb = computed(() => `/thumbs/${props.asset.sha}.webp`)
const duration = computed(() => (props.asset.kind === 'video' ? fmtDur(props.asset.duration_ms) : ''))
const placeholder = computed(() => (props.asset.kind === 'video' ? '▶' : 'RAW'))

/** 未识别设备的问号角标：素材库是剪辑素材，不提示这个（图库/其它视图照旧） */
const showUnknown = computed(() => props.asset.device_unknown === true && props.asset.library !== 'material')
</script>

<template>
  <div
    class="relative aspect-square bg-panel overflow-hidden cursor-pointer select-none"
    :data-id="asset.id"
    :title="asset.model"
    :draggable="!!props.draggable"
    @click="emit('activate', $event)"
    @dragstart="emit('dragstart', $event)"
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
      v-if="asset.raw_ext"
      class="card-badge bottom-[5px] left-1.5 rounded-[5px] bg-[rgba(60,60,67,.6)] px-[5px] py-px text-[10px] tracking-[.5px] [text-shadow:none]"
      title="RAW+JPEG：剪辑出稿时两个文件一起给"
      >RAW+JPEG</span
    >
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

    <!-- 没识别出拍摄设备的照片：右上角一个小问号，点了进灯箱可以手工归到某台设备（素材库不提示） -->
    <span
      v-if="showUnknown"
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
