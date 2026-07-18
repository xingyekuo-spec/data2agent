import { baseFixture, type ScenarioFixture } from './base'

/**
 * API 返回未知错误:handler 对所有 /api/* 短路返回 500。
 * 页面必须显示 error/unknown 与可重试入口,不得显示健康或空成功。
 */
export const unknownErrorFixture = {
  ...baseFixture,
} satisfies ScenarioFixture
