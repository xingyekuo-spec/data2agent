import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** 推送失败:push 节点 failed 带错误摘要;其他节点不得被连带变绿或变空。 */
export const ingestFailedFixture = {
  ...baseFixture,
  services: {
    ingest: { ok: false, method: 'http' },
    mcp: { ok: true, method: 'http' },
    apply: { ok: true, method: 'log_mtime' },
    console: { ok: true, method: 'self' },
  },
  pipeline: {
    generated_at: '2026-07-18T09:40:00+08:00',
    nodes: [
      pipelineNode({ node: 'erp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
      pipelineNode({
        node: 'extract',
        status: 'healthy',
        last_success_at: '2026-07-18T09:10:02+08:00',
        rows_out: 1284,
      }),
      pipelineNode({
        node: 'push',
        status: 'failed',
        last_success_at: '2026-07-18T08:10:03+08:00',
        last_failure_at: '2026-07-18T09:38:11+08:00',
        error: 'ingest 接收端不可达(连接超时 2s):批次 b-20260718-0938 未提交,水位未推进',
      }),
      pipelineNode({ node: 'raw', status: 'stale', last_success_at: '2026-07-18T08:10:05+08:00' }),
      pipelineNode({ node: 'mapping', status: 'idle', last_success_at: '2026-07-18T09:11:20+08:00' }),
      pipelineNode({ node: 'objects', status: 'stale', last_success_at: '2026-07-18T09:11:30+08:00' }),
      pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
    ],
  },
} satisfies ScenarioFixture
