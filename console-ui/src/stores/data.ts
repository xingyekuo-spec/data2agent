/**
 * 数据页 store(M4-T11):raw/object 目录、分页浏览、业务键搜索与请求代际。
 * 显式刷新,无自动轮询;403(未配置 Token)按安全配置指引展示,不降级。
 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type { ApiError } from '@/api/errors'
import {
  getObjectCatalog,
  getObjectRows,
  getRawCatalog,
  getRawPage,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type RawTableCatalogResponse = components['schemas']['RawTableCatalogResponse']
type RawDataPageResponse = components['schemas']['RawDataPageResponse']
type ObjectSummary = components['schemas']['ObjectSummary']
type ObjectRowsPageResponse = components['schemas']['ObjectRowsPageResponse']

function isAuthError(error: ApiError): boolean {
  return error.kind === 'http' && (error.status === 401 || error.status === 403)
}

export const useDataStore = defineStore('data', () => {
  const rawCatalog = ref<RequestState<RawTableCatalogResponse>>({ status: 'idle' })
  const rawCatalogRefreshError = ref<ApiError | null>(null)
  const rawSel = reactive({ source: '', table: '' })
  const rawQuery = reactive({ limit: 50, offset: 0, q: '' })
  const rawPage = ref<RequestState<RawDataPageResponse> | null>(null)
  const rawPageRefreshError = ref<ApiError | null>(null)

  const objCatalog = ref<RequestState<ObjectSummary[]>>({ status: 'idle' })
  const objCatalogRefreshError = ref<ApiError | null>(null)
  const objSel = ref('')
  const objQuery = reactive({ limit: 50, offset: 0, q: '' })
  const objPage = ref<RequestState<ObjectRowsPageResponse> | null>(null)
  const objPageRefreshError = ref<ApiError | null>(null)

  let catGen = 0
  let rawGen = 0
  let objGen = 0

  async function refreshRawCatalog(): Promise<void> {
    const gen = ++catGen
    const rawFirstLoad = rawCatalog.value.status !== 'success'
    const objFirstLoad = objCatalog.value.status !== 'success'
    if (rawFirstLoad) {
      rawCatalog.value = { status: 'loading' }
    }
    if (objFirstLoad) {
      objCatalog.value = { status: 'loading' }
    }
    const [rawResult, objResult] = await Promise.all([getRawCatalog(), getObjectCatalog()])
    if (gen !== catGen) {
      return
    }
    if (rawResult.ok) {
      rawCatalog.value = { status: 'success', data: rawResult.data }
      rawCatalogRefreshError.value = null
    } else if (rawFirstLoad || isAuthError(rawResult.error)) {
      rawCatalog.value = { status: 'error', error: rawResult.error }
      rawCatalogRefreshError.value = null
    } else {
      rawCatalogRefreshError.value = rawResult.error
    }
    if (objResult.ok) {
      objCatalog.value = { status: 'success', data: objResult.data }
      objCatalogRefreshError.value = null
    } else if (objFirstLoad) {
      objCatalog.value = { status: 'error', error: objResult.error }
    } else {
      objCatalogRefreshError.value = objResult.error
    }
  }

  async function browseRaw(): Promise<void> {
    if (!rawSel.source || !rawSel.table) {
      return
    }
    const gen = ++rawGen
    const firstLoad = rawPage.value?.status !== 'success'
    if (firstLoad) {
      rawPage.value = { status: 'loading' }
    }
    const result = await getRawPage(rawSel.source, rawSel.table, rawQuery)
    if (gen !== rawGen) {
      return
    }
    if (result.ok) {
      rawPage.value = { status: 'success', data: result.data }
      rawPageRefreshError.value = null
    } else if (firstLoad || isAuthError(result.error)) {
      rawPage.value = { status: 'error', error: result.error as ApiError }
      rawPageRefreshError.value = null
    } else {
      rawPageRefreshError.value = result.error
    }
  }

  function searchRaw(): void {
    rawQuery.offset = 0
    void browseRaw()
  }

  function selectRaw(source: string, table: string): void {
    rawSel.source = source
    rawSel.table = table
    rawQuery.offset = 0
    rawQuery.q = ''
    rawPage.value = null
    rawPageRefreshError.value = null
    void browseRaw()
  }

  async function browseObject(): Promise<void> {
    if (!objSel.value) {
      return
    }
    const gen = ++objGen
    const firstLoad = objPage.value?.status !== 'success'
    if (firstLoad) {
      objPage.value = { status: 'loading' }
    }
    const result = await getObjectRows(objSel.value, objQuery)
    if (gen !== objGen) {
      return
    }
    if (result.ok) {
      objPage.value = { status: 'success', data: result.data }
      objPageRefreshError.value = null
    } else if (firstLoad) {
      objPage.value = { status: 'error', error: result.error as ApiError }
    } else {
      objPageRefreshError.value = result.error
    }
  }

  function searchObject(): void {
    objQuery.offset = 0
    void browseObject()
  }

  function selectObject(object: string): void {
    objSel.value = object
    objQuery.offset = 0
    objQuery.q = ''
    objPage.value = null
    objPageRefreshError.value = null
    void browseObject()
  }

  return {
    rawCatalog, rawCatalogRefreshError, rawSel, rawQuery, rawPage, rawPageRefreshError,
    objCatalog, objCatalogRefreshError, objSel, objQuery, objPage, objPageRefreshError,
    refreshRawCatalog, browseRaw, searchRaw, selectRaw, browseObject, searchObject, selectObject,
  }
})
