/**
 * Mock 模式的 MSW browser worker。只在 MODE === 'mock' 时由 main.ts 动态
 * 导入,REAL 不会加载 MSW。
 */
import { setupWorker } from 'msw/browser'
import { buildHandlers, strictUnhandledRequest } from './handlers'

export async function startMockWorker(): Promise<void> {
  const worker = setupWorker(...buildHandlers())
  await worker.start({
    // 未匹配请求直接报错:Mock 必须显式声明,不允许静默穿透到真实网络
    onUnhandledRequest: strictUnhandledRequest,
    serviceWorker: {
      url: `${import.meta.env.BASE_URL}mockServiceWorker.js`,
    },
  })
}
