import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useMcpLabStore } from './mcpLab'

vi.mock('@/api/services', () => ({
  postMcpCall: vi.fn(),
  postProposal: vi.fn(),
  getQueryEvidenceDetail: vi.fn(),
  getProposalDetail: vi.fn(),
}))

import { getProposalDetail, getQueryEvidenceDetail, postMcpCall, postProposal } from '@/api/services'

const sampleResult = {
  object: 'Customer',
  display_name: '客户',
  rows: [{ customer_code: 'C-001', contact: '***' }],
  meta: {
    query_id: 'qry_111111111111111111111111',
    tool: 'query_objects' as const,
    target: 'Customer',
    row_count: 1,
    duration_ms: 8,
    masked_fields: ['contact'],
    warnings: ['draft'],
    evidence_scope: 'principal_session' as const,
    session_id: 'd2a_session_test_0123456789',
    result_digest: 'sha256:' + '11'.repeat(32),
    result_summary: { kind: 'query_objects', returned_row_count: 1, rows_preview: [] },
    created_at: '2026-07-22T10:00:00+08:00',
    expires_at: '2026-07-23T10:00:00+08:00',
    dataset_version: 'ds_20260722',
    template_version: '0.1.0',
    binding_hashes: { Customer: 'sha256:' + 'aa'.repeat(32) },
  },
}

describe('useMcpLabStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(postMcpCall).mockReset()
    vi.mocked(postProposal).mockReset()
    vi.mocked(getQueryEvidenceDetail).mockReset()
    vi.mocked(getProposalDetail).mockReset()
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
    expect(store.citableHistory[0]?.query_id).toBe('qry_111111111111111111111111')
    expect(store.citableHistory[0]?.result_digest).toContain('sha256:')
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
          meta: {
            ...sampleResult.meta,
            query_id: 'qry_222222222222222222222222',
            result_digest: 'sha256:' + '22'.repeat(32),
            target: 'Material',
          },
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
      expect((store.objectQuery.data.meta as { query_id: string }).query_id).toBe('qry_222222222222222222222222')
    }
  })

  it('creates proposal from selected evidence', async () => {
    vi.mocked(postProposal).mockResolvedValue({
      ok: true,
      data: {
        proposal_id: 'prp_333333333333333333333333',
        at: '2026-07-21T00:00:00+00:00',
        session_id: 'd2a_session_test_0123456789',
        source: 'digiwin_e10',
        dataset_version: 'ds_20260722',
        object: 'Quotation',
        action: 'quote_review',
        action_desc: '评审',
        tier: '说',
        conclusion: '谨慎接',
        evidence: [{
          claim: '可见',
          query: {
            query_id: 'qry_111111111111111111111111',
            source: 'digiwin_e10',
            tool: 'query_objects',
            target: 'Customer',
            normalized_query: {},
            dataset_version: 'ds_20260722',
            template_version: '0.1.0',
            binding_hashes: { Customer: 'sha256:' + 'aa'.repeat(32) },
            result_digest: 'sha256:' + '11'.repeat(32),
            result_summary: { kind: 'query_objects', returned_row_count: 1, rows_preview: [] },
            warnings: [],
            created_at: '2026-07-21T00:00:00+00:00',
            expires_at: null,
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
      evidence: [{ claim: '可见', query_id: 'qry_111111111111111111111111', result_digest: 'sha256:' + '11'.repeat(32) }],
    })
    expect(store.proposal.status).toBe('success')
  })

  it('clears history when proposal fails with query_expired', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: sampleResult,
      response: new Response(),
    })
    vi.mocked(postProposal).mockResolvedValue({
      ok: false,
      error: {
        kind: 'http',
        status: 409,
        message: 'query ID 已失效',
        retriable: false,
        reason_code: 'query_expired',
      } as import('@/api/services').McpLabApiError,
    })
    const store = useMcpLabStore()
    await store.runObjectQuery({ object: 'Customer' })
    expect(store.citableHistory).toHaveLength(1)
    await store.runProposal({
      object: 'Quotation',
      action: 'quote_review',
      conclusion: '旧证据',
      evidence: [{ claim: 'x', query_id: 'qry_111111111111111111111111', result_digest: 'sha256:' + '11'.repeat(32) }],
    })
    expect(store.citableHistory).toHaveLength(0)
    expect(store.historyClearedHint).toContain('重新查询')
  })

  it('resets all evidence state at a session boundary', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: sampleResult,
      response: new Response(),
    })
    const store = useMcpLabStore()
    await store.runObjectQuery({ object: 'Customer' })
    store.proposal = { status: 'loading' }
    store.queryDetail = { status: 'loading' }
    store.proposalDetail = { status: 'loading' }
    store.objectRefreshError = {
      kind: 'parse', message: '旧会话错误', retriable: false,
    }

    store.resetForSessionBoundary()

    expect(store.objectQuery).toEqual({ status: 'idle' })
    expect(store.metricsQuery).toEqual({ status: 'idle' })
    expect(store.proposal).toEqual({ status: 'idle' })
    expect(store.queryDetail).toEqual({ status: 'idle' })
    expect(store.proposalDetail).toEqual({ status: 'idle' })
    expect(store.history).toEqual([])
    expect(store.historyClearedHint).toBeNull()
    expect(store.objectRefreshError).toBeNull()
  })

  it('loads query/proposal detail states', async () => {
    vi.mocked(getQueryEvidenceDetail).mockResolvedValue({
      ok: true,
      data: {
        query_id: 'qry_111111111111111111111111',
        source: 'digiwin_e10',
        tool: 'query_objects',
        target: 'Customer',
        session_id: 'd2a_session_test_0123456789',
        evidence_scope: 'principal_session',
        normalized_query: {},
        dataset_version: 'ds_20260722',
        template_version: '0.1.0',
        binding_hashes: {},
        result_digest: 'sha256:' + '11'.repeat(32),
        result_summary: { kind: 'query_objects', returned_row_count: 1, rows_preview: [] },
        warnings: [],
        row_count: 1,
        created_at: '2026-07-22T10:00:00+08:00',
        expires_at: '2026-07-23T10:00:00+08:00',
      },
      response: new Response(),
    })
    vi.mocked(getProposalDetail).mockResolvedValue({
      ok: true,
      data: {
        proposal_id: 'prp_333333333333333333333333',
        at: '2026-07-22T10:00:00+08:00',
        session_id: 'd2a_session_test_0123456789',
        source: 'digiwin_e10',
        dataset_version: 'ds_20260722',
        object: 'Quotation',
        action: 'quote_review',
        action_desc: '评审',
        tier: '说',
        conclusion: 'ok',
        evidence: [],
        caveats: [],
        governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
      },
      response: new Response(),
    })
    const store = useMcpLabStore()
    await store.loadQueryDetail('qry_111111111111111111111111')
    await store.loadProposalDetail('prp_333333333333333333333333')
    expect(store.queryDetail.status).toBe('success')
    expect(store.proposalDetail.status).toBe('success')
  })
})
