<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { useMcpLabStore, type QueryHistoryItem } from '@/stores/mcpLab'
import type { components } from '@/types/api'
import type { McpLabApiError } from '@/api/services'

type McpQueryMeta = components['schemas']['McpQueryMeta']

const store = useMcpLabStore()
const {
  objectQuery,
  metricsQuery,
  proposal,
  objectRefreshError,
  metricsRefreshError,
  proposalRefreshError,
  citableHistory,
  historyClearedHint,
} = storeToRefs(store)

const tab = ref<'objects' | 'metrics' | 'proposal'>('objects')
const showObjectRawJson = ref(false)
const showMetricsRawJson = ref(false)
const expandedEvidence = ref<string | null>(null)

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

const activeResult = computed(() => {
  if (tab.value === 'metrics') return metricsQuery.value
  if (tab.value === 'objects') return objectQuery.value
  return null
})

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
    unknown_target: '未知目标',
    not_materialized: '尚未物化',
    query_expired: 'query 已失效',
    tier_forbidden: '档位禁止',
    rate_limited: '限流',
    mcp_unavailable: 'MCP 不可用',
    execution_failed: '执行失败',
  }
  return map[err.reason_code] ?? err.reason_code
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
  await store.runProposal({
    object: proposalForm.object,
    action: proposalForm.action,
    conclusion: proposalForm.conclusion,
    evidence: proposalForm.evidence.map((e) => ({
      claim: e.claim,
      query_id: e.query_id,
    })),
  })
}

function toggleEvidence(queryId: string): void {
  expandedEvidence.value = expandedEvidence.value === queryId ? null : queryId
}

function historyFor(queryId: string): QueryHistoryItem | undefined {
  return store.findHistory(queryId)
}
</script>

<template>
  <section class="mcp-lab" data-testid="mcp-lab-page">
    <div class="d2a-card scope-banner" data-testid="mcp-scope-banner">
      <p>
        query ID 仅在当前 Console 进程内有效；服务重启或配置变更后需重新查询。
        建议卡为「说」档结构化输出，不会执行 ERP 写回或其他「做」档动作。
      </p>
      <p v-if="historyClearedHint" class="scope-banner__hint" data-testid="history-cleared-hint">
        {{ historyClearedHint }}
      </p>
    </div>

    <div class="d2a-card">
      <el-tabs v-model="tab" data-testid="mcp-tabs">
        <el-tab-pane label="对象查询" name="objects">
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
          <ErrorState
            v-else-if="objectQuery.status === 'error'"
            :error="objectQuery.error"
            @retry="onRunObjects"
          />
          <div v-else-if="objectQuery.status === 'success'" data-testid="object-result">
            <div class="result-meta" data-testid="object-result-meta">
              <span>query_id={{ metaOf(objectQuery.data)?.query_id ?? 'null' }}</span>
              <span>耗时 {{ metaOf(objectQuery.data)?.duration_ms ?? '-' }} ms</span>
              <span>行数 {{ metaOf(objectQuery.data)?.row_count ?? rowsOf(objectQuery.data).length }}</span>
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
        </el-tab-pane>

        <el-tab-pane label="指标查询" name="metrics">
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
              <span>耗时 {{ metaOf(metricsQuery.data)?.duration_ms ?? '-' }} ms</span>
              <span>行数 {{ metaOf(metricsQuery.data)?.row_count ?? rowsOf(metricsQuery.data).length }}</span>
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
        </el-tab-pane>

        <el-tab-pane label="建议卡" name="proposal">
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
                  <option value="">选择当前进程内成功查询</option>
                  <option v-for="h in citableHistory" :key="h.query_id" :value="h.query_id">
                    {{ h.query_id }} · {{ h.tool }} · {{ h.target }}
                  </option>
                </select>
              </label>
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
                {{ ev.claim }} ({{ ev.query.query_id }})
              </button>
              <div v-if="expandedEvidence === ev.query.query_id" data-testid="evidence-expand">
                <template v-if="historyFor(ev.query.query_id)">
                  <pre>{{ JSON.stringify(historyFor(ev.query.query_id)?.result, null, 2) }}</pre>
                </template>
                <p v-else class="muted" data-testid="evidence-missing">
                  当前内存中没有该 query 的结果摘要；query ID 仅进程内有效，请重新查询。
                </p>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- keep unused activeResult referenced for future shared pane -->
    <span v-show="false">{{ activeResult?.status }}</span>
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
