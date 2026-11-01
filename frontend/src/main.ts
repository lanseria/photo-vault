import '@unocss/reset/tailwind.css'
import 'virtual:uno.css'
import './styles/main.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'

// 自己恢复滚动位置，别让浏览器在数据回来之前先乱跳（见 composables/scrollMemory.ts）
history.scrollRestoration = 'manual'

const app = createApp(App)
app.use(createPinia())
app.use(router)

// 等首屏路由解析完再挂载：App 里拿到的 route 就是真实位置，
// 刷新时不会先按「/」记一遍再跳一次。
void router.isReady().then(() => app.mount('#app'))
