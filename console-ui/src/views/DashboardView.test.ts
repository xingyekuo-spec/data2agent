import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, type Pinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setScenario } from '@/test/scenario'
import { useOverviewStore } from '@/stores/overview'
import { usePipelineStore } from '@/stores/pipeline'
import DashboardView from './DashboardView.vue'

// ECharts 与 ResizeObserver 的 jsdom 适配集中在 src/test/setup.ts

async function mountDashboard(): Promise<{
  wrapper: ReturnType<typeof mount>
  overview: ReturnType<typeof useOverviewStore>
  pipeline: ReturnType<typeof usePipelineStore>
}> {
  const pinia: Pinia = createPinia()
  const wrapper = mount(DashboardView, { global: { plugins: [pinia, ElementPlus] } })
  const overview = useOverviewStore(pinia)
  const pipeline = usePipelineStore(pinia)
  await Promise.all([overview.refresh(), pipeline.refresh()])
  await flushPromises()
  // TrendChart 为异步组件(echarts 懒加载):首轮渲染才触发 import;
  // 动态 import 走宏任务,flushPromises 只清微任务,须等待其全部落地
  await vi.dynamicImportSettled()
  await flushPromises()
  return { wrapper, overview, pipeline }
}

describe('DashboardView(M3)', () => {
  beforeEach(() => setScenario('healthy'))

  it('healthy:摘要卡 / 状态摘要 / 趋势 / 分布 / 运行 / 版本 / 口径', async () => {
    const { wrapper } = await mountDashboard()
    // 四张摘要卡
    const cards = wrapper.findAll('[data-testid="stat-value"]')
    expect(cards).toHaveLength(4)
    expect(cards[0]?.text()).toBe('187')       // 对象层总行数
    expect(cards[1]?.text()).toBe('4/5')       // 覆盖率
    expect(cards[2]?.text()).toBe('0')         // 隔离待处理
    // 状态摘要:整体 healthy + 数据更新时间 + 告警数
    const strip = wrapper.find('[data-testid="status-strip"]')
    expect(strip.find('[data-status="healthy"]').exists()).toBe(true)
    expect(strip.text()).toContain('数据更新')
    expect(strip.text()).toContain('告警 0 条')
    // 趋势(有点)与对象分布
    expect(wrapper.find('[data-testid="trend-chart"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="object-dist"]').exists()).toBe(true)
    // 最近运行:类型 sync + 状态徽标
    const runs = wrapper.find('[data-testid="recent-runs"]')
    expect(runs.text()).toContain('sync')
    // 无告警
    expect(wrapper.text()).toContain('无告警')
    // 版本:healthy 场景展示已发布 dataset/object 版本
    const versions = wrapper.find('[data-testid="versions"]')
    expect(versions.text()).toContain('ds-20260718-091100-a1b2')
    expect(versions.find('[data-testid="dataset-version-na"]').exists()).toBe(false)
    expect(versions.text()).toContain('0.1.0')   // 模板版本真实
    // 口径说明
    expect(wrapper.find('[data-testid="count-notes"]').text()).toContain('raw_rows')
  })

  it('empty-install:首次安装显示空态与尚未发布,不是 0 即健康', async () => {
    setScenario('empty-install')
    const { wrapper } = await mountDashboard()
    expect(wrapper.text()).toContain('尚未完成首次配置')
    expect(wrapper.find('[data-testid="stat-grid"]').exists()).toBe(false)
  })

  it('quarantine-pending:隔离数 warning + 告警列表', async () => {
    setScenario('quarantine-pending')
    const { wrapper } = await mountDashboard()
    const cards = wrapper.findAll('[data-testid="stat-value"]')
    expect(cards[2]?.text()).toBe('4')
    const alerts = wrapper.find('[data-testid="alerts"]')
    expect(alerts.text()).toContain('存在未处理隔离')
  })

  it('apply-circuit-broken:告警含映射失败与旧结果提示', async () => {
    setScenario('apply-circuit-broken')
    const { wrapper } = await mountDashboard()
    const alerts = wrapper.find('[data-testid="alerts"]')
    expect(alerts.text()).toContain('mapping failed')
    expect(alerts.text()).toContain('上一稳定结果')
    // critical 排在 warning 前
    const tags = wrapper.findAll('[data-testid="alerts"] .el-tag')
    expect(tags[0]?.text()).toBe('critical')
  })

  it('首次加载失败(500):整屏 error 视图', async () => {
    setScenario('unknown-error')
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stat-grid"]').exists()).toBe(false)
  })

  it('刷新失败保留旧数据并标记,不清空模块', async () => {
    const { wrapper, overview } = await mountDashboard()
    expect(wrapper.find('[data-testid="stat-grid"]').exists()).toBe(true)
    setScenario('unknown-error')
    await overview.refresh()
    await flushPromises()
    expect(wrapper.find('[data-testid="refresh-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stat-grid"]').exists()).toBe(true)
  })

  it('服务健康:展示 console/apply/ingest/mcp 四个服务徽标', async () => {
    const { wrapper } = await mountDashboard()
    const strip = wrapper.find('[data-testid="services-strip"]')
    expect(strip.exists()).toBe(true)
    for (const name of ['ingest', 'mcp', 'apply', 'console']) {
      expect(strip.text()).toContain(name)
    }
    // healthy:全部 healthy
    expect(strip.findAll('[data-status="healthy"]')).toHaveLength(4)
  })

  it('服务健康:首次加载 services 失败时显示查询失败(不是空白)', async () => {
    const { HttpResponse, http } = await import('@/test/http')
    const { server } = await import('@/test/fetch-stub')
    server.use(
      http.get('*/api/services', () =>
        HttpResponse.json({ detail: 'probe boom' }, { status: 500 })),
    )
    const { wrapper } = await mountDashboard()
    expect(wrapper.text()).toContain('服务状态查询失败')
    // pipeline / overview 数据仍在(不因 services 失败变空)
    expect(wrapper.find('[data-testid="stat-grid"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="status-strip"]').exists()).toBe(true)
  })

  it('recent/trend 查询失败:显示不可检测而不是空数据', async () => {
    const { HttpResponse, http } = await import('@/test/http')
    const { server } = await import('@/test/fetch-stub')
    const { baseFixture } = await import('@/test/fixtures/base')
    server.use(
      http.get('*/api/overview', () =>
        new HttpResponse(
          JSON.stringify({
            ...baseFixture.overview,
            recent_runs: null,
            sync_trend: null,
          }),
          { headers: { 'Content-Type': 'application/json' } },
        )),
    )
    const { wrapper } = await mountDashboard()
    expect(wrapper.find('[data-testid="runs-unknown"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="trend-unknown"]').exists()).toBe(true)
    // 不是"从未运行/暂无趋势"的空态伪装
    expect(wrapper.text()).not.toContain('从未运行')
    expect(wrapper.text()).not.toContain('暂无趋势数据')
  })
})
