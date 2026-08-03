/**
 * fixture 基座:所有场景共享的完整数据集。
 *
 * 每个场景文件用 `satisfies ScenarioFixture` 做编译期校验(禁止 as any),
 * 只覆盖与基座有差异的部分;`healthy` 不得成为 fixture 解析失败时的回退。
 */
import type { components } from '@/types/api'
import {
  mappingPreviewCurrent,
  mappingPreviewDraft,
  mappingPreviewEmpty,
} from './mapping-preview'

export type HttpError = components['schemas']['HttpError']
export type SetupStatusResponse = components['schemas']['SetupStatusResponse']
export type OverviewResponse = components['schemas']['OverviewResponse']
export type RunSummary = components['schemas']['RunSummary']
export type SourceCard = components['schemas']['SourceCard']
export type SourceDetail = components['schemas']['SourceDetail']
export type IngestConnectionInfo = components['schemas']['IngestConnectionInfo']
export type QuarantineRecord = components['schemas']['QuarantineRecord']
export type AuditRecord = components['schemas']['AuditRecord']
export type AccessAuditPage = components['schemas']['AccessAuditPage']
export type RawTableCatalogResponse = components['schemas']['RawTableCatalogResponse']
export type ServicesStatusResponse = components['schemas']['ServicesStatusResponse']
export type ConfigViewResponse = components['schemas']['ConfigViewResponse']
export type LogsResponse = components['schemas']['LogsResponse']
export type RawTablePageResponse = components['schemas']['RawTablePageResponse']
export type McpToolResult = components['schemas']['McpToolResult']
export type PipelineResponse = components['schemas']['PipelineResponse']
export type PipelineNode = components['schemas']['PipelineNode']
export type RunDetailResponse = components['schemas']['RunDetailResponse']
export type RawDataPageResponse = components['schemas']['RawDataPageResponse']
export type ObjectSummary = components['schemas']['ObjectSummary']
export type ObjectRowsPageResponse = components['schemas']['ObjectRowsPageResponse']
export type TemplateObject = components['schemas']['TemplateObject']
export type TemplateMetric = components['schemas']['TemplateMetric']
export type QuarantineGroup = components['schemas']['QuarantineGroup']
export type QuarantineDetail = components['schemas']['QuarantineDetail']
export type RetryActionResult = components['schemas']['RetryActionResult']
export type ProposalResponse = components['schemas']['ProposalResponse']
export type QueryEvidenceDetailResponse = components['schemas']['QueryEvidenceDetailResponse']
export type DatasetSummary = components['schemas']['DatasetSummary']
export type DatasetDetail = components['schemas']['DatasetDetail']
export type DatasetActionResult = components['schemas']['DatasetActionResult']
export type ApplyActionResult = components['schemas']['ApplyActionResult']
export type MappingPreviewResponse = components['schemas']['MappingPreviewResponse']
export type MappingPreviewError = components['schemas']['MappingPreviewError']

/** 场景在各端点的 200 响应体;401/500 等传输级场景由 handler 统一短路 */
export interface ScenarioFixture {
  setupStatus: SetupStatusResponse
  overview: OverviewResponse
  runs: RunSummary[]
  quarantine: QuarantineRecord[]
  quarantineGroups: QuarantineGroup[]
  quarantineDetail: Record<number, QuarantineDetail>
  audit: AuditRecord[]
  accessAudit: AccessAuditPage
  rawCatalog: RawTableCatalogResponse
  services: ServicesStatusResponse
  config: ConfigViewResponse
  logs: LogsResponse
  rawTable: RawTablePageResponse
  mcpCall: McpToolResult
  pipeline: PipelineResponse
  runDetail: RunDetailResponse
  rawData: RawDataPageResponse
  objects: ObjectSummary[]
  objectRows: ObjectRowsPageResponse
  templates: TemplateObject[]
  templateMetrics: TemplateMetric[]
  proposal: ProposalResponse
  queryEvidenceDetail: QueryEvidenceDetailResponse
  proposalDetail: ProposalResponse
  datasets: DatasetSummary[]
  datasetDetails: Record<string, DatasetDetail>
  datasetAction: DatasetActionResult
  datasetActionStatus: number
  applyAction: ApplyActionResult
  applyActionStatus: number
  /** stage-only(publish=false) 专用响应;缺省时由 handler 派生 published=false。 */
  applyStageOnlyAction?: ApplyActionResult
  retryAction: RetryActionResult | { detail: string; reason_code?: string; run_id?: number | null; step_id?: number | null; detail_path?: string | null }
  retryActionStatus: number
  /** M3: mapping preview — current / draft / empty 成功体;错误场景用 status + error */
  mappingPreviewCurrent: MappingPreviewResponse
  mappingPreviewDraft: MappingPreviewResponse
  mappingPreviewEmpty: MappingPreviewResponse
  mappingPreviewStatus: number
  mappingPreviewError: MappingPreviewError | null
  /** 数据源管理:清单卡片与详情(按源名索引) */
  sources: SourceCard[]
  sourceDetails: Record<string, SourceDetail>
  /** 中间机接入信息 */
  ingestConnectionInfo: IngestConnectionInfo
}

