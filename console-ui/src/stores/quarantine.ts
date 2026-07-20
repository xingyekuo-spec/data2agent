/**
 * 隔离页 store(M5-T08):分组、记录列表(按对象筛选)、详情抽屉(按需)、重试操作。
 * 请求代际防旧覆盖;first failure → error, refresh failure → keep old data + refreshError。
 * detail raw 关闭/切换时从内存清除,不写 sessionStorage/localStorage。
 * 无自动轮询,显式刷新。
 */
import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import {
  getQuarantineGroups,
  getQuarantineList,
  getQuarantineDetail,
  postRetry,
  type QuarantineQuery,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type QuarantineRecord = components['schemas']['QuarantineRecord']
type QuarantineGroup = components['schemas']['QuarantineGroup']
type QuarantineDetail = components['schemas']['QuarantineDetail']
type RetryActionResult = components['schemas']['RetryActionResult']

function isAuthError(error: ApiError): boolean {
  return error.kind === 'http' && (error.status === 401 || error.status === 403)
}

export const useQuarantineStore = defineStore('quarantine', () => {
  /** Object group list */
  const groups = ref<RequestState<QuarantineGroup[]>>({ status: 'idle' })
  const groupsRefreshError = ref<ApiError | null>(null)
  let groupsGen = 0

  /** Selected object filter (null = all) */
  const selectedObject = ref<string | null>(null)

  /** Record list (paginated, filtered by selected object) */
  const page = reactive({ limit: 50, offset: 0 })
  const records = ref<RequestState<QuarantineRecord[]>>({ status: 'idle' })
  const recordsTotal = ref(0)
  const recordsRefreshError = ref<ApiError | null>(null)
  let recordsGen = 0

  /** Detail drawer (on demand, raw data in memory only) */
  const detail = ref<RequestState<QuarantineDetail> | null>(null)
  const detailId = ref<number | null>(null)
  const detailRefreshError = ref<ApiError | null>(null)
  let detailGen = 0

  /** Retry action result */
  const retryResult = ref<RequestState<RetryActionResult> | null>(null)
  const retryError = ref<ApiError | null>(null)

  // ---- computed summary from groups ----

  const summary = computed(() => {
    if (groups.value.status !== 'success') return null
    const data = groups.value.data
    const totalPending = data.reduce((sum, g) => sum + g.pending, 0)
    const affectedObjects = data.filter((g) => g.pending > 0).length
    const overThreshold = data.filter((g) => g.rate_state === 'tripped').length
    let latestTime: string | null = null
    for (const g of data) {
      if (g.latest_created_at) {
        if (!latestTime || g.latest_created_at > latestTime) {
          latestTime = g.latest_created_at
        }
      }
    }
    return { totalPending, affectedObjects, overThreshold, latestTime }
  })

  // ---- groups ----

  async function fetchGroups(): Promise<void> {
    const gen = ++groupsGen
    const firstLoad = groups.value.status !== 'success'
    if (firstLoad) {
      groups.value = { status: 'loading' }
    }
    const result = await getQuarantineGroups()
    if (gen !== groupsGen) return
    if (result.ok) {
      groups.value = { status: 'success', data: result.data }
      groupsRefreshError.value = null
    } else if (firstLoad) {
      groups.value = { status: 'error', error: result.error }
    } else {
      groupsRefreshError.value = result.error
    }
  }

  // ---- records ----

  function selectGroup(object: string | null): void {
    selectedObject.value = object
    page.offset = 0
    void fetchRecords()
  }

  async function fetchRecords(): Promise<void> {
    const gen = ++recordsGen
    const firstLoad = records.value.status !== 'success'
    if (firstLoad) {
      records.value = { status: 'loading' }
    }
    const query: QuarantineQuery = {
      limit: page.limit,
      offset: page.offset,
    }
    if (selectedObject.value) {
      query.object = selectedObject.value
    }
    const result = await getQuarantineList(query)
    if (gen !== recordsGen) return
    if (result.ok) {
      records.value = { status: 'success', data: result.data.items }
      recordsTotal.value = result.data.total
      recordsRefreshError.value = null
    } else if (firstLoad) {
      records.value = { status: 'error', error: result.error }
    } else {
      recordsRefreshError.value = result.error
    }
  }

  function setPage(offset: number, limit: number): void {
    page.offset = offset
    page.limit = limit
    void fetchRecords()
  }

  // ---- detail drawer ----

  async function openDetail(id: number): Promise<void> {
    const gen = ++detailGen
    const firstLoad = detailId.value !== id || detail.value?.status !== 'success'
    detailId.value = id
    if (firstLoad) {
      detail.value = { status: 'loading' }
    }
    const result = await getQuarantineDetail(id)
    if (gen !== detailGen || detailId.value !== id) return
    if (result.ok) {
      detail.value = { status: 'success', data: result.data }
      detailRefreshError.value = null
    } else if (firstLoad || isAuthError(result.error)) {
      detail.value = { status: 'error', error: result.error }
      detailRefreshError.value = null
    } else {
      detailRefreshError.value = result.error
    }
  }

  /** Close drawer: clear raw from memory, never persist to sessionStorage/localStorage */
  function closeDetail(): void {
    detailGen += 1
    detailId.value = null
    detail.value = null
    detailRefreshError.value = null
  }

  // ---- retry ----

  async function retryObject(source: string, object: string): Promise<void> {
    retryResult.value = null
    retryError.value = null
    const result = await postRetry({ source, object, deep: false })
    if (result.ok) {
      retryResult.value = { status: 'success', data: result.data as RetryActionResult }
    } else {
      retryError.value = result.error
      retryResult.value = { status: 'error', error: result.error }
    }
  }

  function clearRetry(): void {
    retryResult.value = null
    retryError.value = null
  }

  return {
    groups, groupsRefreshError,
    selectedObject,
    page, records, recordsTotal, recordsRefreshError,
    detail, detailId, detailRefreshError,
    retryResult, retryError,
    summary,
    fetchGroups, selectGroup, fetchRecords, setPage,
    openDetail, closeDetail,
    retryObject, clearRetry,
  }
})
