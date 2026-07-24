/**
 * 全局 fetch stub:拦截 /api/* 并走场景 handlers。
 * 对外保留与旧 MSW server 相近的 use/reset API,降低测试迁移成本。
 */
import { buildHandlers, strictUnhandledRequest } from './api-handlers'
import { matchHandler, type StubHandler } from './http'

let baseHandlers: StubHandler[] = buildHandlers()
let overlay: StubHandler[] = []
let realFetch: typeof fetch = globalThis.fetch.bind(globalThis)

function activeHandlers(): StubHandler[] {
  return [...overlay, ...baseHandlers]
}

async function stubbedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const request = input instanceof Request ? input : new Request(input, init)
  const matched = matchHandler(activeHandlers(), request)
  if (!matched) {
    strictUnhandledRequest(request)
    return realFetch(request)
  }
  return matched.handler.resolver({
    request,
    params: matched.params,
  })
}

export const server = {
  listen(): void {
    // setup 中用 installFetchStub;此处保留 no-op 兼容
  },
  close(): void {
    overlay = []
    baseHandlers = buildHandlers()
  },
  resetHandlers(): void {
    overlay = []
    baseHandlers = buildHandlers()
  },
  use(...handlers: StubHandler[]): void {
    overlay = [...handlers, ...overlay]
  },
}

export function installFetchStub(): () => void {
  realFetch = globalThis.fetch.bind(globalThis)
  globalThis.fetch = stubbedFetch as typeof fetch
  return () => {
    globalThis.fetch = realFetch
  }
}
