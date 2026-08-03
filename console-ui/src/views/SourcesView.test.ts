import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import SourcesView from './SourcesView.vue'

function mountSources() {
  return mount(SourcesView, {
    global: { plugins: [createPinia(), ElementPlus] },
    attachTo: document.body,
  })
}

describe('SourcesView(数据源管理)', () => {
  beforeEach(() => setScenario('healthy'))

  it('healthy:渲染数据源卡片(类型/接入方式/表数/最近接入)', async () => {
    const wrapper = mountSources()
    await flushPromises()

    const card = wrapper.find('[data-testid="source-card-digiwin_e10"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('鼎捷 E10')
    expect(card.text()).toContain('ERP')
    expect(card.text()).toContain('本地直连')
    expect(card.text()).toContain('2 张表')
    expect(card.text()).toContain('最近接入')
    wrapper.unmount()
  })

  it('点击卡片:抽屉展示表级水位与最近接入记录', async () => {
    const wrapper = mountSources()
    await flushPromises()

    await wrapper.find('[data-testid="source-card-digiwin_e10"]').trigger('click')
    await flushPromises()

    const drawer = document.body.querySelector('[data-testid="source-detail-drawer"]')
    expect(drawer).not.toBeNull()
    const tables = document.body.querySelector('[data-testid="source-tables"]')
    expect(tables?.textContent).toContain('CUSTOMER')
    expect(tables?.textContent).toContain('2026-07-18 08:30:00')
    expect(tables?.textContent).toContain('24')
    const runs = document.body.querySelector('[data-testid="source-runs"]')
    expect(runs?.textContent).toContain('42')
    wrapper.unmount()
  })

  it('添加数据源:ERP 已支持,其余类型规划中', async () => {
    const wrapper = mountSources()
    await flushPromises()

    await wrapper.find('[data-testid="source-add"]').trigger('click')
    await flushPromises()

    const erp = document.body.querySelector('[data-testid="source-type-erp"]')
    expect(erp?.textContent).toContain('已支持')
    expect(erp?.textContent).toContain('中间机')
    for (const t of ['mes', 'srm', 'excel', 'api']) {
      const item = document.body.querySelector(`[data-testid="source-type-${t}"]`)
      expect(item?.textContent).toContain('规划中')
    }
    wrapper.unmount()
  })

  it('empty-install:显示空态引导,不伪造数据', async () => {
    setScenario('empty-install')
    const wrapper = mountSources()
    await flushPromises()

    expect(wrapper.find('[data-testid="sources-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('尚未接入数据源')
    expect(wrapper.find('[data-testid="source-grid"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('unknown-error:显示 ErrorState,可重试', async () => {
    setScenario('unknown-error')
    const wrapper = mountSources()
    await flushPromises()

    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="source-grid"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('接入信息:端点/协议/脱敏 Token,点显示明文出全文', async () => {
    const wrapper = mountSources()
    await flushPromises()

    await wrapper.find('[data-testid="connection-info"]').trigger('click')
    await flushPromises()

    const table = document.body.querySelector('[data-testid="connection-info-table"]')
    expect(table?.textContent).toContain('http://192.168.1.10:8850')
    expect(table?.textContent).toContain('tok-…56')
    expect(table?.textContent).toContain('2 / 3')

    // 配置片段默认不含明文
    const snippet = document.body.querySelector('[data-testid="connection-snippet"]')
    expect(snippet?.textContent).toContain('sink:')
    expect(snippet?.textContent).not.toContain('tok-abcdef-123456')

    // 显示明文后片段含 Token
    const revealBtn = document.body.querySelector('[data-testid="token-reveal"]') as HTMLElement
    revealBtn.click()
    await flushPromises()
    expect(document.body.querySelector('[data-testid="connection-snippet"]')?.textContent)
      .toContain('D2A_INGEST_TOKEN=tok-abcdef-123456')
    wrapper.unmount()
  })
})
