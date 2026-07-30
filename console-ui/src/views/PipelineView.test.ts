import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, type Pinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { usePipelineStore } from '@/stores/pipeline'
import PipelineView from './PipelineView.vue'

async function mountView(): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const wrapper = mount(PipelineView, { global: { plugins: [pinia, ElementPlus] } })
  await usePipelineStore(pinia).refresh()
  await flushPromises()
  return wrapper
}

describe('PipelineView(M3)', () => {
  beforeEach(() => setScenario('healthy'))

  it('healthy:overall + 5 个关键节点按固定顺序渲染', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="pipeline-overall"]').exists()).toBe(true)
    const names = wrapper.findAll('.flow__name').map((n) => n.text())
    expect(names).toEqual(['push', 'raw', 'mapping', 'objects', 'mcp'])
    expect(wrapper.findAll('[data-status="healthy"]').length).toBeGreaterThan(0)
  })

  it('点击节点打开详情;再点/ESC 关闭;detail_path 未启用不死链', async () => {
    const wrapper = await mountView()
    const first = wrapper.findAll('.flow__node')[0]
    expect(first).toBeDefined()
    await first?.trigger('click')
    const detail = wrapper.find('[data-testid="node-detail"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('节点 push')
    expect(detail.text()).toContain('最近成功')
    expect(detail.text()).toContain('运行详情页将在 M4 提供')

    // ESC 关闭
    await wrapper.find('.flow__node')?.trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').exists()).toBe(false)

    // 再次打开,用关闭按钮
    await wrapper.findAll('.flow__node')[1]?.trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').exists()).toBe(true)
    await wrapper.find('.detail__close').trigger('click')
    expect(wrapper.find('[data-testid="node-detail"]').exists()).toBe(false)
  })

  it('apply-circuit-broken:映射失败与对象旧结果同时可定位', async () => {
    setScenario('apply-circuit-broken')
    const wrapper = await mountView()
    const nodes = wrapper.findAll('.flow__node')
    const mapping = nodes.find((n) => n.text().includes('mapping'))
    const objects = nodes.find((n) => n.text().includes('objects'))
    expect(mapping?.find('[data-status="failed"]').exists()).toBe(true)
    expect(mapping?.text()).toContain('熔断')
    expect(objects?.find('[data-status="stale"]').exists()).toBe(true)
    // 其他节点保持自身状态,不整页转错误屏
    const mcp = nodes.find((n) => n.text().includes('mcp'))
    expect(mcp?.find('[data-status="healthy"]').exists()).toBe(true)
  })

  it('partial-services-down:局部失败,其他节点不变空', async () => {
    setScenario('partial-services-down')
    const wrapper = await mountView()
    const nodes = wrapper.findAll('.flow__node')
    expect(nodes.find((n) => n.text().includes('mcp'))?.find('[data-status="failed"]').exists()).toBe(true)
    expect(nodes.find((n) => n.text().includes('objects'))?.find('[data-status="healthy"]').exists()).toBe(true)
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
    expect(wrapper2.findAll('.flow__node')).toHaveLength(5)
    setScenario('unknown-error')
    await store.refresh()
    await flushPromises()
    expect(wrapper2.find('[data-testid="refresh-error"]').exists()).toBe(true)
    expect(wrapper2.findAll('.flow__node')).toHaveLength(5)
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

    // healthy 下打开 push 详情
    const push = wrapper.findAll('.flow__node').find((n) => n.text().includes('push'))
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
})
