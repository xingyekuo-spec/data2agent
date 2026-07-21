import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from 'msw'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/mocks/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/setup'
import DataView from './DataView.vue'
import { baseFixture } from '@/mocks/fixtures/base'

async function mountView(): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push('/data')
  await router.isReady()
  const wrapper = mount(DataView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return wrapper
}

async function mountViewWithQuery(query: Record<string, string>): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/data', query })
  await router.isReady()
  const wrapper = mount(DataView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('DataView(M4)', () => {
  beforeEach(() => setScenario('healthy'))

  it('raw 目录 → 浏览:列驱动表格、脱敏标记、排序与总数', async () => {
    const wrapper = await mountView()
    const catalog = wrapper.find('[data-testid="raw-catalog"]')
    expect(catalog.exists()).toBe(true)
    expect(catalog.text()).toContain('CUSTOMER')
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
    const table = wrapper.find('[data-testid="raw-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('C-001')
    // 敏感列显示脱敏标识,值是 ***
    expect(wrapper.text()).toContain('脱敏')
    expect(wrapper.text()).toContain('***')
    expect(wrapper.text()).toContain('共 36 行')
    expect(wrapper.text()).toContain('pk:CUSTOMER_CODE')
  })

  it('数据集 tab:列表区分待发布/已发布,可发布与回滚', async () => {
    const wrapper = await mountView()
    const panes = wrapper.findAll('.el-tabs__item')
    await panes.find((p) => p.text().includes('数据集'))?.trigger('click')
    await flushPromises()
    await flushPromises()
    const table = wrapper.find('[data-testid="datasets-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('待发布')
    expect(table.text()).toContain('已发布')
    expect(table.text()).toContain('已退役')
    expect(table.text()).toContain('失败')
    expect(wrapper.find('[data-testid="dataset-publish-ds-20260718-095000-e5f6"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="dataset-rollback-ds-20260718-091100-a1b2"]').exists()).toBe(true)

    await wrapper.find('[data-testid="dataset-publish-ds-20260718-095000-e5f6"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="dataset-action-result"]').text()).toContain('ds-20260718-095000-e5f6')

    await wrapper.find('[data-testid="dataset-detail-ds-20260718-095000-e5f6"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="dataset-objects-table"]').text()).toContain('Customer')
    expect(wrapper.find('[data-testid="dataset-objects-table"]').text()).toContain('built')
  })

  it('stage-only apply 返回 published=false', async () => {
    const wrapper = await mountView()
    const panes = wrapper.findAll('.el-tabs__item')
    await panes.find((p) => p.text().includes('数据集'))?.trigger('click')
    await flushPromises()
    await flushPromises()
    const toggle = wrapper.find('[data-testid="stage-only-toggle"] input')
    await toggle.setValue(true)
    await wrapper.find('[data-testid="apply-run"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="apply-result"]').text()).toContain('published=false')
  })

  it('对象目录展示 published object_version', async () => {
    const wrapper = await mountView()
    const panes = wrapper.findAll('.el-tabs__item')
    await panes.find((p) => p.text().includes('对象层'))?.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="obj-version"]').text()).toContain('ov-cust-1')
  })

  it('JSON 面板与表格同源(脱敏值,不含原值)', async () => {
    const wrapper = await mountView()
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
    const wrapper = await mountView()
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="raw-403-guide"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="raw-table"]').exists()).toBe(false)
  })

  it('raw catalog 403:初始目录也显示安全配置指引', async () => {
    server.use(
      http.get('*/api/data/raw', () =>
        HttpResponse.json({ detail: 'raw 浏览需配置控制台 Token 并显式认证' }, { status: 403 })),
    )
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="raw-403-guide"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="raw-catalog"]').exists()).toBe(false)
  })

  it('对象未物化(409):具名错误视图', async () => {
    server.use(
      http.get('*/api/objects/:object', () =>
        HttpResponse.json({ detail: "对象 'Customer' 尚未物化" }, { status: 409 })),
    )
    const wrapper = await mountView()
    const panes = wrapper.findAll('.el-tabs__item')
    await panes.find((p) => p.text().includes('对象层'))?.trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="browse-Customer"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('409')
  })

  it('对象 warnings/truncations 可见', async () => {
    server.use(
      http.get('*/api/objects/:object', ({ request }) => {
        const url = new URL(request.url)
        const limit = Number(url.searchParams.get('limit') ?? 50)
        const offset = Number(url.searchParams.get('offset') ?? 0)
        return HttpResponse.json({
          ...baseFixture.objectRows,
          rows: baseFixture.objectRows.rows.slice(offset, offset + limit),
          offset,
          limit,
          warnings: ['对象列 NOTE 分类未知,按未确认处理展示'],
          truncations: [{ row_index: 0, fields: ['name'] }],
        })
      }),
    )
    const wrapper = await mountView()
    const panes = wrapper.findAll('.el-tabs__item')
    await panes.find((p) => p.text().includes('对象层'))?.trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="browse-Customer"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="obj-warnings"]').text()).toContain('NOTE')
    expect(wrapper.find('[data-testid="obj-truncations"]').text()).toContain('#0(name)')
  })

  it('route query 恢复 tab/resource/search/page', async () => {
    const { wrapper, router } = await mountViewWithQuery({
      tab: 'object',
      object: 'Customer',
      q: 'C-001',
      page: '2',
    })
    expect(wrapper.find('[data-testid="obj-table"]').exists()).toBe(true)
    expect((router.currentRoute.value.query.object)).toBe('Customer')
    expect(wrapper.text()).toContain('Customer · 共 1 行')
  })

  it('刷新失败保留上一次 raw 成功数据并标记失败', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="browse-CUSTOMER"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="raw-table"]').text()).toContain('C-001')
    server.use(
      http.get('*/api/data/raw/:source/:table', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 })),
    )
    await wrapper.find('[data-testid="raw-refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="raw-page-refresh-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="raw-table"]').text()).toContain('C-001')
  })
})
