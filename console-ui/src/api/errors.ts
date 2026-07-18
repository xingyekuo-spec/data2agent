/**
 * 统一错误分类:HTTP、网络、解析、未知。
 *
 * message 是安全摘要:可以包含后端 HttpError 的 detail(设计上就是面向用户的),
 * 但绝不包含 Token、请求头或完整响应正文。401/409/422/501 的语义由 status
 * 原样保留,不得被合并成通用失败。
 */
export type ApiErrorKind = 'http' | 'network' | 'parse' | 'unknown'

export interface ApiError {
  kind: ApiErrorKind
  /** HTTP 状态码(仅 kind === 'http') */
  status?: number
  message: string
  retriable: boolean
}

export function httpError(status: number, detail?: string): ApiError {
  return {
    kind: 'http',
    status,
    message: detail?.trim() || `HTTP ${status}`,
    // 5xx 可重试;501(契约桩未接入)重试无意义
    retriable: status >= 500 && status !== 501,
  }
}

export function toApiError(err: unknown): ApiError {
  if (err instanceof DOMException && err.name === 'AbortError') {
    return { kind: 'network', message: '请求超时或被中止', retriable: true }
  }
  if (err instanceof TypeError) {
    // fetch 网络层失败(DNS / 连接拒绝 / CORS / 相对 URL 构造失败)
    return { kind: 'network', message: err.message || '网络请求失败', retriable: true }
  }
  if (err instanceof SyntaxError) {
    return { kind: 'parse', message: '响应不是合法 JSON', retriable: false }
  }
  if (err instanceof Error) {
    return { kind: 'unknown', message: err.message, retriable: false }
  }
  return { kind: 'unknown', message: String(err), retriable: false }
}
