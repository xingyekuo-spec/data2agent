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

import {
  getOverview,
  postApply,
  postMcpCall,
  type McpLabApiError,
  type McpToolResult,
} from '@/api/services'
import { useDeadStockValidationStore } from '@/stores/deadStockValidation'
import type { components, JsonValueOutput } from '@/types/api'

type OverviewResponse = components['schemas']['OverviewResponse']
type ApplyActionResult = components['schemas']['ApplyActionResult']

function ok<T>(data: T) {
  return { ok: true as const, data, response: new Response() }
}

function mcpResult(
  target: string,
  rows: Record<string, JsonValueOutput>[],
): McpToolResult {
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
    const overview: OverviewResponse = {
      versions: { app: 'dev', template: '0.5.0', dataset: 'ds-v1', object: 'ds-v1' },
      summary: {
        materialized_objects: 15,
        template_objects: 15,
        data_updated_at: '2026-07-23T10:00:00+08:00',
        last_run_at: '2026-07-23T10:00:00+08:00',
        object_rows: 9,
        quarantine_pending: 0,
        raw_rows: 9,
      },
      objects: [
        {
          object: 'DeadStockItem',
          display_name: '呆滞库存',
          rows: 2,
          quarantined: 0,
          mapped_at: '2026-07-23T10:00:00+08:00',
        },
        {
          object: 'DeadStockAttribution',
          display_name: '呆滞归因',
          rows: 6,
          quarantined: 0,
          mapped_at: '2026-07-23T10:00:00+08:00',
        },
        {
          object: 'MaterialSubstituteCandidate',
          display_name: '替代料候选',
          rows: 1,
          quarantined: 0,
          mapped_at: '2026-07-23T10:00:00+08:00',
        },
      ],
      sources: [],
      alerts: [],
      recent_runs: [],
      sync_trend: [],
      needs_setup: false,
      binding_summary: { verified: 15, draft: 0, disabled: 0 },
      count_notes: [],
      generated_at: '2026-07-23T10:00:00+08:00',
      landing: 'landing/factory.sqlite',
      readonly: false,
    }
    vi.mocked(getOverview).mockResolvedValue(ok(overview))

    const applyResult: ApplyActionResult = {
      executed: true,
      aborted: [],
      dataset_version: 'ds-v2',
      published: true,
      results: [],
    }
    vi.mocked(postApply).mockResolvedValue(ok(applyResult))
    vi.mocked(postMcpCall).mockImplementation(async (tool, params) => {
      if (tool === 'query_metrics') {
        const metric = String(params.metric ?? '')
        return ok<McpToolResult>({
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
        })
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
        }]))
      }
      if (object === 'MaterialSubstituteCandidate') {
        return ok(mcpResult(object, [{
          item_code: 'RM-0005',
          candidate_parent_item_code: 'FR-0001',
          potential_consume_qty: 200,
        }]))
      }
      if (object === 'DeadStockAttribution') {
        return ok(mcpResult(object, [{
          item_code: 'RM-0005',
          root_cause: 'R4',
          confidence_level: 'LOW',
          evidence_object: 'SpecialConditionEvidence',
        }]))
      }
      return ok(mcpResult(object, []))
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
    const notPublishedError: McpLabApiError = {
      kind: 'http',
      status: 409,
      message: '当前来源没有可用的已发布数据集',
      retriable: false,
      reason_code: 'not_published',
    }
    vi.mocked(postMcpCall).mockResolvedValue({
      ok: false,
      error: notPublishedError,
    })
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
