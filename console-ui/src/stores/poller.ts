/**
 * 轮询器(M3-T07):唯一轮询所有者。
 *
 * - 固定间隔(默认 5s),失败按上限退避(×2 至 maxBackoffMs);
 * - 防堆积:一次 tick 未完成时到点不重入;
 * - 标签页隐藏时暂停,重新可见时立即刷新;
 * - stop() 清理 timer 与监听器,不留尾巴。
 */
export interface PollerOptions {
  /** 固定轮询间隔(无失败时) */
  intervalMs?: number
  /** 失败退避上限 */
  maxBackoffMs?: number
  /** 每轮执行的任务(内部自行防重入) */
  task: () => Promise<unknown>
  /** 任务失败后由调用方判定(决定退避) */
  isFailing: () => boolean
}

export interface Poller {
  start: () => void
  stop: () => void
  /** 手动立即执行一轮(场景切换 / 认证恢复时调用) */
  tickNow: () => Promise<void>
  readonly running: boolean
}

export function createPoller(options: PollerOptions): Poller {
  const interval = options.intervalMs ?? 5000
  const maxBackoff = options.maxBackoffMs ?? 60000
  let timer: ReturnType<typeof setTimeout> | null = null
  let backoff = 0
  let ticking = false
  let active = false

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function schedule(): void {
    if (!active || document.hidden) {
      return
    }
    timer = setTimeout(() => {
      void tick()
    }, backoff > 0 ? backoff : interval)
  }

  async function tick(): Promise<void> {
    if (ticking) {
      return
    }
    ticking = true
    try {
      await options.task()
      backoff = options.isFailing() ? Math.min(Math.max(interval, backoff * 2), maxBackoff) : 0
    } finally {
      ticking = false
      schedule()
    }
  }

  function onVisibility(): void {
    if (document.hidden) {
      clearTimer()
    } else if (active) {
      void tick()
    }
  }

  return {
    start() {
      if (active) {
        return
      }
      active = true
      document.addEventListener('visibilitychange', onVisibility)
      void tick()
    },
    stop() {
      active = false
      clearTimer()
      document.removeEventListener('visibilitychange', onVisibility)
    },
    async tickNow() {
      clearTimer()
      await tick()
    },
    get running() {
      return active
    },
  }
}
