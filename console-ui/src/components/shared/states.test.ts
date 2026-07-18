import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import { HEALTH_LABELS, type HealthStatus } from '@/types/state'
import EmptyState from './EmptyState.vue'
import ErrorState from './ErrorState.vue'
import FeaturePlaceholder from './FeaturePlaceholder.vue'
import LoadingState from './LoadingState.vue'
import StatusBadge from './StatusBadge.vue'
import UnknownState from './UnknownState.vue'

describe('StatusBadge', () => {
  const statuses: HealthStatus[] = [
    'unknown',
    'idle',
    'running',
    'healthy',
    'warning',
    'failed',
    'stale',
  ]

  it('7 种领域状态各有固定文案与语义 class', () => {
    for (const status of statuses) {
      const w = mount(StatusBadge, { props: { status } })
      expect(w.text()).toBe(HEALTH_LABELS[status])
      expect(w.attributes('data-status')).toBe(status)
      expect(w.attributes('aria-label')).toContain(HEALTH_LABELS[status])
    }
  })

  it('unknown 不使用健康色(灰,不是绿)', () => {
    const w = mount(StatusBadge, { props: { status: 'unknown' } })
    expect(w.classes()).toContain('status-badge--unknown')
    expect(w.classes()).not.toContain('status-badge--healthy')
  })
})

describe('LoadingState', () => {
  it('role=status,文案可配', () => {
    const w = mount(LoadingState, { props: { text: '加载总览…' } })
    expect(w.attributes('role')).toBe('status')
    expect(w.text()).toContain('加载总览…')
  })
})

describe('EmptyState', () => {
  it('空态与错误态是不同组件,带标题与提示', () => {
    const w = mount(EmptyState, {
      props: { title: '没有运行记录', hint: '首次同步后显示' },
    })
    expect(w.text()).toContain('没有运行记录')
    expect(w.text()).toContain('首次同步后显示')
    expect(w.find('[data-testid="error-state"]').exists()).toBe(false)
  })
})

describe('ErrorState', () => {
  it('可重试错误显示重试按钮并发出 retry 事件', async () => {
    const w = mount(ErrorState, {
      props: {
        error: { kind: 'http', status: 500, message: 'db locked', retriable: true },
      },
    })
    expect(w.attributes('role')).toBe('alert')
    expect(w.text()).toContain('HTTP 500')
    expect(w.text()).toContain('db locked')
    await w.find('[data-testid="error-retry"]').trigger('click')
    expect(w.emitted('retry')).toHaveLength(1)
  })

  it('501(契约桩)显示「尚未接入」而非通用失败,且无重试按钮', () => {
    const w = mount(ErrorState, {
      props: {
        error: { kind: 'http', status: 501, message: '契约桩:端点已声明', retriable: false },
      },
    })
    expect(w.find('[data-testid="error-state"]').exists()).toBe(false)
    expect(w.find('[data-testid="not-implemented-state"]').exists()).toBe(true)
    expect(w.text()).toContain('尚未接入')
    expect(w.text()).not.toContain('请求失败')
    expect(w.find('[data-testid="error-retry"]').exists()).toBe(false)
    expect(w.text()).toContain('契约桩')
  })
})

describe('UnknownState', () => {
  it('明确表达尚不可检测,且不同于空态', () => {
    const w = mount(UnknownState)
    expect(w.text()).toContain('尚不可检测')
    expect(w.find('[data-testid="empty-state"]').exists()).toBe(false)
  })
})

describe('FeaturePlaceholder', () => {
  it('标注尚未接入与归属里程碑', () => {
    const w = mount(FeaturePlaceholder, {
      props: { milestone: 'M4' },
      global: { plugins: [ElementPlus] },
    })
    expect(w.text()).toContain('尚未接入')
    expect(w.text()).toContain('M4')
  })
})
