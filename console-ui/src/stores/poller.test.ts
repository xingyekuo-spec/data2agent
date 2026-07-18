import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPoller } from './poller'

async function flushMicrotasks(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
}

describe('createPoller', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('start 立即执行一轮并按固定间隔调度;stop 后不再触发', async () => {
    const task = vi.fn(async () => {})
    const poller = createPoller({ intervalMs: 5000, task, isFailing: () => false })
    poller.start()
    await flushMicrotasks()
    expect(task).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(5000)
    expect(task).toHaveBeenCalledTimes(2)

    poller.stop()
    expect(poller.running).toBe(false)
    await vi.advanceTimersByTimeAsync(15000)
    expect(task).toHaveBeenCalledTimes(2)  // stop 清理,无尾巴
  })

  it('任务未完成时不堆积(防重入)', async () => {
    let release: (() => void) | undefined
    const task = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    )
    const poller = createPoller({ intervalMs: 1000, task, isFailing: () => false })
    poller.start()
    await vi.advanceTimersByTimeAsync(5000)
    expect(task).toHaveBeenCalledTimes(1)  // 第一轮还挂着,不叠加
    release?.()
    await flushMicrotasks()
    await vi.advanceTimersByTimeAsync(1000)
    expect(task).toHaveBeenCalledTimes(2)
    poller.stop()
  })

  it('标签页隐藏暂停,重新可见立即刷新', async () => {
    const task = vi.fn(async () => {})
    const poller = createPoller({ intervalMs: 1000, task, isFailing: () => false })
    poller.start()
    await flushMicrotasks()
    expect(task).toHaveBeenCalledTimes(1)

    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(10000)
    expect(task).toHaveBeenCalledTimes(1)  // 暂停

    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushMicrotasks()
    expect(task).toHaveBeenCalledTimes(2)  // 可见立即刷新
    poller.stop()
  })

  it('失败按上限退避(1s→2s→4s),恢复后回固定间隔', async () => {
    let failing = true
    const task = vi.fn(async () => {})
    const poller = createPoller({
      intervalMs: 1000,
      maxBackoffMs: 4000,
      task,
      isFailing: () => failing,
    })
    poller.start()
    await flushMicrotasks()
    expect(task).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1000)  // +1s:第 2 轮
    expect(task).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1000)
    expect(task).toHaveBeenCalledTimes(2)  // 退避 2s,还没到
    await vi.advanceTimersByTimeAsync(1000)  // +2s:第 3 轮
    expect(task).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(3999)
    expect(task).toHaveBeenCalledTimes(3)  // 退避上限 4s,还没到
    await vi.advanceTimersByTimeAsync(1)
    expect(task).toHaveBeenCalledTimes(4)

    failing = false
    await vi.advanceTimersByTimeAsync(3999)  // 本轮仍按已排定的 4s 退避
    expect(task).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(1)     // 第 5 轮(成功)
    expect(task).toHaveBeenCalledTimes(5)
    await vi.advanceTimersByTimeAsync(1000)  // 恢复后回到固定 1s 间隔
    expect(task).toHaveBeenCalledTimes(6)
    poller.stop()
  })

  it('tickNow 立即刷新并重置调度', async () => {
    const task = vi.fn(async () => {})
    const poller = createPoller({ intervalMs: 5000, task, isFailing: () => false })
    poller.start()
    await flushMicrotasks()
    expect(task).toHaveBeenCalledTimes(1)
    await poller.tickNow()
    expect(task).toHaveBeenCalledTimes(2)
    poller.stop()
  })
})
