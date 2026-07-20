/**
 * typed API service 层:页面 → store → service → openapi-fetch。
 *
 * call() 统一结果包装:HTTP 非 2xx、网络失败、解析失败一律进入 error 分支,
 * 绝不把失败转换成 success + 空数据。具体端点函数随里程碑页面补充。
 */
import { client } from './client'
import { httpError, toApiError, type ApiError } from './errors'

export type ApiResult<T> =
  | { ok: true; data: T; response: Response }
  | { ok: false; error: ApiError }

interface FetchOutcome<T> {
  data?: T
  error?: unknown
  response: Response
}

function detailOf(error: unknown): string {
  if (error && typeof error === 'object' && 'detail' in error) {
    const detail = (error as { detail?: unknown }).detail
    if (typeof detail === 'string') {
      return detail
    }
  }
  return ''
}

export async function call<T>(promise: Promise<FetchOutcome<T>>): Promise<ApiResult<T>> {
  try {
    const { data, error, response } = await promise
    if (!response.ok) {
      return { ok: false, error: httpError(response.status, detailOf(error)) }
    }
    if (data === undefined) {
      return { ok: false, error: { kind: 'parse', message: '成功响应缺少数据', retriable: false } }
    }
    return { ok: true, data, response }
  } catch (err) {
    return { ok: false, error: toApiError(err) }
  }
}

// ---- 端点级 service(M2 垂直切片用;其余端点随里程碑补充)----

export function getOverview() {
  return call(client.GET('/api/overview'))
}

export function getServices() {
  return call(client.GET('/api/services'))
}

export function getConfig() {
  return call(client.GET('/api/config'))
}

/** 契约桩:后端实现前真实调用返回 501,页面据此显示「尚未接入」 */
export function getPipeline() {
  return call(client.GET('/api/pipeline'))
}

// ---- M4:运行 / 审计 / 数据浏览 ----

export interface RunsQuery {
  limit: number
  offset: number
  type?: 'sync' | 'apply' | 'reconcile' | 'ingest' | 'validation'
  status?: 'running' | 'ok' | 'paused' | 'failed' | 'aborted'
}

/** 数组 + X-Total-Count 适配为分页结果 */
export function pageOf<T>(result: ApiResult<T[]>): ApiResult<{ items: T[]; total: number }> {
  if (!result.ok) {
    return result
  }
  const totalHeader = result.response.headers.get('X-Total-Count')
  const total = Number(totalHeader)
  if (totalHeader === null || !Number.isFinite(total)) {
    return {
      ok: false,
      error: {
        kind: 'parse',
        message: '分页响应缺少有效 X-Total-Count',
        retriable: false,
      },
    }
  }
  return {
    ok: true,
    data: { items: result.data, total },
    response: result.response,
  }
}

export async function getRuns(query: RunsQuery) {
  return pageOf(await call(client.GET('/api/runs', { params: { query } })))
}

export function getRunDetail(runId: number) {
  return call(client.GET('/api/runs/{run_id}', { params: { path: { run_id: runId } } }))
}

export interface AuditQuery {
  limit: number
  offset: number
  source?: string
  action?: string
  from?: string
  to?: string
}

export async function getAudit(query: AuditQuery) {
  return pageOf(await call(client.GET('/api/audit', { params: { query } })))
}

export interface AccessAuditQuery {
  limit: number
  offset: number
  subject?: string
  resource_type?: 'raw' | 'object'
  allowed?: boolean
  from?: string
  to?: string
}

export function getAccessAudit(query: AccessAuditQuery) {
  return call(client.GET('/api/audit/access', { params: { query } }))
}

// ---- M4:数据浏览 ----

export function getRawCatalog() {
  return call(client.GET('/api/data/raw'))
}

export interface BrowseQuery {
  limit: number
  offset: number
  q?: string
}

export function getRawPage(source: string, table: string, query: BrowseQuery) {
  return call(
    client.GET('/api/data/raw/{source}/{table}', {
      params: { path: { source, table }, query },
    }),
  )
}

export function getObjectCatalog() {
  return call(client.GET('/api/objects'))
}

export function getObjectRows(object: string, query: BrowseQuery) {
  return call(
    client.GET('/api/objects/{object}', {
      params: { path: { object }, query },
    }),
  )
}

// ---- M5:隔离列表 / 分组 / 详情 ----

export interface QuarantineQuery {
  limit: number
  offset: number
  source?: string
  object?: string
  reason?: string
}

export async function getQuarantineList(query: QuarantineQuery) {
  return pageOf(await call(client.GET('/api/quarantine', { params: { query } })))
}

export function getQuarantineGroups(source?: string) {
  return call(client.GET('/api/quarantine/groups', { params: { query: { source: source ?? null } } }))
}

export function getQuarantineDetail(id: number) {
  return call(client.GET('/api/quarantine/{id}', { params: { path: { id } } }))
}

// ---- M5:模板 / 模板指标 ----

export function getTemplates() {
  return call(client.GET('/api/templates'))
}

export function getTemplateMetrics() {
  return call(client.GET('/api/templates/metrics'))
}

// ---- M5:操作(retry) ----

export interface RetryBody {
  source: string
  object?: string | null
  deep: boolean
}

export function postRetry(body: RetryBody) {
  return call(client.POST('/api/actions/retry', { body }))
}
