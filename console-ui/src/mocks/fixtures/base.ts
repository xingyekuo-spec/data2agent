/**
 * fixture 基座:所有场景共享的完整数据集。
 *
 * 每个场景文件用 `satisfies ScenarioFixture` 做编译期校验(禁止 as any),
 * 只覆盖与基座有差异的部分;`healthy` 不得成为 fixture 解析失败时的回退。
 */
import type { components } from '@/types/api'

export type HttpError = components['schemas']['HttpError']
export type SetupStatusResponse = components['schemas']['SetupStatusResponse']
export type OverviewResponse = components['schemas']['OverviewResponse']
export type RunSummary = components['schemas']['RunSummary']
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
export type ProposalResponse = components['schemas']['ProposalResponse']

/** 场景在各端点的 200 响应体;401/500 等传输级场景由 handler 统一短路 */
export interface ScenarioFixture {
  setupStatus: SetupStatusResponse
  overview: OverviewResponse
  runs: RunSummary[]
  quarantine: QuarantineRecord[]
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
  proposal: ProposalResponse
}

const T = '2026-07-18T09:12:00+08:00'

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
    pipelineNode({ node: 'objects', status: 'healthy', last_success_at: T, rows_out: 1279 }),
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
    version: null,
    searchable: true,
  },
  {
    object: 'Material',
    display_name: '物料(品号)',
    domain: '产品',
    rows: 58,
    mapped_at: T,
    quarantined: 0,
    version: null,
    searchable: true,
  },
  {
    object: 'Quotation',
    display_name: '报价单',
    domain: '销售',
    rows: 41,
    mapped_at: T,
    quarantined: 0,
    version: null,
    searchable: true,
  },
  {
    object: 'SalesOrder',
    display_name: '销售订单',
    domain: '销售',
    rows: 52,
    mapped_at: T,
    quarantined: 0,
    version: null,
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
    bindings: [
      {
        source: 'digiwin_e10',
        tables: ['CUSTOMER', 'CURRENCY'],
        status: 'verified',
        key_map: { customer_code: 'CUSTOMER.CUSTOMER_CODE' },
        field_map: {
          customer_code: 'CUSTOMER.CUSTOMER_CODE',
          name: 'CUSTOMER.CUSTOMER_NAME',
          payment_days: 'CUSTOMER.PAYMENT_TERM_DAYS',
          contact: 'CUSTOMER.CONTACT_EMAIL',
        },
        watermark: 'CUSTOMER.LAST_MODIFIED_DATE',
        notes: '展厅模拟表形;真实环境以现场字典核对为准',
      },
    ],
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
    bindings: [
      {
        source: 'digiwin_e10',
        tables: ['SALES_ORDER'],
        status: 'verified',
        key_map: { order_no: 'SALES_ORDER.ORDER_NO' },
        field_map: {
          order_no: 'SALES_ORDER.ORDER_NO',
          customer: 'SALES_ORDER.CUSTOMER_CODE',
          total_amount: 'SALES_ORDER.TOTAL_AMOUNT',
        },
        watermark: 'SALES_ORDER.LAST_MODIFIED_DATE',
      },
    ],
  },
]

export const baseFixture: ScenarioFixture = {
  setupStatus: { needs_setup: false, config_path: '/home/d2a/platform.yaml', home: '/home/d2a' },
  overview: {
    landing: '/data/landing.sqlite',
    readonly: false,
    actions_sync_reconcile: true,
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
    versions: { app: '0.1.6', template: '0.1.0', dataset: null, object: null },
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
  config: { needs_setup: false, templates: 'templates', landing: '/data/landing.sqlite' },
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
    data: [
      { customer_code: 'C-001', name: '北极星钓具(美国)', payment_days: 60 },
    ],
    meta: { query_id: 'q1', tool: 'query_objects', target: 'Customer', at: T },
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
    proposal_id: 'p1',
    at: T,
    object: 'SalesOrder',
    action: 'review',
    action_desc: '接单评审',
    tier: '说',
    conclusion: '建议接单:毛利高于基线,账期在客户政策内',
    evidence: [
      {
        claim: '该客户近 90 天成交 6 单,平均毛利 31%',
        query: { query_id: 'q1', tool: 'query_objects', target: 'SalesOrder', at: T },
      },
    ],
    caveats: ['指标口径:毛利率按不含税金额计算'],
    governance: '「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理',
  },
}
