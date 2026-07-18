/**
 * overview 单一所有者(M3-T07):overview 请求、旧数据保留、刷新状态。
 *
 * - 首次加载失败 → 请求 error 视图;刷新失败 → 保留上一次成功数据 +
 *   refreshError 标记("刷新失败/数据截至…"),绝不变回健康假象;
 * - 防重入:上一请求未结束时跳过;
 * - Dashboard / TopBar 只消费状态,不直接调 API。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { ApiError } from '@/api/errors'
import { getOverview } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type OverviewResponse = components['schemas']['OverviewResponse']

export const useOverviewStore = defineStore('overview', () => {
  const overview = ref<RequestState<OverviewResponse>>({ status: 'idle' })
  /** 上一次成功刷新时间(旧数据的"数据截至") */
  const lastSuccessAt = ref<Date | null>(null)
  /** 刷新失败(保留旧数据时展示);成功后清空 */
  const refreshError = ref<ApiError | null>(null)
  let inFlight = false

  const data = computed(() =>
    overview.value.status === 'success' ? overview.value.data : null,
  )

  async function refresh(): Promise<void> {
    if (inFlight) {
      return
    }
    inFlight = true
    const firstLoad = overview.value.status !== 'success'
    if (firstLoad) {
      overview.value = { status: 'loading' }
    }
    try {
      const result = await getOverview()
      if (result.ok) {
        overview.value = { status: 'success', data: result.data }
        lastSuccessAt.value = new Date()
        refreshError.value = null
      } else if (firstLoad) {
        overview.value = { status: 'error', error: result.error }
      } else {
        refreshError.value = result.error
      }
    } finally {
      inFlight = false
    }
  }

  return { overview, data, lastSuccessAt, refreshError, refresh }
})
