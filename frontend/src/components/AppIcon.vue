<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ name: string; size?: number; stroke?: number; filled?: boolean }>(),
  { size: 22, stroke: 1.8, filled: false },
)

// 图标全部内联，避免为几个符号引入整个 iconify 集合（离线也能用）。
const ICONS: Record<string, string> = {
  heart:
    '<path d="M12 20.7l-1.2-1.1C6 15.4 3 12.7 3 9.3 3 6.6 5.1 4.5 7.8 4.5c1.5 0 3 .7 4.2 2 1.2-1.3 2.7-2 4.2-2 2.7 0 4.8 2.1 4.8 4.8 0 3.4-3 6.1-7.8 10.3L12 20.7z"/>',
  bin: '<path d="M4 7h16v13H4zM8 7V4h8v3M9 12h6"/>',
  folder:
    '<path d="M3 7h6l2 2h10v11H3z"/><path d="M12 12v5M9.5 14.5L12 12l2.5 2.5"/>',
  tag:
    '<path d="M20.6 13.4L12.6 21.4a2 2 0 01-2.8 0L3 14.6V3h11.6l6 6a2 2 0 010 2.8z"/><circle cx="8" cy="8" r="1.6"/>',
  trash:
    '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  restore: '<path d="M3 12a9 9 0 109-9M3 3v6h6"/>',
  close: '<path d="M5 5l14 14M19 5L5 19"/>',
  'chevron-left': '<path d="M15 4l-8 8 8 8"/>',
  'chevron-right': '<path d="M9 4l8 8-8 8"/>',
  play: '<path d="M8 5l11 7-11 7z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="M4.5 12.5l5 5 10-11"/>',
  sparkles:
    '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M18.5 16.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>',
  film:
    '<path d="M4 4h16v16H4z"/><path d="M8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 17v3h16v-3"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  camera:
    '<path d="M4 8h3l2-2.5h6L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.6"/>',
  refresh: '<path d="M20 12a8 8 0 11-2.4-5.7M20 4v5h-5"/>',
  folderOpen: '<path d="M3 7h6l2 2h10v11H3z"/><path d="M3 13h18"/>',
  calendar:
    '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.6h.01"/>',
}

const body = computed(() => ICONS[props.name] ?? '')
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    :fill="filled ? 'currentColor' : 'none'"
    stroke="currentColor"
    :stroke-width="stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="body"
  />
</template>
