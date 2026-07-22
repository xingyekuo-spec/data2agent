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
import { lineageAvailable } from './fixtures/lineage'
import { getScenario, scenarioFixtures } from './scenario'
import type { components } from '@/types/api'

type RunSummary = components['schemas']['RunSummary']
type AuditRecord = components['schemas']['AuditRecord']
type AccessAuditPage = components['schemas']['AccessAuditPage']
type RawDataPageResponse = components['schemas']['RawDataPageResponse']
type ObjectRowsPageResponse = components['schemas']['ObjectRowsPageResponse']
type QuarantineRecord = components['schemas']['QuarantineRecord']

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

function lineageFixture() {
  return lineageAvailable()
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
    limit: Math.min(Number(url.searchParams.get('limit') ?? 50), 100),
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

/** M5: quarantine list with pagination + filtering */
function quarantineList(fixture: ScenarioFixture, request: Request): { items: QuarantineRecord[]; total: number } {
  const url = new URL(request.url)
  const source = url.searchParams.get('source')
  const object = url.searchParams.get('object')
  const reason = url.searchParams.get('reason')
  const filtered = fixture.quarantine.filter((r) =>
    (source === null || r.source === source)
    && (object === null || r.object === object)
    && (reason === null || r.reason.includes(reason)),
  )
  return page(filtered, request)
}

/** M5: check Bearer auth for quarantine detail (simplified: checks Authorization header presence) */
function authCheck(request: Request): HttpResponse<string> | null {
  const auth = request.headers.get('Authorization')
  if (!auth || !auth.startsWith('Bearer ')) {
    const body: HttpError = { detail: '缺少或无效的 Bearer Token' }
    return json(body, 401)
  }
  return null
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

    // ---- M5: quarantine list (paginated, X-Total-Count) ----
    http.get('*/api/quarantine', ({ request }) => {
      const result = quarantineList(scenarioFixtures[getScenario()], request)
      return respond(() => result.items, () => ({ 'X-Total-Count': String(result.total) }))
    }),

    // ---- M5: quarantine groups ----
    http.get('*/api/quarantine/groups', () => respond((f) => f.quarantineGroups)),

    // ---- M5: quarantine detail (requires Bearer auth) ----
    http.get('*/api/quarantine/:id', ({ request, params }) => {
      const authFail = authCheck(request)
      if (authFail) return authFail

      const fail = transportFailure()
      if (fail) return fail

      const fixture = scenarioFixtures[getScenario()]
      const id = Number(params.id)
      const detail = fixture.quarantineDetail?.[id]
      if (!detail) {
        const body: HttpError = { detail: `隔离记录 ${id} 不存在或已处理` }
        return json(body, 404)
      }
      return json(detail, 200)
    }),

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

    // ---- M4: field lineage ----
    http.get('*/api/objects/:object/:key/lineage', () => {
      const fail = transportFailure()
      if (fail) return fail
      return json(lineageFixture())
    }),

    // ---- M5: templates (real data, no longer 501) ----
    http.get('*/api/templates', () => respond((f) => f.templates)),

    // ---- M5: templates metrics ----
    http.get('*/api/templates/metrics', () => respond((f) => f.templateMetrics)),

    http.post('*/api/debug/mcp-call', () => respond((f) => f.mcpCall)),
    http.post('*/api/gateway/proposals', () => respond((f) => f.proposal)),

    // ---- M5: retry action (structured RetryActionResult / RetryActionError) ----
    http.post('*/api/actions/retry', ({ request }) => {
      const fail = transportFailure()
      if (fail) return fail

      // Check auth
      const authFail = authCheck(request)
      if (authFail) return authFail

      const fixture = scenarioFixtures[getScenario()]
      return json(fixture.retryAction, fixture.retryActionStatus ?? 200)
    }),

    // ---- M2: datasets list/detail/publish/rollback + apply ----
    http.get('*/api/datasets', ({ request }) => {
      const fail = transportFailure()
      if (fail) return fail
      const fixture = scenarioFixtures[getScenario()]
      const url = new URL(request.url)
      const status = url.searchParams.get('status')
      const source = url.searchParams.get('source')
      const limit = Number(url.searchParams.get('limit') ?? '50')
      const offset = Number(url.searchParams.get('offset') ?? '0')
      let items = fixture.datasets
      if (source) {
        items = items.filter((d) => d.source === source)
      }
      if (status) {
        items = items.filter((d) => d.status === status)
      }
      const page = items.slice(offset, offset + limit)
      return json(page, 200, { 'X-Total-Count': String(items.length) })
    }),
    http.get('*/api/datasets/:version', ({ params }) => {
      const fail = transportFailure()
      if (fail) return fail
      const fixture = scenarioFixtures[getScenario()]
      const version = String(params.version)
      const detail = fixture.datasetDetails[version]
      if (!detail) {
        const body: HttpError = { detail: `数据集版本 ${version} 不存在` }
        return json(body, 404)
      }
      return json(detail)
    }),
    http.post('*/api/datasets/:version/publish', ({ params }) => {
      const fail = transportFailure()
      if (fail) return fail
      const fixture = scenarioFixtures[getScenario()]
      const version = String(params.version)
      const detail = fixture.datasetDetails[version]
      if (!detail) {
        return json({ detail: `数据集版本 ${version} 不存在` } satisfies HttpError, 404)
      }
      if (detail.status !== 'building') {
        return json({ detail: 'not_ready' } satisfies HttpError, 409)
      }
      const manifest = detail.object_manifest ?? []
      const objects = detail.objects ?? []
      const ready =
        manifest.length > 0
        && objects.length === manifest.length
        && manifest.every(
          (name) => objects.find((o) => o.object === name)?.status === 'built',
        )
      if (!ready) {
        return json({ detail: 'not_ready' } satisfies HttpError, 409)
      }
      return json(
        { executed: true, dataset_version: version, note: 'published' },
        fixture.datasetActionStatus ?? 200,
      )
    }),
    http.post('*/api/datasets/:version/rollback', ({ params }) => {
      const fail = transportFailure()
      if (fail) return fail
      const fixture = scenarioFixtures[getScenario()]
      const version = String(params.version)
      // `{version}` 是要恢复的 retired 目标(current.previous),不是当前 published。
      const target = fixture.datasetDetails[version]
      if (!target) {
        return json({ detail: `数据集版本 ${version} 不存在` } satisfies HttpError, 404)
      }
      const current = fixture.datasets.find((d) => d.status === 'published')
      if (current && current.dataset_version === version) {
        return json(
          { executed: false, dataset_version: version, note: 'already current' },
          fixture.datasetActionStatus ?? 200,
        )
      }
      if (target.status !== 'retired') {
        return json({ detail: 'illegal_state' } satisfies HttpError, 409)
      }
      if (!current || current.previous_dataset_version !== version) {
          return json({ detail: 'not_direct_previous' } satisfies HttpError, 409)
      }
      return json(
        {
          executed: true,
          dataset_version: version,
          note: 'rolled back',
        },
        fixture.datasetActionStatus ?? 200,
      )
    }),
    http.post('*/api/actions/apply', async ({ request }) => {
      const fail = transportFailure()
      if (fail) return fail
      const fixture = scenarioFixtures[getScenario()]
      let publish = true
      try {
        const body = await request.json() as { publish?: boolean }
        if (body && body.publish === false) {
          publish = false
        }
      } catch {
        publish = true
      }
      if (publish) {
        return json(fixture.applyAction, fixture.applyActionStatus ?? 200)
      }
      const stage = fixture.applyStageOnlyAction ?? {
        ...fixture.applyAction,
        published: false,
        dataset_version: 'ds-20260718-095000-e5f6',
        previous_dataset_version: fixture.applyAction.previous_dataset_version,
      }
      return json(stage, fixture.applyActionStatus ?? 200)
    }),

    // ---- M3: mapping preview (explicit; undeclared /api would throw) ----
    http.post('*/api/mappings/:object/preview', async ({ request, params }) => {
      const scenario = getScenario()
      // Preview 错误体必须是 MappingPreviewError(含 reason_code),不能回落普通 HttpError。
      if (scenario === 'token-invalid') {
        return json(
          {
            status: 401,
            reason_code: 'unauthorized',
            detail: '需要有效的管理界面登录密码(Mock: token-invalid)',
            error_id: null,
          },
          401,
        )
      }
      if (scenario === 'unknown-error') {
        return json(
          {
            status: 500,
            reason_code: 'preview_failed',
            detail: '未处理异常(Mock: unknown-error)',
            error_id: 'err-preview-mock',
          },
          500,
        )
      }

      const fixture = scenarioFixtures[scenario]
      if (fixture.mappingPreviewStatus !== 200 && fixture.mappingPreviewError) {
        return json(fixture.mappingPreviewError, fixture.mappingPreviewStatus)
      }

      let body: { draft_binding?: unknown; source?: string; sample?: unknown } = {}
      try {
        body = await request.json() as typeof body
      } catch {
        body = {}
      }

      const objectName = String(params.object)
      let response = fixture.mappingPreviewCurrent
      if (scenario === 'empty-install') {
        response = fixture.mappingPreviewEmpty
      } else if (body.draft_binding != null) {
        response = fixture.mappingPreviewDraft
      }

      return json({
        ...response,
        object: objectName,
        source: typeof body.source === 'string' ? body.source : response.source,
      })
    }),
  ]
}
