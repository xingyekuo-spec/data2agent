import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import OntologyClassesView from './OntologyClassesView.vue'

async function mountView(query: Record<string, string> = {}): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/ontology/classes', query })
  await router.isReady()
  // attachTo 使 el-drawer Teleport 能渲染到真实 DOM
  const wrapper = mount(OntologyClassesView, {
    attachTo: document.body,
    global: { plugins: [pinia, [ElementPlus, { locale: zhCn }], router] },
  })
  await flushPromises()
  return { wrapper, router }
}

// 详情入口一律行点击(规范 §3.2-3)
async function clickClassRow(wrapper: ReturnType<typeof mount>, text: string): Promise<void> {
  const row = wrapper.findAll('[data-testid="classes-table"] .el-table__row')
    .find((r) => r.text().includes(text))
  expect(row, `目录应包含行 ${text}`).toBeTruthy()
  await row!.trigger('click')
  await flushPromises()
}

// 抽屉 teleport 到 body 且历史用例的抽屉不卸载,取最后一个(最新打开)
function inDrawer(selector: string): Element | undefined {
  return [...document.querySelectorAll(selector)].at(-1)
}

describe('OntologyClassesView(本体库 · 类)', () => {
  beforeEach(() => setScenario('healthy'))

  it('类目录:对象/领域/业务键/属性数/物化状态', async () => {
    const { wrapper } = await mountView()
    const table = wrapper.find('[data-testid="classes-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('客户')
    expect(table.text()).toContain('SalesOrder')
    expect(table.text()).toContain('销售')
    expect(table.text()).toContain('customer_code')
  })

  it('行点击开类详情抽屉:属性/关系/绑定三段', async () => {
    const { wrapper } = await mountView()
    await clickClassRow(wrapper, 'QuoteResponse')
    expect(inDrawer('[data-testid="class-detail-target"]')?.textContent).toContain('QuoteResponse')
    // 属性表:敏感字段带脱敏标记,枚举值透出
    const props = inDrawer('[data-testid="class-props-table"]')
    expect(props?.textContent).toContain('handler_notes')
    expect(props?.textContent).toContain('脱敏')
    expect(props?.textContent).toContain('CNY / USD / EUR')
    // 关系表:两条出边
    const relations = inDrawer('[data-testid="class-relations-table"]')
    expect(relations?.textContent).toContain('Customer')
    expect(relations?.textContent).toContain('Material')
    // 绑定表
    expect(inDrawer('[data-testid="class-bindings-table"]')?.textContent).toContain('QUOTE_RESPONSE')
  })

  it('关系目标类可在抽屉内跳转', async () => {
    const { wrapper } = await mountView()
    await clickClassRow(wrapper, 'SalesOrder')
    const target = [...document.querySelectorAll<HTMLElement>('[data-testid="class-rel-target-Customer"]')].at(-1)
    expect(target).toBeTruthy()
    target!.click()
    await flushPromises()
    expect(inDrawer('[data-testid="class-detail-target"]')?.textContent).toContain('Customer')
  })

  it('route query 深链直接打开类详情抽屉', async () => {
    const { router } = await mountView({ object: 'Material' })
    expect(inDrawer('[data-testid="class-detail-target"]')?.textContent).toContain('Material')
    expect(router.currentRoute.value.query.object).toBe('Material')
  })
})
