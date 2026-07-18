import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** 同步运行中:Run 为 running,带开始时间与进行中的步骤。 */
export const syncRunningFixture = {
  ...baseFixture,
  runs: [
    {
      id: 43,
      source: 'digiwin_e10',
      started_at: '2026-07-18 09:30:00',
      finished_at: null,
      tables: null,
      rows: null,
      status: 'running',
      detail: null,
    },
    ...baseFixture.runs,
  ],
  pipeline: {
    generated_at: '2026-07-18T09:30:20+08:00',
    overall_status: 'running',
    nodes: [
      pipelineNode({ node: 'erp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
      pipelineNode({ node: 'extract', status: 'running', last_success_at: '2026-07-18T09:10:02+08:00' }),
      pipelineNode({ node: 'push', status: 'idle', last_success_at: '2026-07-18T09:10:04+08:00' }),
      pipelineNode({ node: 'raw', status: 'idle', last_success_at: '2026-07-18T09:10:05+08:00' }),
      pipelineNode({ node: 'mapping', status: 'idle', last_success_at: '2026-07-18T09:11:20+08:00' }),
      pipelineNode({ node: 'objects', status: 'healthy', last_success_at: '2026-07-18T09:11:30+08:00' }),
      pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
    ],
  },
  runDetail: {
    id: 43,
    type: 'sync',
    status: 'running',
    source: 'digiwin_e10',
    started_at: '2026-07-18T09:30:00+08:00',
    finished_at: null,
    duration_ms: null,
    dataset_version: null,
    steps: [
      {
        name: 'raw_digiwin_e10__CUSTOMER',
        rows_in: 12,
        rows_out: null,
        quarantined: null,
        watermark_before: '2026-07-18 08:30:00',
        watermark_after: null,
        error: null,
      },
    ],
  },
} satisfies ScenarioFixture
