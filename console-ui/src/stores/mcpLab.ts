/**
 * MCP Lab store(M6):对象/指标查询、进程内查询历史、说档建议卡。
 * 查询历史仅内存保存;失败保留上次成功结果;请求代际防旧覆盖。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import {
  postMcpCall,
  postProposal,
  type McpLabApiError,
  type McpToolResult,
  type ProposalRequest,
  type ProposalResponse,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type McpQueryMeta = components['schemas']['McpQueryMeta']

export interface QueryHistoryItem {
  query_id: string
  tool: 'query_objects' | 'query_metrics'
  target: string
  at: string
  result: McpToolResult
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

  const citableHistory = computed(() =>
    history.value.filter((h) => Boolean(h.query_id)),
  )

  function rememberSuccess(result: McpToolResult): void {
    const meta = metaOf(result)
    if (!meta?.query_id) return
    const item: QueryHistoryItem = {
      query_id: meta.query_id,
      tool: meta.tool,
      target: meta.target,
      at: new Date().toISOString(),
      result,
    }
    history.value = [item, ...history.value.filter((h) => h.query_id !== item.query_id)]
      .slice(0, HISTORY_CAP)
    historyClearedHint.value = null
  }

  function clearHistory(reason = '配置或进程边界已变化,请重新查询后再引用 evidence'): void {
    history.value = []
    historyClearedHint.value = reason
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
    } else if (first) {
      objectQuery.value = { status: 'error', error: result.error }
    } else {
      objectRefreshError.value = result.error
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
    } else if (first) {
      metricsQuery.value = { status: 'error', error: result.error }
    } else {
      metricsRefreshError.value = result.error
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
    } else if (first) {
      proposal.value = { status: 'error', error: result.error }
    } else {
      proposalRefreshError.value = result.error
    }
  }

  function findHistory(queryId: string): QueryHistoryItem | undefined {
    return history.value.find((h) => h.query_id === queryId)
  }

  return {
    objectQuery,
    metricsQuery,
    proposal,
    objectRefreshError,
    metricsRefreshError,
    proposalRefreshError,
    history,
    historyClearedHint,
    citableHistory,
    runObjectQuery,
    runMetricsQuery,
    runProposal,
    clearHistory,
    findHistory,
  }
})
