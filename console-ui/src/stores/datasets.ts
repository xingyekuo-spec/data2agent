/**
 * 数据集版本 store(M2-T10):列表/详情、publish/rollback、stage-only apply。
 * 请求代际防旧覆盖;first failure → error, refresh failure → keep old + refreshError。
 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type { ApiError } from '@/api/errors'
import {
  getDatasetDetail,
  getDatasets,
  postApply,
  postDatasetPublish,
  postDatasetRollback,
  type ApplyBody,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type DatasetSummary = components['schemas']['DatasetSummary']
type DatasetDetail = components['schemas']['DatasetDetail']
type DatasetActionResult = components['schemas']['DatasetActionResult']
type ApplyActionResult = components['schemas']['ApplyActionResult']

export const useDatasetsStore = defineStore('datasets', () => {
  const filters = reactive<{ source: string; status: DatasetSummary['status'] | '' }>({
    source: '',
    status: '',
  })
  const page = reactive({ limit: 50, offset: 0 })
  const list = ref<RequestState<DatasetSummary[]>>({ status: 'idle' })
  const total = ref(0)
  const listRefreshError = ref<ApiError | null>(null)
  let listGen = 0

  const detail = ref<RequestState<DatasetDetail> | null>(null)
  const detailVersion = ref<string | null>(null)
  const detailRefreshError = ref<ApiError | null>(null)
  let detailGen = 0

  const actionResult = ref<RequestState<DatasetActionResult> | null>(null)
  const actionError = ref<ApiError | null>(null)
  let actionGen = 0

  const applyResult = ref<RequestState<ApplyActionResult> | null>(null)
  const applyError = ref<ApiError | null>(null)
  let applyGen = 0

  async function refresh(): Promise<void> {
    const gen = ++listGen
    const firstLoad = list.value.status !== 'success'
    if (firstLoad) {
      list.value = { status: 'loading' }
    }
    const result = await getDatasets({
      limit: page.limit,
      offset: page.offset,
      ...(filters.source ? { source: filters.source } : {}),
      ...(filters.status ? { status: filters.status } : {}),
    })
    if (gen !== listGen) {
      return
    }
    if (result.ok) {
      list.value = { status: 'success', data: result.data.items }
      total.value = result.data.total
      listRefreshError.value = null
    } else if (firstLoad) {
      list.value = { status: 'error', error: result.error }
    } else {
      listRefreshError.value = result.error
    }
  }

  async function openDetail(version: string): Promise<void> {
    const gen = ++detailGen
    detailVersion.value = version
    const firstLoad = detail.value?.status !== 'success' || detail.value.data.dataset_version !== version
    if (firstLoad) {
      detail.value = { status: 'loading' }
    }
    const result = await getDatasetDetail(version)
    if (gen !== detailGen) {
      return
    }
    if (result.ok) {
      detail.value = { status: 'success', data: result.data }
      detailRefreshError.value = null
    } else if (firstLoad || !detail.value || detail.value.status !== 'success') {
      detail.value = { status: 'error', error: result.error }
    } else {
      detailRefreshError.value = result.error
    }
  }

  function closeDetail(): void {
    detailGen += 1
    detail.value = null
    detailVersion.value = null
    detailRefreshError.value = null
  }

  async function publish(version: string): Promise<boolean> {
    const gen = ++actionGen
    actionResult.value = { status: 'loading' }
    actionError.value = null
    const result = await postDatasetPublish(version)
    if (gen !== actionGen) {
      return false
    }
    if (result.ok) {
      actionResult.value = { status: 'success', data: result.data }
      await refresh()
      if (detailVersion.value === version) {
        await openDetail(version)
      }
      return true
    }
    actionResult.value = { status: 'error', error: result.error }
    actionError.value = result.error
    return false
  }

  async function rollback(version: string): Promise<boolean> {
    const gen = ++actionGen
    actionResult.value = { status: 'loading' }
    actionError.value = null
    const result = await postDatasetRollback(version)
    if (gen !== actionGen) {
      return false
    }
    if (result.ok) {
      actionResult.value = { status: 'success', data: result.data }
      await refresh()
      if (detailVersion.value) {
        await openDetail(result.data.dataset_version)
      }
      return true
    }
    actionResult.value = { status: 'error', error: result.error }
    actionError.value = result.error
    return false
  }

  /** publish=true 自动发布;publish=false 仅构建候选(stage-only)。 */
  async function apply(body: ApplyBody): Promise<boolean> {
    const gen = ++applyGen
    applyResult.value = { status: 'loading' }
    applyError.value = null
    const result = await postApply(body)
    if (gen !== applyGen) {
      return false
    }
    if (result.ok) {
      applyResult.value = { status: 'success', data: result.data }
      await refresh()
      return true
    }
    applyResult.value = { status: 'error', error: result.error }
    applyError.value = result.error
    return false
  }

  return {
    filters,
    page,
    list,
    total,
    listRefreshError,
    detail,
    detailVersion,
    detailRefreshError,
    actionResult,
    actionError,
    applyResult,
    applyError,
    refresh,
    openDetail,
    closeDetail,
    publish,
    rollback,
    apply,
  }
})
