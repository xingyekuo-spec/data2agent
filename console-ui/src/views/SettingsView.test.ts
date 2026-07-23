import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import SettingsView from './SettingsView.vue'

vi.mock('@/api/services', () => ({
  getConfig: vi.fn(),
  postConfig: vi.fn(),
  validateConfig: vi.fn(),
}))

import { getConfig } from '@/api/services'

describe('SettingsView', () => {
  it('displays the version returned by the running platform service', async () => {
    vi.mocked(getConfig).mockResolvedValue({
      ok: true,
      data: {
        app_version: '0.4.0',
        build_version: 'manual-808aaaa',
        needs_setup: false,
        templates: 'C:/d2a/app/templates',
        landing: 'C:/d2a/data/factory.sqlite',
      },
      response: new Response(),
    })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="settings-app-version"]').text()).toBe(
      '当前应用版本：v0.4.0（构建 manual-808aaaa）',
    )
  })
})
