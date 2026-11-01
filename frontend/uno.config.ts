import {
  defineConfig,
  presetWind3,
  transformerDirectives,
  transformerVariantGroup,
} from 'unocss'

export default defineConfig({
  presets: [presetWind3()],
  transformers: [transformerDirectives(), transformerVariantGroup()],
  theme: {
    colors: {
      bg: '#000000',
      panel: '#1c1c1e',
      panel2: '#2c2c2e',
      fg: '#ffffff',
      mut: '#8e8e93',
      acc: '#0a84ff',
      heart: '#ff375f',
      line: '#38383a',
      ok: '#30d158',
      warn: '#ff9f0a',
    },
    fontFamily: {
      sans: '-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif',
    },
  },
  shortcuts: {
    tap: 'transition-opacity active:opacity-60',
    chip: 'inline-flex items-center gap-1.5 rounded-full bg-panel text-fg px-3.5 py-1.5 text-[13.5px] leading-none',
    'chip-on': '!bg-acc !text-white',
    'chip-count': 'text-[12px] opacity-55',
    'txt-btn': 'text-acc text-[16px] font-medium px-1 py-1.5 tap disabled:text-mut',
    'icon-btn': 'flex items-center justify-center w-9 h-9 rounded-lg text-acc tap',
    'seg-btn': 'px-2.5 py-1.5 rounded-[7px] text-[12.5px] text-mut tap',
    'seg-on': 'bg-acc text-white',
    'stat-pill': 'text-[11.5px] text-mut border border-line rounded-full px-2 py-0.5 whitespace-nowrap',
    'card-badge': 'absolute font-semibold text-[11px] text-white [text-shadow:0_1px_3px_rgba(0,0,0,.9)]',
    sheet:
      'fixed left-0 right-0 bottom-0 z-[120] bg-panel2 rounded-t-2xl px-4 pt-2.5 pb-safe-18 transition-transform duration-200 ease-out',
    'sheet-grab': 'w-[38px] h-[5px] rounded-full bg-[#5a5a5e] mx-auto mt-0.5 mb-3',
    'sheet-title': 'm-0 mb-3 text-[16px] text-center font-semibold',
  },
  rules: [
    [/^pb-safe$/, () => ({ 'padding-bottom': 'env(safe-area-inset-bottom)' })],
    [/^pb-safe-(\d+)$/, ([, n]) => ({ 'padding-bottom': `calc(${n}px + env(safe-area-inset-bottom))` })],
    [/^mb-safe$/, () => ({ 'margin-bottom': 'env(safe-area-inset-bottom)' })],
  ],
})
