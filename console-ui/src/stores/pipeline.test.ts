import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setScenario } from '@/test/scenario'
import { usePipelineStore } from './pipeline'

describe('pipeline store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setScenario('healthy')
  })

  it('healthy:pipeline 与 services 均成功', async () => {
    const store = usePipelineStore()
    await store.refresh()
    expect(store.pipeline.status).toBe('success')
    expect(store.data?.nodes).toHaveLength(7)
    expect(store.data?.overall_status).toBe('healthy')
    expect(store.services.status).toBe('success')
  })

  it('ingest-failed:失败节点保留在数据中,overall 为 failed', async () => {
    setScenario('ingest-failed')
    const store = usePipelineStore()
    await store.refresh()
    expect(store.data?.overall_status).toBe('failed')
    const push = store.data?.nodes.find((n) => n.node === 'push')
    expect(push?.status).toBe('failed')
    expect(push?.error).toBeTruthy()
  })

  it('刷新失败保留旧 pipeline 数据', async () => {
    const store = usePipelineStore()
    await store.refresh()
    const before = store.data
    setScenario('unknown-error')
    await store.refresh()
    expect(store.data).toEqual(before)
    expect(store.refreshError?.status).toBe(500)
  })
})
