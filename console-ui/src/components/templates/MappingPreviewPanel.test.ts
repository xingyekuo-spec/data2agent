/**
 * MappingPreviewPanel(M3-T07):idle/loading/empty/success/warning/error/unauthorized,
 * 临时草稿复制、无保存/发布入口、提交禁用。
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { HttpResponse, http } from 'msw'
import { createPinia, type Pinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import MappingPreviewPanel from './MappingPreviewPanel.vue'
import {
  mappingPreviewCurrent,
  mappingPreviewDraft,
  mappingPreviewEmpty,
  mappingPreviewUnauthorized,
  mappingPreviewDraftInvalid,
} from '@/mocks/fixtures/mapping-preview'
import { setScenario } from '@/mocks/scenario'
import { server } from '@/test/setup'
import type { components } from '@/types/api'

type TemplateBinding = components['schemas']['TemplateBinding']

const customerBinding: TemplateBinding = {
  source: 'digiwin_e10',
  tables: ['CUSTOMER', 'CURRENCY'],
  status: 'verified',
  enabled: true,
  key_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
  field_map: {
    customer_code: 'CUSTOMER.CUSTOMER_CODE',
    name: 'CUSTOMER.CUSTOMER_NAME',
  },
  watermark: 'CUSTOMER.LAST_MODIFIED_DATE',
  notes: 'fixture notes',
}

async function mountPanel(bindings: TemplateBinding[] = [customerBinding]) {
  const pinia: Pinia = createPinia()
  const wrapper = mount(MappingPreviewPanel, {
    props: { objectName: 'Customer', bindings },
    global: { plugins: [pinia, ElementPlus] },
  })
  await flushPromises()
  return wrapper
}

describe('MappingPreviewPanel(M3-T07)', () => {
  beforeEach(() => setScenario('healthy'))

  it('renders idle controls without save/publish actions', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.find('[data-testid="mapping-preview-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="preview-source"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="preview-submit"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('只读试算')
    expect(wrapper.text()).not.toContain('保存模板')
    expect(wrapper.text()).not.toContain('应用草稿')
    expect(wrapper.text()).not.toContain('发布候选')
  })

  it('defaults source to current binding and opens drawer on submit', async () => {
    const wrapper = await mountPanel()
    const source = wrapper.find('[data-testid="preview-source"]').element as HTMLSelectElement
    expect(source.value).toBe('digiwin_e10')

    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()

    const drawer = wrapper.find('[data-testid="mapping-preview-drawer"]')
    expect(drawer.exists()).toBe(true)
    expect(wrapper.find('[data-testid="preview-readonly-banner"]').text())
      .toContain('只读预览，不会保存/发布')
    expect(wrapper.find('[data-testid="preview-summary"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('fp-preview-current')
    expect(wrapper.find('[data-testid="preview-rows-table"]').exists()).toBe(true)
  })

  it('copies current binding into in-browser draft JSON', async () => {
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-use-draft"]').trigger('click')
    await flushPromises()

    const ta = wrapper.find('[data-testid="preview-draft-text"]')
    expect(ta.exists()).toBe(true)
    const parsed = JSON.parse((ta.element as HTMLTextAreaElement).value)
    expect(parsed.tables).toEqual(['CUSTOMER', 'CURRENCY'])
    expect(parsed.key_map.customer_code).toBe('CUSTOMER.CUSTOMER_CODE')
    expect(parsed.notes).toBe('fixture notes')
    expect(parsed).not.toHaveProperty('status')
    expect(parsed).not.toHaveProperty('source')
  })

  it('starts from empty draft when object has no binding for source', async () => {
    const wrapper = await mountPanel([])
    await wrapper.find('[data-testid="preview-use-draft"]').trigger('click')
    await flushPromises()
    const parsed = JSON.parse(
      (wrapper.find('[data-testid="preview-draft-text"]').element as HTMLTextAreaElement).value,
    )
    expect(parsed.tables).toEqual([])
    expect(parsed.field_map).toEqual({})
  })

  it('treats disabled binding as no current and auto-opens empty draft', async () => {
    const disabled: TemplateBinding = {
      ...customerBinding,
      enabled: false,
      status: 'disabled',
      notes: 'disabled-notes',
    }
    const wrapper = await mountPanel([disabled])
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-draft-text"]').exists()).toBe(true)
    const parsed = JSON.parse(
      (wrapper.find('[data-testid="preview-draft-text"]').element as HTMLTextAreaElement).value,
    )
    // 无 current → 空草稿,不得把 disabled binding 当 current 提交
    expect(parsed.tables).toEqual([])
    expect(wrapper.text()).toContain('已停用·无当前绑定')
    const submit = wrapper.find('[data-testid="preview-submit"]')
    expect((submit.element as HTMLButtonElement).disabled).toBe(false)
  })

  it('resets draft text when switching source while already drafting', async () => {
    const other: TemplateBinding = {
      ...customerBinding,
      source: 'digiwin_yifei',
      tables: ['COPMA'],
      notes: 'yifei-notes',
      field_map: { customer_code: 'COPMA.CUSTOMER_CODE' },
      key_map: { customer_code: 'COPMA.CUSTOMER_CODE' },
    }
    const wrapper = await mountPanel([customerBinding, other])
    await wrapper.find('[data-testid="preview-use-draft"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="preview-draft-text"]').setValue(
      JSON.stringify({ tables: ['STALE'], key_map: {}, field_map: {}, derived: {}, notes: 'stale' }, null, 2),
    )
    await flushPromises()

    const select = wrapper.find('[data-testid="preview-source"]')
    await select.setValue('digiwin_yifei')
    await select.trigger('change')
    await flushPromises()

    const parsed = JSON.parse(
      (wrapper.find('[data-testid="preview-draft-text"]').element as HTMLTextAreaElement).value,
    )
    expect(parsed.tables).toEqual(['COPMA'])
    expect(parsed.notes).toBe('yifei-notes')
    expect(parsed.notes).not.toBe('stale')
  })

  it('allows submitting a new draft when source comes from allowedSources', async () => {
    server.use(
      http.post('*/api/mappings/:object/preview', async ({ request }) => {
        const body = await request.json() as { source?: string; draft_binding?: unknown }
        expect(body.source).toBe('crm_export')
        expect(body.draft_binding).toBeTruthy()
        return HttpResponse.json({
          ...mappingPreviewDraft,
          source: 'crm_export',
          mode: 'draft',
          current: null,
          current_binding_hash: null,
          diff: {
            state: 'unavailable',
            reason: 'no_current_binding',
            summary: { rows_changed: 0, status_changed: 0, fields_changed: 0 },
            rows: [],
          },
        })
      }),
    )
    const pinia: Pinia = createPinia()
    const wrapper = mount(MappingPreviewPanel, {
      props: {
        objectName: 'Customer',
        bindings: [],
        allowedSources: ['crm_export', 'digiwin_e10'],
      },
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const source = wrapper.find('[data-testid="preview-source"]').element as HTMLSelectElement
    expect(source.value).toBe('crm_export')
    expect(wrapper.find('[data-testid="preview-draft-text"]').exists()).toBe(true)

    const submit = wrapper.find('[data-testid="preview-submit"]')
    expect((submit.element as HTMLButtonElement).disabled).toBe(false)
    await submit.trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="mapping-preview-drawer"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('draft')
    expect(wrapper.text()).toContain('fp-preview-draft')
  })

  it('shows local JSON syntax hint without calling semantics', async () => {
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-use-draft"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="preview-draft-text"]').setValue('{not-json')
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-json-hint"]').text()).toContain('合法 JSON')
  })

  it('shows empty sample state with zero summary', async () => {
    server.use(
      http.post('*/api/mappings/:object/preview', () =>
        HttpResponse.json(mappingPreviewEmpty),
      ),
    )
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('样本为空')
    expect(wrapper.find('[data-testid="preview-summary"]').exists()).toBe(true)
  })

  it('shows warnings from draft preview response', async () => {
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-use-draft"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-warnings"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('临时草稿不会保存或发布')
    expect(wrapper.text()).toContain('contact')
  })

  it('shows unauthorized guidance on 401', async () => {
    setScenario('preview-forbidden')
    // override to unauthorized 401 body for this case
    server.use(
      http.post('*/api/mappings/:object/preview', () =>
        HttpResponse.json(mappingPreviewUnauthorized, { status: 401 }),
      ),
    )
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-unauthorized"]').exists()).toBe(true)
  })

  it('shows error state with retry for draft_invalid', async () => {
    server.use(
      http.post('*/api/mappings/:object/preview', () =>
        HttpResponse.json(mappingPreviewDraftInvalid, { status: 422 }),
      ),
    )
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="error-state"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('422')
  })

  it('disables submit while loading', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.post('*/api/mappings/:object/preview', async () => {
        await gate
        return HttpResponse.json(mappingPreviewCurrent)
      }),
    )
    const wrapper = await mountPanel()
    const click = wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    expect(
      (wrapper.find('[data-testid="preview-submit"]').element as HTMLButtonElement).disabled
      || wrapper.find('[data-testid="preview-submit"]').attributes('disabled') !== undefined,
    ).toBeTruthy()
    expect(wrapper.find('[data-testid="loading-state"]').exists()).toBe(true)
    release()
    await click
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-summary"]').exists()).toBe(true)
  })

  it('marks prior result stale when sample changes after success', async () => {
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="preview-limit"]').setValue('10')
    await wrapper.find('[data-testid="preview-limit"]').trigger('change')
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-stale"]').exists()).toBe(true)
  })

  it('renders draft fixture panels and never exposes forbidden write labels', async () => {
    server.use(
      http.post('*/api/mappings/:object/preview', () =>
        HttpResponse.json({
          ...mappingPreviewDraft,
          candidate: {
            ...mappingPreviewDraft.candidate,
            enum_gaps: [{ field: 'status', source_value: 'X', count: 2 }],
            business_key_issues: { missing: 1, duplicate: 0, scope: 'sample' },
            derived_coverage: [{
              field: 'tier',
              eligible_rows: 1,
              matched_rows: 1,
              default_hits: 0,
              unmatched_rows: 0,
              rules_hit: 1,
              rules_total: 1,
              row_coverage: 1,
              rules: [{ index: 0, hit_count: 1 }],
            }],
          },
        }),
      ),
    )
    const wrapper = await mountPanel()
    await wrapper.find('[data-testid="preview-use-draft"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="preview-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="preview-enum-gaps"]').text()).toContain('status')
    expect(wrapper.find('[data-testid="preview-key-issues"]').text()).toContain('缺失: 1')
    expect(wrapper.find('[data-testid="preview-derived-coverage"]').text()).toContain('tier')
    expect(wrapper.html()).not.toContain('保存模板')
    expect(wrapper.html()).not.toContain('发布候选')
  })
})
