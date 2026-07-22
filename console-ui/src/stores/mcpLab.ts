/**
 * MCP Lab store(M6):对象/指标查询、进程内查询历史、说档建议卡。
 * 查询历史仅内存保存;失败保留上次成功结果;请求代际防旧覆盖。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import {
  getProposalDetail,
  getQueryEvidenceDetail,
  postMcpCall,
  postProposal,
  type McpLabApiError,
  type McpToolResult,
  type ProposalRequest,
  type ProposalResponse,
  type QueryEvidenceDetailResponse,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type McpQueryMeta = components['schemas']['McpQueryMeta']

export interface QueryHistoryItem {
  query_id: string
  tool: 'query_objects' | 'query_metrics'
  target: string
  created_at: string | null
  expires_at: string | null
  session_id: string | null
  dataset_version: string | null
  result_digest: string
  result_summary: Record<string, unknown> | null
  warnings: string[]
}

const HISTORY_CAP = 50

function metaOf(result: McpToolResult): McpQueryMeta | null {
  const meta = result.meta
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return null
  const m = meta as Record<string, unknown>
  if (m.tool !== 'query_objects' && m.tool !== 'query_metrics') return null
  if (typeof m.target !== 'string') return null
  return m as unknown as McpQueryMeta
}

export const useMcpLabStore = defineStore('mcpLab', () => {
  const objectQuery = ref<RequestState<McpToolResult>>({ status: 'idle' })
  const metricsQuery = ref<RequestState<McpToolResult>>({ status: 'idle' })
  const proposal = ref<RequestState<ProposalResponse>>({ status: 'idle' })
  const queryDetail = ref<RequestState<QueryEvidenceDetailResponse>>({ status: 'idle' })
  const proposalDetail = ref<RequestState<ProposalResponse>>({ status: 'idle' })
  const objectRefreshError = ref<McpLabApiError | ApiError | null>(null)
  const metricsRefreshError = ref<McpLabApiError | ApiError | null>(null)
  const proposalRefreshError = ref<McpLabApiError | ApiError | null>(null)
  const history = ref<QueryHistoryItem[]>([])
  const historyClearedHint = ref<string | null>(null)

  let objectGen = 0
  let metricsGen = 0
  let proposalGen = 0
  let objectAbort: AbortController | null = null
  let metricsAbort: AbortController | null = null
  let proposalAbort: AbortController | null = null
  let queryDetailGen = 0
  let proposalDetailGen = 0
  let queryDetailAbort: AbortController | null = null
  let proposalDetailAbort: AbortController | null = null

  const citableHistory = computed(() =>
    history.value.filter((h) => Boolean(h.query_id) && Boolean(h.result_digest)),
  )

  function rememberSuccess(result: McpToolResult): void {
    const meta = metaOf(result)
    if (!meta?.query_id || !meta.result_digest) return
    const item: QueryHistoryItem = {
      query_id: meta.query_id,
      tool: meta.tool,
      target: meta.target,
      created_at: meta.created_at ?? null,
      expires_at: meta.expires_at ?? null,
      session_id: meta.session_id ?? null,
      dataset_version: meta.dataset_version ?? null,
      result_digest: meta.result_digest,
      result_summary: (meta.result_summary as Record<string, unknown> | null | undefined) ?? null,
      warnings: [...(meta.warnings ?? [])],
    }
    history.value = [item, ...history.value.filter((h) => h.query_id !== item.query_id)]
      .slice(0, HISTORY_CAP)
    historyClearedHint.value = null
  }

  function clearHistory(reason = '配置或进程边界已变化,请重新查询后再引用 evidence'): void {
    history.value = []
    historyClearedHint.value = reason
  }

  /**
   * 登出或认证失效时销毁当前标签页的 evidence 视图。
   * 不能只清浏览器 header：内存中的 query/proposal snapshot 也属于旧会话，
   * 否则重新登录后页面会继续展示并可能引用旧 evidence。
   */
  function resetForSessionBoundary(): void {
    objectGen += 1
    metricsGen += 1
    proposalGen += 1
    queryDetailGen += 1
    proposalDetailGen += 1
    objectAbort?.abort()
    metricsAbort?.abort()
    proposalAbort?.abort()
    queryDetailAbort?.abort()
    proposalDetailAbort?.abort()
    objectAbort = null
    metricsAbort = null
    proposalAbort = null
    queryDetailAbort = null
    proposalDetailAbort = null
    objectQuery.value = { status: 'idle' }
    metricsQuery.value = { status: 'idle' }
    proposal.value = { status: 'idle' }
    queryDetail.value = { status: 'idle' }
    proposalDetail.value = { status: 'idle' }
    objectRefreshError.value = null
    metricsRefreshError.value = null
    proposalRefreshError.value = null
    history.value = []
    historyClearedHint.value = null
  }

  async function runObjectQuery(params: Record<string, unknown>): Promise<void> {
    objectAbort?.abort()
    objectAbort = new AbortController()
    const gen = ++objectGen
    const first = objectQuery.value.status !== 'success'
    if (first) objectQuery.value = { status: 'loading' }
    const result = await postMcpCall('query_objects', params, { signal: objectAbort.signal })
    if (gen !== objectGen) return
    if (result.ok) {
      objectQuery.value = { status: 'success', data: result.data }
      objectRefreshError.value = null
      rememberSuccess(result.data)
    } else {
      if ((result.error as McpLabApiError).reason_code === 'query_expired') {
        clearHistory('query ID 已失效,请重新查询后再引用 evidence')
      }
      if (first) {
        objectQuery.value = { status: 'error', error: result.error }
      } else {
        objectRefreshError.value = result.error
      }
    }
  }

  async function runMetricsQuery(params: Record<string, unknown>): Promise<void> {
    metricsAbort?.abort()
    metricsAbort = new AbortController()
    const gen = ++metricsGen
    const first = metricsQuery.value.status !== 'success'
    if (first) metricsQuery.value = { status: 'loading' }
    const result = await postMcpCall('query_metrics', params, { signal: metricsAbort.signal })
    if (gen !== metricsGen) return
    if (result.ok) {
      metricsQuery.value = { status: 'success', data: result.data }
      metricsRefreshError.value = null
      rememberSuccess(result.data)
    } else {
      if ((result.error as McpLabApiError).reason_code === 'query_expired') {
        clearHistory('query ID 已失效,请重新查询后再引用 evidence')
      }
      if (first) {
        metricsQuery.value = { status: 'error', error: result.error }
      } else {
        metricsRefreshError.value = result.error
      }
    }
  }

  async function runProposal(body: ProposalRequest): Promise<void> {
    proposalAbort?.abort()
    proposalAbort = new AbortController()
    const gen = ++proposalGen
    const first = proposal.value.status !== 'success'
    if (first) proposal.value = { status: 'loading' }
    const result = await postProposal(body, { signal: proposalAbort.signal })
    if (gen !== proposalGen) return
    if (result.ok) {
      proposal.value = { status: 'success', data: result.data }
      proposalRefreshError.value = null
    } else {
      if ((result.error as McpLabApiError).reason_code === 'query_expired') {
        clearHistory('query ID 已失效,请重新查询后再引用 evidence')
      }
      if (first) {
        proposal.value = { status: 'error', error: result.error }
      } else {
        proposalRefreshError.value = result.error
      }
    }
  }

  function findHistory(queryId: string): QueryHistoryItem | undefined {
    return history.value.find((h) => h.query_id === queryId)
  }

  async function loadQueryDetail(queryId: string): Promise<void> {
    queryDetailAbort?.abort()
    queryDetailAbort = new AbortController()
    const gen = ++queryDetailGen
    queryDetail.value = { status: 'loading' }
    const result = await getQueryEvidenceDetail(queryId, { signal: queryDetailAbort.signal })
    if (gen !== queryDetailGen) return
    if (result.ok) {
      queryDetail.value = { status: 'success', data: result.data }
    } else {
      queryDetail.value = { status: 'error', error: result.error }
    }
  }

  async function loadProposalDetail(proposalId: string): Promise<void> {
    proposalDetailAbort?.abort()
    proposalDetailAbort = new AbortController()
    const gen = ++proposalDetailGen
    proposalDetail.value = { status: 'loading' }
    const result = await getProposalDetail(proposalId, { signal: proposalDetailAbort.signal })
    if (gen !== proposalDetailGen) return
    if (result.ok) {
      proposalDetail.value = { status: 'success', data: result.data }
    } else {
      proposalDetail.value = { status: 'error', error: result.error }
    }
  }

  return {
    objectQuery,
    metricsQuery,
    proposal,
    queryDetail,
    proposalDetail,
    objectRefreshError,
    metricsRefreshError,
    proposalRefreshError,
    history,
    historyClearedHint,
    citableHistory,
    runObjectQuery,
    runMetricsQuery,
    runProposal,
    loadQueryDetail,
    loadProposalDetail,
    clearHistory,
    resetForSessionBoundary,
    findHistory,
  }
})
