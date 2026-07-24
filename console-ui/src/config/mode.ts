/**
 * Console 运行模式:产品与开发均只使用真实 API。
 * Mock 运行模式已移除(M7);Vitest 可在测试 setup 中自行 stub fetch。
 */
export type ConsoleMode = 'real'

export const MODE: ConsoleMode = 'real'
export const IS_MOCK = false
