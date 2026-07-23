import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import DeadStockValidationView from './DeadStockValidationView.vue'

vi.mock('@/api/services', () => ({
  getOverview: vi.fn(),
  postApply: vi.fn(),
  postMcpCall: vi.fn(),
}))

import { getOverview, postApply, postMcpCall } from '@/api/services'
import { useDeadStockValidationStore } from '@/stores/deadStockValidation'

function ok<T>(data: T) {
  return { ok: true as const, data, response: new Response() }
}

function mcpResult(target: string, rows: Record<string, unknown>[]) {
  return {
    object: target,
    rows,
    meta: {
      query_id: `qry_${target}`,
      tool: 'query_objects',
      target,
      result_digest: 'sha256:' + 'ab'.repeat(32),
      warnings: ['draft'],
    },
  }
}

describe('DeadStockValidationView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(getOverview).mockReset()
    vi.mocked(postApply).mockReset()
    vi.mocked(postMcpCall).mockReset()
    vi.mocked(getOverview).mockResolvedValue(ok({
      versions: { app: 'dev', template: '0.5.0', dataset: 'ds-v1', object: 'ds-v1' },
      summary: {
        materialized_objects: 15,
        template_objects: 15,
        data_updated_at: '2026-07-23T10:00:00+08:00',
      },
      objects: [
        { object: 'DeadStockItem', rows: 2, status: 'published', mapped_at: '2026-07-23T10:00:00+08:00' },
        { object: 'DeadStockAttribution', rows: 6, status: 'published', mapped_at: '2026-07-23T10:00:00+08:00' },
        { object: 'MaterialSubstituteCandidate', rows: 1, status: 'published', mapped_at: '2026-07-23T10:00:00+08:00' },
      ],
      sources: [],
      alerts: [],
      recent_runs: [],
      sync_trend: [],
      needs_setup: false,
    } as any) as any)
    vi.mocked(postApply).mockResolvedValue(ok({
      source: 'digiwin_e10',
      dataset_version: 'ds-v2',
      published: true,
      status: 'published',
      results: [],
    } as any) as any)
    vi.mocked(postMcpCall).mockImplementation(async (tool, params) => {
      if (tool === 'query_metrics') {
        const metric = String(params.metric ?? '')
        return ok({
          metric,
          rows: metric === 'substitute_consumable_quantity'
            ? [{ group: 'P01', value: 200 }]
            : [{ group: 'R4', value: 1 }, { group: 'R6', value: 1 }],
          meta: {
            query_id: `qry_${metric}`,
            tool: 'query_metrics',
            target: metric,
            result_digest: 'sha256:' + 'cd'.repeat(32),
            warnings: ['draft'],
          },
        } as any) as any
      }
      const object = String(params.object ?? '')
      if (object === 'DeadStockItem') {
        return ok(mcpResult(object, [{
          item_code: 'RM-0005',
          plant_id: 'P01',
          warehouse_code: 'W01',
          inventory_qty: 300,
          dead_stock_amount: 1200,
          dead_stock_days: 420,
        }]) as any) as any
      }
      if (object === 'MaterialSubstituteCandidate') {
        return ok(mcpResult(object, [{
          item_code: 'RM-0005',
          candidate_parent_item_code: 'FR-0001',
          potential_consume_qty: 200,
        }]) as any) as any
      }
      if (object === 'DeadStockAttribution') {
        return ok(mcpResult(object, [{
          item_code: 'RM-0005',
          root_cause: 'R4',
          confidence_level: 'LOW',
          evidence_object: 'SpecialConditionEvidence',
        }]) as any) as any
      }
      return ok(mcpResult(object, []) as any) as any
    })
  })

  it('loads dead stock verification data and evidence chain', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(DeadStockValidationView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="dead-stock-status"]').text()).toContain('ds-v1')
    expect(wrapper.find('[data-testid="dead-stock-items"]').text()).toContain('RM-0005')
    expect(wrapper.find('[data-testid="dead-stock-evidence"]').text()).toContain('归因标签')
    expect(wrapper.text()).toContain('R4')
    expect(wrapper.text()).toContain('转用候选')
    expect(postMcpCall).toHaveBeenCalledWith('query_objects', expect.objectContaining({
      object: 'DeadStockAttribution',
      filters: expect.objectContaining({ item_code: 'RM-0005' }),
      limit: 20,
    }))
  })

  it('passes filters to the fixed MCP queries', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(DeadStockValidationView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await flushPromises()
    const store = useDeadStockValidationStore()
    store.filters.item_code = 'RM-0010'
    await wrapper.find('[data-testid="dead-stock-apply-filters"]').trigger('click')
    await flushPromises()

    expect(postMcpCall).toHaveBeenCalledWith('query_objects', expect.objectContaining({
      object: 'DeadStockItem',
      filters: expect.objectContaining({ item_code: 'RM-0010' }),
      order_by: 'dead_stock_amount',
    }))
  })

  it('shows a publish entry point when no published dataset exists', async () => {
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: false,
      error: {
        kind: 'http',
        status: 409,
        message: '当前来源没有可用的已发布数据集',
        retriable: false,
        reason_code: 'not_published',
      },
    } as any)
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(DeadStockValidationView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="dead-stock-not-published"]').text()).toContain('还没有已发布数据集')
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(false)
    await wrapper.find('[data-testid="dead-stock-build-publish"]').trigger('click')
    await flushPromises()
    expect(postApply).toHaveBeenCalledWith({ source: 'digiwin_e10', publish: true })
  })
})
