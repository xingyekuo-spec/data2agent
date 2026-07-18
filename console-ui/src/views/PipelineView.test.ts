import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from 'msw'
import { createPinia } from 'pinia'
import { describe, expect, it } from 'vitest'
import { setScenario } from '@/mocks/scenario'
import { server } from '@/test/setup'
import PipelineView from './PipelineView.vue'

function mountView() {
  return mount(PipelineView, { global: { plugins: [createPinia(), ElementPlus] } })
}

describe('PipelineView(shell)', () => {
  it('Mock healthy:渲染 7 个节点徽标,不显示错误', async () => {
    setScenario('healthy')
    const wrapper = mountView()
    await flushPromises()
    const nodes = wrapper.findAll('.pipeline-nodes__item')
    expect(nodes).toHaveLength(7)
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(false)
    expect(wrapper.find('[data-status="healthy"]').exists()).toBe(true)
  })

  it('Mock ingest-failed:push 节点 failed,错误摘要可见', async () => {
    setScenario('ingest-failed')
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-status="failed"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('ingest 接收端不可达')
  })

  it('Real 契约桩(501):显示「尚未接入」而非空管道或通用失败', async () => {
    // 模拟 REAL 模式后端:契约桩返回 501 HttpError
    server.use(
      http.get('*/api/pipeline', () =>
        HttpResponse.json({ detail: '契约桩:端点已声明,将在所属里程碑实现' }, { status: 501 }),
      ),
    )
    setScenario('healthy')
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="not-implemented-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('尚未接入')
    expect(wrapper.find('[data-testid="pipeline-nodes"]').exists()).toBe(false)
  })

  it('后端未知错误(500):显示失败摘要而非空管道', async () => {
    // unknown-error 模拟「后端未实现/不可用」:页面必须是 error 视图
    setScenario('unknown-error')
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-nodes"]').exists()).toBe(false)
  })
})
