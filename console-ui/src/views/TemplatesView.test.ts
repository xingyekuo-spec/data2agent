import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from 'msw'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/mocks/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/setup'
import TemplatesView from './TemplatesView.vue'

async function mountView(): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push('/templates')
  await router.isReady()
  const wrapper = mount(TemplatesView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return wrapper
}

/**
 * Helper: 有 el-tag--<type> 类的元素数量。用于验证 Element Plus tag 颜色。
 * 不用 wrapper.classes():它返回空数组(Element Plus 组件 renders below root)。
 */
function countTagsByType(wrapper: ReturnType<typeof mount>, type: string): number {
  return wrapper.findAll(`.el-tag--${type}`).length
}

/**
 * Helper: 切换到指定名称的 tab。
 */
async function switchTab(
  wrapper: ReturnType<typeof mount>,
  tabName: string,
): Promise<void> {
  const tabs = wrapper.findAll('.el-tabs__item')
  const tab = tabs.find((t) => t.text().includes(tabName))
  if (tab) {
    await tab.trigger('click')
    await flushPromises()
  }
}

describe('TemplatesView(M5)', () => {
  beforeEach(() => setScenario('healthy'))

  it('renders object list with all objects', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('客户')
    expect(wrapper.text()).toContain('销售订单')
    expect(wrapper.text()).toContain('报价回复')
    expect(wrapper.text()).toContain('物料(品号)')
    // Check binding counts are shown
    expect(wrapper.text()).toContain('1 个绑定')
  })

  it('shows materialization state tags in object list', async () => {
    const wrapper = await mountView()
    const matTags = wrapper.findAll('[data-testid="mat-tag"]')
    expect(matTags.length).toBeGreaterThanOrEqual(4)
    const matTexts = matTags.map((t) => t.text())
    expect(matTexts).toContain('已物化')
    expect(matTexts).toContain('未物化')
  })

  it('shows quarantine tag for objects with pending items', async () => {
    const wrapper = await mountView()
    // QuoteResponse has quarantine_pending: 3
    expect(wrapper.text()).toContain('隔离3')
  })

  it('clicking object shows detail tabs', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    const tabs = wrapper.find('[data-testid="tpl-detail-tabs"]')
    expect(tabs.exists()).toBe(true)
    expect(tabs.text()).toContain('概览')
    expect(tabs.text()).toContain('属性')
    expect(tabs.text()).toContain('绑定')
    expect(tabs.text()).toContain('指标')
  })

  it('overview tab shows description, domain, keys, and materialization', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    // The display_name "报价回复" is in the card title above the overview grid
    expect(wrapper.text()).toContain('报价回复')
    expect(wrapper.text()).toContain('QuoteResponse')
    const overview = wrapper.find('[data-testid="tpl-overview"]')
    expect(overview.exists()).toBe(true)
    expect(overview.text()).toContain('销售')
    expect(overview.text()).toContain('quote_id')
    expect(overview.text()).toContain('digiwin_e10')
    // knowledge_refs
    expect(overview.text()).toContain('报价流程SOP')
  })

  it('overview shows materialization info correctly', async () => {
    const wrapper = await mountView()
    // Customer is materialized
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    const mat = wrapper.find('[data-testid="tpl-materialization"]')
    expect(mat.text()).toContain('已物化')
    expect(mat.text()).toContain('36')
    expect(mat.text()).toContain('digiwin_e10')

    // QuoteResponse is not_materialized
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    const mat2 = wrapper.find('[data-testid="tpl-materialization"]')
    expect(mat2.text()).toContain('未物化')
    expect(mat2.text()).toContain('—')
  })

  it('overview shows warnings when present', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    const warn = wrapper.find('[data-testid="tpl-warnings"]')
    expect(warn.exists()).toBe(true)
    expect(warn.text()).toContain('3 条报价记录暂存隔离区')
  })

  it('properties tab shows sensitive tags as danger (red)', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '属性')

    const propsTable = wrapper.find('[data-testid="tpl-properties"]')
    expect(propsTable.exists()).toBe(true)
    // Customer has contact as sensitive → el-tag--danger
    expect(propsTable.text()).toContain('敏感')
    const dangerTagsInTable = propsTable.findAll('.el-tag--danger')
    expect(dangerTagsInTable.length).toBeGreaterThanOrEqual(1)
    expect(dangerTagsInTable[0].text()).toBe('敏感')
  })

  it('properties tab shows enum_values', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '属性')

    const propsTable = wrapper.find('[data-testid="tpl-properties"]')
    expect(propsTable.text()).toContain('CNY')
    expect(propsTable.text()).toContain('USD')
    expect(propsTable.text()).toContain('EUR')
    expect(propsTable.text()).toContain('pending')
    expect(propsTable.text()).toContain('accepted')
    expect(propsTable.text()).toContain('rejected')
    expect(propsTable.text()).toContain('expired')
  })

  it('bindings tab shows draft status as warning (orange, not green)', async () => {
    // Use draft-governance scenario where ALL bindings are draft
    setScenario('draft-governance')
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    const statusTags = wrapper.findAll('[data-testid="binding-status-tag"]')
    expect(statusTags.length).toBeGreaterThanOrEqual(1)
    for (const tag of statusTags) {
      expect(tag.text()).toBe('未校准')
    }
    // draft → warning (orange), NOT success (green)
    const warningCount = countTagsByType(wrapper, 'warning')
    expect(warningCount).toBeGreaterThanOrEqual(1)
    // No success tag on binding status tags themselves (verified → '已验证')
    // (the materialized tag may show '已物化' with success in the overview tab or list)
    for (const tag of statusTags) {
      expect(tag.text()).not.toBe('已验证')
    }
  })

  it('bindings tab shows verified status as success (green)', async () => {
    // Use healthy scenario (default) - Customer has verified binding
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    const statusTags = wrapper.findAll('[data-testid="binding-status-tag"]')
    expect(statusTags.length).toBeGreaterThanOrEqual(1)
    expect(statusTags[0].text()).toBe('已验证')
    // verified → success (green)
    const successCount = countTagsByType(wrapper, 'success')
    expect(successCount).toBeGreaterThanOrEqual(1)
  })

  it('bindings tab shows disabled status as info (gray, not success)', async () => {
    const wrapper = await mountView()
    // QuoteResponse has both draft and disabled bindings
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    const statusTags = wrapper.findAll('[data-testid="binding-status-tag"]')
    expect(statusTags.length).toBe(2)
    // First binding (digiwin_e10) is draft
    expect(statusTags[0].text()).toBe('未校准')
    // Second binding (crm_export) is disabled
    expect(statusTags[1].text()).toBe('已禁用')
    // disabled → info (gray), NOT success
    const infoCount = countTagsByType(wrapper, 'info')
    expect(infoCount).toBeGreaterThanOrEqual(1)
    // Binding tags should not show '已验证' (no success tag for disabled binding)
    for (const tag of statusTags) {
      expect(tag.text()).not.toBe('已验证')
    }
  })

  it('bindings tab shows enum_map structured display (source value → target value)', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    const enumTable = wrapper.find('[data-testid="enum-map-table"]')
    expect(enumTable.exists()).toBe(true)
    // Check source → target mapping
    expect(enumTable.text()).toContain('CNY')
    expect(enumTable.text()).toContain('人民币')
    expect(enumTable.text()).toContain('USD')
    expect(enumTable.text()).toContain('美元')
    // Should show arrow
    expect(enumTable.text()).toContain('→')
    // Should NOT show raw expression like { CNY: '人民币' }
  })

  it('bindings tab shows derived rules for QuoteResponse', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-QuoteResponse"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    // Should have derived rules section
    const derivedTable = wrapper.find('[data-testid="derived-rules-table"]')
    expect(derivedTable.exists()).toBe(true)
    expect(derivedTable.text()).toContain('QUOTE_RESPONSE.EXPIRY_DATE')
    expect(derivedTable.text()).toContain('QUOTE_RESPONSE.STATUS')
    expect(derivedTable.text()).toContain("'expired'")
    expect(derivedTable.text()).toContain("'pending'")
  })

  it('bindings tab shows key_map and field_map tables', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    // Key map
    expect(wrapper.text()).toContain('键映射')
    expect(wrapper.text()).toContain('customer_code')
    expect(wrapper.text()).toContain('CUSTOMER.CUSTOMER_CODE')

    // Field map
    expect(wrapper.text()).toContain('字段映射')
    expect(wrapper.text()).toContain('CUSTOMER.CUSTOMER_NAME')
    expect(wrapper.text()).toContain('CUSTOMER.PAYMENT_TERM_DAYS')
  })

  it('bindings tab shows watermark and notes', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    expect(wrapper.text()).toContain('水位线')
    expect(wrapper.text()).toContain('CUSTOMER.LAST_MODIFIED_DATE')
    expect(wrapper.text()).toContain('E10-like 参考表形')
  })

  it('metrics tab shows calibration_state tags with correct colors', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '指标')

    const table = wrapper.find('[data-testid="tpl-metrics"]')
    expect(table.exists()).toBe(true)

    const calTags = table.findAll('[data-testid="calibration-tag"]')
    expect(calTags.length).toBe(4)

    // gross_margin_pct is calibrated → success (green)
    expect(calTags[0].text()).toBe('已校准')

    // avg_payment_days is uncalibrated → warning (orange)
    expect(calTags[1].text()).toBe('未校准')

    // quote_response_hours is uncalibrated → warning (orange)
    expect(calTags[2].text()).toBe('未校准')

    // inventory_days is deprecated → info (gray)
    expect(calTags[3].text()).toBe('已废弃')

    // Verify class colors using CSS selectors
    // calibrated → el-tag--success
    expect(table.findAll('.el-tag--success').length).toBeGreaterThanOrEqual(1)
    // uncalibrated → el-tag--warning
    expect(table.findAll('.el-tag--warning').length).toBeGreaterThanOrEqual(2)
    // deprecated → el-tag--info
    expect(table.findAll('.el-tag--info').length).toBeGreaterThanOrEqual(1)
  })

  it('metrics tab shows uncalibrated as warning (never success)', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '指标')

    const table = wrapper.find('[data-testid="tpl-metrics"]')
    // uncalibrated tags must use warning class, not success
    const calTags = table.findAll('[data-testid="calibration-tag"]')
    // avg_payment_days (index 1) is uncalibrated
    const uncalTag = calTags[1]
    expect(uncalTag.text()).toBe('未校准')
    // The table should have warning tags (from uncalibrated items)
    const warningTags = table.findAll('.el-tag--warning')
    expect(warningTags.length).toBeGreaterThanOrEqual(2)
  })

  it('metrics tab shows metric draft status as warning', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '指标')

    const table = wrapper.find('[data-testid="tpl-metrics"]')
    expect(table.text()).toContain('avg_payment_days')
    expect(table.text()).toContain('平均账期')
  })

  it('metrics tab shows all metric info: formula, grain, dimensions, caveats', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '指标')

    const table = wrapper.find('[data-testid="tpl-metrics"]')
    // Formula
    expect(table.text()).toContain('sum(revenue - cost)')
    // Grain
    expect(table.text()).toContain('SalesOrder')
    // Dimensions
    expect(table.text()).toContain('Customer')
    expect(table.text()).toContain('Material')
    // Caveats
    expect(table.text()).toContain('指标口径待现场校准')
    // Freshness SLA
    expect(table.text()).toContain('T+1')
  })

  it('metrics tab shows quote_response_hours metric', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '指标')

    expect(wrapper.text()).toContain('quote_response_hours')
    expect(wrapper.text()).toContain('报价响应时长')
  })

  it('no edit/save/publish buttons present anywhere', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()

    const forbiddenActions = ['保存模板', '应用草稿', '发布候选', '编辑模板']
    const tabs = ['概览', '属性', '绑定', '指标'] as const
    for (const tab of tabs) {
      if (tab !== '概览') await switchTab(wrapper, tab)
      const text = wrapper.text()
      for (const label of forbiddenActions) {
        expect(text).not.toContain(label)
      }
    }
    // Preview 只读声明可含「保存/发布」字样,但不得出现写入口按钮
    expect(wrapper.find('[data-testid="mapping-preview-panel"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('不会保存或发布')
  })

  it('shows no toggle controls for verified/certified', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '绑定')

    // No checkbox/switch for verified/certified toggle
    expect(wrapper.findAll('.el-switch').length).toBe(0)
    expect(wrapper.findAll('.el-checkbox').length).toBe(0)
  })

  it('empty metrics shows empty state', async () => {
    // Override metrics endpoint to return empty array
    server.use(
      http.get('*/api/templates/metrics', () =>
        HttpResponse.json([], { status: 200 })),
    )
    const wrapper = await mountView()
    await wrapper.find('[data-testid="tpl-item-SalesOrder"]').trigger('click')
    await flushPromises()
    await switchTab(wrapper, '指标')

    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('没有模板指标')
  })

  it('refresh button reloads templates', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('客户')
    expect(wrapper.text()).toContain('销售订单')

    // Override templates to return empty
    server.use(
      http.get('*/api/templates', () =>
        HttpResponse.json([], { status: 200 })),
    )
    await wrapper.find('[data-testid="tpl-refresh"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
  })

  it('shows refresh error banner when refresh fails', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('客户')

    // Override templates to return error
    server.use(
      http.get('*/api/templates', () =>
        HttpResponse.json({ detail: '服务异常' }, { status: 500 })),
    )
    await wrapper.find('[data-testid="tpl-refresh"]').trigger('click')
    await flushPromises()

    // Should show refresh error (keeping old data)
    expect(wrapper.find('[data-testid="templates-refresh-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('服务异常')
    // Old data should still be there
    expect(wrapper.text()).toContain('客户')
  })

  it('shows error state on first load failure', async () => {
    // Override templates to return 500 from the start
    server.use(
      http.get('*/api/templates', () =>
        HttpResponse.json({ detail: '服务不可用' }, { status: 500 })),
    )
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('500')
  })

  it('deselects object if removed from template list on refresh', async () => {
    const wrapper = await mountView()
    // Select a template
    await wrapper.find('[data-testid="tpl-item-Customer"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="tpl-detail-tabs"]').exists()).toBe(true)

    // Refresh with a list that excludes Customer
    server.use(
      http.get('*/api/templates', () =>
        HttpResponse.json(
          [{
            object: 'SalesOrder',
            display_name: '销售订单',
            domain: '销售',
            keys: ['order_no'],
            properties: [],
            source_of_truth: 'digiwin_e10',
            quarantine_pending: 0,
            bindings: [],
          }],
          { status: 200 },
        )),
    )
    await wrapper.find('[data-testid="tpl-refresh"]').trigger('click')
    await flushPromises()

    // Detail should be gone
    expect(wrapper.text()).toContain('请从左侧选择一个模板对象')
    expect(wrapper.find('[data-testid="tpl-detail-tabs"]').exists()).toBe(false)
  })
})
