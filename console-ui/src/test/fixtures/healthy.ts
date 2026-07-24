import { baseFixture, type ScenarioFixture } from './base'

/** 全链路正常:服务可达、最近运行成功、无隔离。不得作为其他场景的回退。 */
export const healthyFixture = {
  ...baseFixture,
} satisfies ScenarioFixture
