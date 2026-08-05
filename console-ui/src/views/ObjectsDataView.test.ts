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

// 点击目录行(详情入口一律行点击,规范 §3.2-3)
async function clickCatalogRow(wrapper: ReturnType<typeof mount>, text: string): Promise<void> {
  const row = wrapper.findAll('[data-testid="obj-catalog"] .el-table__row')
    .find((r) => r.text().includes(text))
  expect(row, `目录应包含行 ${text}`).toBeTruthy()
  await row!.trigger('click')
  await flushPromises()
}

// 抽屉 teleport 到 body 且历史用例的抽屉不卸载,取最后一个(最新打开)
function inDrawer(selector: string): Element | undefined {
  return [...document.querySelectorAll(selector)].at(-1)
}

describe('ObjectsDataView(对象数据)', () => {
  beforeEach(() => setScenario('healthy'))

  it('对象目录展示 published object_version', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.find('[data-testid="obj-version"]').text()).toContain('ov-cust-1')
  })

  it('对象未物化(409):抽屉内具名错误视图', async () => {
    server.use(
      http.get('*/api/objects/:object', () =>
        HttpResponse.json({ detail: "对象 'Customer' 尚未物化" }, { status: 409 })),
    )
    const { wrapper } = await mountView()
    await clickCatalogRow(wrapper, 'Customer')
    // el-drawer 不透传 data-testid,直接查抽屉内容(teleport 到 body)
    const errorState = inDrawer('[data-testid="error-state"]')
    expect(errorState).toBeTruthy()
    expect(errorState!.textContent).toContain('409')
  })

  it('对象 warnings/truncations 在浏览抽屉内可见', async () => {
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
    await clickCatalogRow(wrapper, 'Customer')
    expect(inDrawer('[data-testid="obj-warnings"]')?.textContent).toContain('NOTE')
    expect(inDrawer('[data-testid="obj-truncations"]')?.textContent).toContain('#0(name)')
  })

  it('route query 恢复 resource/search/page 并打开浏览抽屉', async () => {
    const { router } = await mountView({
      object: 'Customer',
      q: 'C-001',
      page: '2',
    })
    expect(inDrawer('[data-testid="obj-table"]')).toBeTruthy()
    expect((router.currentRoute.value.query.object)).toBe('Customer')
    expect(inDrawer('[data-testid="obj-drawer-target"]')?.textContent).toContain('Customer')
    expect(inDrawer('[data-testid="obj-table"]')?.textContent).toBeTruthy()
    expect(inDrawer('.toolbar-meta')?.textContent).toContain('排序')
    expect(inDrawer('[data-testid="obj-pager"]')?.textContent).toContain('共 1 条')
  })
})
