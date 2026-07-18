import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base 固定 /v1/:开发(dev)与生产(build)一致,Vue Console 由 FastAPI 挂载于 /v1。
// 开发代理把 /api 转发到本机控制台(:8849),前端一律同源相对路径,无双前缀。
export default defineConfig(({ command }) => {
  const isBuild = command === 'build'
  return {
    base: '/v1/',
    plugins: [vue()],
    resolve: {
      alias: [
        // 顺序敏感:具体路径必须先于 '@' 前缀(第一个匹配生效)
        ...(isBuild
          ? [
              // 生产构建把 Mock 入口别名到占位:MSW、fixtures、场景切换器不进产物
              {
                find: '@/mocks/browser',
                replacement: fileURLToPath(new URL('./src/mocks/disabled.ts', import.meta.url)),
              },
              {
                find: '@/components/shared/ScenarioSwitcher.vue',
                replacement: fileURLToPath(
                  new URL('./src/components/shared/ScenarioSwitcherStub.vue', import.meta.url),
                ),
              },
            ]
          : []),
        { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
      ],
    },
    // mockServiceWorker.js 仅供开发期 Mock;生产产物不携带
    publicDir: isBuild ? false : 'public',
    server: {
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8849',
          changeOrigin: true,
        },
      },
    },
  }
})