const T = '2026-07-18T09:12:00+08:00'
const DS_PUBLISHED = 'ds-20260718-091100-a1b2'
const DS_PREVIOUS = 'ds-20260717-220000-c3d4'
const DS_READY = 'ds-20260718-095000-e5f6'
const DS_FAILED = 'ds-20260718-083000-f9a0'

const publishedObjectVersions = [
  {
    object: 'Customer',
    object_version: 'ov-cust-1',
    binding_hash: 'sha256:' + 'aa'.repeat(32),
    row_count: 36,
    batch_id: 'b-20260718-0910',
    build_table: 'objv_demo_customer_a1',
    status: 'published' as const,
    built_at: T,
    published_at: T,
  },
  {
    object: 'Material',
    object_version: 'ov-mat-1',
    binding_hash: 'sha256:' + 'bb'.repeat(32),
    row_count: 58,
    batch_id: 'b-20260718-0910',
    build_table: 'objv_demo_material_a1',
    status: 'published' as const,
    built_at: T,
    published_at: T,
  },
  {
    object: 'Quotation',
    object_version: 'ov-quo-1',
    binding_hash: 'sha256:' + 'cc'.repeat(32),
    row_count: 41,
    batch_id: 'b-20260718-0910',
    build_table: 'objv_demo_quotation_a1',
    status: 'published' as const,
    built_at: T,
    published_at: T,
  },
  {
    object: 'SalesOrder',
    object_version: 'ov-so-1',
    binding_hash: 'sha256:' + 'dd'.repeat(32),
    row_count: 52,
    batch_id: 'b-20260718-0910',
    build_table: 'objv_demo_salesorder_a1',
    status: 'published' as const,
    built_at: T,
    published_at: T,
  },
]

const readyObjectVersions = publishedObjectVersions.map((o) => ({
  ...o,
  status: 'built' as const,
  published_at: null,
  object_version: `${o.object_version}-ready`,
  build_table: `${o.build_table}_ready`,
}))

const baseDatasets: DatasetSummary[] = [
  {
    dataset_version: DS_READY,
    source: 'digiwin_e10',
    template_version: '0.1.0',
    status: 'building',
    built_at: '2026-07-18T09:50:00+08:00',
    published_at: null,
    previous_dataset_version: DS_PUBLISHED,
    error: null,
    error_id: null,
    object_manifest: ['Customer', 'Material', 'Quotation', 'SalesOrder'],
  },
  {
    dataset_version: DS_PUBLISHED,
    source: 'digiwin_e10',
    template_version: '0.1.0',
    status: 'published',
    built_at: T,
    published_at: T,
    previous_dataset_version: DS_PREVIOUS,
    error: null,
    error_id: null,
    object_manifest: ['Customer', 'Material', 'Quotation', 'SalesOrder'],
  },
  {
    dataset_version: DS_PREVIOUS,
    source: 'digiwin_e10',
    template_version: '0.1.0',
    status: 'retired',
    built_at: '2026-07-17T22:00:00+08:00',
    published_at: '2026-07-17T22:00:00+08:00',
    previous_dataset_version: null,
    error: null,
    error_id: null,
    object_manifest: ['Customer', 'Material', 'Quotation', 'SalesOrder'],
  },
  {
    dataset_version: DS_FAILED,
    source: 'digiwin_e10',
    template_version: '0.1.0',
    status: 'failed',
    built_at: '2026-07-18T08:30:00+08:00',
    published_at: null,
    previous_dataset_version: DS_PUBLISHED,
    error: 'build_failed',
    error_id: 'err-fixture-failed',
    object_manifest: ['Customer', 'Material', 'Quotation', 'SalesOrder'],
  },
]

const baseDatasetDetails: Record<string, DatasetDetail> = {
  [DS_READY]: {
    ...baseDatasets[0]!,
    objects: readyObjectVersions,
  },
  [DS_PUBLISHED]: {
    ...baseDatasets[1]!,
    objects: publishedObjectVersions,
  },
  [DS_PREVIOUS]: {
    ...baseDatasets[2]!,
    objects: publishedObjectVersions.map((o) => ({
      ...o,
      status: 'retired' as const,
      published_at: '2026-07-17T22:00:00+08:00',
    })),
  },
  [DS_FAILED]: {
    ...baseDatasets[3]!,
    objects: readyObjectVersions.map((o) => ({
      ...o,
      status: o.object === 'Quotation' ? 'failed' as const : 'built' as const,
      object_version: `${o.object_version}-failed`,
      build_table: null,
    })),
  },
}

/** 管道节点构造 helper:未提供的字段显式为 null(unknown 语义,不是省略) */
export function pipelineNode(
  partial: Partial<PipelineNode> & Pick<PipelineNode, 'node' | 'status'>,
): PipelineNode {
  return {
    status_reason: '',
    observed_at: null,
    last_success_at: null,
    last_failure_at: null,
    rows_in: null,
    rows_out: null,
    duration_ms: null,
    error: null,
    version: null,
    run_id: null,
    source: null,
    detail_path: null,
    ...partial,
  }
}

