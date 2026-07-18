/**
 * overview 垂直切片:API → service → store → view 的完整样例。
 *
 * Mock 与 Real 使用同一 store;两个请求的状态各自独立(一个失败不把另一个
 * 变成空数据)。M3 在此模式上扩展真实 Dashboard。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getOverview, getServices } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type OverviewResponse = components['schemas']['OverviewResponse']
type ServicesStatusResponse = components['schemas']['ServicesStatusResponse']

export const useOverviewStore = defineStore('overview', () => {
  const overview = ref<RequestState<OverviewResponse>>({ status: 'idle' })
  const services = ref<RequestState<ServicesStatusResponse>>({ status: 'idle' })
  let inFlight = false

  async function refresh(): Promise<void> {
    // 防重复:已有刷新在途时直接返回
    if (inFlight) {
      return
    }
    inFlight = true
    overview.value = { status: 'loading' }
    services.value = { status: 'loading' }
    try {
      const [ov, sv] = await Promise.all([getOverview(), getServices()])
      overview.value = ov.ok
        ? { status: 'success', data: ov.data }
        : { status: 'error', error: ov.error }
      services.value = sv.ok
        ? { status: 'success', data: sv.data }
        : { status: 'error', error: sv.error }
    } finally {
      inFlight = false
    }
  }

  return { overview, services, refresh }
})
