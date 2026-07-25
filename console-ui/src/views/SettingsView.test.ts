import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import SettingsView from './SettingsView.vue'

vi.mock('@/api/services', () => ({
  getConfig: vi.fn(),
  postConfig: vi.fn(),
  validateConfig: vi.fn(),
  getUpdateStatus: vi.fn(),
  postUpdateCheck: vi.fn(),
  postUpdateDownload: vi.fn(),
}))

import { getConfig, getUpdateStatus, postUpdateCheck } from '@/api/services'

function mockConfig(): void {
  vi.mocked(getConfig).mockResolvedValue({
    ok: true,
    data: {
      app_version: '0.5.0',
      build_version: 'manual-808aaaa',
      needs_setup: false,
      templates: 'C:/d2a/app/templates',
      landing: 'C:/d2a/data/factory.sqlite',
    },
    response: new Response(),
  })
}

function mockUpdateStatus(overrides: Record<string, unknown> = {}): void {
  vi.mocked(getUpdateStatus).mockResolvedValue({
    ok: true,
    data: {
      available: true,
      phase: 'idle',
      current_version: 'v0.5.1',
      ...overrides,
    } as never,
    response: new Response(),
  })
}

describe('SettingsView', () => {
  it('displays the version returned by the running platform service', async () => {
    mockConfig()
    mockUpdateStatus({ available: false })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="settings-app-version"]').text()).toBe(
      '当前应用版本：v0.5.0（构建 manual-808aaaa）',
    )
  })

  it('hides update card when not a portable install', async () => {
    mockConfig()
    mockUpdateStatus({ available: false })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="update-card"]').exists()).toBe(false)
  })

  it('shows ready instructions with bat path when update staged', async () => {
    mockConfig()
    mockUpdateStatus({
      phase: 'ready',
      target_version: 'v0.6.0',
      bat_path: 'C:\\d2a\\升级.bat',
    })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const ready = wrapper.find('[data-testid="update-ready"]')
    expect(ready.exists()).toBe(true)
    expect(ready.text()).toContain('升级.bat')
    expect(ready.text()).toContain('v0.6.0')
  })

  it('offers download button after a successful check', async () => {
    mockConfig()
    mockUpdateStatus()
    vi.mocked(postUpdateCheck).mockResolvedValue({
      ok: true,
      data: {
        ok: true,
        current_version: 'v0.5.1',
        latest_version: 'v0.6.0',
        update_available: true,
        protocol_ok: true,
      } as never,
      response: new Response(),
    })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.find('[data-testid="update-check"]').trigger('click')
    await flushPromises()
    const download = wrapper.find('[data-testid="update-download"]')
    expect(download.exists()).toBe(true)
    expect(download.text()).toContain('v0.6.0')
  })

  it('shows blocked reason instead of download when protocol incompatible', async () => {
    mockConfig()
    mockUpdateStatus()
    vi.mocked(postUpdateCheck).mockResolvedValue({
      ok: true,
      data: {
        ok: true,
        current_version: 'v0.5.1',
        latest_version: 'v0.6.0',
        update_available: true,
        protocol_ok: false,
        blocked_reason: '新版本不再支持现场中间机使用的推送协议(v2),需先升级中间机',
      } as never,
      response: new Response(),
    })
    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.find('[data-testid="update-check"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="update-download"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="update-blocked"]').text()).toContain('中间机')
  })
})
