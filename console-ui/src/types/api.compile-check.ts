/**
 * 生成类型的编译期契约校验(仅由 vue-tsc 检查,不进运行时 bundle)。
 *
 * 证明:
 * - SetupResponse 能按必填 ok 的 boolean 字面量收窄(M2-T03 门禁);
 * - 递归 JsonValue 不是 unknown/any,动态 JSON 有确定类型;
 * - v0.2 契约桩的具名 schema 与状态字面量可用。
 *
 * 本文件不导出运行时代码;被 import 的符号只用于类型断言。
 */
import type { components, paths } from './api'

// ---- SetupResponse:ok === true 收窄出 message,ok !== true 收窄出 errors ----

type SetupResponse =
  paths['/api/setup']['post']['responses'][200]['content']['application/json']

declare const setupResp: SetupResponse

export function narrowSetup(): string | string[] {
  if (setupResp.ok === true) {
    // SetupSuccessResponse:message 必填 string
    return setupResp.message
  }
  // SetupFailureResponse:errors 必填,元素具名 FieldError
  return setupResp.errors.map((e) => `${e.field}:${e.message}`)
}

// ---- JsonValue:递归、非 unknown、非 any ----

type JsonOut = components['schemas']['JsonValue-Output']

export const jsonSamples: JsonOut[] = [
  null,
  1,
  2.5,
  's',
  true,
  [1, 'a', null, { k: [false] }],
  { nested: { list: [1, 2, { x: 'y' }] } },
]

// unknown 会让这两行编译失败;any 会让 @ts-expect-error 行编译失败(双重防呆)
export const jsonConcrete: { readonly a: number } = { a: 1 }
export const jsonAsObject: JsonOut = jsonConcrete

// @ts-expect-error JsonValue 不接受 undefined(unknown 才会接受)
export const jsonUndefined: JsonOut = undefined

// ---- v0.2 契约桩:具名 schema、状态字面量、分页字段 ----

type PipelineResponse = components['schemas']['PipelineResponse']
export const pipelineStatuses: PipelineResponse['nodes'][number]['status'][] = [
  'unknown',
  'idle',
  'running',
  'healthy',
  'warning',
  'failed',
  'stale',
]

type RunDetail = components['schemas']['RunDetailResponse']
export const runTypes: RunDetail['type'][] = ['sync', 'apply', 'reconcile', 'ingest', 'validation']
export const runStatuses: RunDetail['status'][] = [
  'running',
  'ok',
  'paused',
  'failed',
  'aborted',
]

type TemplateObject = components['schemas']['TemplateObject']
export const bindingStatuses: TemplateObject['bindings'][number]['status'][] = [
  'draft',
  'verified',
  'disabled',
]

// 桩端点的 200 响应通过 paths 可达(前端 service 层直接消费此形状)
type RawPage =
  paths['/api/data/raw/{source}/{table}']['get']['responses'][200]['content']['application/json']
export const rawPageShape: Pick<RawPage, 'source' | 'table' | 'offset' | 'limit' | 'total'> =
  {
    source: 's',
    table: 'raw_t',
    offset: 0,
    limit: 50,
    total: 0,
  }

// HttpError:错误体只有 detail 字符串,不会被误认为成功响应
type HttpError = components['schemas']['HttpError']
export const errShape: HttpError = { detail: 'x' }

// ---- M5:T08 evidence/detail 契约 ----

type ProposalInput = components['schemas']['ProposalEvidenceInput']
export const proposalInputShape: ProposalInput = {
  claim: 'x',
  query_id: 'qry_1234567890abcdef12345678',
  result_digest: 'sha256:' + '1'.repeat(64),
}

type QueryDetail =
  paths['/api/gateway/queries/{query_id}']['get']['responses'][200]['content']['application/json']
export const queryDetailShape: Pick<QueryDetail, 'query_id' | 'session_id' | 'result_digest'> = {
  query_id: 'qry_1234567890abcdef12345678',
  session_id: 'd2a_session_compile_check_1234',
  result_digest: 'sha256:' + '2'.repeat(64),
}

type ProposalDetail =
  paths['/api/gateway/proposals/{proposal_id}']['get']['responses'][200]['content']['application/json']
export const proposalDetailShape: Pick<ProposalDetail, 'proposal_id' | 'session_id' | 'source'> = {
  proposal_id: 'prp_1234567890abcdef12345678',
  session_id: 'd2a_session_compile_check_1234',
  source: 'digiwin_e10',
}
