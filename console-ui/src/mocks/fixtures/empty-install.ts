import { baseFixture, pipelineNode, type ScenarioFixture } from './base'

/** 首次安装、没有数据:空集合 / 从未运行;页面必须是空态或 unknown,不是 0 即健康。 */
export const emptyInstallFixture = {
  ...baseFixture,
  setupStatus: { needs_setup: true, config_path: null, home: '/home/d2a' },
  overview: {
    landing: '',
    readonly: true,
    actions_sync_reconcile: false,
    sources: [],
    objects: [],
    needs_setup: true,
  },
  runs: [],
  quarantine: [],
  audit: [],
  config: { needs_setup: true, templates: '', landing: '' },
  rawTable: { table: '', offset: 0, limit: 50, total: 0, rows: [] },
  pipeline: {
    generated_at: '2026-07-18T09:12:00+08:00',
    nodes: [
      pipelineNode({ node: 'erp', status: 'unknown' }),
      pipelineNode({ node: 'extract', status: 'unknown' }),
      pipelineNode({ node: 'push', status: 'unknown' }),
      pipelineNode({ node: 'raw', status: 'unknown' }),
      pipelineNode({ node: 'mapping', status: 'unknown' }),
      pipelineNode({ node: 'objects', status: 'unknown' }),
      pipelineNode({ node: 'mcp', status: 'unknown' }),
    ],
  },
  rawData: { source: 'digiwin_e10', table: '', offset: 0, limit: 50, total: 0, rows: [] },
  objects: [],
  objectRows: { object: '', offset: 0, limit: 50, total: 0, rows: [] },
  mcpCall: { data: [], meta: { usage: '带 object 参数查询数据' } },
} satisfies ScenarioFixture
