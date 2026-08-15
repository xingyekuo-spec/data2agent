import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import ObjectGraphView from './ObjectGraphView.vue'

async function mountView(): Promise<ReturnType<typeof mount>> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push('/ontology/object-graph')
  await router.isReady()
  const wrapper = mount(ObjectGraphView, { global: { plugins: [pinia, ElementPlus, router] } })
  await flushPromises()
  return wrapper
}

describe('ObjectGraphView', () => {
  beforeEach(() => setScenario('healthy'))

  it('renders object relation graph as a standalone page', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="object-graph"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="object-graph-stats"]').text()).toContain('4 对象')
    expect(wrapper.find('[data-testid="object-graph-stats"]').text()).toContain('3 关系')
    expect(wrapper.text()).toContain('SalesOrder')
    expect(wrapper.text()).toContain('Customer')
    expect(wrapper.text()).toContain('QuoteResponse')
  })

  it('selects nodes and shows incoming/outgoing relation detail', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="graph-node-Customer"]').trigger('click')
    await flushPromises()
    const detail = wrapper.find('[data-testid="object-graph-detail"]')
    expect(detail.text()).toContain('客户')
    expect(detail.text()).toContain('销售订单')
    expect(detail.text()).toContain('报价回复')
    expect(detail.text()).toContain('customer')

    await wrapper.find('[data-testid="graph-node-QuoteResponse"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="object-graph-outgoing"]').text()).toContain('Material')
  })

  it('filters graph nodes by keyword', async () => {
    const wrapper = await mountView()
    await wrapper
      .findComponent({ name: 'ElInput' })
      .vm.$emit('update:modelValue', 'Quote')
    await flushPromises()
    expect(wrapper.find('[data-testid="graph-node-QuoteResponse"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="graph-node-SalesOrder"]').exists()).toBe(false)
  })
})
