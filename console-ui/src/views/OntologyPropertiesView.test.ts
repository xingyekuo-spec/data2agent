import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import OntologyPropertiesView from './OntologyPropertiesView.vue'

async function mountView(query: Record<string, string> = {}): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/ontology/properties', query })
  await router.isReady()
  // attachTo 使 el-drawer Teleport 能渲染到真实 DOM
  const wrapper = mount(OntologyPropertiesView, {
    attachTo: document.body,
    global: { plugins: [pinia, [ElementPlus, { locale: zhCn }], router] },
  })
  await flushPromises()
  return { wrapper, router }
}

// 详情入口一律行点击(规范 §3.2-3)
async function clickPropRow(wrapper: ReturnType<typeof mount>, text: string): Promise<void> {
  const row = wrapper.findAll('[data-testid="props-table"] .el-table__row')
    .find((r) => r.text().includes(text))
  expect(row, `字典应包含行 ${text}`).toBeTruthy()
  await row!.trigger('click')
  await flushPromises()
}

// 抽屉 teleport 到 body 且历史用例的抽屉不卸载,取最后一个(最新打开)
function inDrawer(selector: string): Element | undefined {
  return [...document.querySelectorAll(selector)].at(-1)
}

describe('OntologyPropertiesView(本体库 · 属性)', () => {
  beforeEach(() => setScenario('healthy'))

  it('全量属性字典:平铺所有类的属性并分页', async () => {
    const { wrapper } = await mountView()
    const table = wrapper.find('[data-testid="props-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('customer_code')
    expect(table.text()).toContain('handler_notes')
    expect(table.text()).toContain('unit_cost')
    // fixture:Customer 4 + SalesOrder 3 + QuoteResponse 6 + Material 5 = 18 属性
    expect(wrapper.find('[data-testid="props-pager"]').text()).toContain('共 18 条')
  })

  it('行点击开属性详情抽屉:定义 + 映射表达式', async () => {
    const { wrapper } = await mountView()
    await clickPropRow(wrapper, 'handler_notes')
    const target = inDrawer('[data-testid="prop-detail-target"]')
    expect(target?.textContent).toContain('QuoteResponse')
    expect(target?.textContent).toContain('handler_notes')
    // 敏感属性在 summary 中标注脱敏
    expect(inDrawer('.el-drawer .summary')?.textContent).toContain('出网默认脱敏')
    // 映射表达式来自 binding field_map
    expect(inDrawer('[data-testid="prop-mappings-table"]')?.textContent)
      .toContain('QUOTE_RESPONSE.NOTES')
  })

  it('敏感筛选:只保留脱敏属性(经 route query 恢复)', async () => {
    const { wrapper, router } = await mountView({ sensitive: 'yes' })
    expect(router.currentRoute.value.query.sensitive).toBe('yes')
    const table = wrapper.find('[data-testid="props-table"]')
    expect(table.text()).toContain('contact')
    expect(table.text()).toContain('handler_notes')
    expect(table.text()).toContain('unit_cost')
    expect(table.text()).not.toContain('customer_code')
    // 敏感属性:Customer.contact + QuoteResponse.handler_notes + Material.unit_cost = 3
    expect(wrapper.find('[data-testid="props-pager"]').text()).toContain('共 3 条')
  })

  it('route query 深链直接打开属性详情抽屉', async () => {
    await mountView({ owner: 'Customer', prop: 'contact' })
    expect(inDrawer('[data-testid="prop-detail-target"]')?.textContent).toContain('contact')
  })
})
