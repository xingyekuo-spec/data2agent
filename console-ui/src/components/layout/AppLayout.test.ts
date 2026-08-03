import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { NAV_GROUPS, NAV_ITEMS, createAppRouter } from '@/router'
import AppLayout from './AppLayout.vue'

async function mountLayout(path = '/') {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(AppLayout, {
    global: { plugins: [router, createPinia(), ElementPlus] },
  })
  await flushPromises()
  return wrapper
}

describe('AppLayout', () => {
  it('渲染顶栏环境标识、分组两级菜单与路由出口', async () => {
    const wrapper = await mountLayout('/')

    // 顶栏持续展示环境模式(产品恒为 REAL)
    const badge = wrapper.find('[data-testid="env-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('REAL')

    // 顶栏为白底 + 下边线(参考 UI)
    const header = wrapper.find('.app-shell__header')
    expect(header.exists()).toBe(true)

    // 两级菜单:4 个分组标题 + 当前导航页面项
    const groupTitles = wrapper.findAll('.sidemenu__group-title')
    expect(groupTitles.map((g) => g.text())).toEqual(NAV_GROUPS.map((g) => g.title))
    const items = wrapper.findAll('.el-menu-item')
    expect(items).toHaveLength(NAV_ITEMS.length)
    expect(items[items.length - 1]?.text()).toContain('验收报告')

    // 顶栏白底 + 当前页面标题(参考 UI);路由出口渲染仪表盘
    expect(wrapper.find('[data-testid="topbar-title"]').text()).toBe('仪表盘')
    expect(wrapper.text()).toContain('仪表盘')
  })
})
