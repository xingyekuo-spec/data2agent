import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { server } from '@/test/fetch-stub'
import { http } from '@/test/http'
import { setScenario } from '@/test/scenario'
import LogsView from './LogsView.vue'

function mountLogs() {
  return mount(LogsView, { global: { plugins: [ElementPlus] } })
}

describe('LogsView', () => {
  beforeEach(() => setScenario('healthy'))

  it('healthy:初始加载 console 日志并显示读取状态', async () => {
    const wrapper = mountLogs()
    await flushPromises()
    expect(wrapper.find('[data-testid="logs-output"]').text()).toContain('sync done rows=1284')
    expect(wrapper.find('[data-testid="logs-status"]').text()).toBe('已读取')
  })

  it('level 过滤:输入 ERROR 回车后带查询参数重新请求', async () => {
    let captured: URL | null = null
    server.use(
      http.get('*/api/logs', ({ request }) => {
        captured = new URL(request.url)
        return new Response(JSON.stringify({ ok: true, text: 'ERROR boom' }), {
          headers: { 'Content-Type': 'application/json' },
        })
      }),
    )
    const wrapper = mountLogs()
    await flushPromises()

    const input = wrapper.find('[data-testid="logs-level-input"]')
    await input.setValue('ERROR')
    await input.trigger('keyup.enter')
    await flushPromises()

    expect(captured).not.toBeNull()
    expect(captured!.searchParams.get('level')).toBe('ERROR')
    expect(wrapper.find('[data-testid="logs-output"]').text()).toContain('ERROR boom')
  })

  it('查询失败:显示 ErrorState,不渲染日志区', async () => {
    setScenario('unknown-error')
    const wrapper = mountLogs()
    await flushPromises()
    expect(wrapper.find('[data-testid="logs-output"]').exists()).toBe(false)
    expect(wrapper.find('.el-button').exists()).toBe(true) // 工具栏仍可操作(可重试)
  })

  it('自动刷新:开启后按 5s 轮询,卸载即停', async () => {
    vi.useFakeTimers()
    try {
      let calls = 0
      server.use(
        http.get('*/api/logs', () => {
          calls += 1
          return new Response(JSON.stringify({ ok: true, text: `tick ${calls}` }), {
            headers: { 'Content-Type': 'application/json' },
          })
        }),
      )
      const wrapper = mountLogs()
      await flushPromises()
      const base = calls

      await wrapper.find('[data-testid="logs-auto-refresh"] input[type="checkbox"]').setValue(true)
      await flushPromises()
      expect(calls).toBeGreaterThan(base) // 开启立即刷一轮

      const afterStart = calls
      await vi.advanceTimersByTimeAsync(5000)
      expect(calls).toBeGreaterThan(afterStart)

      wrapper.unmount()
      const afterUnmount = calls
      await vi.advanceTimersByTimeAsync(10000)
      expect(calls).toBe(afterUnmount) // 卸载后不再轮询
    } finally {
      vi.useRealTimers()
    }
  })
})
