import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from '@/router'
import App from '@/App.vue'

describe('App', () => {
  it('挂载后以真实模式渲染仪表盘壳', async () => {
    const router = createAppRouter(createMemoryHistory())
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, {
      global: { plugins: [router, createPinia(), ElementPlus] },
      attachTo: document.body,
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="env-badge"]').text()).toBe('REAL')
    expect(wrapper.find('[data-testid="scenario-switcher"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('仪表盘')

    wrapper.unmount()
  })
})
