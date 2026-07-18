import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  // 与 vite.config.ts 保持一致:BASE_URL 固定 /v1/(node 环境下 vitest 不
  // 从 base 推导 import.meta.env.BASE_URL,用 define 显式固定)
  base: '/v1/',
  define: {
    'import.meta.env.BASE_URL': '"/v1/"',
  },
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.ts'],
    css: false,
  },
})
