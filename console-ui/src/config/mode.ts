/**
 * 环境模式解析:mock | real。
 *
 * 模式只来自构建/部署配置 VITE_CONSOLE_MODE,绝不读取 URL(query/hash 不得
 * 切换模式):
 * - 开发(dev):未设置默认 mock;
 * - 生产(build):未设置强制 real;mock 永不允许进入生产构建产物。
 */
export type ConsoleMode = 'mock' | 'real'

export interface ModeEnv {
  VITE_CONSOLE_MODE?: string
  PROD?: boolean
}

export function resolveMode(env: ModeEnv): ConsoleMode {
  const raw = env.VITE_CONSOLE_MODE?.trim().toLowerCase()
  if (env.PROD) {
    return 'real'
  }
  if (raw === 'mock' || raw === 'real') {
    return raw
  }
  return 'mock'
}

export const MODE: ConsoleMode = resolveMode(import.meta.env)
export const IS_MOCK = MODE === 'mock'
