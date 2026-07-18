import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** apply 熔断:映射 failed、对象层 stale(继续服务上一稳定版本),双重语义。 */
export const applyCircuitBrokenFixture = {
  ...baseFixture,
  overview: {
    ...baseFixture.overview,
    objects: [
      { object: 'Customer', display_name: '客户', rows: 36, mapped_at: '2026-07-17 22:00:00', quarantined: 41 },
      { object: 'Material', display_name: '物料(品号)', rows: 58, mapped_at: '2026-07-17 22:00:02', quarantined: 0 },
      { object: 'Quotation', display_name: '报价单', rows: 41, mapped_at: '2026-07-17 22:00:05', quarantined: 0 },
      { object: 'SalesOrder', display_name: '销售订单', rows: 52, mapped_at: '2026-07-17 22:00:08', quarantined: 3 },
    ],
  },
  pipeline: {
    generated_at: '2026-07-18T09:50:00+08:00',
    nodes: [
      pipelineNode({ node: 'erp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
      pipelineNode({ node: 'extract', status: 'healthy', last_success_at: '2026-07-18T09:10:02+08:00' }),
      pipelineNode({ node: 'push', status: 'healthy', last_success_at: '2026-07-18T09:10:04+08:00' }),
      pipelineNode({ node: 'raw', status: 'healthy', last_success_at: '2026-07-18T09:10:05+08:00' }),
      pipelineNode({
        node: 'mapping',
        status: 'failed',
        last_success_at: '2026-07-17T22:00:10+08:00',
        last_failure_at: '2026-07-18T09:48:40+08:00',
        error: 'Customer 隔离率 53% 超过熔断阈值 20%,apply 已中止',
      }),
      pipelineNode({
        node: 'objects',
        status: 'stale',
        last_success_at: '2026-07-17T22:00:12+08:00',
        version: '上一稳定版本(2026-07-17 22:00)',
      }),
      pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
    ],
  },
  runDetail: {
    id: 44,
    type: 'apply',
    status: 'aborted',
    source: 'digiwin_e10',
    started_at: '2026-07-18T09:48:00+08:00',
    finished_at: '2026-07-18T09:48:40+08:00',
    duration_ms: 40000,
    dataset_version: null,
    steps: [
      {
        name: 'Customer',
        rows_in: 77,
        rows_out: 36,
        quarantined: 41,
        watermark_before: null,
        watermark_after: null,
        error: '隔离率 53% 超过阈值 20%,熔断',
      },
    ],
  },
} satisfies ScenarioFixture
