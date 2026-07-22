import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ValidationView from './ValidationView.vue'

vi.mock('@/api/services', () => ({
  postValidationRun: vi.fn(),
  getValidationReport: vi.fn(),
}))

import { getValidationReport, postValidationRun } from '@/api/services'

const report = {
  report_schema_version: 1 as const,
  run_id: 81,
  source: 'digiwin_e10',
  overall_status: 'warning' as const,
  started_at: '2026-07-22T10:00:00+08:00',
  finished_at: '2026-07-22T10:00:01+08:00',
  deployment: { config_loaded: true },
  dataset_version: 'ds_81',
  template_version: 'v0.3',
  summary: { check_count: 13, pass_count: 11, warning_count: 1, fail_count: 0, skipped_count: 1 },
  checks: [{
    check_id: 'mapping_preview', title: '映射治理状态', status: 'warning' as const,
    blocking: true, summary: '存在草稿映射。',
    started_at: '2026-07-22T10:00:00+08:00', finished_at: '2026-07-22T10:00:01+08:00',
    detail: {}, evidence: [],
  }],
}

describe('ValidationView(M6)', () => {
  beforeEach(() => {
    vi.mocked(postValidationRun).mockReset()
    vi.mocked(getValidationReport).mockReset()
  })

  it('starts the readonly run, renders the frozen report, and exposes JSON download', async () => {
    vi.mocked(postValidationRun).mockResolvedValue({
      ok: true, data: { run_id: 81, overall_status: 'warning', report_path: '/api/validation/runs/81' }, response: new Response(),
    })
    vi.mocked(getValidationReport).mockResolvedValue({ ok: true, data: report, response: new Response() })
    const wrapper = mount(ValidationView, { global: { plugins: [ElementPlus] } })
    expect(wrapper.text()).toContain('不执行同步、发布或写回')
    await wrapper.find('[data-testid="validation-run"]').trigger('click')
    await flushPromises()
    expect(postValidationRun).toHaveBeenCalledWith(true)
    expect(getValidationReport).toHaveBeenCalledWith(81)
    expect(wrapper.find('[data-testid="validation-report"]').text()).toContain('ds_81')
    expect(wrapper.find('[data-testid="validation-report"]').text()).toContain('存在草稿映射')
    expect(wrapper.find('[data-testid="validation-download"]').attributes('href')).toBe('/api/validation/runs/81/report.json')
  })
})
