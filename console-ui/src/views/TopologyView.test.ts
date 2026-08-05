import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import OntologyGraph from '@/components/ontology/OntologyGraph.vue'
import TopologyView from './TopologyView.vue'

async function mountView(): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/ontology/topology' })
  await router.isReady()
  // attachTo 使 el-drawer Teleport 能渲染到真实 DOM
  const wrapper = mount(TopologyView, {
    attachTo: document.body,
    global: { plugins: [pinia, [ElementPlus, { locale: zhCn }], router] },
  })
  await flushPromises()
  return { wrapper, router }
}

function graphOf(wrapper: ReturnType<typeof mount>) {
  return wrapper.findComponent(OntologyGraph)
}

describe('TopologyView(本体库 · 拓扑)', () => {
  beforeEach(() => setScenario('healthy'))

  it('默认只画类-关系层:类节点 + 关系边,筛选项齐全', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="filter-domain"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="property-toggle"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="topology-refresh"]').exists()).toBe(true)
    const graph = graphOf(wrapper)
    expect(graph.exists()).toBe(true)
    const nodes = graph.props('nodes')
    // 默认不展开属性层:4 个类节点,无属性节点
    expect(nodes).toHaveLength(4)
    expect(nodes.every((n) => n.kind === 'class')).toBe(true)
    // fixture 3 条 relations 均在包含的类之间
    expect(graph.props('links')).toHaveLength(3)
  })

  it('展开属性层:属性节点 + has-property 边 + ref 虚线边', async () => {
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="property-toggle"] .el-switch__core').trigger('click')
    await flushPromises()
    const graph = graphOf(wrapper)
    const nodes = graph.props('nodes')
    // 4 类 + 18 属性 = 22 节点
    expect(nodes).toHaveLength(22)
    expect(nodes.filter((n) => n.kind === 'property')).toHaveLength(18)
    // 业务键属性为菱形,敏感属性橙描边(规则视觉编码)
    const key = nodes.find((n) => n.id === 'prop:Customer.customer_code')
    expect(key?.symbol).toBe('diamond')
    const sensitive = nodes.find((n) => n.id === 'prop:Customer.contact')
    expect(sensitive?.itemStyle?.borderWidth).toBe(2)
    const links = graph.props('links')
    // 18 has-property + 3 relation + 2 ref(currency→? 不在类中;customer/quote ref 均不在类中)
    expect(links.filter((l) => l.labelText === 'ref')).toHaveLength(0)
    expect(links.length).toBe(18 + 3)
  })

  it('领域筛选:节点与边同步收敛', async () => {
    const { wrapper } = await mountView()
    await wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', '制造')
    await flushPromises()
    const nodes = graphOf(wrapper).props('nodes')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].refObject).toBe('Material')
    // Material 无出边,且指向它的关系因目标过滤被保留(source 不在集合中则被剔除)
    expect(graphOf(wrapper).props('links')).toHaveLength(0)
  })

  it('点类节点在当前页就地展开/收起该类属性层', async () => {
    const { wrapper, router } = await mountView()
    const graph = graphOf(wrapper)
    graph.vm.$emit('node-click', {
      id: 'class:Customer', name: '客户', kind: 'class', category: 0, refObject: 'Customer',
    })
    await flushPromises()
    // 就地展开:Customer 的 4 个属性节点出现,其他类不展开;不跳转
    let nodes = graph.props('nodes')
    const props1 = nodes.filter((n) => n.kind === 'property')
    expect(props1).toHaveLength(4)
    expect(props1.every((n) => n.refObject === 'Customer')).toBe(true)
    expect(router.currentRoute.value.path).toBe('/ontology/topology')
    // has-property 边同步出现
    expect(graph.props('links').length).toBe(3 + 4)
    // 再次点击:收起
    graph.vm.$emit('node-click', {
      id: 'class:Customer', name: '客户', kind: 'class', category: 0, refObject: 'Customer',
    })
    await flushPromises()
    nodes = graph.props('nodes')
    expect(nodes.filter((n) => n.kind === 'property')).toHaveLength(0)
  })

  it('点属性节点在当前页弹详情抽屉,不跳转属性页', async () => {
    const { wrapper, router } = await mountView()
    const graph = graphOf(wrapper)
    // 先展开 Customer 属性层,再点其中的属性节点
    graph.vm.$emit('node-click', {
      id: 'class:Customer', name: '客户', kind: 'class', category: 0, refObject: 'Customer',
    })
    await flushPromises()
    graph.vm.$emit('node-click', {
      id: 'prop:Customer.contact', name: 'contact', kind: 'property',
      category: 1, refObject: 'Customer', propName: 'contact',
    })
    await flushPromises()
    const target = [...document.querySelectorAll('[data-testid="prop-detail-target"]')].at(-1)
    expect(target?.textContent).toContain('Customer')
    expect(target?.textContent).toContain('contact')
    expect([...document.querySelectorAll('[data-testid="prop-mappings-table"]')].at(-1)?.textContent)
      .toContain('CUSTOMER.CONTACT_EMAIL')
    // 全程未离开拓扑页
    expect(router.currentRoute.value.path).toBe('/ontology/topology')
  })
})
