/**
 * QuarantineView(M5-T08) tests:
 * empty state, group table, record table (filtered), detail drawer, retry flow.
 * Mock ElMessageBox.confirm so confirm dialogs resolve immediately in jsdom.
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from '@/test/http'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { baseFixture } from '@/test/fixtures/base'
import { quarantinePendingFixture } from '@/test/fixtures/quarantine-pending'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/fetch-stub'
import QuarantineView from './QuarantineView.vue'

// Mock ElMessageBox.confirm so dialog interactions resolve in tests.
// vitest hoists vi.mock to the top; importOriginal preserves the ElementPlus
// default export (Vue plugin) while we replace ElMessageBox.
vi.mock('element-plus', async (importOriginal) => {
  const actual: Record<string, unknown> = await importOriginal()
  return {
    ...actual,
    ElMessageBox: {
      confirm: vi.fn(),
    },
  }
})

import { ElMessageBox } from 'element-plus'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const confirmMock = vi.mocked(ElMessageBox.confirm) as any as ReturnType<typeof vi.fn>

async function mountView(): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push('/quarantine')
  await router.isReady()
  const wrapper = mount(QuarantineView, {
    global: { plugins: [pinia, ElementPlus, router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('QuarantineView(M5)', () => {
  beforeEach(() => {
    setScenario('quarantine-pending')
    confirmMock.mockResolvedValue('confirm')
  })

  // ---- empty state ----

  it('shows empty state when no quarantine records', async () => {
    setScenario('healthy')
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('隔离区为空')
  })

  // ---- group table ----

  it('renders group table with correct data', async () => {
    const { wrapper } = await mountView()
    const table = wrapper.find('[data-testid="quarantine-groups-table"]')
    expect(table.exists()).toBe(true)
    // 3 groups in quarantine-pending
    expect(table.text()).toContain('客户')
    expect(table.text()).toContain('销售订单')
    expect(table.text()).toContain('报价单')
    // pending counts
    expect(table.text()).toContain('2') // Customer has 2 pending
  })

  // ---- rate_state tags ----

  it('rate_state tags render with correct type', async () => {
    const { wrapper } = await mountView()
    // quarantine-pending has all rate_state=ok
    const span = wrapper.find('[data-testid="rate-state-ok"]')
    expect(span.exists()).toBe(true)
    expect(span.text()).toBe('正常')
    // el-tag should render the type class on its root element
    const tag = span.find('.el-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.classes()).toContain('el-tag--success')
  })

  it('rate_state tripped uses danger tag', async () => {
    setScenario('apply-circuit-broken')
    const { wrapper } = await mountView()
    const span = wrapper.find('[data-testid="rate-state-tripped"]')
    expect(span.exists()).toBe(true)
    expect(span.text()).toBe('熔断')
    const tag = span.find('.el-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.classes()).toContain('el-tag--danger')
  })

  // ---- serving_state tags ----

  it('serving_state tags render with correct type', async () => {
    const { wrapper } = await mountView()
    // quarantine-pending has all serving_state=fresh
    const span = wrapper.find('[data-testid="serving-state-fresh"]')
    expect(span.exists()).toBe(true)
    expect(span.text()).toBe('新鲜')
    const tag = span.find('.el-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.classes()).toContain('el-tag--success')
  })

  it('serving_state stale uses warning tag in apply-circuit-broken', async () => {
    setScenario('apply-circuit-broken')
    const { wrapper } = await mountView()
    const span = wrapper.find('[data-testid="serving-state-stale"]')
    expect(span.exists()).toBe(true)
    expect(span.text()).toBe('旧版本')
    const tag = span.find('.el-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.classes()).toContain('el-tag--warning')
  })

  // ---- record table ----

  it('renders record table with all quarantine records', async () => {
    const { wrapper } = await mountView()
    const table = wrapper.find('[data-testid="quarantine-records-table"]')
    expect(table.exists()).toBe(true)
    // quarantine-pending has 4 records total
    expect(wrapper.text()).toContain('共 4 条')
    expect(table.text()).toContain('C-ERR-X')
    expect(table.text()).toContain('C-NULL')
  })

  // ---- group click filters records ----

  it('clicking a group row filters record table', async () => {
    const { wrapper } = await mountView()
    // Click on Customer row (first row)
    const rows = wrapper.find('[data-testid="quarantine-groups-table"]').findAll('tbody tr')
    expect(rows.length).toBeGreaterThanOrEqual(1)
    await rows[0]!.trigger('click')
    await flushPromises()
    // Should show "隔离记录:客户" and only Customer records
    expect(wrapper.text()).toContain('隔离记录:客户')
    // Customer has 2 records in the fixture
    expect(wrapper.text()).toContain('共 2 条')
  })

  // ---- clear group filter ----

  it('clear group filter shows all records again', async () => {
    const { wrapper } = await mountView()
    const rows = wrapper.find('[data-testid="quarantine-groups-table"]').findAll('tbody tr')
    await rows[0]!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('共 2 条')
    await wrapper.find('[data-testid="clear-group-filter"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('所有隔离记录')
    expect(wrapper.text()).toContain('共 4 条')
  })

  // ---- detail drawer (auth error in mock) ----

  it('detail drawer opens and shows auth error when no token', async () => {
    const { wrapper } = await mountView()
    // Click a record row
    const recordTable = wrapper.find('[data-testid="quarantine-records-table"]')
    const recordRows = recordTable.findAll('tbody tr')
    expect(recordRows.length).toBeGreaterThan(0)
    await recordRows[0]!.trigger('click')
    await flushPromises()
    // Drawer opens
    const drawer = wrapper.find('[data-testid="quarantine-detail-drawer"]')
    expect(drawer.exists()).toBe(true)
    // Auth error message (mock requires Bearer token)
    expect(wrapper.find('[data-testid="detail-auth-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('需要配置控制台 Token 才能查看隔离详情')
  })

  // ---- detail drawer with raw (override auth) ----

  it('detail drawer shows raw data when auth valid', async () => {
    // Override detail handler to skip auth check
    server.use(
      http.get('*/api/quarantine/:id', ({ params }) => {
        const id = Number(params.id)
        const detail = quarantinePendingFixture.quarantineDetail[id]
        if (!detail) {
          return HttpResponse.json({ detail: `隔离记录 ${id} 不存在` }, { status: 404 })
        }
        return HttpResponse.json(detail)
      }),
    )
    const { wrapper } = await mountView()
    // Click first record
    const recordTable = wrapper.find('[data-testid="quarantine-records-table"]')
    await recordTable.find('tbody tr').trigger('click')
    await flushPromises()
    // Drawer opens with success state
    expect(wrapper.find('[data-testid="detail-raw-content"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-raw-content"]').text()).toContain('CUSTOMER_CODE')
    expect(wrapper.find('[data-testid="detail-keys"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="detail-reason"]').exists()).toBe(true)
  })

  // ---- summary stats ----

  it('summary shows correct aggregate stats', async () => {
    const { wrapper } = await mountView()
    const summary = wrapper.find('[data-testid="quarantine-summary"]')
    expect(summary.exists()).toBe(true)
    // quarantine-pending: 2+1+1 = 4 pending
    expect(wrapper.find('[data-testid="summary-pending"]').text()).toBe('4')
    // 3 groups all have pending > 0
    expect(wrapper.find('[data-testid="summary-affected"]').text()).toBe('3')
    // 0 tripped in quarantine-pending
    expect(wrapper.find('[data-testid="summary-tripped"]').text()).toBe('0')
  })

  it('summary shows over-threshold when tripped', async () => {
    setScenario('apply-circuit-broken')
    const { wrapper } = await mountView()
    // apply-circuit-broken has 1 group, rate_state=tripped
    expect(wrapper.find('[data-testid="summary-tripped"]').text()).toBe('1')
  })

  // ---- retry success ----

  it('retry button shows confirmation then executes retry', async () => {
    // Override retry handler to skip auth and return success
    server.use(
      http.post('*/api/actions/retry', () =>
        HttpResponse.json(baseFixture.retryAction, { status: 200 }),
      ),
    )
    const { wrapper } = await mountView()
    // Click retry button on first group
    const retryBtn = wrapper.find('[data-testid="retry-Customer"]')
    expect(retryBtn.exists()).toBe(true)
    await retryBtn.trigger('click')
    await flushPromises()
    // ElMessageBox.confirm should have been called
    expect(confirmMock).toHaveBeenCalled()
    // Wait for retry to complete
    await flushPromises()
    // Success dialog should appear
    expect(wrapper.find('[data-testid="retry-success"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-run-link"]').exists()).toBe(true)
  })

  // ---- retry error ----

  it('retry failure shows error message', async () => {
    // Override retry handler to return error with plain detail only (no structured fields)
    server.use(
      http.post('*/api/actions/retry', () =>
        HttpResponse.json(
          { detail: 'Customer 隔离率 53% 超过熔断阈值 20%' },
          { status: 409 },
        ),
      ),
    )
    const { wrapper } = await mountView()
    const retryBtn = wrapper.find('[data-testid="retry-Customer"]')
    await retryBtn.trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('[data-testid="retry-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-error-detail"]').text()).toContain('53%')
  })

  it('retry failure shows structured error with reason_code', async () => {
    server.use(
      http.post('*/api/actions/retry', () =>
        HttpResponse.json(
          {
            detail: 'Customer 隔离率 53% 超过熔断阈值 20%,请先处理隔离数据或调整阈值',
            reason_code: 'circuit_breaker',
          },
          { status: 409 },
        ),
      ),
    )
    const { wrapper } = await mountView()
    const retryBtn = wrapper.find('[data-testid="retry-Customer"]')
    await retryBtn.trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('[data-testid="retry-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-error-detail"]').text()).toContain('53%')
    expect(wrapper.find('[data-testid="retry-error-reason"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-error-reason"]').text()).toContain('circuit_breaker')
  })

  it('retry failure with run_id shows error run link', async () => {
    server.use(
      http.post('*/api/actions/retry', () =>
        HttpResponse.json(
          {
            detail: 'Apply 执行失败: step 3 报错',
            reason_code: 'execution_failed',
            run_id: 44,
            step_id: 3,
          },
          { status: 500 },
        ),
      ),
    )
    const { wrapper } = await mountView()
    const retryBtn = wrapper.find('[data-testid="retry-Customer"]')
    await retryBtn.trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('[data-testid="retry-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-error-reason"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-error-reason"]').text()).toContain('execution_failed')
    expect(wrapper.find('[data-testid="retry-error-run-link"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="retry-error-run-link"]').text()).toContain('44')
  })

  // ---- retry cancelled ----

  it('cancelling retry confirmation does not execute retry', async () => {
    confirmMock.mockRejectedValueOnce('cancel')
    server.use(
      http.post('*/api/actions/retry', () =>
        HttpResponse.json(baseFixture.retryAction, { status: 200 }),
      ),
    )
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="retry-Customer"]').trigger('click')
    await flushPromises()
    // Retry dialog should not appear (cancelled)
    expect(wrapper.find('[data-testid="retry-result-dialog"]').exists()).toBe(false)
  })

  // ---- refresh ----

  it('refresh failure keeps old data and shows warning', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="quarantine-groups-table"]').text()).toContain('客户')
    // Override groups endpoint to return 500
    server.use(
      http.get('*/api/quarantine/groups', () =>
        HttpResponse.json({ detail: 'refresh error' }, { status: 500 }),
      ),
    )
    await wrapper.find('[data-testid="quarantine-refresh"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="groups-refresh-error"]').exists()).toBe(true)
    // Old data still visible
    expect(wrapper.find('[data-testid="quarantine-groups-table"]').text()).toContain('客户')
  })

  // ---- unknown-error scenario ----

  it('unknown-error shows error state', async () => {
    setScenario('unknown-error')
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
  })

  // ---- record pagination ----

  it('record table shows paginator', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="quarantine-pager"]').exists()).toBe(true)
  })

  // ---- source+object pair in group selection ----

  it('group selection passes both source and object to record query', async () => {
    // intercept quarantine list and capture query params
    let capturedUrl = ''
    server.use(
      http.get('*/api/quarantine', ({ request }) => {
        capturedUrl = request.url
        const result = { items: quarantinePendingFixture.quarantine.filter(
          (r) => r.source === 'digiwin_e10' && r.object === 'Customer',
        ), total: 2 }
        return new HttpResponse(JSON.stringify(result.items), {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Total-Count': '2' },
        })
      }),
    )
    const { wrapper } = await mountView()
    // Click on Customer row
    const rows = wrapper.find('[data-testid="quarantine-groups-table"]').findAll('tbody tr')
    await rows[0]!.trigger('click')
    await flushPromises()
    // query should include both source and object
    const urlParams = new URL(capturedUrl).searchParams
    expect(urlParams.get('source')).toBe('digiwin_e10')
    expect(urlParams.get('object')).toBe('Customer')
  })

  // ---- retry button disabled: backend says retry not allowed ----

  it('retry button is disabled with tooltip when retry_allowed is false', async () => {
    server.use(
      http.get('*/api/quarantine/groups', () =>
        HttpResponse.json([{
          source: 'unknown_source',
          object: 'MysteryObj',
          display_name: null,
          pending: 5,
          quarantine_rate: 0.0,
          rate_state: 'unknown',
          serving_state: 'unknown',
          breaker_threshold: 0.2,
          retry_allowed: false,
          retry_disabled_reason: '模板未识别此对象，无法重试',
        }]),
      ),
    )
    const { wrapper } = await mountView()
    await flushPromises()
    const retryBtn = wrapper.find('[data-testid="retry-MysteryObj"]')
    expect(retryBtn.exists()).toBe(true)
    // button should be disabled
    expect(retryBtn.attributes('disabled')).toBeDefined()
    // el-tooltip wraps the button and has the content prop (teleported, not in HTML)
    const tooltip = wrapper.findComponent({ name: 'ElTooltip' })
    expect(tooltip.exists()).toBe(true)
    expect(tooltip.props('content')).toBe('模板未识别此对象，无法重试')
  })

  // ---- retry button disabled: backend says not_materialized ----

  it('retry button is disabled with tooltip when backend says retry not allowed', async () => {
    server.use(
      http.get('*/api/quarantine/groups', () =>
        HttpResponse.json([{
          source: 'demo',
          object: 'EmptyObj',
          display_name: '空对象',
          pending: 3,
          quarantine_rate: 0.0,
          rate_state: 'ok',
          serving_state: 'not_materialized',
          breaker_threshold: 0.2,
          retry_allowed: false,
          retry_disabled_reason: '对象尚未物化，无需重试',
        }]),
      ),
    )
    const { wrapper } = await mountView()
    await flushPromises()
    const retryBtn = wrapper.find('[data-testid="retry-EmptyObj"]')
    expect(retryBtn.exists()).toBe(true)
    expect(retryBtn.attributes('disabled')).toBeDefined()
    const tooltip = wrapper.findComponent({ name: 'ElTooltip' })
    expect(tooltip.exists()).toBe(true)
    expect(tooltip.props('content')).toBe('对象尚未物化，无需重试')
  })

  // ---- retry button enabled for normal group ----

  it('retry button is enabled for a normal group with known source', async () => {
    setScenario('quarantine-pending')
    const { wrapper } = await mountView()
    const retryBtn = wrapper.find('[data-testid="retry-Customer"]')
    expect(retryBtn.exists()).toBe(true)
    expect(retryBtn.attributes('disabled')).toBeUndefined()
  })
})
