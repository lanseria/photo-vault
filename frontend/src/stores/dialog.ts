import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface DialogOptions {
  title: string
  message?: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

export interface PromptOptions extends DialogOptions {
  value?: string
  placeholder?: string
  /** 输入为空时是否禁用确认 */
  required?: boolean
}

type Request =
  | ({ kind: 'confirm' } & DialogOptions)
  | ({ kind: 'prompt'; value: string; placeholder: string; required: boolean } & PromptOptions)

let resolveCurrent: ((value: string | boolean | null) => void) | null = null

/**
 * 应用内的确认框 / 输入框。
 * 用 Promise 包一层，调用处就是 `if (!(await dialog.confirm(...))) return`，
 * 比 window.confirm 更好看，也不会把页面冻住。
 */
export const useDialogStore = defineStore('dialog', () => {
  const current = ref<Request | null>(null)

  function confirm(options: DialogOptions): Promise<boolean> {
    return new Promise(resolve => {
      resolveCurrent = value => resolve(value === true)
      current.value = { kind: 'confirm', ...options }
    })
  }

  function prompt(options: PromptOptions): Promise<string | null> {
    return new Promise(resolve => {
      resolveCurrent = value => resolve(typeof value === 'string' ? value : null)
      current.value = {
        kind: 'prompt',
        value: options.value ?? '',
        placeholder: options.placeholder ?? '',
        required: options.required ?? false,
        ...options,
      }
    })
  }

  function settle(value: string | boolean | null) {
    const resolve = resolveCurrent
    resolveCurrent = null
    current.value = null
    resolve?.(value)
  }

  return { current, confirm, prompt, settle }
})
