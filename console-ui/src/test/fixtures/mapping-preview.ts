/**
 * Mapping Preview Mock 响应与错误体(typed against generated schemas)。
 * 场景矩阵:current 成功、draft+diff、空样本、401/403/422/500。
 */
import type { components } from '@/types/api'

export type MappingPreviewResponse = components['schemas']['MappingPreviewResponse']
export type MappingPreviewError = components['schemas']['MappingPreviewError']
export type MappingPreviewEvaluation = components['schemas']['MappingPreviewEvaluation']

const HASH_CURRENT = `sha256:${'aa'.repeat(32)}`
const HASH_DRAFT = `sha256:${'ee'.repeat(32)}`

function evaluation(partial?: {
  total?: number
  mapped?: number
  quarantined?: number
  rows?: MappingPreviewEvaluation['rows']
  enum_gaps?: MappingPreviewEvaluation['enum_gaps']
}): MappingPreviewEvaluation {
  const total = partial?.total ?? 1
  const mapped = partial?.mapped ?? total
  const quarantined = partial?.quarantined ?? 0
  return {
    summary: {
      total,
      mapped,
      quarantined,
      quarantine_rate: total === 0 ? 0 : quarantined / total,
      would_trip_breaker: false,
    },
    rows: partial?.rows ?? (total === 0
      ? []
      : [{
          sample_row_id: 'r0:C-001',
          status: 'mapped',
          output: {
            customer_code: 'C-001',
            name: '北极星钓具(美国)',
            payment_days: 60,
            contact: '***',
          },
          issues: [],
        }]),
    enum_gaps: partial?.enum_gaps ?? [],
    business_key_issues: { missing: 0, duplicate: 0, scope: 'sample' },
    derived_coverage: [],
  }
}

/** current binding 试算:有样本、无 diff 变化 */
export const mappingPreviewCurrent: MappingPreviewResponse = {
  object: 'Customer',
  source: 'digiwin_e10',
  mode: 'current',
  template_version: '0.1.0',
  current_binding_hash: HASH_CURRENT,
  candidate_binding_hash: HASH_CURRENT,
  sample: {
    anchor_table: 'CUSTOMER',
    offset: 0,
    limit: 50,
    requested_batch_id: null,
    sample_batch_ids: ['b-20260718-0910'],
    sampled_rows: 1,
    sample_fingerprint: 'fp-preview-current',
  },
  current: evaluation(),
  candidate: evaluation(),
  diff: {
    state: 'available',
    reason: null,
    summary: { rows_changed: 0, status_changed: 0, fields_changed: 0 },
    rows: [],
  },
  warnings: [],
}

/** draft 试算:与 current 有可观察字段差异 */
export const mappingPreviewDraft: MappingPreviewResponse = {
  object: 'Customer',
  source: 'digiwin_e10',
  mode: 'draft',
  template_version: '0.1.0',
  current_binding_hash: HASH_CURRENT,
  candidate_binding_hash: HASH_DRAFT,
  sample: {
    anchor_table: 'CUSTOMER',
    offset: 0,
    limit: 50,
    requested_batch_id: null,
    sample_batch_ids: ['b-20260718-0910'],
    sampled_rows: 1,
    sample_fingerprint: 'fp-preview-draft',
  },
  current: evaluation(),
  candidate: evaluation({
    rows: [{
      sample_row_id: 'r0:C-001',
      status: 'mapped',
      output: {
        customer_code: 'C-001',
        name: '北极星钓具(美国)',
        payment_days: 60,
      },
      issues: [],
    }],
  }),
  diff: {
    state: 'available',
    reason: null,
    summary: { rows_changed: 1, status_changed: 0, fields_changed: 1 },
    rows: [{
      sample_row_id: 'r0:C-001',
      status_before: 'mapped',
      status_after: 'mapped',
      fields: [{ field: 'contact', before: '***', after: null }],
    }],
  },
  warnings: ['只读预览:临时草稿不会保存或发布'],
}

/** 空 raw 样本:200 + 零计数,不是 404 */
export const mappingPreviewEmpty: MappingPreviewResponse = {
  object: 'Customer',
  source: 'digiwin_e10',
  mode: 'current',
  template_version: '0.1.0',
  current_binding_hash: HASH_CURRENT,
  candidate_binding_hash: HASH_CURRENT,
  sample: {
    anchor_table: 'CUSTOMER',
    offset: 0,
    limit: 50,
    requested_batch_id: null,
    sample_batch_ids: [],
    sampled_rows: 0,
    sample_fingerprint: 'fp-preview-empty',
  },
  current: evaluation({ total: 0, mapped: 0, quarantined: 0, rows: [] }),
  candidate: evaluation({ total: 0, mapped: 0, quarantined: 0, rows: [] }),
  diff: {
    state: 'available',
    reason: null,
    summary: { rows_changed: 0, status_changed: 0, fields_changed: 0 },
    rows: [],
  },
  warnings: [],
}

export function mappingPreviewError(
  status: number,
  reason_code: MappingPreviewError['reason_code'],
  detail: string,
  error_id: string | null = null,
): MappingPreviewError {
  return { status, reason_code, detail, error_id }
}

export const mappingPreviewUnauthorized = mappingPreviewError(
  401,
  'unauthorized',
  '需要有效的管理界面登录密码(Mock: preview unauthorized)',
)

export const mappingPreviewForbidden = mappingPreviewError(
  403,
  'token_not_configured',
  '控制台未配置可用于 raw 的 Token(Mock: preview forbidden)',
)

export const mappingPreviewDraftInvalid = mappingPreviewError(
  422,
  'draft_invalid',
  '草稿 binding 不满足契约(Mock: draft_invalid)',
)

export const mappingPreviewFailed = mappingPreviewError(
  500,
  'preview_failed',
  'Preview 内部失败(Mock: preview_failed)',
  'err-preview-mock',
)
