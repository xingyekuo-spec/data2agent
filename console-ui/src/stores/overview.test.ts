import { HttpResponse, http } from '@/test/http'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { server } from '@/test/fetch-stub'
import { useOverviewStore } from './overview'

const OVERVIEW_MINIMAL = {
  landing: '',
  readonly: true,
  sources: [],
  objects: [],
  needs_setup: false,
  generated_at: '2026-07-18T09:12:00+08:00',
  summary: {
    raw_rows: 0,
    object_rows: null,
    materialized_objects: 0,
    template_objects: 5,
    quarantine_pending: 0,
    last_run_at: null,
    data_updated_at: null,
  },
  versions: { app: '0.2.0', template: '0.1.0', dataset: null, object: null },
  binding_summary: { verified: 0, draft: 10, disabled: 0 },
  alerts: [],
  recent_runs: [],
  sync_trend: [],
  count_notes: [],
}

describe('overview store(单一所有者)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setScenario('healthy')
  })

  it('idle → loading → success,记录成功时间', async () => {
    const store = useOverviewStore()
    expect(store.overview.status).toBe('idle')
    const pending = store.refresh()
    expect(store.overview.status).toBe('loading')
    await pending
    expect(store.overview.status).toBe('success')
    expect(store.data?.sources.length).toBeGreaterThan(0)
    expect(store.lastSuccessAt).not.toBeNull()
    expect(store.refreshError).toBeNull()
  })

  it('首次加载失败 → 请求 error(不是空数据)', async () => {
    setScenario('unknown-error')
    const store = useOverviewStore()
    await store.refresh()
    expect(store.overview.status).toBe('error')
    if (store.overview.status === 'error') {
      expect(store.overview.error.status).toBe(500)
    }
  })

  it('刷新失败保留旧数据并标记 refreshError;恢复后清除', async () => {
    const store = useOverviewStore()
    await store.refresh()
    expect(store.overview.status).toBe('success')
    const before = store.data

    setScenario('unknown-error')
    await store.refresh()
    // 旧数据仍在,refreshError 标记,不变成健康假象
    expect(store.overview.status).toBe('success')
    expect(store.data).toEqual(before)
    expect(store.refreshError?.status).toBe(500)

    setScenario('healthy')
    await store.refresh()
    expect(store.refreshError).toBeNull()
  })

  it('防重入:在途请求未完成时跳过', async () => {
    let calls = 0
    server.use(
      http.get('*/api/overview', () => {
        calls += 1
        return new HttpResponse(JSON.stringify(OVERVIEW_MINIMAL), {
          headers: { 'Content-Type': 'application/json' },
        })
      }),
    )
    const store = useOverviewStore()
    await Promise.all([store.refresh(), store.refresh(), store.refresh()])
    expect(calls).toBe(1)
  })
})
