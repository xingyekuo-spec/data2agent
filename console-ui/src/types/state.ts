/**
 * 前端状态模型:请求生命周期与领域健康状态是两类信息,不得用一个布尔混用。
 *
 * - 请求状态(idle|loading|success|error):网络/解析层;网络失败是 error,
 *   不能转成 success + 空数组;
 * - 领域健康状态(unknown|idle|running|healthy|warning|failed|stale):
 *   业务层结论;unknown 表示后端无法检测,绝不能显示为正常;
 * - 数据为空:是成功响应之后的 empty 视图,与 error 视觉和语义都不同。
 */
import type { ApiError } from '@/api/errors'

export type RequestStatus = 'idle' | 'loading' | 'success' | 'error'

export type HealthStatus =
  | 'unknown'
  | 'idle'
  | 'running'
  | 'healthy'
  | 'warning'
  | 'failed'
  | 'stale'

/** 请求状态判别联合:error 必须携带 ApiError;success 才允许有数据 */
export type RequestState<T> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: T }
  | { status: 'error'; error: ApiError }

export const HEALTH_LABELS: Record<HealthStatus, string> = {
  unknown: '未知',
  idle: '空闲',
  running: '运行中',
  healthy: '正常',
  warning: '警告',
  failed: '失败',
  stale: '旧版本',
}
