import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import UnoCSS from 'unocss/vite'

// 后端（FastAPI）默认在 7878。开发时前端跑 5173，只把接口和媒体路径代理过去，
// 这样 vite 的 HMR 和后端的 Range/缩略图缓存都能各自发挥。
const BACKEND = process.env.VITE_BACKEND ?? 'http://127.0.0.1:7878'

const proxied = ['/api', '/thumbs', '/faces', '/frames', '/file', '/preview', '/transcode']

export default defineConfig({
  plugins: [vue(), UnoCSS()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // 直接产出到 Python 包里：后端只认 vault/static/index.html，没有第二份拷贝
    outDir: '../vault/static',
    emptyOutDir: true,
    target: 'es2022',
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // xgplayer 单独分包：合进主 chunk 后 esbuild minify 的输入会超 1MB，
        // 触发 esbuild 的临时文件回退，Windows 上有杀软时这个临时文件删不掉，
        // 构建直接报 "remove ... Access is denied"（也顺带把播放器拆成独立并行下载）
        manualChunks: id => (id.includes('xgplayer') ? 'xgplayer' : undefined),
      },
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: Object.fromEntries(proxied.map(p => [p, { target: BACKEND, changeOrigin: false }])),
  },
})
