<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { useMcpLabStore, type QueryHistoryItem } from '@/stores/mcpLab'
import type { components } from '@/types/api'
import type { McpLabApiError } from '@/api/services'
import { ensureEvidenceSessionId } from '@/api/client'

type McpQueryMeta = components['schemas']['McpQueryMeta']
type ProposalEvidence = components['schemas']['ProposalEvidence']

const store = useMcpLabStore()
const {
  objectQuery,
  metricsQuery,
  proposal,
  queryDetail,
  proposalDetail,
  objectRefreshError,
  metricsRefreshError,
  proposalRefreshError,
  citableHistory,
  historyClearedHint,
} = storeToRefs(store)

const showObjectRawJson = ref(false)
const showMetricsRawJson = ref(false)
const expandedEvidence = ref<string | null>(null)
const detailForm = reactive({
  query_id: '',
  proposal_id: '',
})

const objectForm = reactive({
  object: 'Customer',
  filtersJson: '{}',
  order_by: '',
  desc: false,
  limit: 20,
})

const metricsForm = reactive({
  metric: 'gross_margin_rate',
  group_by: '',
  limit: 24,
})

const proposalForm = reactive({
  object: 'Quotation',
  action: 'quote_review',
  conclusion: '',
  evidence: [{ claim: '', query_id: '' }] as { claim: string; query_id: string }[],
})

const evidenceSessionId = computed(() => ensureEvidenceSessionId())

type McpInterfaceRow = {
  tool: 'query_objects' | 'query_metrics' | 'propose_action'
  tier: '看' | '说'
  mode: string
  target: string
  params: string
  evidence: string
}

const mcpInterfaces: McpInterfaceRow[] = [
  {
    tool: 'query_objects',
    tier: '看',
    mode: '只读查询',
    target: '业务对象 / 对象目录',
    params: 'object, filters, order_by, desc, limit<=200',
    evidence: '返回 query_id + result_digest',
  },
  {
    tool: 'query_metrics',
    tier: '看',
    mode: '只读指标',
    target: '经营指标 / 指标目录',
    params: 'metric, group_by, limit<=200',
    evidence: '返回 query_id + result_digest',
  },
  {
    tool: 'propose_action',
    tier: '说',
    mode: '建议卡',
    target: '对象动作',
    params: 'object, action, conclusion, evidence[]',
    evidence: '引用同 session 查询证据',
  },
]

const selectedTool = ref<McpInterfaceRow['tool'] | null>(null)

const selectedInterface = computed(() =>
  mcpInterfaces.find((item) => item.tool === selectedTool.value) ?? null,
)

function openInterface(tool: McpInterfaceRow['tool']): void {
  selectedTool.value = tool
}

function backToList(): void {
  selectedTool.value = null
}

function metaOf(data: Record<string, unknown> | null | undefined): McpQueryMeta | null {
  const meta = data?.meta
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return null
  return meta as McpQueryMeta
}

function rowsOf(data: Record<string, unknown> | null | undefined): Record<string, unknown>[] {
  const rows = data?.rows
  return Array.isArray(rows) ? (rows as Record<string, unknown>[]) : []
}

function reasonLabel(err: McpLabApiError | null | undefined): string {
  if (!err || !('reason_code' in err) || !err.reason_code) return ''
  const map: Record<string, string> = {
    invalid_params: '参数错误',
    invalid_session: '会话无效',
    unknown_target: '未知目标',
    not_materialized: '尚未物化',
    not_published: '未发布',
    query_expired: 'query 已失效',
    evidence_not_found: '证据不存在',
    evidence_principal_mismatch: '主体不匹配',
    evidence_session_mismatch: '会话不匹配',
    evidence_source_mismatch: '来源不匹配',
    dataset_version_mismatch: '数据集版本不匹配',
    result_digest_mismatch: '摘要不匹配',
    evidence_integrity_failed: '证据损坏',
    tier_forbidden: '档位禁止',
    rate_limited: '限流',
    mcp_unavailable: 'MCP 不可用',
    evidence_store_unavailable: '证据存储不可用',
    execution_failed: '执行失败',
  }
  return map[err.reason_code] ?? err.reason_code
}

