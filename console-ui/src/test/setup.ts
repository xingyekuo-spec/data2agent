import { afterAll, afterEach, beforeAll, vi } from 'vitest'
import { installFetchStub, server } from './fetch-stub'
import { setScenario } from './scenario'

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

// jsdom 无 matchMedia(AppLayout 窄屏断点用):补最小 stub,matches 恒 false(宽屏)
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList
}

// Node 22+ 自带实验性 webstorage 全局:未开 --experimental-webstorage 时
// globalThis.localStorage 为 undefined,且 vitest jsdom 环境不会再用 jsdom
// 实现补位,导致测试里 localStorage.clear() 抛 TypeError。
// 测试只需行为等价的 Storage,这里补一个内存实现(sessionStorage 正常,不动)。
if (typeof localStorage === 'undefined') {
  class MemoryStorage implements Storage {
    private data = new Map<string, string>()
    get length(): number {
      return this.data.size
    }
    clear(): void {
      this.data.clear()
    }
    getItem(key: string): string | null {
      return this.data.has(key) ? (this.data.get(key) as string) : null
    }
    key(index: number): string | null {
      return [...this.data.keys()][index] ?? null
    }
    removeItem(key: string): void {
      this.data.delete(key)
    }
    setItem(key: string, value: string): void {
      this.data.set(key, String(value))
    }
  }
  Object.defineProperty(globalThis, 'localStorage', {
    value: new MemoryStorage(),
    configurable: true,
    writable: true,
  })
}

export { server }

let uninstallFetch: (() => void) | undefined

beforeAll(() => {
  uninstallFetch = installFetchStub()
})

afterEach(() => {
  server.resetHandlers()
  setScenario('healthy')
  try { sessionStorage.clear() } catch { /* jsdom 不支持 */ }
  try { localStorage.clear() } catch { /* jsdom 不支持 */ }
})

afterAll(() => {
  uninstallFetch?.()
  server.close()
})
