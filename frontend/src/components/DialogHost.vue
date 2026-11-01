<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useDialogStore } from '@/stores/dialog'

const dialog = useDialogStore()
const text = ref('')
const input = ref<HTMLInputElement | null>(null)

const current = computed(() => dialog.current)
const isPrompt = computed(() => current.value?.kind === 'prompt')
const required = computed(() =>
  current.value?.kind === 'prompt' ? !!current.value.required : false,
)
const placeholder = computed(() =>
  current.value?.kind === 'prompt' ? current.value.placeholder : '',
)
const canSubmit = computed(() => !required.value || !!text.value.trim())

watch(current, async value => {
  if (!value) return
  text.value = value.kind === 'prompt' ? value.value : ''
  await nextTick()
  input.value?.focus()
  input.value?.select()
})

function submit() {
  if (!current.value || !canSubmit.value) return
  dialog.settle(isPrompt.value ? text.value.trim() : true)
}

function cancel() {
  dialog.settle(null)
}
</script>

<template>
  <div
    v-if="current"
    class="fixed inset-0 z-[180] flex items-center justify-center bg-black/60 px-6"
    @click.self="cancel"
    @keydown.esc="cancel"
  >
    <form
      class="w-full max-w-[420px] rounded-2xl bg-panel2 px-5 pb-4 pt-5"
      @submit.prevent="submit"
    >
      <h3 class="m-0 text-[16px] font-semibold">{{ current.title }}</h3>
      <p v-if="current.message" class="mb-0 mt-2 text-[13px] leading-relaxed text-mut">
        {{ current.message }}
      </p>
      <input
        v-if="isPrompt"
        ref="input"
        v-model="text"
        :placeholder="placeholder"
        class="mt-3 w-full rounded-[9px] border border-line bg-panel px-3 py-2.5 text-[14px] outline-none"
        @keydown.esc="cancel"
      />
      <div class="mt-4 flex gap-2.5">
        <button
          type="button"
          class="flex-1 rounded-[10px] bg-panel py-2.5 text-[14.5px]"
          @click="cancel"
        >
          {{ current.cancelText || '取消' }}
        </button>
        <button
          type="submit"
          class="flex-1 rounded-[10px] py-2.5 text-[14.5px] font-semibold disabled:opacity-50"
          :class="current.danger ? 'bg-heart text-white' : 'bg-acc text-white'"
          :disabled="!canSubmit"
        >
          {{ current.confirmText || '确定' }}
        </button>
      </div>
    </form>
  </div>
</template>
