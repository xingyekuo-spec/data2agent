import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** 存在未处理隔离:隔离数量、对象和原因可见,warning + 待处理数。 */
export const quarantinePendingFixture = {
  ...baseFixture,
  quarantine: [
    {
      id: 101,
      source: 'digiwin_e10',
      object: 'SalesOrder',
      keys_json: '{"order_no": "SO-20260701-X7"}',
      reason: '枚举未覆盖:ORDER_STATE=Y9',
      created_at: '2026-07-18 09:11:29',
    },
    {
      id: 100,
      source: 'digiwin_e10',
      object: 'Customer',
      keys_json: '{"customer_code": "C-NULL"}',
      reason: '业务键缺失:customer_code 为空',
      created_at: '2026-07-18 09:11:21',
    },
  ],
  overview: {
    ...baseFixture.overview,
    sources: [{ ...baseFixture.overview.sources[0]!, quarantined: 5 }],
    objects: [
      { object: 'Customer', display_name: '客户', rows: 36, mapped_at: '2026-07-18 09:11:20', quarantined: 2 },
      { object: 'Material', display_name: '物料(品号)', rows: 58, mapped_at: '2026-07-18 09:11:22', quarantined: 0 },
      { object: 'Quotation', display_name: '报价单', rows: 41, mapped_at: '2026-07-18 09:11:25', quarantined: 1 },
      { object: 'SalesOrder', display_name: '销售订单', rows: 52, mapped_at: '2026-07-18 09:11:30', quarantined: 2 },
    ],
    summary: { ...baseFixture.overview.summary, quarantine_pending: 5 },
    alerts: [
      {
        id: 'node-mapping',
        severity: 'warning',
        title: '管道节点 mapping warning',
        reason: '5 行待处理隔离(未达熔断阈值)',
        source: 'digiwin_e10',
        observed_at: '2026-07-18T09:11:20+08:00',
        detail_path: null,
      },
      {
        id: 'quarantine-pending',
        severity: 'warning',
        title: '存在未处理隔离',
        reason: '5 行数据因映射失败等待处理',
        source: null,
        observed_at: '2026-07-18T09:12:00+08:00',
        detail_path: null,
      },
    ],
  },
  pipeline: {
    ...baseFixture.pipeline,
    overall_status: 'warning',
    nodes: [
      pipelineNode({ node: 'erp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
      pipelineNode({ node: 'extract', status: 'healthy', last_success_at: '2026-07-18T09:10:02+08:00' }),
      pipelineNode({ node: 'push', status: 'healthy', last_success_at: '2026-07-18T09:10:04+08:00' }),
      pipelineNode({ node: 'raw', status: 'healthy', last_success_at: '2026-07-18T09:10:05+08:00' }),
      pipelineNode({
        node: 'mapping',
        status: 'warning',
        status_reason: '5 行待处理隔离(未达熔断阈值)',
        last_success_at: '2026-07-18T09:11:20+08:00',
        rows_out: 1279,
        error: '5 行待处理隔离(未达熔断阈值)',
      }),
      pipelineNode({ node: 'objects', status: 'warning', status_reason: '存在待处理隔离', last_success_at: '2026-07-18T09:11:30+08:00' }),
      pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
    ],
  },
} satisfies ScenarioFixture
