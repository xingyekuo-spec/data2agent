import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, vi } from 'vitest'
import { buildHandlers, strictUnhandledRequest } from '@/mocks/handlers'
import { setScenario } from '@/mocks/scenario'

// jsdom 无 canvas:全局 mock ECharts(真实渲染由浏览器验收覆盖)
vi.mock('echarts/core', () => ({
  use: () => {},
  init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }),
}))
vi.mock('echarts/charts', () => ({ BarChart: {} }))
vi.mock('echarts/components', () => ({ GridComponent: {}, TooltipComponent: {} }))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

// jsdom 缺 ResizeObserver:补最小 stub,组件 resize 路径仍可测试
if (typeof globalThis.ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver =
    ResizeObserverStub as unknown as typeof globalThis.ResizeObserver
}

const server = setupServer(...buildHandlers())

export { server }

beforeAll(() => {
  server.listen({ onUnhandledRequest: strictUnhandledRequest })
})

afterEach(() => {
  server.resetHandlers()
  setScenario('healthy')
  sessionStorage.clear()
  localStorage.clear()
})

afterAll(() => {
  server.close()
})
