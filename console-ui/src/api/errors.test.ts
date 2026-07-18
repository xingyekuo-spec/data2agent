import { describe, expect, it } from 'vitest'
import { httpError, toApiError } from './errors'

describe('toApiError', () => {
  it('fetch 网络失败(TypeError)→ network,可重试', () => {
    const err = toApiError(new TypeError('fetch failed'))
    expect(err.kind).toBe('network')
    expect(err.retriable).toBe(true)
  })

  it('中止(AbortError)→ network,可重试', () => {
    const err = toApiError(new DOMException('aborted', 'AbortError'))
    expect(err.kind).toBe('network')
    expect(err.retriable).toBe(true)
  })

  it('JSON 解析失败(SyntaxError)→ parse,不重试', () => {
    const err = toApiError(new SyntaxError('Unexpected token'))
    expect(err.kind).toBe('parse')
    expect(err.retriable).toBe(false)
  })

  it('其他 Error → unknown', () => {
    expect(toApiError(new Error('boom')).kind).toBe('unknown')
    expect(toApiError('str').kind).toBe('unknown')
    expect(toApiError(undefined).kind).toBe('unknown')
  })
})

describe('httpError', () => {
  it('保留状态码语义,5xx 可重试', () => {
    expect(httpError(500).retriable).toBe(true)
    expect(httpError(502).retriable).toBe(true)
  })

  it('501(契约桩未接入)不重试', () => {
    const err = httpError(501, '契约桩')
    expect(err.status).toBe(501)
    expect(err.retriable).toBe(false)
    expect(err.message).toBe('契约桩')
  })

  it('401/409/422 语义各自保留且不重试', () => {
    for (const status of [401, 403, 409, 422]) {
      const err = httpError(status)
      expect(err.status).toBe(status)
      expect(err.kind).toBe('http')
      expect(err.retriable).toBe(false)
    }
  })

  it('detail 空白时回退状态行,不产生空消息', () => {
    expect(httpError(409, '  ').message).toBe('HTTP 409')
  })
})
