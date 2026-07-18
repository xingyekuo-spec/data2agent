import { HttpResponse, http } from 'msw'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/mocks/scenario'
import { server } from '@/test/setup'
import { useOverviewStore } from './overview'

describe('overview store(垂直切片)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setScenario('healthy')
  })

  it('idle → loading → success,数据完整', async () => {
    const store = useOverviewStore()
    expect(store.overview.status).toBe('idle')
    const pending = store.refresh()
    expect(store.overview.status).toBe('loading')
    await pending
    expect(store.overview.status).toBe('success')
    if (store.overview.status === 'success') {
      expect(store.overview.data.sources.length).toBeGreaterThan(0)
      expect(store.overview.data.objects.length).toBeGreaterThan(0)
    }
    expect(store.services.status).toBe('success')
  })

  it('失败保留错误(500),不变成空数据', async () => {
    setScenario('unknown-error')
    const store = useOverviewStore()
    await store.refresh()
    expect(store.overview.status).toBe('error')
    if (store.overview.status === 'error') {
      expect(store.overview.error.status).toBe(500)
      expect(store.overview.error.retriable).toBe(true)
    }
    expect(store.services.status).toBe('error')
  })

  it('empty-install:成功 + 空集合,与请求失败是两种语义', async () => {
    setScenario('empty-install')
    const store = useOverviewStore()
    await store.refresh()
    expect(store.overview.status).toBe('success')
    if (store.overview.status === 'success') {
      expect(store.overview.data.needs_setup).toBe(true)
      expect(store.overview.data.sources).toEqual([])
    }
  })

  it('并发刷新防重复:在途时后续调用不再发请求', async () => {
    let overviewCalls = 0
    server.use(
      http.get('*/api/overview', () => {
        overviewCalls += 1
        return new HttpResponse(
          JSON.stringify({
            landing: '',
            readonly: true,
            actions_sync_reconcile: false,
            sources: [],
            objects: [],
            needs_setup: false,
          }),
          { headers: { 'Content-Type': 'application/json' } },
        )
      }),
    )
    const store = useOverviewStore()
    await Promise.all([store.refresh(), store.refresh(), store.refresh()])
    expect(overviewCalls).toBe(1)
  })
})
