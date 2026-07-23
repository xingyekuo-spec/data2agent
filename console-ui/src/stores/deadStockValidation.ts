import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import {
  getOverview,
  postApply,
  postMcpCall,
  type McpLabApiError,
  type McpToolResult,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type OverviewResponse = components['schemas']['OverviewResponse']
type ApplyActionResult = components['schemas']['ApplyActionResult']

export interface DeadStockFilters {
  plant_id: string
  item_code: string
  root_cause: string
  confidence_level: string
}

export interface EvidenceSection {
  object: string
  title: string
  state: RequestState<McpToolResult>
}

const EVIDENCE_OBJECTS = [
  { object: 'DeadStockAttribution', title: '归因标签' },
  { object: 'PurchaseOverbuyEvidence', title: '采购超采证据' },
  { object: 'ProductionLossEvidence', title: '生产领料证据' },
  { object: 'MaterialOrderEvidence', title: '订单取消/减量证据' },
  { object: 'EcnChangeEvidence', title: 'ECN 变更证据' },
  { object: 'SpecialConditionEvidence', title: '特殊使用条件' },
  { object: 'DuplicateMaterialCandidate', title: '重复料候选' },
  { object: 'MaterialBomUsage', title: 'BOM 使用' },
  { object: 'MaterialSubstituteCandidate', title: '可消耗候选' },
] as const

function rowsOf(result: McpToolResult | null | undefined): Record<string, unknown>[] {
  const rows = result?.rows
  return Array.isArray(rows) ? (rows as Record<string, unknown>[]) : []
}

function metaWarnings(result: McpToolResult | null | undefined): string[] {
  const meta = result?.meta
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return []
  const warnings = (meta as { warnings?: unknown }).warnings
  return Array.isArray(warnings) ? warnings.map(String) : []
}

function resultError(state: RequestState<unknown>): ApiError | McpLabApiError | null {
  return state.status === 'error' ? state.error : null
}

export const useDeadStockValidationStore = defineStore('deadStockValidation', () => {
  const overview = ref<RequestState<OverviewResponse>>({ status: 'idle' })
  const deadItems = ref<RequestState<McpToolResult>>({ status: 'idle' })
  const attributionDistribution = ref<RequestState<McpToolResult>>({ status: 'idle' })
  const consumableMetric = ref<RequestState<McpToolResult>>({ status: 'idle' })
  const substituteCandidates = ref<RequestState<McpToolResult>>({ status: 'idle' })
  const publishState = ref<RequestState<ApplyActionResult> | null>(null)
  const refreshError = ref<ApiError | McpLabApiError | null>(null)
  const selectedItemCode = ref('')
  const filters = reactive<DeadStockFilters>({
    plant_id: '',
    item_code: '',
    root_cause: '',
    confidence_level: '',
  })
  const evidenceSections = ref<EvidenceSection[]>(
    EVIDENCE_OBJECTS.map((item) => ({ ...item, state: { status: 'idle' } })),
  )

  let refreshGen = 0
  let evidenceGen = 0

  const deadItemRows = computed(() => rowsOf(deadItems.value.status === 'success' ? deadItems.value.data : null))
  const attributionRows = computed(() =>
    rowsOf(attributionDistribution.value.status === 'success' ? attributionDistribution.value.data : null),
  )
  const substituteRows = computed(() =>
    rowsOf(substituteCandidates.value.status === 'success' ? substituteCandidates.value.data : null),
  )
  const consumableRows = computed(() =>
    rowsOf(consumableMetric.value.status === 'success' ? consumableMetric.value.data : null),
  )
  const warnings = computed(() => {
    const out = new Set<string>()
    for (const state of [
      deadItems.value,
      attributionDistribution.value,
      consumableMetric.value,
      substituteCandidates.value,
      ...evidenceSections.value.map((section) => section.state),
    ]) {
      if (state.status === 'success') {
        for (const warning of metaWarnings(state.data as McpToolResult)) out.add(warning)
      }
    }
    return [...out]
  })
  const error = computed(() =>
    refreshError.value
    ?? resultError(overview.value)
    ?? resultError(deadItems.value)
    ?? resultError(attributionDistribution.value)
    ?? resultError(consumableMetric.value)
    ?? resultError(substituteCandidates.value),
  )
  const notPublished = computed(() => {
    const err = error.value as McpLabApiError | null
    return err?.status === 409 && err.reason_code === 'not_published'
  })
  const loading = computed(() =>
    overview.value.status === 'loading'
    || deadItems.value.status === 'loading'
    || attributionDistribution.value.status === 'loading'
    || consumableMetric.value.status === 'loading'
    || substituteCandidates.value.status === 'loading',
  )

  function objectFilters(extra: Record<string, unknown> = {}): Record<string, unknown> {
    const out: Record<string, unknown> = { ...extra }
    if (filters.plant_id.trim()) out.plant_id = filters.plant_id.trim()
    if (filters.item_code.trim()) out.item_code = filters.item_code.trim()
    return out
  }

  async function runObjectQuery(params: Record<string, unknown>) {
    const result = await postMcpCall('query_objects', params)
    if (result.ok) {
      return { status: 'success', data: result.data } as RequestState<McpToolResult>
    }
    return { status: 'error', error: result.error } as RequestState<McpToolResult>
  }

  async function refresh(): Promise<void> {
    const gen = ++refreshGen
    refreshError.value = null
    overview.value = overview.value.status === 'success' ? overview.value : { status: 'loading' }
    deadItems.value = deadItems.value.status === 'success' ? deadItems.value : { status: 'loading' }
    attributionDistribution.value = attributionDistribution.value.status === 'success'
      ? attributionDistribution.value
      : { status: 'loading' }
    consumableMetric.value = consumableMetric.value.status === 'success'
      ? consumableMetric.value
      : { status: 'loading' }
    substituteCandidates.value = substituteCandidates.value.status === 'success'
      ? substituteCandidates.value
      : { status: 'loading' }

    const [
      overviewResult,
      itemsResult,
      distributionResult,
      consumableResult,
      substituteResult,
    ] = await Promise.all([
      getOverview(),
      postMcpCall('query_objects', {
        object: 'DeadStockItem',
        filters: objectFilters(),
        order_by: 'dead_stock_amount',
        desc: true,
        limit: 50,
      }),
      postMcpCall('query_metrics', {
        metric: 'attribution_distribution',
        group_by: filters.root_cause ? '置信度等级' : '根因',
        limit: 50,
      }),
      postMcpCall('query_metrics', {
        metric: 'substitute_consumable_quantity',
        group_by: filters.item_code ? '物料' : '来源工厂',
        limit: 50,
      }),
      postMcpCall('query_objects', {
        object: 'MaterialSubstituteCandidate',
        filters: objectFilters(),
        limit: 30,
      }),
    ])
    if (gen !== refreshGen) return

    overview.value = overviewResult.ok
      ? { status: 'success', data: overviewResult.data }
      : { status: 'error', error: overviewResult.error }
    deadItems.value = itemsResult.ok
      ? { status: 'success', data: itemsResult.data }
      : { status: 'error', error: itemsResult.error }
    attributionDistribution.value = distributionResult.ok
      ? { status: 'success', data: distributionResult.data }
      : { status: 'error', error: distributionResult.error }
    consumableMetric.value = consumableResult.ok
      ? { status: 'success', data: consumableResult.data }
      : { status: 'error', error: consumableResult.error }
    substituteCandidates.value = substituteResult.ok
      ? { status: 'success', data: substituteResult.data }
      : { status: 'error', error: substituteResult.error }

    const first = deadItemRows.value[0]
    const nextSelection = selectedItemCode.value || (typeof first?.item_code === 'string' ? first.item_code : '')
    if (nextSelection) {
      await selectItem(nextSelection)
    }
  }

  async function selectItem(itemCode: string): Promise<void> {
    const code = itemCode.trim()
    selectedItemCode.value = code
    if (!code) {
      evidenceSections.value = EVIDENCE_OBJECTS.map((item) => ({ ...item, state: { status: 'idle' } }))
      return
    }
    const gen = ++evidenceGen
    evidenceSections.value = evidenceSections.value.map((section) => ({
      ...section,
      state: section.state.status === 'success' ? section.state : { status: 'loading' },
    }))
    const requests = await Promise.all(evidenceSections.value.map(async (section) => {
      const filtersForSection: Record<string, unknown> = { item_code: code }
      if (filters.root_cause.trim() && section.object === 'DeadStockAttribution') {
        filtersForSection.root_cause = filters.root_cause.trim()
      }
      if (filters.confidence_level.trim() && section.object === 'DeadStockAttribution') {
        filtersForSection.confidence_level = filters.confidence_level.trim()
      }
      if (filters.plant_id.trim() && section.object !== 'DuplicateMaterialCandidate') {
        filtersForSection.plant_id = filters.plant_id.trim()
      }
      return {
        ...section,
        state: await runObjectQuery({
          object: section.object,
          filters: filtersForSection,
          limit: 20,
        }),
      }
    }))
    if (gen !== evidenceGen) return
    evidenceSections.value = requests
  }

  async function applyFilters(): Promise<void> {
    selectedItemCode.value = filters.item_code.trim()
    await refresh()
  }

  async function buildAndPublish(): Promise<void> {
    publishState.value = { status: 'loading' }
    const result = await postApply({ source: 'digiwin_e10', publish: true })
    if (result.ok) {
      publishState.value = { status: 'success', data: result.data }
      await refresh()
    } else {
      publishState.value = { status: 'error', error: result.error }
    }
  }

  return {
    overview,
    deadItems,
    attributionDistribution,
    consumableMetric,
    substituteCandidates,
    publishState,
    evidenceSections,
    filters,
    selectedItemCode,
    deadItemRows,
    attributionRows,
    consumableRows,
    substituteRows,
    warnings,
    error,
    notPublished,
    loading,
    refresh,
    selectItem,
    applyFilters,
    buildAndPublish,
  }
})
