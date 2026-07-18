import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** 部分服务不可达:console 正常,MCP / ingest 失败;局部 failed,整页不转空。 */
export const partialServicesDownFixture = {
  ...baseFixture,
  overview: {
    ...baseFixture.overview,
    alerts: [
      {
        id: 'node-mcp',
        severity: 'critical',
        title: '管道节点 mcp failed',
        reason: 'MCP :8848 不可达(tcp 探测失败)',
        source: 'digiwin_e10',
        observed_at: '2026-07-18T09:55:00+08:00',
        detail_path: null,
      },
    ],
  },
  services: {
    ingest: { ok: false, method: 'http' },
    mcp: { ok: false, method: 'tcp' },
    apply: { ok: true, method: 'process' },
    console: { ok: true, method: 'self' },
  },
  pipeline: {
    ...baseFixture.pipeline,
    overall_status: 'failed',
    nodes: [
      pipelineNode({ node: 'erp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
      pipelineNode({ node: 'extract', status: 'healthy', last_success_at: '2026-07-18T09:10:02+08:00' }),
      pipelineNode({ node: 'push', status: 'warning', last_success_at: '2026-07-18T09:10:04+08:00' }),
      pipelineNode({ node: 'raw', status: 'healthy', last_success_at: '2026-07-18T09:10:05+08:00' }),
      pipelineNode({ node: 'mapping', status: 'healthy', last_success_at: '2026-07-18T09:11:20+08:00' }),
      pipelineNode({ node: 'objects', status: 'healthy', last_success_at: '2026-07-18T09:11:30+08:00' }),
      pipelineNode({
        node: 'mcp',
        status: 'failed',
        status_reason: 'MCP :8848 不可达(tcp 探测失败)',
        last_success_at: '2026-07-18T08:00:00+08:00',
        last_failure_at: '2026-07-18T09:55:00+08:00',
        error: 'MCP :8848 不可达(tcp 探测失败)',
      }),
    ],
  },
} satisfies ScenarioFixture
