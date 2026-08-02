import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createAppRouter } from '@/router'
import { getToken } from '@/api/client'
import { setScenario } from '@/test/scenario'
import SetupView from './SetupView.vue'

async function mountSetup() {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/setup')
  await router.isReady()
  const wrapper = mount(SetupView, {
    global: { plugins: [router, createPinia(), ElementPlus] },
  })
  await flushPromises()
  // 成功/已配置路径会 router.replace('/'),目标视图为懒加载,须等动态 import 落地
  await vi.dynamicImportSettled()
  await flushPromises()
  return { wrapper, router }
}

describe('SetupView(首次配置)', () => {
  beforeEach(() => setScenario('empty-install'))

  it('needs_setup:渲染表单与安装目录提示', async () => {
    const { wrapper } = await mountSetup()
    expect(wrapper.text()).toContain('平台首次配置')
    expect(wrapper.text()).toContain('安装目录:/home/d2a')
    expect(wrapper.find('[data-testid="setup-ingest-token"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="setup-console-token"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="setup-mcp-token"]').exists()).toBe(true)
  })

  it('提交合法 Token:登录并跳转控制台首页', async () => {
    const { wrapper, router } = await mountSetup()
    await wrapper.find('[data-testid="setup-ingest-token"]').setValue('ingest-secret')
    await wrapper.find('[data-testid="setup-console-token"]').setValue('console-secret')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await vi.dynamicImportSettled()
    await flushPromises()

    // 成功:用 console_token 完成登录,跳转 /
    expect(getToken()).toBe('console-secret')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('提交空 Token:展示字段级错误,不跳转', async () => {
    const { wrapper, router } = await mountSetup()
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Token 不能为空')
    expect(getToken()).toBeNull()
    expect(router.currentRoute.value.path).toBe('/setup')
  })

  it('已完成配置(needs_setup=false):自动跳转首页', async () => {
    setScenario('healthy')
    const { router } = await mountSetup()
    expect(router.currentRoute.value.path).toBe('/')
  })
})
