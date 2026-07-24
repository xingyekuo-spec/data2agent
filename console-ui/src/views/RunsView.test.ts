import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from '@/test/http'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { baseFixture } from '@/test/fixtures/base'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/fetch-stub'
import RunsView from './RunsView.vue'

async function mountView(query: Record<string, string> = {}): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/runs', query })
  await router.isReady()
  const wrapper = mount(RunsView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('RunsView(M4)', () => {
  beforeEach(() => setScenario('healthy'))

  it('列表渲染运行、总数与无动作按钮', async () => {
    const { wrapper } = await mountView()
    const table = wrapper.find('[data-testid="runs-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('sync')
    expect(wrapper.text()).toContain(`共 ${baseFixture.runs.length} 条`)
    // 只读:不出现 sync/apply/reconcile/retry 动作按钮
    expect(wrapper.text()).not.toContain('发起同步')
    expect(wrapper.text()).not.toContain('执行 apply')
    expect(wrapper.text()).not.toContain('重试')
  })

  it('筛选与分页区显示(总数来自响应头)', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="filter-type"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-status"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runs-pager"]').exists()).toBe(true)
  })

  it('点击行打开详情抽屉:step 表与水位列', async () => {
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="runs-table"] tbody tr').trigger('click')
    await flushPromises()
    const drawer = wrapper.find('[data-testid="run-detail-drawer"]')
    expect(drawer.exists()).toBe(true)
    const steps = wrapper.find('[data-testid="steps-table"]')
    expect(steps.exists()).toBe(true)
    expect(steps.text()).toContain('CUSTOMER')
    expect(steps.text()).toContain('2026-07-17 08:30:00')
  })

  it('legacy_unavailable 显示无证据提示而不是 0 步骤', async () => {
    server.use(
      http.get('*/api/runs/:runId', () =>
        HttpResponse.json(
          { ...baseFixture.runDetail, steps_state: 'legacy_unavailable', steps: [] },
          { headers: { 'Content-Type': 'application/json' } },
        )),
    )
    const { wrapper } = await mountView({ run_id: '41' })
    await flushPromises()
    expect(wrapper.find('[data-testid="legacy-note"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('历史记录没有逐步证据')
    expect(wrapper.find('[data-testid="steps-table"]').exists()).toBe(false)
  })

  it('无效 run_id 深链显示可恢复 404', async () => {
    server.use(
      http.get('*/api/runs/:runId', () =>
        HttpResponse.json({ detail: '运行 #99999 不存在' }, { status: 404 })),
    )
    const { wrapper } = await mountView({ run_id: '99999' })
    await flushPromises()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('404')
  })

  it('安全 JSON 视图与表格同源', async () => {
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="runs-table"] tbody tr').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="json-toggle"]').trigger('click')
    const json = wrapper.find('[data-testid="json-view"]')
    expect(json.exists()).toBe(true)
    expect(json.text()).toContain('"steps_state"')
  })

  it('route query 恢复筛选与详情抽屉', async () => {
    const { wrapper } = await mountView({ type: 'sync', status: 'ok', run_id: '42' })
    expect(wrapper.find('[data-testid="runs-table"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-drawer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="steps-table"]').text()).toContain('CUSTOMER')
  })

  it('刷新失败保留上一次运行列表并标记失败', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="runs-table"]').text()).toContain('sync')
    server.use(
      http.get('*/api/runs', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })),
    )
    await wrapper.find('[data-testid="refresh-button"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="runs-refresh-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runs-table"]').text()).toContain('sync')
  })
})
