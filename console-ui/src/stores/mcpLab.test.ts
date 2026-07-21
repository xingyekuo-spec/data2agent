import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useMcpLabStore } from './mcpLab'

vi.mock('@/api/services', () => ({
  postMcpCall: vi.fn(),
  postProposal: vi.fn(),
}))

import { postMcpCall, postProposal } from '@/api/services'

const sampleResult = {
  object: 'Customer',
  display_name: '客户',
  rows: [{ customer_code: 'C-001', contact: '***' }],
  meta: {
    query_id: 'q1',
    tool: 'query_objects' as const,
    target: 'Customer',
    row_count: 1,
    duration_ms: 8,
    masked_fields: ['contact'],
    warnings: ['draft'],
    evidence_scope: 'process' as const,
  },
}

describe('useMcpLabStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(postMcpCall).mockReset()
    vi.mocked(postProposal).mockReset()
  })

  it('records citable history from successful object query', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: sampleResult,
      response: new Response(),
    })
    const store = useMcpLabStore()
    await store.runObjectQuery({ object: 'Customer', limit: 1 })
    expect(store.objectQuery.status).toBe('success')
    expect(store.citableHistory).toHaveLength(1)
    expect(store.citableHistory[0]?.query_id).toBe('q1')
  })

  it('keeps last success when a later query fails', async () => {
    vi.mocked(postMcpCall)
      .mockResolvedValueOnce({
        ok: true,
        data: sampleResult,
        response: new Response(),
      })
      .mockResolvedValueOnce({
        ok: false,
        error: {
          kind: 'http',
          status: 409,
          message: '尚未物化',
          retriable: false,
          reason_code: 'not_materialized',
        } as import('@/api/services').McpLabApiError,
      })
    const store = useMcpLabStore()
    await store.runObjectQuery({ object: 'Customer' })
    await store.runObjectQuery({ object: 'Customer' })
    expect(store.objectQuery.status).toBe('success')
    expect(store.objectRefreshError?.message).toContain('尚未物化')
  })

  it('ignores stale responses when a newer request finishes first', async () => {
    let resolveSlow: ((v: unknown) => void) | undefined
    const slow = new Promise((resolve) => {
      resolveSlow = resolve
    })
    vi.mocked(postMcpCall)
      .mockImplementationOnce(() => slow as ReturnType<typeof postMcpCall>)
      .mockResolvedValueOnce({
        ok: true,
        data: {
          ...sampleResult,
          meta: { ...sampleResult.meta, query_id: 'q2', target: 'Material' },
        },
        response: new Response(),
      })
    const store = useMcpLabStore()
    const p1 = store.runObjectQuery({ object: 'Customer' })
    const p2 = store.runObjectQuery({ object: 'Material' })
    resolveSlow?.({
      ok: true,
      data: sampleResult,
      response: new Response(),
    })
    await Promise.all([p1, p2])
    expect(store.objectQuery.status).toBe('success')
    if (store.objectQuery.status === 'success') {
      expect((store.objectQuery.data.meta as { query_id: string }).query_id).toBe('q2')
    }
  })

  it('creates proposal from selected evidence', async () => {
    vi.mocked(postProposal).mockResolvedValue({
      ok: true,
      data: {
        proposal_id: 'p1',
        at: '2026-07-21T00:00:00+00:00',
        object: 'Quotation',
        action: 'quote_review',
        action_desc: '评审',
        tier: '说',
        conclusion: '谨慎接',
        evidence: [{
          claim: '可见',
          query: {
            query_id: 'q1',
            tool: 'query_objects',
            target: 'Quotation',
            at: '2026-07-21T00:00:00+00:00',
          },
        }],
        caveats: [],
        governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
      },
      response: new Response(),
    })
    const store = useMcpLabStore()
    await store.runProposal({
      object: 'Quotation',
      action: 'quote_review',
      conclusion: '谨慎接',
      evidence: [{ claim: '可见', query_id: 'q1' }],
    })
    expect(store.proposal.status).toBe('success')
  })
})
