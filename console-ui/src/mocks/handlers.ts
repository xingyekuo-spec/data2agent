/**
 * MSW handlers:按当前场景返回 typed fixture。
 *
 * - token-invalid / unknown-error 是传输级场景:对所有 /api/* 短路 401 / 500,
 *   不返回任何业务数据;
 * - 未匹配请求由 worker/server 的 onUnhandledRequest: 'error' 拒绝,
 *   不允许静默穿透到真实网络。
 */
import { http, HttpResponse, type HttpHandler } from 'msw'
import type { HttpError, ScenarioFixture } from './fixtures/base'
import { getScenario, scenarioFixtures } from './scenario'

/**
 * 未匹配请求策略:
 * - /api/* 未声明 handler → 抛错(Mock 必须显式声明,禁止静默穿透);
 * - 非 API 请求(Vite dev 模块 / 静态资源 / HMR)→ 返回 undefined 放行到真实
 *   网络。worker scope 是 /v1/,Vite dev 也在 /v1/src/ 下加载模块,一刀切抛错
 *   会把动态 import 打断(500),导致路由无法挂载。
 */
export function strictUnhandledRequest(request: Request): void {
  const { pathname } = new URL(request.url)
  if (pathname.startsWith('/api/')) {
    throw new Error(
      `MSW 未匹配的 API 请求: ${request.method} ${request.url}(必须为 Mock 显式声明 handler)`,
    )
  }
}

/**
 *  typed fixture 直接序列化为 JSON 响应。
 *  不用 HttpResponse.json:其 JsonBodyType 要求索引签名,与生成接口类型摩擦,
 *  会迫使调用处写 as;JSON.stringify 输出等价且无类型逃逸。
 */
function json<T>(body: T, status = 200): HttpResponse<string> {
  return new HttpResponse(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function transportFailure(): HttpResponse<string> | null {
  const id = getScenario()
  if (id === 'token-invalid') {
    const body: HttpError = { detail: '需要有效的管理界面登录密码(Mock: token-invalid)' }
    return json(body, 401)
  }
  if (id === 'unknown-error') {
    const body: HttpError = { detail: '未处理异常(Mock: unknown-error)' }
    return json(body, 500)
  }
  return null
}

function respond<T>(body: (fixture: ScenarioFixture) => T): HttpResponse<string> {
  const fail = transportFailure()
  if (fail) {
    return fail
  }
  return json(body(scenarioFixtures[getScenario()]))
}

export function buildHandlers(): HttpHandler[] {
  return [
    http.get('*/api/setup/status', () => respond((f) => f.setupStatus)),
    http.get('*/api/overview', () => respond((f) => f.overview)),
    http.get('*/api/runs', () => respond((f) => f.runs)),
    http.get('*/api/runs/:runId', () => respond((f) => f.runDetail)),
    http.get('*/api/quarantine', () => respond((f) => f.quarantine)),
    http.get('*/api/audit', () => respond((f) => f.audit)),
    http.get('*/api/config', () => respond((f) => f.config)),
    http.get('*/api/services', () => respond((f) => f.services)),
    http.get('*/api/logs', () => respond((f) => f.logs)),
    http.get('*/api/debug/raw-table', () => respond((f) => f.rawTable)),
    http.get('*/api/pipeline', () => respond((f) => f.pipeline)),
    http.get('*/api/data/raw/:source/:table', () => respond((f) => f.rawData)),
    http.get('*/api/objects', () => respond((f) => f.objects)),
    http.get('*/api/objects/:object', () => respond((f) => f.objectRows)),
    http.get('*/api/templates', () => respond((f) => f.templates)),
    http.post('*/api/debug/mcp-call', () => respond((f) => f.mcpCall)),
    http.post('*/api/gateway/proposals', () => respond((f) => f.proposal)),
  ]
}
