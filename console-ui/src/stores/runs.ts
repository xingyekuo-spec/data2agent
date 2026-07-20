/**
 * 运行页 store(M4-T09):筛选/分页/总数、详情抽屉、请求代际防旧覆盖。
 * 不加入 M3 的全局 5 秒轮询;显式刷新。route query 由视图同步。
 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type { ApiError } from '@/api/errors'
import { getRunDetail, getRuns, type RunsQuery } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type RunSummary = components['schemas']['RunSummary']
type RunDetailResponse = components['schemas']['RunDetailResponse']

export const useRunsStore = defineStore('runs', () => {
  const filters = reactive<{ type: RunsQuery['type'] | ''; status: RunsQuery['status'] | '' }>({
    type: '',
    status: '',
  })
  const page = reactive({ limit: 50, offset: 0 })
  const list = ref<RequestState<RunSummary[]>>({ status: 'idle' })
  const total = ref(0)
  const refreshError = ref<ApiError | null>(null)
  /** null = 抽屉关闭 */
  const detail = ref<RequestState<RunDetailResponse> | null>(null)
  const detailId = ref<number | null>(null)
  const detailRefreshError = ref<ApiError | null>(null)
  let generation = 0
  let detailGeneration = 0

  async function refresh(): Promise<void> {
    const gen = ++generation
    const firstLoad = list.value.status !== 'success'
    if (firstLoad) {
      list.value = { status: 'loading' }
    }
    const result = await getRuns({
      limit: page.limit,
      offset: page.offset,
      ...(filters.type ? { type: filters.type } : {}),
      ...(filters.status ? { status: filters.status } : {}),
    })
    if (gen !== generation) {
      return  // 旧请求不覆盖新筛选
    }
    if (result.ok) {
      list.value = { status: 'success', data: result.data.items }
      total.value = result.data.total
      refreshError.value = null
    } else if (firstLoad) {
      list.value = { status: 'error', error: result.error }
    } else {
      refreshError.value = result.error
    }
  }

  function setPage(offset: number, limit: number): void {
    page.offset = offset
    page.limit = limit
    void refresh()
  }

  async function openDetail(runId: number): Promise<void> {
    const gen = ++detailGeneration
    const firstLoad = detailId.value !== runId || detail.value?.status !== 'success'
    detailId.value = runId
    if (firstLoad) {
      detail.value = { status: 'loading' }
    }
    const result = await getRunDetail(runId)
    if (gen !== detailGeneration || detailId.value !== runId) {
      return
    }
    if (result.ok) {
      detail.value = { status: 'success', data: result.data }
      detailRefreshError.value = null
    } else if (firstLoad) {
      detail.value = { status: 'error', error: result.error }
    } else {
      detailRefreshError.value = result.error
    }
  }

  function closeDetail(): void {
    detailGeneration += 1
    detailId.value = null
    detail.value = null
    detailRefreshError.value = null
  }

  return {
    filters, page, list, total, refreshError,
    detail, detailId, detailRefreshError,
    refresh, setPage, openDetail, closeDetail,
  }
})
