import { describe, expect, it, vi } from 'vitest'
import {
  SCENARIOS,
  SCENARIO_IDS,
  getScenario,
  onScenarioChange,
  scenarioFixtures,
  setScenario,
} from './scenario'

describe('scenario registry', () => {
  it('必备 10 场景全部注册且有 fixture', () => {
    expect(SCENARIO_IDS).toHaveLength(10)
    expect(SCENARIOS.map((s) => s.id)).toEqual([...SCENARIO_IDS])
    for (const id of SCENARIO_IDS) {
      expect(scenarioFixtures[id], id).toBeDefined()
    }
  })

  it('默认场景 healthy;切换场景通知订阅者', () => {
    expect(getScenario()).toBe('healthy')
    const fn = vi.fn()
    const off = onScenarioChange(fn)
    setScenario('sync-running')
    expect(getScenario()).toBe('sync-running')
    expect(fn).toHaveBeenCalledWith('sync-running')
    off()
    setScenario('healthy')
  })

  it('拒绝未知场景', () => {
    expect(() => setScenario('nope' as never)).toThrow('未知 Mock 场景')
  })
})
