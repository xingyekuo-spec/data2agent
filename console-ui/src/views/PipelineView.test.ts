import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, type Pinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { server } from '@/test/fetch-stub'
import { http } from '@/test/http'
import { baseFixture } from '@/test/fixtures/base'
import { setScenario } from '@/test/scenario'
import { useOverviewStore } from '@/stores/overview'
import { usePipelineStore } from '@/stores/pipeline'
import PipelineView from './PipelineView.vue'

const basePipelineFixture = baseFixture.pipeline

async function mountView(): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const wrapper = mount(PipelineView, { global: { plugins: [pinia, ElementPlus] } })
  await usePipelineStore(pinia).refresh()
  await flushPromises()
  return wrapper
}

describe('PipelineView(M3)', () => {
  beforeEach(() => setScenario('healthy'))

  it('healthy:overall + 数据源合成节点 + 5 个关键节点按固定顺序渲染', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="pipeline-overall"]').exists()).toBe(true)
    const names = wrapper.findAll('.flow__name').map((n) => n.text())
    expect(names).toEqual(['数据源(中间机)', '推送', '落地', '映射', '对象层', 'MCP 网关'])
    expect(wrapper.findAll('[data-status="healthy"]').length).toBeGreaterThan(0)

    // P2:连接线流量标签(下游节点行数 + 时间)+ 页头刷新指示
    const labels = wrapper.findAll('[data-testid="connector-label"]').map((n) => n.text())
    expect(labels).toContain('1284 行 · 09:12')
    expect(wrapper.find('[data-testid="pipeline-poll-at"]').text()).toContain('刷新于')
  })

  it('点击节点打开详情;再点/ESC 关闭;无 detail_path 不死链', async () => {
    const wrapper = await mountView()
    const nodes = wrapper.findAll('.flow__node')
    // 首节点为数据源合成节点,详情含跳数据源管理链接
    await nodes[0]?.trigger('click')
    const detail = wrapper.find('[data-testid="node-detail"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('数据源(中间机)')
    expect(detail.text()).toContain('最近成功')
    expect(detail.text()).toContain('查看详情')

    // ESC 关闭(点同一节点)
    await nodes[0]?.trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').exists()).toBe(false)

    // 推送节点(索引 1):无 detail_path 时不渲染死链
    await wrapper.findAll('.flow__node')[1]?.trigger('click')
    const detail2 = wrapper.find('[data-testid="node-detail"]')
    expect(detail2.exists()).toBe(true)
    expect(detail2.text()).toContain('推送')
    await wrapper.find('.detail__close').trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').exists()).toBe(false)
  })

  it('apply-circuit-broken:映射失败与对象旧结果同时可定位', async () => {
    setScenario('apply-circuit-broken')
    const wrapper = await mountView()
    const nodes = wrapper.findAll('.flow__node')
    const mapping = nodes.find((n) => n.text().includes('映射'))
    const objects = nodes.find((n) => n.text().includes('对象层'))
    expect(mapping?.find('[data-status="failed"]').exists()).toBe(true)
    expect(mapping?.text()).toContain('熔断')
    expect(objects?.find('[data-status="stale"]').exists()).toBe(true)
    // 其他节点保持自身状态,不整页转错误屏
    const mcp = nodes.find((n) => n.text().includes('MCP 网关'))
    expect(mcp?.find('[data-status="healthy"]').exists()).toBe(true)
  })

  it('partial-services-down:局部失败,其他节点不变空', async () => {
    setScenario('partial-services-down')
    const wrapper = await mountView()
    const nodes = wrapper.findAll('.flow__node')
    expect(nodes.find((n) => n.text().includes('MCP 网关'))?.find('[data-status="failed"]').exists()).toBe(true)
    expect(nodes.find((n) => n.text().includes('对象层'))?.find('[data-status="healthy"]').exists()).toBe(true)
  })

  it('首次加载失败(500):error 视图;刷新失败保留节点并标记', async () => {
    setScenario('unknown-error')
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)

    setScenario('healthy')
    const pinia: Pinia = createPinia()
    const wrapper2 = mount(PipelineView, { global: { plugins: [pinia, ElementPlus] } })
    const store = usePipelineStore(pinia)
    await store.refresh()
    await flushPromises()
    expect(wrapper2.findAll('.flow__node')).toHaveLength(6)
    setScenario('unknown-error')
    await store.refresh()
    await flushPromises()
    expect(wrapper2.find('[data-testid="refresh-error"]').exists()).toBe(true)
    expect(wrapper2.findAll('.flow__node')).toHaveLength(6)
  })

  it('键盘可打开详情(button 原生可聚焦可触发)', async () => {
    const wrapper = await mountView()
    const first = wrapper.findAll('.flow__node')[0]
    expect(first?.element.tagName).toBe('BUTTON')
    await first?.trigger('keydown.enter')
    // button 的 click 由浏览器在 Enter 时触发;jsdom 手动 click 模拟
    await first?.trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').exists()).toBe(true)
  })

  it('轮询更新后详情面板展示当前快照而不是旧对象', async () => {
    const pinia: Pinia = createPinia()
    const wrapper = mount(PipelineView, { global: { plugins: [pinia, ElementPlus] } })
    const store = usePipelineStore(pinia)
    await store.refresh()
    await flushPromises()

    // healthy 下打开推送详情
    const push = wrapper.findAll('.flow__node').find((n) => n.text().includes('推送'))
    await push?.trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').text()).toContain('healthy')

    // 轮询刷新到 ingest-failed:详情必须同步为 failed,无需关闭重开
    setScenario('ingest-failed')
    await store.refresh()
    await flushPromises()
    const detail = wrapper.find('[data-testid="node-detail"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('failed')
    expect(detail.text()).toContain('ingest 接收端不可达')
  })

  it('节点详情按状态给行动链接(诊断 → 下一步)', async () => {
    // mapping failed(熔断)→ 去校准映射
    setScenario('apply-circuit-broken')
    const wrapper = await mountView()
    const mapping = wrapper.findAll('.flow__node').find((n) => n.text().includes('映射'))
    await mapping?.trigger('click')
    const detail = wrapper.find('[data-testid="node-detail"]')
    expect(detail.text()).toContain('去校准映射')
    expect(detail.find('[data-testid="node-action--templates"]').exists()).toBe(true)
    wrapper.unmount()

    // quarantine-pending:对象层节点给「处理待确认数据」
    setScenario('quarantine-pending')
    const pinia2: Pinia = createPinia()
    const wrapper2 = mount(PipelineView, { global: { plugins: [pinia2, ElementPlus] } })
    await usePipelineStore(pinia2).refresh()
    await useOverviewStore(pinia2).refresh()
    await flushPromises()
    const objects = wrapper2.findAll('.flow__node').find((n) => n.text().includes('对象层'))
    await objects?.trigger('click')
    expect(wrapper2.find('[data-testid="node-detail"]').text()).toContain('处理待确认数据(4)')

    // 数据源合成节点 → detail_path 即 /sources(行动去重,单链接)
    const datasource = wrapper2.findAll('.flow__node')[0]
    await datasource?.trigger('click')
    const detail3 = wrapper2.find('[data-testid="node-detail"]')
    expect(detail3.text()).toContain('查看详情')
    const links = detail3.findAll('router-link')
    expect(links.filter((l) => l.attributes('to') === '/sources')).toHaveLength(1)
    wrapper2.unmount()
  })

  it('多源:切换数据源带 source 参数重查,快照作废不冒充', async () => {
    let captured: string | null = null
    server.use(
      http.get('*/api/pipeline', ({ request }) => {
        captured = new URL(request.url).searchParams.get('source')
        return new Response(JSON.stringify(basePipelineFixture), {
          headers: { 'Content-Type': 'application/json' },
        })
      }),
    )
    const pinia: Pinia = createPinia()
    const wrapper = mount(PipelineView, { global: { plugins: [pinia, ElementPlus] } })
    const store = usePipelineStore(pinia)
    await store.refresh()
    await flushPromises()
    expect(captured).toBeNull() // 缺省不带参数(兼容旧行为)

    store.setSource('kunshan_e10')
    await store.refresh()
    await flushPromises()
    expect(captured).toBe('kunshan_e10')
    wrapper.unmount()
  })
})
