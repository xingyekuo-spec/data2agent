import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import McpLabView from './McpLabView.vue'

vi.mock('@/api/services', () => ({
  postMcpCall: vi.fn(),
  postProposal: vi.fn(),
  getQueryEvidenceDetail: vi.fn(),
  getProposalDetail: vi.fn(),
}))

import { getProposalDetail, getQueryEvidenceDetail, postMcpCall, postProposal } from '@/api/services'

async function openDetail(wrapper: ReturnType<typeof mount>, tool: string): Promise<void> {
  await flushPromises()
  await wrapper.find(`[data-testid="open-interface-${tool}"]`).trigger('click')
  await flushPromises()
}

describe('McpLabView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(postMcpCall).mockReset()
    vi.mocked(postProposal).mockReset()
    vi.mocked(getQueryEvidenceDetail).mockReset()
    vi.mocked(getProposalDetail).mockReset()
  })

  it('shows process scope banner and no execute/write controls', async () => {
    const w = mount(McpLabView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    expect(w.find('[data-testid="mcp-scope-banner"]').text()).toContain('标签页 session')
    expect(w.find('[data-testid="mcp-interface-count"]').text()).toContain('共 3 个')
    expect(w.find('[data-testid="mcp-interface-table"]').text()).toContain('query_objects')
    expect(w.find('[data-testid="mcp-interface-table"]').text()).toContain('query_metrics')
    expect(w.find('[data-testid="mcp-interface-table"]').text()).toContain('propose_action')
    expect(w.find('[data-testid="object-interface-panel"]').exists()).toBe(false)
    expect(w.find('[data-testid="metrics-interface-panel"]').exists()).toBe(false)
    expect(w.find('[data-testid="proposal-interface-panel"]').exists()).toBe(false)
    expect(w.find('[data-testid="evidence-detail-panel"]').exists()).toBe(false)
    expect(w.findAll('button').some((b) => b.text() === '执行建议')).toBe(false)
    expect(w.find('[data-testid="feature-placeholder"]').exists()).toBe(false)
    expect(w.find('[data-testid="evidence-session-id"]').text()).toContain('d2a_session_')
  })

  it('runs object query and shows masked fields + version meta + json toggle', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: {
        object: 'Customer',
        display_name: '客户',
        rows: [{ customer_code: 'C-001', contact: '***' }],
        meta: {
          query_id: 'qry_111111111111111111111111',
          tool: 'query_objects',
          target: 'Customer',
          row_count: 1,
          duration_ms: 9,
          masked_fields: ['contact'],
          warnings: ['draft'],
          evidence_scope: 'principal_session',
          session_id: 'd2a_session_test_0123456789',
          result_digest: 'sha256:' + '11'.repeat(32),
          result_summary: { kind: 'query_objects', returned_row_count: 1, rows_preview: [] },
          created_at: '2026-07-22T10:00:00+08:00',
          expires_at: '2026-07-23T10:00:00+08:00',
          dataset_version: 'ds-v1',
          template_version: '0.1.0',
          binding_hashes: { Customer: 'sha256:aa' },
        },
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await openDetail(w, 'query_objects')
    await w.find('[data-testid="object-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="object-result"]').exists()).toBe(true)
    expect(w.find('[data-testid="object-masked"]').text()).toContain('contact')
    expect(w.find('[data-testid="object-dataset-version"]').text()).toContain('ds-v1')
    expect(w.find('[data-testid="object-template-version"]').text()).toContain('0.1.0')
    expect(w.find('[data-testid="object-result-meta"]').text()).toContain('principal_session')
    expect(w.find('[data-testid="object-result-meta"]').text()).toContain('digest=')
    await w.find('[data-testid="toggle-object-json"]').trigger('click')
    expect(w.find('[data-testid="object-raw-json"]').text()).toContain('qry_111111111111111111111111')
    expect(w.find('[data-testid="object-raw-json"]').text()).toContain('***')
  })

  it('maps not_published reason for empty install queries', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: false,
      error: {
        kind: 'http',
        status: 409,
        message: '当前来源没有可用的已发布数据集',
        retriable: false,
        reason_code: 'not_published',
      } as import('@/api/services').McpLabApiError,
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await openDetail(w, 'query_objects')
    await w.find('[data-testid="object-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="object-result"]').exists()).toBe(false)
    expect(w.find('[data-testid="object-error-reason"]').text()).toBe('未发布')
  })

  it('keeps meta and raw json when object query returns zero rows', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: {
        object: 'Customer',
        display_name: '客户',
        rows: [],
        meta: {
          query_id: 'qry_eeeeeeeeeeeeeeeeeeeeeeee',
          tool: 'query_objects',
          target: 'Customer',
          row_count: 0,
          duration_ms: 3,
          masked_fields: ['contact'],
          warnings: ['draft'],
          evidence_scope: 'principal_session',
          session_id: 'd2a_session_test_0123456789',
          result_digest: 'sha256:' + 'ee'.repeat(32),
          result_summary: { kind: 'query_objects', returned_row_count: 0, rows_preview: [] },
          created_at: '2026-07-22T10:00:00+08:00',
          expires_at: '2026-07-23T10:00:00+08:00',
        },
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await openDetail(w, 'query_objects')
    await w.find('[data-testid="object-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="object-result"]').exists()).toBe(true)
    expect(w.find('[data-testid="object-result-meta"]').text()).toContain('qry_eeeeeeeeeeeeeeeeeeeeeeee')
    expect(w.find('[data-testid="object-result-meta"]').text()).toContain('行数 0')
    await w.find('[data-testid="toggle-object-json"]').trigger('click')
    expect(w.find('[data-testid="object-raw-json"]').text()).toContain('"row_count": 0')
  })

  it('shows metrics raw json toggle', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: {
        metric: 'gross_margin_rate',
        rows: [{ value: 0.3 }],
        meta: {
          query_id: 'qry_m11111111111111111111111',
          tool: 'query_metrics',
          target: 'gross_margin_rate',
          row_count: 1,
          duration_ms: 4,
          masked_fields: [],
          warnings: ['caveat'],
          evidence_scope: 'principal_session',
          session_id: 'd2a_session_test_0123456789',
          result_digest: 'sha256:' + '33'.repeat(32),
          result_summary: { kind: 'query_metrics', returned_row_count: 1, rows_preview: [{ value: 0.3 }] },
          created_at: '2026-07-22T10:00:00+08:00',
          expires_at: '2026-07-23T10:00:00+08:00',
        },
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await openDetail(w, 'query_metrics')
    await w.find('[data-testid="metrics-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="metrics-result"]').exists()).toBe(true)
    await w.find('[data-testid="toggle-metrics-json"]').trigger('click')
    expect(w.find('[data-testid="metrics-raw-json"]').text()).toContain('qry_m11111111111111111111111')
  })

  it('renders proposal snapshot and loads detail API', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: {
        object: 'Customer',
        display_name: '客户',
        rows: [{ customer_code: 'C-001' }],
        meta: {
          query_id: 'qry_111111111111111111111111',
          tool: 'query_objects',
          target: 'Customer',
          row_count: 1,
          duration_ms: 9,
          masked_fields: [],
          warnings: [],
          evidence_scope: 'principal_session',
          session_id: 'd2a_session_test_0123456789',
          result_digest: 'sha256:' + '11'.repeat(32),
          result_summary: { kind: 'query_objects', returned_row_count: 1, rows_preview: [] },
          created_at: '2026-07-22T10:00:00+08:00',
          expires_at: '2026-07-23T10:00:00+08:00',
          dataset_version: 'ds-v1',
          template_version: '0.1.0',
          binding_hashes: {},
        },
      },
      response: new Response(),
    })
    vi.mocked(postProposal).mockResolvedValue({
      ok: true,
      data: {
        proposal_id: 'prp_222222222222222222222222',
        at: '2026-07-22T10:05:00+08:00',
        session_id: 'd2a_session_test_0123456789',
        source: 'digiwin_e10',
        dataset_version: 'ds-v1',
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
            dataset_version: 'ds-v1',
            template_version: '0.1.0',
            binding_hashes: {},
            result_digest: 'sha256:' + '11'.repeat(32),
            result_summary: { kind: 'query_objects', returned_row_count: 1, rows_preview: [] },
            warnings: [],
            created_at: '2026-07-22T10:00:00+08:00',
            expires_at: null,
          },
        }],
        caveats: [],
        governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
      },
      response: new Response(),
    })
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
        dataset_version: 'ds-v1',
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
        proposal_id: 'prp_222222222222222222222222',
        at: '2026-07-22T10:05:00+08:00',
        session_id: 'd2a_session_test_0123456789',
        source: 'digiwin_e10',
        dataset_version: 'ds-v1',
        object: 'Quotation',
        action: 'quote_review',
        action_desc: '评审',
        tier: '说',
        conclusion: '谨慎接',
        evidence: [],
        caveats: [],
        governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await openDetail(w, 'query_objects')
    await w.find('[data-testid="object-run"]').trigger('click')
    await flushPromises()
    await w.find('[data-testid="back-to-interface-list"]').trigger('click')
    await flushPromises()
    await openDetail(w, 'propose_action')
    await w.find('[data-testid="evidence-claim-0"]').setValue('可见')
    await w.find('[data-testid="evidence-query-0"]').setValue('qry_111111111111111111111111')
    await w.find('[data-testid="proposal-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="proposal-result"]').text()).toContain('prp_222222222222222222222222')
    await w.find('.evidence-item button').trigger('click')
    expect(w.find('[data-testid="evidence-expand"]').text()).toContain('result_digest')
    await w.find('[data-testid="detail-query-id"]').setValue('qry_111111111111111111111111')
    await w.find('[data-testid="detail-query-load"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="detail-query-result"]').text()).toContain('principal_session')
  })
})