const basePipeline: PipelineResponse = {
  generated_at: T,
  overall_status: 'healthy',
  nodes: [
    pipelineNode({ node: 'erp', status: 'healthy', last_success_at: T }),
    pipelineNode({
      node: 'extract',
      status: 'healthy',
      last_success_at: T,
      rows_in: 1284,
      rows_out: 1284,
      duration_ms: 3210,
    }),
    pipelineNode({
      node: 'push',
      status: 'healthy',
      last_success_at: T,
      rows_in: 1284,
      rows_out: 1284,
      duration_ms: 1180,
    }),
    pipelineNode({ node: 'raw', status: 'healthy', last_success_at: T, rows_in: 1284, rows_out: 1284 }),
    pipelineNode({
      node: 'mapping',
      status: 'healthy',
      last_success_at: T,
      rows_in: 1284,
      rows_out: 1279,
      duration_ms: 640,
    }),
    pipelineNode({
      node: 'objects',
      status: 'healthy',
      last_success_at: T,
      rows_out: 1279,
      version: DS_PUBLISHED,
    }),
    pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: T }),
  ],
}

const baseObjects: ObjectSummary[] = [
  {
    object: 'Customer',
    display_name: '客户',
    domain: '销售',
    rows: 36,
    mapped_at: T,
    quarantined: 0,
    version: 'ov-cust-1',
    searchable: true,
  },
  {
    object: 'Material',
    display_name: '物料(品号)',
    domain: '产品',
    rows: 58,
    mapped_at: T,
    quarantined: 0,
    version: 'ov-mat-1',
    searchable: true,
  },
  {
    object: 'Quotation',
    display_name: '报价单',
    domain: '销售',
    rows: 41,
    mapped_at: T,
    quarantined: 0,
    version: 'ov-quo-1',
    searchable: true,
  },
  {
    object: 'SalesOrder',
    display_name: '销售订单',
    domain: '销售',
    rows: 52,
    mapped_at: T,
    quarantined: 0,
    version: 'ov-so-1',
    searchable: true,
  },
]

const customerColumns: RawDataPageResponse['columns'] = [
  {
    name: 'CUSTOMER_CODE',
    data_type: 'TEXT',
    role: 'business_key',
    classification: 'normal',
    masked: false,
    searchable: true,
  },
  {
    name: 'CUSTOMER_NAME',
    data_type: 'TEXT',
    role: 'data',
    classification: 'normal',
    masked: false,
    searchable: false,
  },
  {
    name: 'CONTACT_EMAIL',
    data_type: 'TEXT',
    role: 'data',
    classification: 'sensitive',
    masked: true,
    searchable: false,
  },
  {
    name: 'LAST_MODIFIED_DATE',
    data_type: 'TEXT',
    role: 'data',
    classification: 'unknown',
    masked: false,
    searchable: false,
  },
]

const customerObjColumns: RawDataPageResponse['columns'] = [
  {
    name: 'customer_code',
    data_type: 'TEXT',
    role: 'business_key',
    classification: 'normal',
    masked: false,
    searchable: true,
  },
  {
    name: 'name',
    data_type: 'TEXT',
    role: 'data',
    classification: 'normal',
    masked: false,
    searchable: false,
  },
  {
    name: 'payment_days',
    data_type: 'INTEGER',
    role: 'data',
    classification: 'normal',
    masked: false,
    searchable: false,
  },
  {
    name: 'contact',
    data_type: 'TEXT',
    role: 'data',
    classification: 'sensitive',
    masked: true,
    searchable: false,
  },
]

