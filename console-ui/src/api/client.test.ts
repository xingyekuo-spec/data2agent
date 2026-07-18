import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  TOKEN_KEY,
  clearToken,
  client,
  getToken,
  setToken,
  setUnauthorizedHandler,
} from './client'
import { call } from './services'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const OVERVIEW_OK = {
  landing: '/tmp/landing.sqlite',
  readonly: true,
  actions_sync_reconcile: false,
  sources: [],
  objects: [],
  needs_setup: false,
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    return Promise.resolve(handler(url, init))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('api client', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    setUnauthorizedHandler(null)
    sessionStorage.clear()
    localStorage.clear()
  })

  it('同源 /api 单前缀,不产生 /api/api 双前缀', async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, OVERVIEW_OK))
    await client.GET('/api/overview')
    const url = new URL(String(fetchMock.mock.calls[0]?.[0] instanceof Request
      ? (fetchMock.mock.calls[0][0] as Request).url
      : fetchMock.mock.calls[0]?.[0]))
    expect(url.pathname).toBe('/api/overview')
    expect(url.pathname.startsWith('/api/api')).toBe(false)
  })

  it('有 Token 时加 Bearer;无 Token 不加', async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, OVERVIEW_OK))
    await client.GET('/api/overview')
    let req = fetchMock.mock.calls[0]?.[0] as Request
    expect(req.headers.get('Authorization')).toBeNull()

    setToken('tok-1')
    await client.GET('/api/overview')
    req = fetchMock.mock.calls[1]?.[0] as Request
    expect(req.headers.get('Authorization')).toBe('Bearer tok-1')
  })

  it('Token 只写 sessionStorage,不进 localStorage / URL', async () => {
    setToken('tok-secret')
    expect(getToken()).toBe('tok-secret')
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.length).toBe(0)

    const fetchMock = stubFetch(() => jsonResponse(200, OVERVIEW_OK))
    await client.GET('/api/overview')
    const req = fetchMock.mock.calls[0]?.[0] as Request
    expect(req.url).not.toContain('tok-secret')
    clearToken()
    expect(getToken()).toBeNull()
  })

  it('401 清除 Token 并回调认证提示', async () => {
    setToken('stale')
    const on401 = vi.fn()
    setUnauthorizedHandler(on401)
    stubFetch(() => jsonResponse(401, { detail: '需要有效的管理界面登录密码' }))
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.kind).toBe('http')
      expect(result.error.status).toBe(401)
    }
    expect(getToken()).toBeNull()
    expect(on401).toHaveBeenCalledTimes(1)
  })

  it('409/422/501 语义各自保留', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/pipeline')) {
        return jsonResponse(501, { detail: '契约桩:端点已声明' })
      }
      return jsonResponse(409, { detail: '只读模式' })
    })
    const stub = await call(client.GET('/api/pipeline'))
    expect(stub.ok).toBe(false)
    if (!stub.ok) {
      expect(stub.error.status).toBe(501)
      expect(stub.error.retriable).toBe(false)
      expect(stub.error.message).toContain('契约桩')
    }
    const conflict = await call(client.GET('/api/overview'))
    expect(conflict.ok).toBe(false)
    if (!conflict.ok) {
      expect(conflict.error.status).toBe(409)
    }
  })
})

describe('services.call', () => {
  beforeEach(() => sessionStorage.clear())
  afterEach(() => vi.unstubAllGlobals())

  it('成功响应返回数据', async () => {
    stubFetch(() => jsonResponse(200, OVERVIEW_OK))
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.readonly).toBe(true)
    }
  })

  it('网络失败进入 error 分支,绝不变成 success + 空数据', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('fetch failed'))),
    )
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.kind).toBe('network')
      expect(result.error.retriable).toBe(true)
    }
  })

  it('500 带 detail:保留状态码与摘要,可重试', async () => {
    stubFetch(() => jsonResponse(500, { detail: 'db locked' }))
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.status).toBe(500)
      expect(result.error.message).toBe('db locked')
      expect(result.error.retriable).toBe(true)
    }
  })
})
