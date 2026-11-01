import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 底部胶囊提示；支持一个「撤销 / 查看」动作按钮。 */
export const useToastStore = defineStore('toast', () => {
  const message = ref('')
  const actionLabel = ref('')
  const visible = ref(false)

  let timer: ReturnType<typeof setTimeout> | undefined
  let action: (() => unknown) | null = null

  function hide() {
    visible.value = false
    action = null
  }

  function show(msg: string, label = '', fn?: () => unknown) {
    message.value = msg
    actionLabel.value = label
    action = fn ?? null
    visible.value = true
    clearTimeout(timer)
    timer = setTimeout(hide, 4200)
  }

  function run() {
    const fn = action
    hide()
    try {
      void Promise.resolve(fn?.()).catch(() => undefined)
    } catch {
      /* 撤销失败就算了，不打扰用户 */
    }
  }

  return { message, actionLabel, visible, show, hide, run }
})
