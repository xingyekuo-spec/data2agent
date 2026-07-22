/**
 * M4-T08: lineage store 单元测试。
 * 覆盖:加载成功/错误、迟到响应丢弃、property 过滤、重置。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useLineageStore } from './lineage'
import * as services from '@/api/services'

vi.mock('@/api/services', () => ({
  getObjectLineage: vi.fn(),
}))

const mockGet = vi.mocked(services.getObjectLineage)

const MOCK_RESPONSE = {
  state: 'available' as const,
  reason_code: null,
  source: 'digiwin_e10',
  object: 'SalesOrderLine',
  display_name: '销售订单明细',
  object_key: [['order_no', 'SO-001'], ['line_no', 10]],
  key_token: 'ab'.repeat(32),
  dataset_version: 'ds-001',
  object_version: 'ov-001',
  template_version: '0.1.0',
  binding_hash: 'sha256:ab',
  binding_status: 'verified' as const,
  map_batch_id: 'mb-001',
  fields: [],
  warnings: [],
  generated_at: '2026-07-22T10:00:00+08:00',
}

describe('lineage store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads lineage successfully', async () => {
    mockGet.mockResolvedValue({
      ok: true,
      data: MOCK_RESPONSE,
      response: new Response(),
    })

    const store = useLineageStore()
    store.setTarget('SalesOrderLine', 'ab'.repeat(32))
    await store.load()

    expect(store.lineage.status).toBe('success')
    expect(store.isAvailable).toBe(true)
    if (store.lineage.status === 'success') {
      expect(store.lineage.data.object).toBe('SalesOrderLine')
    }
  })

  it('handles error response', async () => {
    mockGet.mockResolvedValue({
      ok: false,
      error: {
        kind: 'http' as const,
        status: 404,
        message: '记录不存在',
        retriable: false,
      },
    })

    const store = useLineageStore()
    store.setTarget('SalesOrderLine', 'ff'.repeat(32))
    await store.load()

    expect(store.lineage.status).toBe('error')
    expect(store.isAvailable).toBe(false)
  })

  it('discards stale response when target changes', async () => {
    let resolveFirst!: (v: unknown) => void
    const firstPromise = new Promise((r) => { resolveFirst = r })

    mockGet
      .mockReturnValueOnce(firstPromise as never)
      .mockResolvedValueOnce({
        ok: true,
        data: { ...MOCK_RESPONSE, object: 'Customer' },
        response: new Response(),
      })

    const store = useLineageStore()
    store.setTarget('SalesOrderLine', 'ab'.repeat(32))
    const p1 = store.load()

    // 切换到新目标
    store.setTarget('Customer', 'cd'.repeat(32))
    const p2 = store.load()

    // 第一个请求迟到返回
    resolveFirst({
      ok: true,
      data: MOCK_RESPONSE,
      response: new Response(),
    })

    await Promise.all([p1, p2])

    // 应显示第二个请求的结果
    if (store.lineage.status === 'success') {
      expect(store.lineage.data.object).toBe('Customer')
    }
  })

  it('passes property filter', async () => {
    mockGet.mockResolvedValue({
      ok: true,
      data: MOCK_RESPONSE,
      response: new Response(),
    })

    const store = useLineageStore()
    store.setTarget('SalesOrderLine', 'ab'.repeat(32), 'status')
    await store.load()

    expect(mockGet).toHaveBeenCalledWith(
      'SalesOrderLine',
      'ab'.repeat(32),
      expect.objectContaining({ property: 'status' }),
    )
  })

  it('reset clears state', async () => {
    mockGet.mockResolvedValue({
      ok: true,
      data: MOCK_RESPONSE,
      response: new Response(),
    })

    const store = useLineageStore()
    store.setTarget('SalesOrderLine', 'ab'.repeat(32))
    await store.load()
    expect(store.lineage.status).toBe('success')

    store.reset()
    expect(store.lineage.status).toBe('idle')
    expect(store.object).toBe('')
    expect(store.keyToken).toBe('')
  })

  it('detects unavailable old dataset', async () => {
    mockGet.mockResolvedValue({
      ok: true,
      data: {
        ...MOCK_RESPONSE,
        state: 'unavailable' as const,
        reason_code: 'lineage_not_recorded' as const,
        fields: [],
      },
      response: new Response(),
    })

    const store = useLineageStore()
    store.setTarget('SalesOrderLine', 'ab'.repeat(32))
    await store.load()

    expect(store.isUnavailable).toBe(true)
    expect(store.isAvailable).toBe(false)
  })
})
