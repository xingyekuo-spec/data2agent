import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getRunDetail } from '@/api/services'
import type { ApiResult } from '@/api/services'
import type { components } from '@/types/api'
import { useRunsStore } from './runs'

vi.mock('@/api/services', () => ({
  getRuns: vi.fn(),
  getRunDetail: vi.fn(),
}))

type RunDetailResponse = components['schemas']['RunDetailResponse']

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function ok(data: RunDetailResponse): ApiResult<RunDetailResponse> {
  return { ok: true, data, response: new Response() }
}

function detail(id: number, marker: string): RunDetailResponse {
  return {
    id,
    source: 'digiwin_e10',
    type: 'sync',
    status: 'ok',
    started_at: '2026-07-18T09:00:00+08:00',
    finished_at: '2026-07-18T09:00:01+08:00',
    tables: 1,
    rows: 1,
    detail: marker,
    steps_state: 'available',
    steps: [],
  }
}

describe('runs store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('同一 run 详情后发请求先返回时,旧响应不得覆盖新响应', async () => {
    const first = deferred<ApiResult<RunDetailResponse>>()
    const second = deferred<ApiResult<RunDetailResponse>>()
    vi.mocked(getRunDetail)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)

    const store = useRunsStore()
    const firstLoad = store.openDetail(42)
    const secondLoad = store.openDetail(42)

    second.resolve(ok(detail(42, 'newer')))
    await secondLoad
    expect(store.detail?.status).toBe('success')
    if (store.detail?.status === 'success') {
      expect(store.detail.data.detail).toBe('newer')
    }

    first.resolve(ok(detail(42, 'older')))
    await firstLoad
    expect(store.detail?.status).toBe('success')
    if (store.detail?.status === 'success') {
      expect(store.detail.data.detail).toBe('newer')
    }
  })
})
