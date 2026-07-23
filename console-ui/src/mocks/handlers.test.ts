import { afterEach, describe, expect, it } from 'vitest'
import { call } from '@/api/services'
import { client } from '@/api/client'
import { strictUnhandledRequest } from './handlers'
import { getScenario, setScenario } from './scenario'

/**
 * handlers 行为测试:经 test/setup.ts 的 MSW node server 拦截真实 client 请求。
 * 每个场景至少覆盖一处关键断言(M2 计划 §6)。
 */
describe('mock handlers', () => {
  afterEach(() => setScenario('healthy'))

  it('healthy:总览 200,返回来源与对象', async () => {
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.sources.length).toBeGreaterThan(0)
      expect(result.data.needs_setup).toBe(false)
    }
  })

  it('empty-install:空集合 + needs_setup,不是 0 即健康', async () => {
    setScenario('empty-install')
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.needs_setup).toBe(true)
      expect(result.data.sources).toEqual([])
      expect(result.data.objects).toEqual([])
    }
    const runs = await call(client.GET('/api/runs'))
    expect(runs.ok && runs.data).toEqual([])
  })

  it('sync-running:Run 为 running 且无完成时间', async () => {
    setScenario('sync-running')
    const result = await call(client.GET('/api/runs'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data[0]?.status).toBe('running')
      expect(result.data[0]?.finished_at).toBeNull()
    }
    const pipeline = await call(client.GET('/api/pipeline'))
    expect(pipeline.ok).toBe(true)
    if (pipeline.ok) {
      expect(pipeline.data.nodes.some((n) => n.status === 'running')).toBe(true)
    }
  })

  it('ingest-failed:push 节点 failed 带摘要,其他节点不连带变绿', async () => {
    setScenario('ingest-failed')
    const result = await call(client.GET('/api/pipeline'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      const push = result.data.nodes.find((n) => n.node === 'push')
      expect(push?.status).toBe('failed')
      expect(push?.error).toBeTruthy()
      const mcp = result.data.nodes.find((n) => n.node === 'mcp')
      expect(mcp?.status).toBe('healthy')
    }
    const services = await call(client.GET('/api/services'))
    expect(services.ok).toBe(true)
    if (services.ok) {
      expect(services.data.ingest.ok).toBe(false)
      expect(services.data.console.ok).toBe(true)
    }
  })

  it('apply-circuit-broken:mapping failed + objects stale 双重语义', async () => {
    setScenario('apply-circuit-broken')
    const result = await call(client.GET('/api/pipeline'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.nodes.find((n) => n.node === 'mapping')?.status).toBe('failed')
      expect(result.data.nodes.find((n) => n.node === 'objects')?.status).toBe('stale')
    }
  })

  it('partial-services-down:局部 failed,不整页转空', async () => {
    setScenario('partial-services-down')
    const result = await call(client.GET('/api/services'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.mcp.ok).toBe(false)
      expect(result.data.console.ok).toBe(true)
    }
  })

  it('quarantine-pending:隔离记录带对象与原因', async () => {
    setScenario('quarantine-pending')
    const result = await call(client.GET('/api/quarantine'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.length).toBeGreaterThan(0)
      expect(result.data[0]?.reason).toBeTruthy()
    }
  })

  it('draft-governance:binding 全部 draft', async () => {
    setScenario('draft-governance')
    const result = await call(client.GET('/api/templates'))
    expect(result.ok).toBe(true)
    if (result.ok) {
      const statuses = result.data.flatMap((t) => t.bindings.map((b) => b.status))
      expect(statuses.length).toBeGreaterThan(0)
      expect(new Set(statuses)).toEqual(new Set(['draft']))
    }
  })

  it('token-invalid:所有端点 401,不返回业务数据', async () => {
    setScenario('token-invalid')
    for (const path of ['/api/overview', '/api/pipeline', '/api/templates'] as const) {
      const result = await call(client.GET(path))
      expect(result.ok).toBe(false)
      if (!result.ok) {
        expect(result.error.status).toBe(401)
      }
    }
    expect(getScenario()).toBe('token-invalid')
  })

  it('unknown-error:所有端点 500,可重试但不得显示成功', async () => {
    setScenario('unknown-error')
    const result = await call(client.GET('/api/overview'))
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.status).toBe(500)
      expect(result.error.retriable).toBe(true)
    }
  })

  it('未匹配请求直接报错,不静默穿透', async () => {
    // /api/actions/sync 未在 Mock 中声明:strictUnhandledRequest 抛错,
    // 客户端必须收到失败(绝不能变成成功或空数据)
    const result = await call(
      client.POST('/api/actions/sync', { body: { source: 'digiwin_e10', object: null, deep: false } }),
    )
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.kind === 'http' || result.error.kind === 'network').toBe(true)
    }
  })

  it('/api/* 未匹配即抛错;Vite 模块等非 API 请求放行不抛错', () => {
    expect(() =>
      strictUnhandledRequest(new Request('http://localhost:5174/api/not-declared')),
    ).toThrow('MSW 未匹配的 API 请求')

    // 回归:Vite dev 在 /src 下加载模块,worker 拦截后必须放行,
    // 否则动态 import 被打断、路由无法挂载(本次事故根因)
    expect(() =>
      strictUnhandledRequest(new Request('http://localhost:5174/src/views/DashboardView.vue')),
    ).not.toThrow()
    expect(() =>
      strictUnhandledRequest(new Request('http://localhost:5174/assets/index.js')),
    ).not.toThrow()
  })
})

