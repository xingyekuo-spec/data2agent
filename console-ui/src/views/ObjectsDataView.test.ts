import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { HttpResponse, http } from '@/test/http'
import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { createAppRouter } from '@/router'
import { server } from '@/test/fetch-stub'
import { baseFixture } from '@/test/fixtures/base'
import ObjectsDataView from './ObjectsDataView.vue'

async function mountView(query: Record<string, string> = {}): Promise<{
  wrapper: ReturnType<typeof mount>
  router: ReturnType<typeof createAppRouter>
}> {
  const pinia: Pinia = createPinia()
  const router = createAppRouter(createMemoryHistory())
  await router.push({ path: '/data/objects', query })
  await router.isReady()
  // attachTo 使 el-drawer Teleport 能渲染到真实 DOM
  const wrapper = mount(ObjectsDataView, {
    attachTo: document.body,
    global: { plugins: [pinia, [ElementPlus, { locale: zhCn }], router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('ObjectsDataView(对象数据)', () => {
  beforeEach(() => setScenario('healthy'))

  it('对象目录展示 published object_version', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="obj-version"]').text()).toContain('ov-cust-1')
  })

  it('对象未物化(409):具名错误视图', async () => {
    server.use(
      http.get('*/api/objects/:object', () =>
        HttpResponse.json({ detail: "对象 'Customer' 尚未物化" }, { status: 409 })),
    )
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="browse-Customer"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('409')
  })

  it('对象 warnings/truncations 可见', async () => {
    server.use(
      http.get('*/api/objects/:object', ({ request }) => {
        const url = new URL(request.url)
        const limit = Number(url.searchParams.get('limit') ?? 50)
        const offset = Number(url.searchParams.get('offset') ?? 0)
        return HttpResponse.json({
          ...baseFixture.objectRows,
          rows: baseFixture.objectRows.rows.slice(offset, offset + limit),
          offset,
          limit,
          warnings: ['对象列 NOTE 分类未知,按未确认处理展示'],
          truncations: [{ row_index: 0, fields: ['name'] }],
        })
      }),
    )
    const { wrapper } = await mountView()
    await wrapper.find('[data-testid="browse-Customer"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="obj-warnings"]').text()).toContain('NOTE')
    expect(wrapper.find('[data-testid="obj-truncations"]').text()).toContain('#0(name)')
  })

  it('route query 恢复 resource/search/page', async () => {
    const { wrapper, router } = await mountView({
      object: 'Customer',
      q: 'C-001',
      page: '2',
    })
    expect(wrapper.find('[data-testid="obj-table"]').exists()).toBe(true)
    expect((router.currentRoute.value.query.object)).toBe('Customer')
    expect(wrapper.text()).toContain('Customer · 排序')
    expect(wrapper.find('[data-testid="obj-pager"]').text()).toContain('共 1 条')
  })
})
