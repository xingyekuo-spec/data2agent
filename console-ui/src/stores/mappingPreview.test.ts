import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { ApiResult } from '@/api/services'
import type { components } from '@/types/api'
import { useMappingPreviewStore } from './mappingPreview'

vi.mock('@/api/services', () => ({
  postMappingPreview: vi.fn(),
}))

import { postMappingPreview } from '@/api/services'

type MappingPreviewResponse = components['schemas']['MappingPreviewResponse']

function ok(data: MappingPreviewResponse): ApiResult<MappingPreviewResponse> {
  return { ok: true, data, response: new Response() }
}

function sampleResponse(partial?: Partial<MappingPreviewResponse>): MappingPreviewResponse {
  return {
    object: 'Customer',
    source: 'digiwin_e10',
    mode: 'current',
    template_version: '0.1.0',
    current_binding_hash: 'sha256:aa',
    candidate_binding_hash: 'sha256:aa',
    sample: {
      anchor_table: 'CUSTOMER',
      offset: 0,
      limit: 50,
      requested_batch_id: null,
      sample_batch_ids: ['b1'],
      sampled_rows: 1,
      sample_fingerprint: 'fp1',
    },
    current: {
      summary: {
        total: 1,
        mapped: 1,
        quarantined: 0,
        quarantine_rate: 0,
        would_trip_breaker: false,
      },
      rows: [],
      enum_gaps: [],
      business_key_issues: { missing: 0, duplicate: 0, scope: 'sample' },
      derived_coverage: [],
    },
    candidate: {
      summary: {
        total: 1,
        mapped: 1,
        quarantined: 0,
        quarantine_rate: 0,
        would_trip_breaker: false,
      },
      rows: [],
      enum_gaps: [],
      business_key_issues: { missing: 0, duplicate: 0, scope: 'sample' },
      derived_coverage: [],
    },
    diff: {
      state: 'available',
      reason: null,
      summary: { rows_changed: 0, status_changed: 0, fields_changed: 0 },
      rows: [],
    },
    warnings: [],
    ...partial,
  }
}

