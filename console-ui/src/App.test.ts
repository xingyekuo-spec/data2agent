import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from '@/router'
import { getScenario } from '@/mocks/scenario'
import App from '@/App.vue'

/**
 * 用户操作全链路复现:挂载 App(含 ScenarioSwitcher),
 * 通过切换器的 <select> 切场景,断言当前页面立即变化。
 */
describe('场景切换全链路(模拟用户操作)', () => {
  it('在仪表盘切换 healthy → unknown-error → healthy,页面立即变化', async () => {
    const router = createAppRouter(createMemoryHistory())
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, {
      global: { plugins: [router, createPinia(), ElementPlus] },
      attachTo: document.body,
    })
    await flushPromises()

    expect(getScenario()).toBe('healthy')
    expect(wrapper.text()).toContain('raw_rows')

    const select = wrapper.find('[data-testid="scenario-switcher"] select')
    expect(select.exists()).toBe(true)
    await select.setValue('unknown-error')
    await flushPromises()

    expect(getScenario()).toBe('unknown-error')
    // 刷新失败:标记可见 + 旧数据保留,不是整屏错误
    expect(wrapper.find('[data-testid="refresh-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('raw_rows')

    await select.setValue('healthy')
    await flushPromises()
    expect(wrapper.find('[data-testid="refresh-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('raw_rows')

    wrapper.unmount()
  })
})
