import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base 固定 /:开发(dev)与生产(build)一致,Vue Console 由 FastAPI 挂载于根路径。
// 开发代理把 /api 转发到本机控制台(:8849),前端一律同源相对路径,无双前缀。
export default defineConfig(({ command }) => ({
  base: '/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  // 生产构建不拷贝 public/(避免历史 mock worker 进入产物)
  publicDir: command === 'build' ? false : 'public',
  build: {
    rollupOptions: {
      output: {
        // echarts 拆为独立 chunk:配合 TrendChart 异步加载,非仪表盘页面不下载
        // (Vite 8 / rolldown 仅支持函数形式;zrender 是 echarts 的渲染依赖)
        manualChunks(id: string) {
          if (id.includes('node_modules/echarts') || id.includes('node_modules/zrender')) {
            return 'echarts'
          }
        },
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8849',
        changeOrigin: true,
      },
    },
  },
}))
