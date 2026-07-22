/**
 * typed API service 层:页面 → store → service → openapi-fetch。
 *
 * call() 统一结果包装:HTTP 非 2xx、网络失败、解析失败一律进入 error 分支,
 * 绝不把失败转换成 success + 空数据。具体端点函数随里程碑页面补充。
 */
import { client } from './client'
import { httpError, toApiError, type ApiError } from './errors'
import type { components } from '@/types/api'

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
  type?: 'sync' | 'apply' | 'reconcile' | 'ingest' | 'validation' | 'publish' | 'rollback'
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
  resource_type?: 'raw' | 'object' | 'quarantine_raw'
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

// ---- M2:数据集版本 / apply(publish 开关) ----

export interface DatasetsQuery {
  limit: number
  offset: number
  source?: string
  status?: components['schemas']['DatasetSummary']['status']
}

export async function getDatasets(query: DatasetsQuery) {
  return pageOf(await call(client.GET('/api/datasets', { params: { query } })))
}

export function getDatasetDetail(version: string) {
  return call(
    client.GET('/api/datasets/{version}', { params: { path: { version } } }),
  )
}

export function postDatasetPublish(version: string) {
  return call(
    client.POST('/api/datasets/{version}/publish', { params: { path: { version } } }),
  )
}

export function postDatasetRollback(version: string) {
  return call(
    client.POST('/api/datasets/{version}/rollback', { params: { path: { version } } }),
  )
}

export interface ApplyBody {
  source: string
  /** true=构建并自动发布;false=仅构建候选(stage-only) */
  publish?: boolean
}

export function postApply(body: ApplyBody) {
  return call(
    client.POST('/api/actions/apply', {
      body: {
        source: body.source,
        publish: body.publish ?? true,
      },
    }),
  )
}

// ---- M5:操作(retry) ----

export interface RetryBody {
  source: string
  object?: string | null
  deep: boolean
}

/**
 * retry 失败时的结构化错误,保留后端返回的 reason_code / run_id / step_id / detail_path,
 * 前端据此展示原因码与可点击的运行链接。
 */
export interface RetryApiError extends ApiError {
  reason_code?: string
  run_id?: number | null
  step_id?: number | null
  detail_path?: string | null
}

export async function postRetry(body: RetryBody): Promise<ApiResult<components['schemas']['RetryActionResult']>> {
  try {
    const { data, error, response } = await client.POST('/api/actions/retry', { body })
    if (!response.ok) {
      // 提取完整错误体,保留后端返回的 reason_code / run_id / step_id / detail_path
      const err = error as Record<string, unknown> | undefined
      const apiError: RetryApiError = {
        kind: 'http',
        status: response.status,
        message: typeof err?.detail === 'string' ? err.detail : `HTTP ${response.status}`,
        retriable: response.status >= 500 && response.status !== 501,
        reason_code: typeof err?.reason_code === 'string' ? err.reason_code : undefined,
        run_id: typeof err?.run_id === 'number' ? (err.run_id as number) : undefined,
        step_id: typeof err?.step_id === 'number' ? (err.step_id as number) : undefined,
        detail_path: typeof err?.detail_path === 'string' ? err.detail_path : undefined,
      }
      return { ok: false, error: apiError }
    }
    if (data === undefined) {
      return { ok: false, error: { kind: 'parse', message: '成功响应缺少数据', retriable: false } }
    }
    return { ok: true, data, response }
  } catch (err) {
    return { ok: false, error: toApiError(err) }
  }
}

// ---- M6:MCP Lab ----

export type McpToolResult = components['schemas']['McpToolResult']
export type ProposalResponse = components['schemas']['ProposalResponse']
export type ProposalRequest = components['schemas']['ProposalRequest']
export type McpLabErrorBody = components['schemas']['McpLabError']

export interface McpLabApiError extends ApiError {
  reason_code?: McpLabErrorBody['reason_code']
  tool?: string | null
  error_id?: string | null
}

function mcpLabErrorFrom(status: number, error: unknown): McpLabApiError {
  const err = error as Record<string, unknown> | undefined
  const detail = typeof err?.detail === 'string' ? err.detail : detailOf(error)
  return {
    kind: 'http',
    status,
    message: detail || `HTTP ${status}`,
    retriable: status >= 500 && status !== 501,
    reason_code: typeof err?.reason_code === 'string'
      ? (err.reason_code as McpLabErrorBody['reason_code'])
      : undefined,
    tool: typeof err?.tool === 'string' ? err.tool : null,
    error_id: typeof err?.error_id === 'string' ? err.error_id : null,
  }
}

