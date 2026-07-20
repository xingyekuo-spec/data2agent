import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from 'msw'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/mocks/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/setup'
import AuditView from './AuditView.vue'

async function mountView(): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push('/audit')
  await router.isReady()
  const wrapper = mount(AuditView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return wrapper
}

async function mountViewWithQuery(query: Record<string, string>): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/audit', query })
  await router.isReady()
  const wrapper = mount(AuditView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return wrapper
}

describe('AuditView(M4)', () => {
  beforeEach(() => setScenario('healthy'))

  it('SQL tab:筛选、总数、表格与默认折叠 SQL', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="filter-source"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-action"]').exists()).toBe(true)
    const table = wrapper.find('[data-testid="sql-table"]')
    expect(table.exists()).toBe(true)
    // SQL 默认折叠:主列是截断预览,全文在 expand 里
    expect(wrapper.find('.sql-preview').exists()).toBe(true)
    expect(wrapper.find('[data-testid="sql-full"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('共 1 条')
  })

  it('SQL 展开行显示全文(转义文本,不是 HTML 执行)', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="sql-table"] .el-table__expand-icon').trigger('click')
    await flushPromises()
    const full = wrapper.find('[data-testid="sql-full"]')
    expect(full.exists()).toBe(true)
    expect(full.text()).toContain('SELECT * FROM CUSTOMER')
  })

  it('数据访问 tab:范围说明、允许/拒绝记录与筛选', async () => {
    const wrapper = await mountView()
    const panes = wrapper.findAll('.el-tabs__item')
    const accessTab = panes.find((p) => p.text().includes('数据访问'))
    await accessTab?.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="access-scope-note"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('仅覆盖 raw')
    const table = wrapper.find('[data-testid="access-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('console-admin')
    expect(table.text()).toContain('anonymous')
    expect(table.text()).toContain('not_in_catalog')
    expect(wrapper.find('[data-testid="filter-subject"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-resource-type"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-allowed"]').exists()).toBe(true)
  })

  it('unknown-error:SQL tab 为错误视图而非空数据', async () => {
    setScenario('unknown-error')
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
  })

  it('route query 恢复访问审计 tab 与筛选', async () => {
    const wrapper = await mountViewWithQuery({
      tab: 'access',
      subject: 'console-admin',
      allowed: 'true',
    })
    const table = wrapper.find('[data-testid="access-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('console-admin')
    expect(table.text()).not.toContain('anonymous')
  })

  it('刷新失败保留上一次 SQL 审计数据并标记失败', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="sql-table"]').text()).toContain('SELECT * FROM CUSTOMER')
    server.use(
      http.get('*/api/audit', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })),
    )
    await wrapper.find('[data-testid="sql-refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="sql-refresh-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="sql-table"]').text()).toContain('SELECT * FROM CUSTOMER')
  })
})