const baseTemplates: TemplateObject[] = [
  {
    object: 'Customer',
    display_name: '客户',
    description: '下单主体(渔具 OEM/ODM 场景多为海外品牌商 / 贸易商)',
    domain: '销售',
    keys: ['customer_code'],
    properties: [
      { name: 'customer_code', type: 'string', desc: '客户编号', sensitive: false },
      { name: 'name', type: 'string', desc: '客户名称', sensitive: false },
      { name: 'payment_days', type: 'int', desc: '账期(天)', sensitive: false },
      { name: 'contact', type: 'string', desc: '联系方式', sensitive: true },
    ],
    source_of_truth: 'digiwin_e10',
    quarantine_pending: 0,
    relations: [],
    bindings: [
      {
        source: 'digiwin_e10',
        tables: ['CUSTOMER', 'CURRENCY'],
        status: 'verified',
        enabled: true,
        key_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
        field_map: {
          customer_code: 'CUSTOMER.CUSTOMER_CODE',
          name: 'CUSTOMER.CUSTOMER_NAME',
          payment_days: 'CUSTOMER.PAYMENT_TERM_DAYS',
          contact: 'CUSTOMER.CONTACT_EMAIL',
        },
        watermark: 'CUSTOMER.LAST_MODIFIED_DATE',
        notes: 'E10-like 参考表形;真实环境以现场字典核对为准',
      },
    ],
    materialized: {
      state: 'materialized',
      source: 'digiwin_e10',
      rows: 36,
      mapped_at: T,
      batch_id: 'b-20260718-0910',
    },
  },
  {
    object: 'SalesOrder',
    display_name: '销售订单',
    description: '已确认订单主档;接单评审的历史成交依据',
    domain: '销售',
    keys: ['order_no'],
    properties: [
      { name: 'order_no', type: 'string', desc: '订单编号', sensitive: false },
      { name: 'customer', type: 'ref', desc: '客户', sensitive: false },
      { name: 'total_amount', type: 'money', desc: '订单金额', sensitive: false },
    ],
    source_of_truth: 'digiwin_e10',
    quarantine_pending: 0,
    relations: [
      { name: 'customer', target: 'Customer', cardinality: '1', desc: '订单客户' },
    ],
    bindings: [
      {
        source: 'digiwin_e10',
        tables: ['SALES_ORDER'],
        status: 'verified',
        enabled: true,
        key_map: { order_no: 'SALES_ORDER.ORDER_NO' },
        field_map: {
          order_no: 'SALES_ORDER.ORDER_NO',
          customer: 'SALES_ORDER.CUSTOMER_CODE',
          total_amount: 'SALES_ORDER.TOTAL_AMOUNT',
        },
        watermark: 'SALES_ORDER.LAST_MODIFIED_DATE',
      },
    ],
    materialized: {
      state: 'materialized',
      source: 'digiwin_e10',
      rows: 52,
      mapped_at: T,
      batch_id: 'b-20260718-0910',
    },
  },
  {
    object: 'QuoteResponse',
    display_name: '报价回复',
    description: '业务员对询价的回复记录;包含报价金额、有效期和客户反馈',
    domain: '销售',
    keys: ['quote_id'],
    properties: [
      { name: 'quote_id', type: 'string', desc: '报价编号', sensitive: false },
      { name: 'customer_code', type: 'ref', desc: '客户', sensitive: false },
      { name: 'amount', type: 'money', desc: '报价金额', sensitive: false },
      { name: 'currency', type: 'string', desc: '币种', sensitive: false, ref: 'Currency', enum_values: ['CNY', 'USD', 'EUR'] },
      { name: 'response_status', type: 'string', desc: '回复状态', sensitive: false, enum_values: ['pending', 'accepted', 'rejected', 'expired'] },
      { name: 'handler_notes', type: 'string', desc: '经办备注', sensitive: true },
    ],
    source_of_truth: 'digiwin_e10',
    quarantine_pending: 3,
    knowledge_refs: ['https://wiki.internal/报价流程SOP', 'https://wiki.internal/客户信用政策'],
    relations: [
      { name: 'customer', target: 'Customer', cardinality: '1', desc: '报价客户' },
      { name: 'material', target: 'Material', cardinality: '0..1', desc: '报价物料' },
    ],
    bindings: [
      {
        source: 'digiwin_e10',
        tables: ['QUOTE_RESPONSE', 'CURRENCY'],
        status: 'draft',
        enabled: true,
        key_map: { quote_id: 'QUOTE_RESPONSE.QUOTE_ID' },
        field_map: {
          quote_id: 'QUOTE_RESPONSE.QUOTE_ID',
          customer_code: 'QUOTE_RESPONSE.CUSTOMER_CODE',
          amount: 'QUOTE_RESPONSE.AMOUNT',
          currency: 'QUOTE_RESPONSE.CURRENCY_CODE',
          response_status: 'QUOTE_RESPONSE.STATUS',
          handler_notes: 'QUOTE_RESPONSE.NOTES',
        },
        enum_map: {
          currency: { CNY: '人民币', USD: '美元', EUR: '欧元' },
          response_status: { P: 'pending', A: 'accepted', R: 'rejected', X: 'expired' },
        },
        derived: {
          response_status: {
            rules: [
              { when: { 'QUOTE_RESPONSE.EXPIRY_DATE': null }, value: "'expired'" },
              { when: { 'QUOTE_RESPONSE.STATUS': 'P' }, value: "'pending'" },
              { when: { 'QUOTE_RESPONSE.STATUS': 'A' }, value: "'accepted'" },
              { when: { 'QUOTE_RESPONSE.STATUS': 'R' }, value: "'rejected'" },
            ],
            default: "'pending'",
          },
        },
        watermark: 'QUOTE_RESPONSE.LAST_MODIFIED_DATE',
        notes: '报价回复口径待现场业务确认;枚举映射以实际字典为准',
      },
      {
        source: 'crm_export',
        tables: ['QUOTATIONS'],
        status: 'disabled',
        enabled: false,
        key_map: { quote_id: 'QUOTATIONS.ID' },
        field_map: {
          quote_id: 'QUOTATIONS.ID',
          customer_code: 'QUOTATIONS.CLIENT_CODE',
          amount: 'QUOTATIONS.PRICE',
          currency: 'QUOTATIONS.CCY',
          response_status: 'QUOTATIONS.REPLY_STATUS',
          handler_notes: 'QUOTATIONS.COMMENT',
        },
        watermark: 'QUOTATIONS.UPDATED_AT',
        notes: 'CRM 导出仅作参考,暂不启用',
      },
    ],
    materialized: {
      state: 'not_materialized',
      source: null,
      rows: null,
      mapped_at: null,
      batch_id: null,
    },
    warnings: ['3 条报价记录暂存隔离区,待字段映射确认后物化'],
  },
  {
    object: 'Material',
    display_name: '物料(品号)',
    description: '渔具物料主数据:原材料、半成品、成品品号及规格',
    domain: '制造',
    keys: ['material_code'],
    properties: [
      { name: 'material_code', type: 'string', desc: '品号', sensitive: false },
      { name: 'name', type: 'string', desc: '品名', sensitive: false },
      { name: 'spec', type: 'string', desc: '规格', sensitive: false },
      { name: 'unit', type: 'string', desc: '单位', sensitive: false, enum_values: ['pc', 'kg', 'm', 'set'] },
      { name: 'unit_cost', type: 'money', desc: '单位成本', sensitive: true },
    ],
    source_of_truth: 'digiwin_e10',
    quarantine_pending: 0,
    relations: [],
    bindings: [
      {
        source: 'digiwin_e10',
        tables: ['MATERIAL_MASTER'],
        status: 'verified',
        enabled: true,
        key_map: { material_code: 'MATERIAL_MASTER.MATERIAL_CODE' },
        field_map: {
          material_code: 'MATERIAL_MASTER.MATERIAL_CODE',
          name: 'MATERIAL_MASTER.MATERIAL_NAME',
          spec: 'MATERIAL_MASTER.SPECIFICATION',
          unit: 'MATERIAL_MASTER.UNIT',
          unit_cost: 'MATERIAL_MASTER.UNIT_COST',
        },
        watermark: 'MATERIAL_MASTER.LAST_MODIFIED_DATE',
      },
    ],
    materialized: {
      state: 'materialized',
      source: 'digiwin_e10',
      rows: 58,
      mapped_at: T,
      batch_id: 'b-20260718-0910',
    },
  },
]

