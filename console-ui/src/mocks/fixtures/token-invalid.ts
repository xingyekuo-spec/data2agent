import { baseFixture, type ScenarioFixture } from './base'

/**
 * Token 无效:handler 对所有 /api/* 短路返回 401 HttpError。
 * fixture 数据保留类型完整,但不应被任何页面渲染成业务数据。
 */
export const tokenInvalidFixture = {
  ...baseFixture,
} satisfies ScenarioFixture
