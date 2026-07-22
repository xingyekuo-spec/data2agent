import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { NAV_GROUPS, NAV_ITEMS, createAppRouter } from '@/router'
import { setScenario } from '@/mocks/scenario'
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

    // 顶栏持续展示环境模式(jsdom 测试环境 MODE=mock)
    const badge = wrapper.find('[data-testid="env-badge"]')
    expect(badge.exists()).toBe(true)
    expect(['MOCK', 'REAL']).toContain(badge.text())

    // 顶栏为白底 + 下边线(参考 UI)
    const header = wrapper.find('.app-shell__header')
    expect(header.exists()).toBe(true)

    // 两级菜单:4 个分组标题 + 当前导航页面项
    const groupTitles = wrapper.findAll('.sidemenu__group-title')
    expect(groupTitles.map((g) => g.text())).toEqual(NAV_GROUPS.map((g) => g.title))
    const items = wrapper.findAll('.el-menu-item')
    expect(items).toHaveLength(NAV_ITEMS.length)
    expect(items[items.length - 1]?.text()).toContain('日志')

    // 顶栏白底 + 当前页面标题(参考 UI);路由出口渲染仪表盘
    expect(wrapper.find('[data-testid="topbar-title"]').text()).toBe('仪表盘')
    expect(wrapper.text()).toContain('仪表盘')
  })

  it('切换 Mock 场景后当前视图重挂载并重新取数', async () => {
    setScenario('healthy')
    const wrapper = await mountLayout('/')
    // healthy:仪表盘成功渲染摘要卡与口径说明
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('raw_rows')

    // 切到 unknown-error:刷新失败 → 标记 + 旧数据保留(M3 语义)
    setScenario('unknown-error')
    await flushPromises()
    expect(wrapper.find('[data-testid="refresh-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('raw_rows')

    // 切回 healthy:标记清除,数据正常
    setScenario('healthy')
    await flushPromises()
    expect(wrapper.find('[data-testid="refresh-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('raw_rows')
  })
})
