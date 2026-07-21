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
})