export async function postMcpCall(
  tool: 'query_objects' | 'query_metrics',
  params: Record<string, unknown>,
  init?: { signal?: AbortSignal },
): Promise<ApiResult<McpToolResult>> {
  try {
    const { data, error, response } = await client.POST('/api/debug/mcp-call', {
      body: { tool, params: params as components['schemas']['McpCallBody']['params'] },
      signal: init?.signal,
    })
    if (!response.ok) {
      return { ok: false, error: mcpLabErrorFrom(response.status, error) }
    }
    if (data === undefined) {
      return { ok: false, error: { kind: 'parse', message: '成功响应缺少数据', retriable: false } }
    }
    return { ok: true, data, response }
  } catch (err) {
    return { ok: false, error: toApiError(err) }
  }
}

export async function postProposal(
  body: ProposalRequest,
  init?: { signal?: AbortSignal },
): Promise<ApiResult<ProposalResponse>> {
  try {
    const { data, error, response } = await client.POST('/api/gateway/proposals', {
      body,
      signal: init?.signal,
    })
    if (!response.ok) {
      return { ok: false, error: mcpLabErrorFrom(response.status, error) }
    }
    if (data === undefined) {
      return { ok: false, error: { kind: 'parse', message: '成功响应缺少数据', retriable: false } }
    }
    return { ok: true, data, response }
  } catch (err) {
    return { ok: false, error: toApiError(err) }
  }
}

// ---- M3: Mapping Preview ----

export type MappingPreviewRequest = components['schemas']['MappingPreviewRequest']
export type MappingPreviewResponse = components['schemas']['MappingPreviewResponse']
export type MappingPreviewErrorBody = components['schemas']['MappingPreviewError']
export type MappingPreviewDraftBinding = components['schemas']['MappingPreviewDraftBinding']

/**
 * Preview 失败时的结构化错误,保留后端 MappingPreviewError 的 reason_code / error_id,
 * 前端按 status/reason_code 分支,不解析中文 detail。
 */
export interface MappingPreviewApiError extends ApiError {
  reason_code?: MappingPreviewErrorBody['reason_code']
  error_id?: string | null
}

function mappingPreviewErrorFrom(status: number, error: unknown): MappingPreviewApiError {
  const err = error as Record<string, unknown> | undefined
  const detail = typeof err?.detail === 'string' ? err.detail : detailOf(error)
  return {
    kind: 'http',
    status,
    message: detail || `HTTP ${status}`,
    retriable: status >= 500 && status !== 501,
    reason_code: typeof err?.reason_code === 'string'
      ? (err.reason_code as MappingPreviewErrorBody['reason_code'])
      : undefined,
    error_id: typeof err?.error_id === 'string' ? err.error_id : null,
  }
}

export async function postMappingPreview(
  object: string,
  body: MappingPreviewRequest,
  init?: { signal?: AbortSignal },
): Promise<ApiResult<MappingPreviewResponse>> {
  try {
    const { data, error, response } = await client.POST('/api/mappings/{object}/preview', {
      params: { path: { object } },
      body,
      signal: init?.signal,
    })
    if (!response.ok) {
      return { ok: false, error: mappingPreviewErrorFrom(response.status, error) }
    }
    if (data === undefined) {
      return { ok: false, error: { kind: 'parse', message: '成功响应缺少数据', retriable: false } }
    }
    return { ok: true, data, response }
  } catch (err) {
    return { ok: false, error: toApiError(err) }
  }
}

// ---- M4: Field Lineage ----

export type ObjectLineageResponse = components['schemas']['ObjectLineageResponse']
export type ObjectLineageErrorBody = components['schemas']['ObjectLineageError']
export type ObjectLineageField = components['schemas']['ObjectLineageField']

export interface LineageApiError extends ApiError {
  reason_code?: ObjectLineageErrorBody['reason_code']
  error_id?: string | null
}

function lineageErrorFrom(status: number, error: unknown): LineageApiError {
  const err = error as Record<string, unknown> | undefined
  const detail = typeof err?.detail === 'string' ? err.detail : detailOf(error)
  return {
    kind: 'http',
    status,
    message: detail || `HTTP ${status}`,
    retriable: status >= 500,
    reason_code: typeof err?.reason_code === 'string'
      ? (err.reason_code as ObjectLineageErrorBody['reason_code'])
      : undefined,
    error_id: typeof err?.error_id === 'string' ? err.error_id : null,
  }
}

export async function getObjectLineage(
  object: string,
  keyToken: string,
  init?: { signal?: AbortSignal; property?: string },
): Promise<ApiResult<ObjectLineageResponse>> {
  try {
    const params: Record<string, unknown> = {
      path: { object, key: keyToken },
    }
    if (init?.property) {
      params.query = { property: init.property }
    }
    const { data, error, response } = await client.GET(
      '/api/objects/{object}/{key}/lineage',
      { params: params as never, signal: init?.signal },
    )
    if (!response.ok) {
      return { ok: false, error: lineageErrorFrom(response.status, error) }
    }
    if (data === undefined) {
      return { ok: false, error: { kind: 'parse', message: '成功响应缺少数据', retriable: false } }
    }
    return { ok: true, data, response }
  } catch (err) {
    return { ok: false, error: toApiError(err) }
  }
}
