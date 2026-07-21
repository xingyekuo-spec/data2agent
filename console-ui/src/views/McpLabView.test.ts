import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import McpLabView from './McpLabView.vue'

vi.mock('@/api/services', () => ({
  postMcpCall: vi.fn(),
  postProposal: vi.fn(),
}))

import { postMcpCall } from '@/api/services'

describe('McpLabView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(postMcpCall).mockReset()
  })

  it('shows process scope banner and no execute/write controls', () => {
    const w = mount(McpLabView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    expect(w.find('[data-testid="mcp-scope-banner"]').text()).toContain('进程内有效')
    expect(w.find('[data-testid="no-execute-hint"]').text()).toContain('不提供执行建议')
    expect(w.findAll('button').some((b) => b.text() === '执行建议')).toBe(false)
    expect(w.find('[data-testid="feature-placeholder"]').exists()).toBe(false)
  })

  it('runs object query and shows masked fields + json toggle', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: {
        object: 'Customer',
        display_name: '客户',
        rows: [{ customer_code: 'C-001', contact: '***' }],
        meta: {
          query_id: 'q1',
          tool: 'query_objects',
          target: 'Customer',
          row_count: 1,
          duration_ms: 9,
          masked_fields: ['contact'],
          warnings: ['draft'],
          evidence_scope: 'process',
        },
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await w.find('[data-testid="object-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="object-result"]').exists()).toBe(true)
    expect(w.find('[data-testid="object-masked"]').text()).toContain('contact')
    await w.find('[data-testid="toggle-object-json"]').trigger('click')
    expect(w.find('[data-testid="object-raw-json"]').text()).toContain('q1')
    expect(w.find('[data-testid="object-raw-json"]').text()).toContain('***')
  })

  it('keeps meta and raw json when object query returns zero rows', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: true,
      data: {
        object: 'Customer',
        display_name: '客户',
        rows: [],
        meta: {
          query_id: 'q-empty',
          tool: 'query_objects',
          target: 'Customer',
          row_count: 0,
          duration_ms: 3,
          masked_fields: ['contact'],
          warnings: ['draft'],
          evidence_scope: 'process',
        },
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await w.find('[data-testid="object-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="object-result"]').exists()).toBe(true)
    expect(w.find('[data-testid="object-result-meta"]').text()).toContain('q-empty')
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
          query_id: 'qm1',
          tool: 'query_metrics',
          target: 'gross_margin_rate',
          row_count: 1,
          duration_ms: 4,
          masked_fields: [],
          warnings: ['caveat'],
          evidence_scope: 'process',
        },
      },
      response: new Response(),
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(McpLabView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    // Element Plus tabs keep panes mounted; click metrics run directly
    await w.find('[data-testid="metrics-run"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="metrics-result"]').exists()).toBe(true)
    await w.find('[data-testid="toggle-metrics-json"]').trigger('click')
    expect(w.find('[data-testid="metrics-raw-json"]').text()).toContain('qm1')
  })
})
