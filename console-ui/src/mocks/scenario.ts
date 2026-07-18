/**
 * Mock 场景注册表与运行时切换。
 *
 * 场景只在 Mock 模式可见、可切换;切换场景不等于切换环境模式(MODE 仍由
 * VITE_CONSOLE_MODE 决定)。每个场景对应 fixtures/ 下同形的一份 typed fixture。
 */
import { scenarioEpoch } from '@/config/scenario-epoch'
import { applyCircuitBrokenFixture } from './fixtures/apply-circuit-broken'
import type { ScenarioFixture } from './fixtures/base'
import { draftGovernanceFixture } from './fixtures/draft-governance'
import { emptyInstallFixture } from './fixtures/empty-install'
import { healthyFixture } from './fixtures/healthy'
import { ingestFailedFixture } from './fixtures/ingest-failed'
import { partialServicesDownFixture } from './fixtures/partial-services-down'
import { quarantinePendingFixture } from './fixtures/quarantine-pending'
import { syncRunningFixture } from './fixtures/sync-running'
import { tokenInvalidFixture } from './fixtures/token-invalid'
import { unknownErrorFixture } from './fixtures/unknown-error'

export const SCENARIO_IDS = [
  'healthy',
  'empty-install',
  'sync-running',
  'ingest-failed',
  'apply-circuit-broken',
  'partial-services-down',
  'quarantine-pending',
  'draft-governance',
  'token-invalid',
  'unknown-error',
] as const

export type ScenarioId = (typeof SCENARIO_IDS)[number]

export interface ScenarioMeta {
  id: ScenarioId
  label: string
  description: string
}

/** 必备 Mock 场景矩阵(M2 计划 §6) */
export const SCENARIOS: readonly ScenarioMeta[] = [
  { id: 'healthy', label: '全链路正常', description: '服务可达、最近运行成功、无隔离' },
  { id: 'empty-install', label: '首次安装', description: '没有数据:空集合 / 从未运行' },
  { id: 'sync-running', label: '同步运行中', description: 'Run 为 running,带开始时间/步骤' },
  { id: 'ingest-failed', label: '推送失败', description: 'push 节点 failed,带错误摘要' },
  {
    id: 'apply-circuit-broken',
    label: 'apply 熔断',
    description: '映射失败、对象层 stale 继续用旧版本',
  },
  {
    id: 'partial-services-down',
    label: '部分服务不可达',
    description: 'console 正常,MCP / ingest 失败',
  },
  { id: 'quarantine-pending', label: '存在未处理隔离', description: '隔离数量、对象和原因' },
  { id: 'draft-governance', label: 'binding 仍为 draft', description: '未经现场校准' },
  { id: 'token-invalid', label: 'Token 无效', description: '全部 API 返回 401' },
  { id: 'unknown-error', label: '未知错误', description: '全部 API 返回 500' },
]

export const scenarioFixtures: Record<ScenarioId, ScenarioFixture> = {
  healthy: healthyFixture,
  'empty-install': emptyInstallFixture,
  'sync-running': syncRunningFixture,
  'ingest-failed': ingestFailedFixture,
  'apply-circuit-broken': applyCircuitBrokenFixture,
  'partial-services-down': partialServicesDownFixture,
  'quarantine-pending': quarantinePendingFixture,
  'draft-governance': draftGovernanceFixture,
  'token-invalid': tokenInvalidFixture,
  'unknown-error': unknownErrorFixture,
}

const DEFAULT_SCENARIO: ScenarioId = 'healthy'

let current: ScenarioId = DEFAULT_SCENARIO
const listeners = new Set<(id: ScenarioId) => void>()

export function getScenario(): ScenarioId {
  return current
}

export function setScenario(id: ScenarioId): void {
  if (!SCENARIO_IDS.includes(id)) {
    throw new Error(`未知 Mock 场景: ${id}`)
  }
  current = id
  // 通知 AppLayout 重挂载当前视图,触发 onMounted 重新请求
  scenarioEpoch.value += 1
  for (const fn of listeners) {
    fn(id)
  }
}

export function onScenarioChange(fn: (id: ScenarioId) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