describe('useMappingPreviewStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(postMappingPreview).mockReset()
  })

  it('loads current preview into success state', async () => {
    vi.mocked(postMappingPreview).mockResolvedValue(ok(sampleResponse()))
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    await store.submit()
    expect(store.preview.status).toBe('success')
    expect(store.isEmpty).toBe(false)
    expect(postMappingPreview).toHaveBeenCalledWith(
      'Customer',
      expect.objectContaining({ source: 'digiwin_e10' }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('treats sampled_rows=0 as empty success', async () => {
    vi.mocked(postMappingPreview).mockResolvedValue(ok(sampleResponse({
      sample: {
        anchor_table: 'CUSTOMER',
        offset: 0,
        limit: 50,
        requested_batch_id: null,
        sample_batch_ids: [],
        sampled_rows: 0,
        sample_fingerprint: 'fp-empty',
      },
      current: {
        summary: {
          total: 0,
          mapped: 0,
          quarantined: 0,
          quarantine_rate: 0,
          would_trip_breaker: false,
        },
        rows: [],
        enum_gaps: [],
        business_key_issues: { missing: 0, duplicate: 0, scope: 'sample' },
        derived_coverage: [],
      },
      candidate: {
        summary: {
          total: 0,
          mapped: 0,
          quarantined: 0,
          quarantine_rate: 0,
          would_trip_breaker: false,
        },
        rows: [],
        enum_gaps: [],
        business_key_issues: { missing: 0, duplicate: 0, scope: 'sample' },
        derived_coverage: [],
      },
    })))
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    await store.submit()
    expect(store.preview.status).toBe('success')
    expect(store.isEmpty).toBe(true)
  })

  it('keeps last success and sets refreshError on later failure', async () => {
    vi.mocked(postMappingPreview)
      .mockResolvedValueOnce(ok(sampleResponse()))
      .mockResolvedValueOnce({
        ok: false,
        error: {
          kind: 'http',
          status: 500,
          message: 'preview failed',
          retriable: true,
          reason_code: 'preview_failed',
          error_id: 'e1',
        } as import('@/api/services').MappingPreviewApiError,
      })
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    await store.submit()
    await store.submit()
    expect(store.preview.status).toBe('success')
    expect(store.refreshError?.message).toContain('preview failed')
  })

  it('ignores late responses when a newer request finishes first', async () => {
    let resolveSlow: ((v: unknown) => void) | undefined
    const slow = new Promise((resolve) => {
      resolveSlow = resolve
    })
    vi.mocked(postMappingPreview)
      .mockImplementationOnce(() => slow as ReturnType<typeof postMappingPreview>)
      .mockResolvedValueOnce(ok(sampleResponse({
        object: 'Material',
        sample: {
          anchor_table: 'MATERIAL_MASTER',
          offset: 0,
          limit: 50,
          requested_batch_id: null,
          sample_batch_ids: [],
          sampled_rows: 2,
          sample_fingerprint: 'fp-mat',
        },
      })))

    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    const p1 = store.submit()
    // 切换选择会 abort + 清空;再提交新对象
    store.setObject('Material')
    const p2 = store.submit()
    resolveSlow?.(ok(sampleResponse({ object: 'Customer' })))
    await Promise.all([p1, p2])
    expect(store.preview.status).toBe('success')
    if (store.preview.status === 'success') {
      expect(store.preview.data.object).toBe('Material')
      expect(store.preview.data.sample.sampled_rows).toBe(2)
    }
  })

  it('ignores duplicate submit while loading', async () => {
    let resolveFirst: ((v: unknown) => void) | undefined
    const first = new Promise((resolve) => {
      resolveFirst = resolve
    })
    vi.mocked(postMappingPreview).mockImplementationOnce(
      () => first as ReturnType<typeof postMappingPreview>,
    )
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    const p1 = store.submit()
    expect(store.isLoading).toBe(true)
    await store.submit()
    expect(postMappingPreview).toHaveBeenCalledTimes(1)
    resolveFirst?.(ok(sampleResponse()))
    await p1
    expect(store.preview.status).toBe('success')
  })

  it('clears prior result when object/source changes', async () => {
    vi.mocked(postMappingPreview).mockResolvedValue(ok(sampleResponse()))
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    await store.submit()
    expect(store.preview.status).toBe('success')
    store.setObject('Material')
    expect(store.preview.status).toBe('idle')
    expect(store.stale).toBe(false)
  })

  it('marks result stale when draft/sample changes', async () => {
    vi.mocked(postMappingPreview).mockResolvedValue(ok(sampleResponse()))
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    await store.submit()
    store.setSample({ limit: 10 })
    expect(store.preview.status).toBe('success')
    expect(store.stale).toBe(true)
    store.setUseDraft(true)
    store.setDraftText('{"tables":["CUSTOMER"]}')
    expect(store.stale).toBe(true)
  })

  it('surfaces unauthorized reason_code', async () => {
    vi.mocked(postMappingPreview).mockResolvedValue({
      ok: false,
      error: {
        kind: 'http',
        status: 401,
        message: 'unauthorized',
        retriable: false,
        reason_code: 'unauthorized',
        error_id: null,
      } as import('@/api/services').MappingPreviewApiError,
    })
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    await store.submit()
    expect(store.preview.status).toBe('error')
    expect(store.isUnauthorized).toBe(true)
  })

  it('rejects invalid draft JSON without calling API', async () => {
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    store.setUseDraft(true)
    store.setDraftText('{not-json')
    await store.submit()
    expect(postMappingPreview).not.toHaveBeenCalled()
    expect(store.preview.status).toBe('error')
    expect(store.draftParseError).toContain('JSON')
  })

  it('sends structured draft when useDraft is set', async () => {
    vi.mocked(postMappingPreview).mockResolvedValue(ok(sampleResponse({ mode: 'draft' })))
    const store = useMappingPreviewStore()
    store.setObject('Customer')
    store.setSource('digiwin_e10')
    store.setUseDraft(true)
    store.setDraft({
      tables: ['CUSTOMER'],
      key_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
      field_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
      derived: {},
      watermark: null,
      notes: '',
    })
    await store.submit()
    expect(postMappingPreview).toHaveBeenCalledWith(
      'Customer',
      expect.objectContaining({
        draft_binding: expect.objectContaining({ tables: ['CUSTOMER'] }),
      }),
      expect.any(Object),
    )
  })
})
