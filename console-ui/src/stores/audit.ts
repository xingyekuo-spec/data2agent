/**
 * 审计页 store(M4-T10):SQL 操作 / 数据访问双 tab 的筛选、分页与请求代际。
 * 无自动轮询;筛选变化即淘汰旧请求。
 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type { ApiError } from '@/api/errors'
import { getAccessAudit, getAudit } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type AuditRecord = components['schemas']['AuditRecord']
type AccessAuditPage = components['schemas']['AccessAuditPage']

export const useAuditStore = defineStore('audit', () => {
  const sqlFilters = reactive({ source: '', action: '', from: '', to: '' })
  const accessFilters = reactive({
    subject: '',
    resource_type: '' as '' | 'raw' | 'object',
    allowed: '' as '' | 'true' | 'false',
    from: '',
    to: '',
  })
  const sqlPage = reactive({ limit: 50, offset: 0 })
  const accessPage = reactive({ limit: 50, offset: 0 })
  const sql = ref<RequestState<AuditRecord[]>>({ status: 'idle' })
  const sqlTotal = ref(0)
  const sqlRefreshError = ref<ApiError | null>(null)
  const access = ref<RequestState<AccessAuditPage>>({ status: 'idle' })
  const accessRefreshError = ref<ApiError | null>(null)
  let sqlGen = 0
  let accessGen = 0

  async function refreshSql(): Promise<void> {
    const gen = ++sqlGen
    const firstLoad = sql.value.status !== 'success'
    if (firstLoad) {
      sql.value = { status: 'loading' }
    }
    const result = await getAudit({
      limit: sqlPage.limit,
      offset: sqlPage.offset,
      ...(sqlFilters.source ? { source: sqlFilters.source } : {}),
      ...(sqlFilters.action ? { action: sqlFilters.action } : {}),
      ...(sqlFilters.from ? { from: sqlFilters.from } : {}),
      ...(sqlFilters.to ? { to: sqlFilters.to } : {}),
    })
    if (gen !== sqlGen) {
      return
    }
    if (result.ok) {
      sql.value = { status: 'success', data: result.data.items }
      sqlTotal.value = result.data.total
      sqlRefreshError.value = null
    } else if (firstLoad) {
      sql.value = { status: 'error', error: result.error }
    } else {
      sqlRefreshError.value = result.error
    }
  }

  function filterSql(): void {
    sqlPage.offset = 0
    void refreshSql()
  }

  async function refreshAccess(): Promise<void> {
    const gen = ++accessGen
    const firstLoad = access.value.status !== 'success'
    if (firstLoad) {
      access.value = { status: 'loading' }
    }
    const result = await getAccessAudit({
      limit: accessPage.limit,
      offset: accessPage.offset,
      ...(accessFilters.subject ? { subject: accessFilters.subject } : {}),
      ...(accessFilters.resource_type ? { resource_type: accessFilters.resource_type } : {}),
      ...(accessFilters.allowed ? { allowed: accessFilters.allowed === 'true' } : {}),
      ...(accessFilters.from ? { from: accessFilters.from } : {}),
      ...(accessFilters.to ? { to: accessFilters.to } : {}),
    })
    if (gen !== accessGen) {
      return
    }
    if (result.ok) {
      access.value = { status: 'success', data: result.data }
      accessRefreshError.value = null
    } else if (firstLoad) {
      access.value = { status: 'error', error: result.error }
    } else {
      accessRefreshError.value = result.error
    }
  }

  function filterAccess(): void {
    accessPage.offset = 0
    void refreshAccess()
  }

  return {
    sqlFilters, accessFilters, sqlPage, accessPage,
    sql, sqlTotal, sqlRefreshError, access, accessRefreshError,
    refreshSql, refreshAccess, filterSql, filterAccess,
  }
})
