import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** binding / 指标仍为 draft:warning,明确「未经现场校准」。 */
export const draftGovernanceFixture = {
  ...baseFixture,
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
    nodes: [
      pipelineNode({ node: 'erp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
      pipelineNode({ node: 'extract', status: 'healthy', last_success_at: '2026-07-18T09:10:02+08:00' }),
      pipelineNode({ node: 'push', status: 'healthy', last_success_at: '2026-07-18T09:10:04+08:00' }),
      pipelineNode({ node: 'raw', status: 'healthy', last_success_at: '2026-07-18T09:10:05+08:00' }),
      pipelineNode({
        node: 'mapping',
        status: 'warning',
        last_success_at: '2026-07-18T09:11:20+08:00',
        error: '全部 binding 为 draft,未经现场校准',
      }),
      pipelineNode({ node: 'objects', status: 'warning', last_success_at: '2026-07-18T09:11:30+08:00' }),
      pipelineNode({ node: 'mcp', status: 'healthy', last_success_at: '2026-07-18T09:10:00+08:00' }),
    ],
  },
} satisfies ScenarioFixture
