import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { HttpResponse, http } from '@/test/http'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/fetch-stub'
import RawDataView from './RawDataView.vue'

async function mountView(query: Record<string, string> = {}): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/data/raw', query })
  await router.isReady()
  // attachTo 使 el-drawer Teleport 能渲染到真实 DOM
  const wrapper = mount(RawDataView, {
    attachTo: document.body,
    global: { plugins: [pinia, [ElementPlus, { locale: zhCn }], router] },
  })
  await flushPromises()
  return { wrapper, router }
}

// 点击目录行(详情入口一律行点击,规范 §3.2-3)
async function clickCatalogRow(wrapper: ReturnType<typeof mount>, text: string): Promise<void> {
  const row = wrapper.findAll('[data-testid="raw-catalog"] .el-table__row')
    .find((r) => r.text().includes(text))
  expect(row, `目录应包含行 ${text}`).toBeTruthy()
  await row!.trigger('click')
  await flushPromises()
}

describe('RawDataView(原始数据)', () => {
  beforeEach(() => setScenario('healthy'))

  it('raw 目录 → 行点击浏览:列驱动表格、脱敏标记、排序与总数', async () => {
    const { wrapper } = await mountView()
    const catalog = wrapper.find('[data-testid="raw-catalog"]')
    expect(catalog.exists()).toBe(true)
    expect(catalog.text()).toContain('CUSTOMER')
    await clickCatalogRow(wrapper, 'CUSTOMER')
    // el-drawer teleports to body — find table in document.body
    const table = document.querySelector('[data-testid="raw-drawer-table"]')
    expect(table).toBeTruthy()
    expect(table!.textContent).toContain('C-001')
    // 敏感列显示脱敏标识,值是 ***
    expect(document.body.textContent).toContain('脱敏')
    expect(document.body.textContent).toContain('***')
    expect(document.body.textContent).toContain('共 36 行')
    expect(document.body.textContent).toContain('pk:CUSTOMER_CODE')
  })

  it('JSON 面板折叠在浏览抽屉内(脱敏值,不含原值)', async () => {
    const { wrapper } = await mountView()
    await clickCatalogRow(wrapper, 'CUSTOMER')
    const toggle = [...document.querySelectorAll<HTMLElement>('[data-testid="json-toggle"]')].at(-1)
    expect(toggle).toBeTruthy()
    toggle!.click()
    await flushPromises()
    const json = [...document.querySelectorAll('[data-testid="json-view"]')].at(-1)
    expect(json).toBeTruthy()
    expect(json!.textContent).toContain('"columns"')
    expect(json!.textContent).toContain('"***"')
  })

  it('目录筛选:来源/表/抽取时间段控件存在,按表筛选生效', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="filter-source"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-table"]').exists()).toBe(true)
    // el-date-picker 不透传 data-testid 到根节点,用 class 定位
    expect(wrapper.find('.toolbar-daterange').exists()).toBe(true)
    const catalog = wrapper.find('[data-testid="raw-catalog"]')
    expect(catalog.text()).toContain('CUSTOMER')
    expect(catalog.text()).toContain('SALES_ORDER')
    // 打开表筛选下拉并选择 CUSTOMER(此前用例挂载的旧组件也渲染了下拉项,
    // 需在可见下拉中查找,避免点到隐藏的旧组件)
    await wrapper.find('[data-testid="filter-table"] .el-select__wrapper').trigger('click')
    await flushPromises()
    const visibleDropdown = [...document.querySelectorAll<HTMLElement>('.el-select-dropdown')]
      .filter((d) => d.style.display !== 'none')
      .at(-1)
    const option = [...(visibleDropdown?.querySelectorAll('.el-select-dropdown__item') ?? [])]
      .find((el) => el.textContent?.trim() === 'CUSTOMER')
    expect(option).toBeTruthy()
    ;(option as HTMLElement).click()
    await flushPromises()
    const filtered = wrapper.find('[data-testid="raw-catalog"]')
    expect(filtered.text()).toContain('CUSTOMER')
    expect(filtered.text()).not.toContain('SALES_ORDER')
  })

  it('raw catalog 加载失败:显示错误态而非安全指引', async () => {
    server.use(
      http.get('*/api/data/raw', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 })),
    )
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="raw-catalog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="raw-403-guide"]').exists()).toBe(false)
  })

  it('刷新失败保留上一次 raw 成功数据并标记失败', async () => {
    const { wrapper } = await mountView()
    await clickCatalogRow(wrapper, 'CUSTOMER')
    // the drawer component renders; data loads from store
    const html0 = wrapper.html()
    expect(html0).toContain('C-001')

    server.use(
      http.get('*/api/data/raw/:source/:table', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 })),
    )
    // click refresh in the already-open drawer
    await wrapper.find('[data-testid="raw-drawer-refresh"]').trigger('click')
    await flushPromises()
    const html = wrapper.html()
    expect(html).toContain('C-001')           // old success data preserved
    expect(html).toContain('raw-drawer-refresh-error') // error banner visible
  })
})
