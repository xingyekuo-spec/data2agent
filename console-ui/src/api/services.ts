/**
 * typed API service 层:页面 → store → service → openapi-fetch。
 *
 * call() 统一结果包装:HTTP 非 2xx、网络失败、解析失败一律进入 error 分支,
 * 绝不把失败转换成 success + 空数据。具体端点函数随里程碑页面补充。
 */
import { client } from './client'
import { httpError, toApiError, type ApiError } from './errors'

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError }

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
    return { ok: true, data }
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