export const baseFixture: ScenarioFixture = {
  setupStatus: { needs_setup: false, config_path: '/home/d2a/platform.yaml', home: '/home/d2a' },
  overview: {
    landing: '/data/landing.sqlite',
    readonly: false,
    sources: [
      {
        source: 'digiwin_e10',
        state: [
          {
            table_name: 'raw_digiwin_e10__CUSTOMER',
            watermark_col: 'LAST_MODIFIED_DATE',
            high_water: '2026-07-18 08:30:00',
            last_run_at: '2026-07-18 09:10:02',
          },
          {
            table_name: 'raw_digiwin_e10__SALES_ORDER',
            watermark_col: 'LAST_MODIFIED_DATE',
            high_water: '2026-07-18 08:30:00',
            last_run_at: '2026-07-18 09:10:04',
          },
        ],
        quarantined: 0,
      },
    ],
    objects: [
      { object: 'Customer', display_name: '客户', rows: 36, mapped_at: '2026-07-18 09:11:20', quarantined: 0 },
      { object: 'Material', display_name: '物料(品号)', rows: 58, mapped_at: '2026-07-18 09:11:22', quarantined: 0 },
      { object: 'Quotation', display_name: '报价单', rows: 41, mapped_at: '2026-07-18 09:11:25', quarantined: 0 },
      { object: 'SalesOrder', display_name: '销售订单', rows: 52, mapped_at: '2026-07-18 09:11:30', quarantined: 0 },
    ],
    needs_setup: false,
    generated_at: T,
    summary: {
      raw_rows: 1284,
      object_rows: 187,
      materialized_objects: 4,
      template_objects: 5,
      quarantine_pending: 0,
      last_run_at: T,
      data_updated_at: T,
    },
    versions: { app: '0.2.0', template: '0.1.0', dataset: DS_PUBLISHED, object: DS_PUBLISHED },
    binding_summary: { verified: 10, draft: 0, disabled: 0 },
    alerts: [],
    recent_runs: [
      {
        id: 42,
        run_type: 'sync',
        source: 'digiwin_e10',
        status: 'ok',
        rows: 1284,
        tables: 4,
        started_at: T,
        finished_at: T,
      },
    ],
    sync_trend: [
      { bucket: '2026-07-18T08:00:00+08:00', rows: 36, runs: 1 },
      { bucket: '2026-07-18T09:00:00+08:00', rows: 1284, runs: 1 },
    ],
    count_notes: [
      { name: 'raw_rows', semantics: '当前配置范围内、未逻辑删除的 raw 活跃行数合计', source: 'raw_* 表 COUNT(*)' },
      { name: 'object_rows', semantics: '已物化 obj_* 表行数合计;与 raw 因隔离/软删有差', source: 'obj_* 表 COUNT(*)' },
    ],
  },
  runs: [
    {
      id: 42,
      type: 'sync',
      status: 'ok',
      source: 'digiwin_e10',
      started_at: '2026-07-18T09:10:00+08:00',
      finished_at: '2026-07-18T09:10:06+08:00',
      duration_ms: 6000,
      tables: 4,
      rows: 1284,
      quarantined: 0,
      dataset_version: null,
      detail: null,
      error: null,
      error_id: null,
    },
    {
      id: 41,
      type: 'sync',
      status: 'ok',
      source: 'digiwin_e10',
      started_at: '2026-07-18T08:10:00+08:00',
      finished_at: '2026-07-18T08:10:05+08:00',
      duration_ms: 5000,
      tables: 4,
      rows: 36,
      quarantined: 0,
      dataset_version: null,
      detail: null,
      error: null,
      error_id: null,
    },
  ],
  quarantine: [],
  audit: [
    {
      id: 1,
      ts: '2026-07-18T09:10:01+08:00',
      source: 'digiwin_e10',
      action: 'select',
      sql: 'SELECT * FROM CUSTOMER WHERE LAST_MODIFIED_DATE > ?',
      rows: 36,
      duration_ms: 12.4,
    },
  ],
  accessAudit: {
    items: [
      {
        id: 1,
        ts: '2026-07-18T09:20:00+08:00',
        subject: 'console-admin',
        resource_type: 'raw',
        source: 'digiwin_e10',
        resource: 'CUSTOMER',
        allowed: true,
        reason_code: 'ok',
        offset: 0,
        limit: 50,
        returned_rows: 24,
        request_id: null,
      },
      {
        id: 2,
        ts: '2026-07-18T09:21:00+08:00',
        subject: 'anonymous',
        resource_type: 'raw',
        source: null,
        resource: 'sqlite_master',
        allowed: false,
        reason_code: 'not_in_catalog',
        offset: null,
        limit: null,
        returned_rows: null,
        request_id: null,
      },
    ],
    offset: 0,
    limit: 50,
    total: 2,
    generated_at: T,
  },
  rawCatalog: {
    items: [
      {
        source: 'digiwin_e10',
        table: 'CUSTOMER',
        display_name: 'CUSTOMER',
        rows: 24,
        latest_batch_id: 'b-20260718-0910',
        extracted_at: T,
        searchable: true,
        classification_warning: true,
      },
      {
        source: 'digiwin_e10',
        table: 'SALES_ORDER',
        display_name: 'SALES_ORDER',
        rows: 97,
        latest_batch_id: 'b-20260718-0910',
        extracted_at: T,
        searchable: true,
        classification_warning: false,
      },
    ],
    warnings: [],
    generated_at: T,
  },
  services: {
    ingest: { ok: true, method: 'http' },
    mcp: { ok: true, method: 'http' },
    apply: { ok: true, method: 'log_mtime' },
    console: { ok: true, method: 'self' },
  },
  config: { app_version: '0.5.0', build_version: 'manual-808aaaa', needs_setup: false, templates: 'templates', landing: '/data/landing.sqlite' },
  logs: { ok: true, text: '2026-07-18 09:10:06 INFO sync done rows=1284' },
  rawTable: {
    table: 'raw_digiwin_e10__CUSTOMER',
    offset: 0,
    limit: 50,
    total: 36,
    rows: [
      {
        CUSTOMER_CODE: 'C-001',
        CUSTOMER_NAME: '北极星钓具(美国)',
        COUNTRY_REGION: 'US',
        LAST_MODIFIED_DATE: '2026-07-17 18:02:11',
      },
    ],
  },
  mcpCall: {
    object: 'Customer',
    display_name: '客户',
    rows: [
      { customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60, contact: '***' },
    ],
    meta: {
      query_id: 'qry_111111111111111111111111',
      tool: 'query_objects',
      target: 'Customer',
      row_count: 1,
      duration_ms: 12,
      masked_fields: ['contact'],
      warnings: ['binding 为 draft:字段映射按参考表形构造,口径未经现场校准'],
      evidence_scope: 'principal_session',
      session_id: 'd2a_session_mock_0123456789',
      result_digest: 'sha256:' + '11'.repeat(32),
      result_summary: {
        kind: 'query_objects',
        returned_row_count: 1,
        rows_preview: [
          { customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60, contact: '***' },
        ],
      },
      created_at: T,
      expires_at: '2026-07-19T09:12:00+08:00',
      dataset_version: DS_PUBLISHED,
      template_version: '0.1.0',
      binding_hashes: { Customer: 'sha256:' + 'aa'.repeat(32) },
    },
  },
  pipeline: basePipeline,
  runDetail: {
    id: 42,
    type: 'sync',
    status: 'ok',
    source: 'digiwin_e10',
    started_at: '2026-07-18T09:10:00+08:00',
    finished_at: '2026-07-18T09:10:06+08:00',
    duration_ms: 6000,
    tables: 4,
    rows: 1284,
    quarantined: 0,
    dataset_version: null,
    detail: null,
    error: null,
    error_id: null,
    steps_state: 'available',
    steps: [
      {
        id: 1,
        ordinal: 1,
        kind: 'table',
        name: 'CUSTOMER',
        status: 'ok',
        started_at: '2026-07-18T09:10:01+08:00',
        finished_at: '2026-07-18T09:10:03+08:00',
        duration_ms: 2000,
        batch_id: 'b-20260718-0910',
        rows_in: 36,
        rows_out: 36,
        quarantined: 0,
        repaired: null,
        soft_deleted: null,
        watermark_before: '2026-07-17 08:30:00',
        watermark_after: '2026-07-18 08:30:00',
        error: null,
        error_id: null,
      },
    ],
  },
  rawData: {
    source: 'digiwin_e10',
    table: 'CUSTOMER',
    columns: customerColumns,
    rows: [
      {
        CUSTOMER_CODE: 'C-001',
        CUSTOMER_NAME: '北极星钓具(美国)',
        CONTACT_EMAIL: '***',
        LAST_MODIFIED_DATE: '2026-07-17 18:02:11',
      },
    ],
    truncations: [],
    offset: 0,
    limit: 50,
    total: 36,
    sort: 'pk:CUSTOMER_CODE',
    query: '',
    searchable: true,
    warnings: ['列 LAST_MODIFIED_DATE 分类未知,按未确认处理展示'],
    generated_at: T,
  },
  objects: baseObjects,
  objectRows: {
    object: 'Customer',
    columns: customerObjColumns,
    rows: [{ customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60, contact: '***' }],
    truncations: [],
    offset: 0,
    limit: 50,
    total: 36,
    sort: 'pk:customer_code',
    query: '',
    searchable: true,
    warnings: [],
    generated_at: T,
  },
  templates: baseTemplates,
  proposal: {
    proposal_id: 'prp_222222222222222222222222',
    at: T,
    session_id: 'd2a_session_mock_0123456789',
    source: 'digiwin_e10',
    dataset_version: DS_PUBLISHED,
    object: 'Quotation',
    action: 'quote_review',
    action_desc: '发起接单评审链生成评审卡',
    tier: '说',
    conclusion: '建议接单:毛利高于基线,账期在客户政策内',
    evidence: [
      {
        claim: '该客户近 90 天成交 6 单,平均毛利 31%',
        query: {
          query_id: 'qry_111111111111111111111111',
          source: 'digiwin_e10',
          tool: 'query_objects',
          target: 'Customer',
          normalized_query: {
            tool: 'query_objects',
            source: 'digiwin_e10',
            object: 'Customer',
            filters: {},
            order_by: null,
            desc: false,
            limit: 20,
          },
          dataset_version: DS_PUBLISHED,
          template_version: '0.1.0',
          binding_hashes: { Customer: 'sha256:' + 'aa'.repeat(32) },
          result_digest: 'sha256:' + '11'.repeat(32),
          result_summary: {
            kind: 'query_objects',
            returned_row_count: 1,
            rows_preview: [
              { customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60, contact: '***' },
            ],
          },
          warnings: ['binding 为 draft:字段映射按参考表形构造,口径未经现场校准'],
          created_at: T,
          expires_at: null,
        },
      },
    ],
    caveats: ['指标口径:毛利率按不含税金额计算'],
    governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
  },
  queryEvidenceDetail: {
    query_id: 'qry_111111111111111111111111',
    source: 'digiwin_e10',
    tool: 'query_objects',
    target: 'Customer',
    session_id: 'd2a_session_mock_0123456789',
    evidence_scope: 'principal_session',
    normalized_query: {
      tool: 'query_objects',
      source: 'digiwin_e10',
      object: 'Customer',
      filters: {},
      order_by: null,
      desc: false,
      limit: 20,
    },
    dataset_version: DS_PUBLISHED,
    template_version: '0.1.0',
    binding_hashes: { Customer: 'sha256:' + 'aa'.repeat(32) },
    result_digest: 'sha256:' + '11'.repeat(32),
    result_summary: {
      kind: 'query_objects',
      returned_row_count: 1,
      rows_preview: [
        { customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60, contact: '***' },
      ],
    },
    warnings: ['binding 为 draft:字段映射按参考表形构造,口径未经现场校准'],
    row_count: 1,
    created_at: T,
    expires_at: '2026-07-19T09:12:00+08:00',
  },
  proposalDetail: {
    proposal_id: 'prp_222222222222222222222222',
    at: T,
    session_id: 'd2a_session_mock_0123456789',
    source: 'digiwin_e10',
    dataset_version: DS_PUBLISHED,
    object: 'Quotation',
    action: 'quote_review',
    action_desc: '发起接单评审链生成评审卡',
    tier: '说',
    conclusion: '建议接单:毛利高于基线,账期在客户政策内',
    evidence: [
      {
        claim: '该客户近 90 天成交 6 单,平均毛利 31%',
        query: {
          query_id: 'qry_111111111111111111111111',
          source: 'digiwin_e10',
          tool: 'query_objects',
          target: 'Customer',
          normalized_query: {
            tool: 'query_objects',
            source: 'digiwin_e10',
            object: 'Customer',
            filters: {},
            order_by: null,
            desc: false,
            limit: 20,
          },
          dataset_version: DS_PUBLISHED,
          template_version: '0.1.0',
          binding_hashes: { Customer: 'sha256:' + 'aa'.repeat(32) },
          result_digest: 'sha256:' + '11'.repeat(32),
          result_summary: {
            kind: 'query_objects',
            returned_row_count: 1,
            rows_preview: [
              { customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60, contact: '***' },
            ],
          },
          warnings: ['binding 为 draft:字段映射按参考表形构造,口径未经现场校准'],
          created_at: T,
          expires_at: null,
        },
      },
    ],
    caveats: ['指标口径:毛利率按不含税金额计算'],
    governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
  },

  // ---- M5: quarantine groups ----
  quarantineGroups: [],

  // ---- M5: quarantine detail (keyed by id) ----
  quarantineDetail: {},

  // ---- M5: template metrics ----
  templateMetrics: [
    {
      metric: 'gross_margin_pct',
      display_name: '毛利率',
      formula: 'sum(revenue - cost) * 100.0 / sum(revenue)',
      status: 'certified',
      calibration_state: 'calibrated',
      freshness_sla: 'T+1',
      caveats: '',
      dimensions: ['Customer', 'Material'],
      grain: ['SalesOrder'],
    },
    {
      metric: 'avg_payment_days',
      display_name: '平均账期',
      formula: 'avg(payment_days)',
      status: 'draft',
      calibration_state: 'uncalibrated',
      freshness_sla: 'T+1',
      caveats: '指标口径待现场校准',
    },
    {
      metric: 'quote_response_hours',
      display_name: '报价响应时长',
      formula: 'avg(response_hours)',
      status: 'draft',
      calibration_state: 'uncalibrated',
      freshness_sla: 'T+1',
      caveats: '响应时长口径待现场校准;含非工作时间统计口径未定',
      dimensions: ['Customer'],
      grain: ['QuoteResponse'],
    },
    {
      metric: 'inventory_days',
      display_name: '库存周转天数',
      formula: 'avg(inventory_qty) * 365 / sum(consumed_qty)',
      status: 'deprecated',
      calibration_state: 'deprecated',
      freshness_sla: '已下线',
      caveats: '该指标已被 inventory_turnover_rate 替代',
      dimensions: ['Material'],
      grain: ['Material'],
    },
  ],

  // ---- M5: retry action (healthy → success) ----
  retryAction: {
    executed: true,
    object: 'Customer',
    status: 'ok',
    mapped: 36,
    quarantined: 0,
    total: 36,
    run_id: 42,
    step_id: 1,
    detail_path: '/runs/42',
    dataset_version: DS_PUBLISHED,
  },
  retryActionStatus: 200,

  // ---- M2: datasets / apply ----
  datasets: baseDatasets,
  datasetDetails: baseDatasetDetails,
  datasetAction: {
    executed: true,
    dataset_version: DS_PUBLISHED,
    note: 'published',
  },
  datasetActionStatus: 200,
  applyAction: {
    executed: true,
    results: [
      { object: 'Customer', total: 36, mapped: 36, quarantined: 0, status: 'ok' },
    ],
    aborted: [],
    dataset_version: DS_PUBLISHED,
    published: true,
    previous_dataset_version: DS_PREVIOUS,
  },
  applyActionStatus: 200,
  applyStageOnlyAction: {
    executed: true,
    results: [
      { object: 'Customer', total: 36, mapped: 36, quarantined: 0, status: 'ok' },
    ],
    aborted: [],
    dataset_version: DS_READY,
    published: false,
    previous_dataset_version: DS_PUBLISHED,
  },

  // ---- M3: mapping preview ----
  mappingPreviewCurrent,
  mappingPreviewDraft,
  mappingPreviewEmpty,
  mappingPreviewStatus: 200,
  mappingPreviewError: null,

  // ---- 数据源管理 ----
  sources: [
    {
      source: 'digiwin_e10',
      display_name: '鼎捷 E10',
      source_type: 'erp',
      access_mode: 'local',
      status: 'healthy',
      status_reason: '',
      tables: 2,
      quarantined: 0,
      last_run_at: T,
      last_run_status: 'ok',
      registered: false,
      registry_status: null,
    },
  ],
  sourceDetails: {
    digiwin_e10: {
      source: 'digiwin_e10',
      display_name: '鼎捷 E10',
      source_type: 'erp',
      access_mode: 'local',
      status: 'healthy',
      status_reason: '',
      tables: 2,
      quarantined: 0,
      last_run_at: T,
      last_run_status: 'ok',
      registered: false,
      registry_status: null,
      table_states: [
        {
          table_name: 'CUSTOMER',
          watermark_col: 'LAST_MODIFIED_DATE',
          high_water: '2026-07-18 08:30:00',
          last_run_at: T,
          rows: 24,
        },
        {
          table_name: 'SALES_ORDER',
          watermark_col: 'LAST_MODIFIED_DATE',
          high_water: '2026-07-18 08:30:00',
          last_run_at: T,
          rows: 52,
        },
      ],
      recent_runs: [
        {
          id: 42,
          type: 'sync',
          status: 'ok',
          source: 'digiwin_e10',
          started_at: T,
          finished_at: T,
          duration_ms: 1800,
          tables: 2,
          rows: 76,
          quarantined: 0,
          dataset_version: null,
          detail: null,
          error: null,
          error_id: null,
        },
      ],
    },
  },

  // ---- 中间机接入信息 ----
  ingestConnectionInfo: {
    endpoint: 'http://192.168.1.10:8850',
    token_configured: true,
    token_masked: 'tok-…56',
    active_protocol_version: '3',
    supported_protocol_versions: ['2', '3'],
  },
}
