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
import type { components } from '@/types/api'

type RunSummary = components['schemas']['RunSummary']
type AuditRecord = components['schemas']['AuditRecord']
type AccessAuditPage = components['schemas']['AccessAuditPage']
type RawDataPageResponse = components['schemas']['RawDataPageResponse']
type ObjectRowsPageResponse = components['schemas']['ObjectRowsPageResponse']

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
function json<T>(body: T, status = 200, headers: Record<string, string> = {}): HttpResponse<string> {
  return new HttpResponse(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
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

function respond<T>(
  body: (fixture: ScenarioFixture) => T,
  extraHeaders?: (fixture: ScenarioFixture) => Record<string, string>,
): HttpResponse<string> {
  const fail = transportFailure()
  if (fail) {
    return fail
  }
  const fixture = scenarioFixtures[getScenario()]
  return json(body(fixture), 200, extraHeaders ? extraHeaders(fixture) : {})
}

function pageParams(request: Request): { limit: number; offset: number } {
  const url = new URL(request.url)
  return {
    limit: Number(url.searchParams.get('limit') ?? 50),
    offset: Number(url.searchParams.get('offset') ?? 0),
  }
}

function page<T>(items: T[], request: Request): { items: T[]; total: number } {
  const { limit, offset } = pageParams(request)
  return { items: items.slice(offset, offset + limit), total: items.length }
}

function inTimeRange(value: string, from: string | null, to: string | null): boolean {
  const t = Date.parse(value)
  return (from === null || t >= Date.parse(from)) && (to === null || t < Date.parse(to))
}

function runsFor(fixture: ScenarioFixture, request: Request): { items: RunSummary[]; total: number } {
  const url = new URL(request.url)
  const type = url.searchParams.get('type')
  const status = url.searchParams.get('status')
  return page(
    fixture.runs.filter((r) =>
      (type === null || r.type === type) && (status === null || r.status === status),
    ),
    request,
  )
}

function auditFor(fixture: ScenarioFixture, request: Request): { items: AuditRecord[]; total: number } {
  const url = new URL(request.url)
  const source = url.searchParams.get('source')
  const action = url.searchParams.get('action')
  const from = url.searchParams.get('from')
  const to = url.searchParams.get('to')
  return page(
    fixture.audit.filter((r) =>
      (source === null || r.source === source)
      && (action === null || r.action === action)
      && inTimeRange(r.ts, from, to),
    ),
    request,
  )
}

function accessFor(fixture: ScenarioFixture, request: Request): AccessAuditPage {
  const url = new URL(request.url)
  const subject = url.searchParams.get('subject')
  const resourceType = url.searchParams.get('resource_type')
  const allowed = url.searchParams.get('allowed')
  const from = url.searchParams.get('from')
  const to = url.searchParams.get('to')
  const filtered = fixture.accessAudit.items.filter((r) =>
    (subject === null || r.subject === subject)
    && (resourceType === null || r.resource_type === resourceType)
    && (allowed === null || String(r.allowed) === allowed)
    && inTimeRange(r.ts, from, to),
  )
  const { limit, offset } = pageParams(request)
  return {
    ...fixture.accessAudit,
    items: filtered.slice(offset, offset + limit),
    offset,
    limit,
    total: filtered.length,
  }
}

function dataPage<T extends RawDataPageResponse | ObjectRowsPageResponse>(
  data: T,
  request: Request,
): T {
  const url = new URL(request.url)
  const { limit, offset } = pageParams(request)
  const q = url.searchParams.get('q') ?? ''
  const searchable = data.columns.filter((c) => c.searchable).map((c) => c.name)
  const rows = q
    ? data.rows.filter((row) =>
      searchable.some((name) => String(row[name] ?? '').includes(q)),
    )
    : data.rows
  return {
    ...data,
    rows: rows.slice(offset, offset + limit),
    offset,
    limit,
    total: q ? rows.length : data.total,
    query: q,
  }
}

export function buildHandlers(): HttpHandler[] {
  return [
    http.get('*/api/setup/status', () => respond((f) => f.setupStatus)),
    http.get('*/api/overview', () => respond((f) => f.overview)),
    http.get('*/api/runs', ({ request }) => {
      const result = runsFor(scenarioFixtures[getScenario()], request)
      return respond(() => result.items, () => ({ 'X-Total-Count': String(result.total) }))
    }),
    http.get('*/api/runs/:runId', () => respond((f) => f.runDetail)),
    http.get('*/api/quarantine', () => respond((f) => f.quarantine)),
    http.get('*/api/audit', ({ request }) => {
      const result = auditFor(scenarioFixtures[getScenario()], request)
      return respond(() => result.items, () => ({ 'X-Total-Count': String(result.total) }))
    }),
    http.get('*/api/audit/access', ({ request }) => respond((f) => accessFor(f, request))),
    http.get('*/api/config', () => respond((f) => f.config)),
    http.get('*/api/services', () => respond((f) => f.services)),
    http.get('*/api/logs', () => respond((f) => f.logs)),
    http.get('*/api/debug/raw-table', () => respond((f) => f.rawTable)),
    http.get('*/api/pipeline', () => respond((f) => f.pipeline)),
    http.get('*/api/data/raw', () => respond((f) => f.rawCatalog)),
    http.get('*/api/data/raw/:source/:table', ({ params, request }) => {
      const fail = transportFailure()
      if (fail) {
        return fail
      }
      const fixture = scenarioFixtures[getScenario()]
      if (params.source !== fixture.rawData.source || params.table !== fixture.rawData.table) {
        const body: HttpError = { detail: `Mock raw 资源不存在: ${params.source}/${params.table}` }
        return json(body, 404)
      }
      return json(dataPage(fixture.rawData, request))
    }),
    http.get('*/api/objects', () => respond((f) => f.objects)),
    http.get('*/api/objects/:object', ({ params, request }) => {
      const fail = transportFailure()
      if (fail) {
        return fail
      }
      const fixture = scenarioFixtures[getScenario()]
      if (params.object !== fixture.objectRows.object) {
        const body: HttpError = { detail: `Mock object 资源不存在: ${params.object}` }
        return json(body, 404)
      }
      return json(dataPage(fixture.objectRows, request))
    }),
    http.get('*/api/templates', () => respond((f) => f.templates)),
    http.post('*/api/debug/mcp-call', () => respond((f) => f.mcpCall)),
    http.post('*/api/gateway/proposals', () => respond((f) => f.proposal)),
  ]
}
