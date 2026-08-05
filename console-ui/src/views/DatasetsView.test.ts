import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import DatasetsView from './DatasetsView.vue'

async function mountView(query: Record<string, string> = {}): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/data/datasets', query })
  await router.isReady()
  // attachTo 使 el-drawer Teleport 能渲染到真实 DOM
  const wrapper = mount(DatasetsView, {
    attachTo: document.body,
    global: { plugins: [pinia, [ElementPlus, { locale: zhCn }], router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('DatasetsView(数据集版本)', () => {
  beforeEach(() => setScenario('healthy'))

  it('列表区分待发布/已发布,可发布与回滚', async () => {
    const { wrapper } = await mountView()
    const table = wrapper.find('[data-testid="datasets-table"]')
    expect(table.exists()).toBe(true)
    expect(table.text()).toContain('待发布')
    expect(table.text()).toContain('已发布')
    expect(table.text()).toContain('已退役')
    expect(table.text()).toContain('失败')
    expect(wrapper.find('[data-testid="dataset-publish-ds-20260718-095000-e5f6"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="dataset-rollback-ds-20260718-091100-a1b2"]').exists()).toBe(true)

    await wrapper.find('[data-testid="dataset-publish-ds-20260718-095000-e5f6"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="dataset-action-result"]').text()).toContain('ds-20260718-095000-e5f6')

    // 行点击开详情抽屉(详情入口一律行点击,规范 §3.2-3)
    const row = wrapper.findAll('[data-testid="datasets-table"] .el-table__row')
      .find((r) => r.text().includes('ds-20260718-095000-e5f6'))
    expect(row).toBeTruthy()
    await row!.trigger('click')
    await flushPromises()
    // el-drawer teleport 到 body
    const objectsTable = [...document.querySelectorAll('[data-testid="dataset-objects-table"]')].at(-1)
    expect(objectsTable).toBeTruthy()
    expect(objectsTable!.textContent).toContain('Customer')
    expect(objectsTable!.textContent).toContain('built')
  })

  it('stage-only apply 返回 published=false', async () => {
    const { wrapper } = await mountView()
    const toggle = wrapper.find('[data-testid="stage-only-toggle"] input')
    await toggle.setValue(true)
    await wrapper.find('[data-testid="apply-run"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="apply-result"]').text()).toContain('published=false')
  })
})
