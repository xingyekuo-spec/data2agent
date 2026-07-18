/**
 * 生产构建占位:vite.config.ts 在 build 时把 @/mocks/browser 别名到本文件,
 * MSW 不进入生产产物。生产模式下 IS_MOCK 恒为 false,本函数永远不会被调用。
 */
export async function startMockWorker(): Promise<void> {
  throw new Error('生产构建不包含 Mock(worker 已被构建期排除)')
}