function shortValue(value: string | null | undefined, keep = 10): string {
  if (!value) return '—'
  return value.length <= keep * 2 ? value : `${value.slice(0, keep)}…${value.slice(-keep)}`
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function evidenceSnapshot(ev: ProposalEvidence): Record<string, unknown> {
  return {
    query_id: ev.query.query_id,
    tool: ev.query.tool,
    target: ev.query.target,
    source: ev.query.source,
    dataset_version: ev.query.dataset_version ?? null,
    created_at: ev.query.created_at,
    expires_at: ev.query.expires_at ?? null,
    result_digest: ev.query.result_digest,
    result_summary: ev.query.result_summary ?? {},
    warnings: ev.query.warnings ?? [],
    normalized_query: ev.query.normalized_query ?? {},
  }
}

async function onRunObjects(): Promise<void> {
  let filters: Record<string, unknown>
  try {
    filters = objectForm.filtersJson.trim()
      ? (JSON.parse(objectForm.filtersJson) as Record<string, unknown>)
      : {}
  } catch {
    objectRefreshError.value = {
      kind: 'parse',
      message: 'filters JSON 无效',
      retriable: false,
    }
    return
  }
  const params: Record<string, unknown> = {
    object: objectForm.object || undefined,
    filters,
    limit: objectForm.limit,
    desc: objectForm.desc,
  }
  if (objectForm.order_by.trim()) params.order_by = objectForm.order_by.trim()
  await store.runObjectQuery(params)
}

async function onRunMetrics(): Promise<void> {
  const params: Record<string, unknown> = {
    metric: metricsForm.metric || undefined,
    limit: metricsForm.limit,
  }
  if (metricsForm.group_by.trim()) params.group_by = metricsForm.group_by.trim()
  await store.runMetricsQuery(params)
}

function addEvidenceRow(): void {
  proposalForm.evidence.push({ claim: '', query_id: '' })
}

function removeEvidenceRow(idx: number): void {
  if (proposalForm.evidence.length <= 1) return
  proposalForm.evidence.splice(idx, 1)
}

async function onRunProposal(): Promise<void> {
  const evidence = []
  for (const row of proposalForm.evidence) {
    const claim = row.claim.trim()
    const queryId = row.query_id.trim()
    if (!claim || !queryId) {
      proposalRefreshError.value = {
        kind: 'parse',
        message: 'evidence 需要同时填写 claim 和 query_id',
        retriable: false,
      }
      return
    }
    const history = store.findHistory(queryId)
    if (!history?.result_digest) {
      proposalRefreshError.value = {
        kind: 'parse',
        message: '所选 query 缺少 result_digest，请重新查询后再引用',
        retriable: false,
      }
      return
    }
    evidence.push({
      claim,
      query_id: queryId,
      result_digest: history.result_digest,
    })
  }
  await store.runProposal({
    object: proposalForm.object,
    action: proposalForm.action,
    conclusion: proposalForm.conclusion,
    evidence,
  })
}

function toggleEvidence(queryId: string): void {
  expandedEvidence.value = expandedEvidence.value === queryId ? null : queryId
}

function historyFor(queryId: string): QueryHistoryItem | undefined {
  return store.findHistory(queryId)
}

async function onLoadQueryDetail(): Promise<void> {
  await store.loadQueryDetail(detailForm.query_id.trim())
}

async function onLoadProposalDetail(): Promise<void> {
  await store.loadProposalDetail(detailForm.proposal_id.trim())
}
</script>

<template>
  <section class="mcp-lab" data-testid="mcp-lab-page">
    <div class="d2a-card scope-banner" data-testid="mcp-scope-banner">
      <p>
        query evidence 以当前标签页 session 隔离；建议卡为「说」档结构化输出，不会执行 ERP 写回或其他「做」档动作。
      </p>
      <p class="scope-banner__hint" data-testid="evidence-session-id">
        session={{ evidenceSessionId ?? '尚未生成' }}
      </p>
      <p v-if="historyClearedHint" class="scope-banner__hint" data-testid="history-cleared-hint">
        {{ historyClearedHint }}
      </p>
    </div>

    <div
      v-if="selectedTool === null"
      class="d2a-card interface-catalog"
      data-testid="mcp-interface-catalog"
    >
      <div class="interface-catalog__head">
        <h3>MCP 接口清单</h3>
        <span data-testid="mcp-interface-count">共 {{ mcpInterfaces.length }} 个</span>
      </div>
      <el-table
        :data="mcpInterfaces"
        size="small"
        data-testid="mcp-interface-table"
      >
        <el-table-column prop="tool" label="接口" min-width="150" />
        <el-table-column prop="tier" label="档位" width="72">
          <template #default="{ row }">
            <el-tag size="small" :type="row.tier === '说' ? 'warning' : 'success'">
              {{ row.tier }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="mode" label="能力" min-width="100" />
        <el-table-column prop="target" label="目标" min-width="150" />
        <el-table-column prop="params" label="参数" min-width="260" />
        <el-table-column prop="evidence" label="证据" min-width="190" />
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button
              size="small"
              :data-testid="`open-interface-${row.tool}`"
              @click="openInterface(row.tool)"
            >
              查看详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <section
      v-else
      class="d2a-card interface-detail"
      data-testid="mcp-interface-detail"
    >
      <div class="interface-detail__head">
        <el-button size="small" data-testid="back-to-interface-list" @click="backToList">返回列表</el-button>
        <div>
          <h3>{{ selectedInterface?.tool }}</h3>
          <p>{{ selectedInterface?.mode }} · {{ selectedInterface?.target }}</p>
        </div>
      </div>
      <dl class="interface-detail__meta">
        <div>
          <dt>档位</dt>
          <dd>{{ selectedInterface?.tier }}</dd>
        </div>
        <div>
          <dt>参数</dt>
          <dd>{{ selectedInterface?.params }}</dd>
        </div>
        <div>
          <dt>证据</dt>
          <dd>{{ selectedInterface?.evidence }}</dd>
        </div>
      </dl>
    </section>

    <section
      v-if="selectedTool === 'query_objects'"
      class="d2a-card interface-panel"
      data-testid="object-interface-panel"
    >
      <div class="interface-panel__head">
        <h3>query_objects</h3>
        <p>对象查询 · 不带 object 返回对象目录</p>
      </div>
      <div class="form-grid" data-testid="object-query-form">
        <label>对象 <input v-model="objectForm.object" data-testid="object-name" /></label>
        <label>filters(JSON)
          <textarea v-model="objectForm.filtersJson" rows="3" data-testid="object-filters" />
        </label>
        <label>order_by <input v-model="objectForm.order_by" data-testid="object-order-by" /></label>
        <label class="inline">
          <input v-model="objectForm.desc" type="checkbox" data-testid="object-desc" /> desc
        </label>
        <label>limit
          <input v-model.number="objectForm.limit" type="number" min="1" max="200" data-testid="object-limit" />
        </label>
        <el-button type="primary" data-testid="object-run" @click="onRunObjects">执行查询</el-button>
      </div>
      <p v-if="objectRefreshError" class="refresh-warning" data-testid="object-refresh-error">
        刷新失败({{ reasonLabel(objectRefreshError as McpLabApiError) || objectRefreshError.message }})，保留上次成功结果
      </p>
      <LoadingState v-if="objectQuery.status === 'loading'" />
      <template v-else-if="objectQuery.status === 'error'">
        <p
          v-if="reasonLabel(objectQuery.error as McpLabApiError)"
          class="reason-code"
          data-testid="object-error-reason"
        >
          {{ reasonLabel(objectQuery.error as McpLabApiError) }}
        </p>
        <ErrorState
          :error="objectQuery.error"
          @retry="onRunObjects"
        />
      </template>
      <div v-else-if="objectQuery.status === 'success'" data-testid="object-result">
        <div class="result-meta" data-testid="object-result-meta">
          <span>query_id={{ metaOf(objectQuery.data)?.query_id ?? 'null' }}</span>
          <span>scope={{ metaOf(objectQuery.data)?.evidence_scope ?? '—' }}</span>
          <span>session={{ shortValue(metaOf(objectQuery.data)?.session_id) }}</span>
          <span>digest={{ shortValue(metaOf(objectQuery.data)?.result_digest) }}</span>
          <span>耗时 {{ metaOf(objectQuery.data)?.duration_ms ?? '-' }} ms</span>
          <span>行数 {{ metaOf(objectQuery.data)?.row_count ?? rowsOf(objectQuery.data).length }}</span>
          <span data-testid="object-dataset-version">
            dataset={{ metaOf(objectQuery.data)?.dataset_version ?? '尚未发布' }}
          </span>
          <span data-testid="object-template-version">
            template={{ metaOf(objectQuery.data)?.template_version ?? '—' }}
          </span>
        </div>
        <p v-if="metaOf(objectQuery.data)?.masked_fields?.length" data-testid="object-masked">
          已脱敏: {{ metaOf(objectQuery.data)?.masked_fields?.join(', ') }}
        </p>
        <ul v-if="metaOf(objectQuery.data)?.warnings?.length" data-testid="object-warnings">
          <li v-for="w in metaOf(objectQuery.data)?.warnings" :key="w">{{ w }}</li>
        </ul>
        <EmptyState
          v-if="rowsOf(objectQuery.data).length === 0"
          title="查询成功，无行返回"
        />
        <el-table
          v-else
          :data="rowsOf(objectQuery.data)"
          size="small"
          data-testid="object-rows-table"
        >
          <el-table-column
            v-for="col in Object.keys(rowsOf(objectQuery.data)[0] ?? {})"
            :key="col"
            :prop="col"
            :label="col"
            min-width="120"
          />
        </el-table>
        <el-button size="small" data-testid="toggle-object-json" @click="showObjectRawJson = !showObjectRawJson">
          {{ showObjectRawJson ? '隐藏' : '显示' }}原始 JSON
        </el-button>
        <pre v-if="showObjectRawJson" data-testid="object-raw-json">{{ JSON.stringify(objectQuery.data, null, 2) }}</pre>
      </div>
    </section>

    <section
      v-if="selectedTool === 'query_metrics'"
      class="d2a-card interface-panel"
      data-testid="metrics-interface-panel"
    >
      <div class="interface-panel__head">
        <h3>query_metrics</h3>
        <p>指标查询 · 不带 metric 返回指标目录</p>
      </div>
      <div class="form-grid" data-testid="metrics-query-form">
        <label>指标 <input v-model="metricsForm.metric" data-testid="metric-name" /></label>
        <label>group_by <input v-model="metricsForm.group_by" data-testid="metric-group-by" /></label>
        <label>limit
          <input v-model.number="metricsForm.limit" type="number" min="1" max="200" data-testid="metric-limit" />
        </label>
        <el-button type="primary" data-testid="metrics-run" @click="onRunMetrics">执行查询</el-button>
      </div>
      <p v-if="metricsRefreshError" class="refresh-warning" data-testid="metrics-refresh-error">
        刷新失败({{ reasonLabel(metricsRefreshError as McpLabApiError) || metricsRefreshError.message }})，保留上次成功结果
      </p>
      <LoadingState v-if="metricsQuery.status === 'loading'" />
      <ErrorState
        v-else-if="metricsQuery.status === 'error'"
        :error="metricsQuery.error"
        @retry="onRunMetrics"
      />
      <div v-else-if="metricsQuery.status === 'success'" data-testid="metrics-result">
        <div class="result-meta" data-testid="metrics-result-meta">
          <span>query_id={{ metaOf(metricsQuery.data)?.query_id ?? 'null' }}</span>
          <span>scope={{ metaOf(metricsQuery.data)?.evidence_scope ?? '—' }}</span>
          <span>session={{ shortValue(metaOf(metricsQuery.data)?.session_id) }}</span>
          <span>digest={{ shortValue(metaOf(metricsQuery.data)?.result_digest) }}</span>
          <span>耗时 {{ metaOf(metricsQuery.data)?.duration_ms ?? '-' }} ms</span>
          <span>行数 {{ metaOf(metricsQuery.data)?.row_count ?? rowsOf(metricsQuery.data).length }}</span>
          <span data-testid="metrics-dataset-version">
            dataset={{ metaOf(metricsQuery.data)?.dataset_version ?? '尚未发布' }}
          </span>
        </div>
        <ul v-if="metaOf(metricsQuery.data)?.warnings?.length" data-testid="metrics-warnings">
          <li v-for="w in metaOf(metricsQuery.data)?.warnings" :key="w">{{ w }}</li>
        </ul>
        <EmptyState
          v-if="rowsOf(metricsQuery.data).length === 0"
          title="查询成功，无行返回"
        />
        <el-table
          v-else
          :data="rowsOf(metricsQuery.data)"
          size="small"
          data-testid="metrics-rows-table"
        >
          <el-table-column
            v-for="col in Object.keys(rowsOf(metricsQuery.data)[0] ?? {})"
            :key="col"
            :prop="col"
            :label="col"
            min-width="120"
          />
        </el-table>
        <el-button size="small" data-testid="toggle-metrics-json" @click="showMetricsRawJson = !showMetricsRawJson">
          {{ showMetricsRawJson ? '隐藏' : '显示' }}原始 JSON
        </el-button>
        <pre v-if="showMetricsRawJson" data-testid="metrics-raw-json">{{ JSON.stringify(metricsQuery.data, null, 2) }}</pre>
      </div>
    </section>

    <section
      v-if="selectedTool === 'propose_action'"
      class="d2a-card interface-panel"
      data-testid="proposal-interface-panel"
    >
      <div class="interface-panel__head">
        <h3>propose_action</h3>
        <p>说档建议卡 · 引用同一 session 的 query evidence</p>
      </div>
      <div class="form-grid" data-testid="proposal-form">
        <label>对象 <input v-model="proposalForm.object" data-testid="proposal-object" /></label>
        <label>动作 <input v-model="proposalForm.action" data-testid="proposal-action" /></label>
        <label>结论
          <textarea v-model="proposalForm.conclusion" rows="2" data-testid="proposal-conclusion" />
        </label>
        <div
          v-for="(row, idx) in proposalForm.evidence"
          :key="idx"
          class="evidence-row"
          data-testid="evidence-row"
        >
          <label>claim <input v-model="row.claim" :data-testid="`evidence-claim-${idx}`" /></label>
          <label>query_id
            <select v-model="row.query_id" :data-testid="`evidence-query-${idx}`">
              <option value="">选择当前会话中的持久 query evidence</option>
              <option v-for="h in citableHistory" :key="h.query_id" :value="h.query_id">
                {{ h.query_id }} · {{ h.tool }} · {{ h.target }} · {{ shortValue(h.result_digest, 6) }}
              </option>
            </select>
          </label>
          <p v-if="historyFor(row.query_id)?.result_digest" class="muted">
            digest={{ shortValue(historyFor(row.query_id)?.result_digest, 10) }}
          </p>
          <el-button size="small" @click="removeEvidenceRow(idx)">删除</el-button>
        </div>
        <el-button size="small" data-testid="evidence-add" @click="addEvidenceRow">添加 evidence</el-button>
        <el-button type="primary" data-testid="proposal-run" @click="onRunProposal">生成建议卡</el-button>
        <p class="muted" data-testid="no-execute-hint">本页不提供执行建议或写回控件。</p>
      </div>
      <p v-if="proposalRefreshError" class="refresh-warning" data-testid="proposal-refresh-error">
        {{ reasonLabel(proposalRefreshError as McpLabApiError) || proposalRefreshError.message }}
      </p>
      <LoadingState v-if="proposal.status === 'loading'" />
      <ErrorState
        v-else-if="proposal.status === 'error'"
        :error="proposal.error"
        @retry="onRunProposal"
      />
      <div v-else-if="proposal.status === 'success'" class="proposal-card" data-testid="proposal-result">
        <h3>{{ proposal.data.conclusion }}</h3>
        <p>档位: {{ proposal.data.tier }} · {{ proposal.data.action_desc }}</p>
        <p data-testid="proposal-summary-meta">
          proposal_id={{ proposal.data.proposal_id }} · session={{ shortValue(proposal.data.session_id) }} ·
          dataset={{ proposal.data.dataset_version ?? '—' }}
        </p>
        <p data-testid="proposal-governance">{{ proposal.data.governance }}</p>
        <ul v-if="proposal.data.caveats?.length" data-testid="proposal-caveats">
          <li v-for="c in proposal.data.caveats" :key="c">{{ c }}</li>
        </ul>
        <div
          v-for="ev in proposal.data.evidence"
          :key="ev.query.query_id + ev.claim"
          class="evidence-item"
          data-testid="proposal-evidence-item"
        >
          <button type="button" @click="toggleEvidence(ev.query.query_id)">
            {{ ev.claim }} ({{ ev.query.query_id }}) · {{ shortValue(ev.query.result_digest, 8) }}
          </button>
          <div v-if="expandedEvidence === ev.query.query_id" data-testid="evidence-expand">
            <pre>{{ prettyJson(evidenceSnapshot(ev)) }}</pre>
          </div>
        </div>
      </div>
    </section>

    <div v-if="selectedTool !== null" class="d2a-card" data-testid="evidence-detail-panel">
      <h3>持久 Evidence 详情</h3>
      <div class="form-grid">
        <label>query_id
          <input v-model="detailForm.query_id" data-testid="detail-query-id" />
        </label>
        <el-button size="small" data-testid="detail-query-load" @click="onLoadQueryDetail">读取 Query 详情</el-button>
        <LoadingState v-if="queryDetail.status === 'loading'" />
        <ErrorState
          v-else-if="queryDetail.status === 'error'"
          :error="queryDetail.error"
          @retry="onLoadQueryDetail"
        />
        <pre v-else-if="queryDetail.status === 'success'" data-testid="detail-query-result">
{{ prettyJson(queryDetail.data) }}</pre>
      </div>
      <div class="form-grid detail-panel">
        <label>proposal_id
          <input v-model="detailForm.proposal_id" data-testid="detail-proposal-id" />
        </label>
        <el-button size="small" data-testid="detail-proposal-load" @click="onLoadProposalDetail">读取 Proposal 详情</el-button>
        <LoadingState v-if="proposalDetail.status === 'loading'" />
        <ErrorState
          v-else-if="proposalDetail.status === 'error'"
          :error="proposalDetail.error"
          @retry="onLoadProposalDetail"
        />
        <pre v-else-if="proposalDetail.status === 'success'" data-testid="detail-proposal-result">
{{ prettyJson(proposalDetail.data) }}</pre>
      </div>
    </div>
  </section>
</template>

<style scoped>
.mcp-lab {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.scope-banner p {
  margin: 0;
  line-height: 1.5;
  color: var(--el-text-color-regular);
}
.scope-banner__hint {
  margin-top: 8px !important;
  color: var(--el-color-warning);
}
.interface-catalog__head,
.interface-panel__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.interface-catalog__head h3,
.interface-panel__head h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.interface-catalog__head span,
.interface-panel__head p {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.interface-detail__head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}
.interface-detail__head h3,
.interface-detail__head p {
  margin: 0;
}
.interface-detail__head p {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.interface-detail__meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 16px 0 0;
}
.interface-detail__meta div {
  padding: 10px 12px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}
.interface-detail__meta dt {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.interface-detail__meta dd {
  margin: 4px 0 0;
  font-size: 13px;
}
.interface-panel {
  display: grid;
  gap: 12px;
}
.form-grid {
  display: grid;
  gap: 10px;
  max-width: 720px;
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.form-grid input,
.form-grid textarea,
.form-grid select {
  font: inherit;
  padding: 6px 8px;
}
.form-grid .inline {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}
.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.refresh-warning {
  color: var(--el-color-warning);
  font-size: 13px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.evidence-row {
  display: grid;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--el-border-color-lighter);
}
.detail-panel {
  margin-top: 16px;
}
.proposal-card h3 {
  margin: 0 0 8px;
}
.evidence-item {
  margin-top: 8px;
}
.evidence-item button {
  background: none;
  border: none;
  color: var(--el-color-primary);
  cursor: pointer;
  padding: 0;
  text-align: left;
}
pre {
  overflow: auto;
  max-height: 320px;
  font-size: 12px;
  background: var(--el-fill-color-light);
  padding: 8px;
}
</style>
