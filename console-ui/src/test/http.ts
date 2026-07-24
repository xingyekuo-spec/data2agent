/**
 * Vitest 用轻量 HTTP stub(替代 MSW):pattern 匹配 + Response 构造。
 * 仅测试基础设施;不进入产品包。
 */
export type StubResolverInfo = {
  request: Request
  params: Record<string, string>
}

export type StubResolver = (
  info: StubResolverInfo,
) => Response | Promise<Response>

export type StubHandler = {
  method: string
  pattern: string
  resolver: StubResolver
}

function patternToRegex(pattern: string): { regex: RegExp; paramNames: string[] } {
  // MSW 风格:`*/api/foo/:id` → 任意前缀 + 具名段
  const paramNames: string[] = []
  const body = pattern.replace(/^\*\//, '/')
  const escaped = body
    .split('/')
    .map((segment) => {
      if (segment.startsWith(':')) {
        paramNames.push(segment.slice(1))
        return '([^/]+)'
      }
      return segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    })
    .join('/')
  return { regex: new RegExp(`^${escaped}/?$`), paramNames }
}

export function matchHandler(
  handlers: readonly StubHandler[],
  request: Request,
): { handler: StubHandler; params: Record<string, string> } | null {
  const url = new URL(request.url)
  const method = request.method.toUpperCase()
  for (const handler of handlers) {
    if (handler.method !== method && handler.method !== '*') {
      continue
    }
    const { regex, paramNames } = patternToRegex(handler.pattern)
    const m = url.pathname.match(regex)
    if (!m) {
      continue
    }
    const params: Record<string, string> = {}
    paramNames.forEach((name, i) => {
      params[name] = decodeURIComponent(m[i + 1] ?? '')
    })
    return { handler, params }
  }
  return null
}

/** 与 MSW HttpResponse 在测试中的用法对齐 */
export class HttpResponse extends Response {
  static json(body: unknown, init: ResponseInit = {}): HttpResponse {
    const headers = new Headers(init.headers)
    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    return new HttpResponse(JSON.stringify(body), { ...init, headers })
  }
}

export const http = {
  get(pattern: string, resolver: StubResolver): StubHandler {
    return { method: 'GET', pattern, resolver }
  },
  post(pattern: string, resolver: StubResolver): StubHandler {
    return { method: 'POST', pattern, resolver }
  },
}
