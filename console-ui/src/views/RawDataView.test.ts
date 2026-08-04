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

describe('RawDataView(原始数据)', () => {
  beforeEach(() => setScenario('healthy'))

  it('raw 目录 → 浏览:列驱动表格、脱敏标记、排序与总数', async () => {
    const { wrapper } = await mountView()
    const catalog = wrapper.find('[data-testid="raw-catalog"]')
    expect(catalog.exists()).toBe(true)
    expect(catalog.text()).toContain('CUSTOMER')
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
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

  it('JSON 面板与表格同源(脱敏值,不含原值)', async () => {
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="json-toggle"]').trigger('click')
    const json = wrapper.find('[data-testid="json-view"]')
    expect(json.exists()).toBe(true)
    expect(json.text()).toContain('"columns"')
    expect(json.text()).toContain('"***"')
  })

  it('raw 403:显示安全配置指引,不降级', async () => {
    server.use(
      http.get('*/api/data/raw/:source/:table', () =>
        HttpResponse.json({ detail: 'raw 浏览需配置控制台 Token 并显式认证' }, { status: 403 })),
    )
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
    // drawer 虽然打开了,但 raw403 安全指引会显示(drawer 内显示 error state)
    expect(wrapper.find('[data-testid="raw-403-guide"]').exists()).toBe(true)
  })

  it('raw catalog 403:初始目录也显示安全配置指引', async () => {
    server.use(
      http.get('*/api/data/raw', () =>
        HttpResponse.json({ detail: 'raw 浏览需配置控制台 Token 并显式认证' }, { status: 403 })),
    )
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="raw-403-guide"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="raw-catalog"]').exists()).toBe(false)
  })

  it('刷新失败保留上一次 raw 成功数据并标记失败', async () => {
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
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
