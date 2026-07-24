import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** binding / 指标仍为 draft:warning,明确「未经现场校准」。 */
export const draftGovernanceFixture = {
  ...baseFixture,
  overview: {
    ...baseFixture.overview,
    binding_summary: { verified: 0, draft: 10, disabled: 0 },
    alerts: [
      {
        id: 'binding-draft',
        severity: 'info',
        title: 'binding 未经现场校准',
        reason: '10 个 binding 仍为 draft,口径以现场数据字典核对为准',
        source: null,
        observed_at: '2026-07-18T09:12:00+08:00',
        detail_path: null,
      },
    ],
  },
  templates: baseFixture.templates.map((tpl) => ({
    ...tpl,
    bindings: tpl.bindings.map((b) => ({
      ...b,
      status: 'draft' as const,
      notes: `${b.notes ?? ''}(未经现场校准)`.trim(),
    })),
  })),
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
        status_reason: '全部 binding 为 draft,未经现场校准',
        last_success_at: '2026-07-18T09:11:20+08:00',
        error: '全部 binding 为 draft,未经现场校准',
      }),
      pipelineNode({ node: 'objects', status: 'warning', status_reason: '治理项未完成校准', last_success_at: '2026-07-18T09:11:30+08:00' }),
      pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
    ],
  },
} satisfies ScenarioFixture