describe('mapping preview handlers', () => {
  afterEach(() => setScenario('healthy'))

  it('healthy current:200 + mode=current', async () => {
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', { source: 'digiwin_e10' })
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.mode).toBe('current')
      expect(result.data.sample.sampled_rows).toBeGreaterThan(0)
      expect(result.data.diff.state).toBe('available')
    }
  })

  it('healthy draft:200 + mode=draft with diff', async () => {
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', {
      source: 'digiwin_e10',
      draft_binding: {
        tables: ['CUSTOMER'],
        key_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
        field_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
        derived: {},
        watermark: null,
        notes: '',
      },
    })
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.mode).toBe('draft')
      expect(result.data.diff.summary.fields_changed).toBeGreaterThan(0)
      expect(result.data.current_binding_hash).not.toBe(result.data.candidate_binding_hash)
    }
  })

  it('empty-install:200 + sampled_rows=0', async () => {
    setScenario('empty-install')
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', { source: 'digiwin_e10' })
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.sample.sampled_rows).toBe(0)
      expect(result.data.candidate.summary.total).toBe(0)
    }
  })

  it('token-invalid:401 unauthorized MappingPreviewError', async () => {
    setScenario('token-invalid')
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', { source: 'digiwin_e10' })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.status).toBe(401)
      expect((result.error as { reason_code?: string }).reason_code).toBe('unauthorized')
    }
  })

  it('preview-forbidden:403 token_not_configured', async () => {
    setScenario('preview-forbidden')
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', { source: 'digiwin_e10' })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.status).toBe(403)
      expect((result.error as { reason_code?: string }).reason_code).toBe('token_not_configured')
    }
  })

  it('preview-draft-invalid:422 draft_invalid', async () => {
    setScenario('preview-draft-invalid')
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', {
      source: 'digiwin_e10',
      draft_binding: {
        tables: ['CUSTOMER'],
        key_map: {},
        field_map: {},
        derived: {},
        watermark: null,
        notes: '',
      },
    })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.status).toBe(422)
      expect((result.error as { reason_code?: string }).reason_code).toBe('draft_invalid')
    }
  })

  it('unknown-error:500 preview_failed + error_id', async () => {
    setScenario('unknown-error')
    const { postMappingPreview } = await import('@/api/services')
    const result = await postMappingPreview('Customer', { source: 'digiwin_e10' })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.error.status).toBe(500)
      expect(result.error.retriable).toBe(true)
      expect((result.error as { reason_code?: string }).reason_code).toBe('preview_failed')
      expect((result.error as { error_id?: string | null }).error_id).toBeTruthy()
    }
  })
})
