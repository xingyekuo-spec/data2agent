<script setup lang="ts">
/**
 * Mapping Preview 结果抽屉(M3-T07):只读摘要、行 diff、enum/业务键/derived 面板。
 * 无保存/发布入口;始终展示只读预览声明。
 */
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatCard from '@/components/dashboard/StatCard.vue'
import { useMappingPreviewStore } from '@/stores/mappingPreview'
import { formatJsonValue, formatPercent } from '@/utils/format'
import type { components } from '@/types/api'

type MappingPreviewIssue = components['schemas']['MappingPreviewIssue']
type MappingPreviewDiffField = components['schemas']['MappingPreviewDiffField']
type MappingPreviewRow = components['schemas']['MappingPreviewRow']
type MappingPreviewDiffRow = components['schemas']['MappingPreviewDiffRow']

const props = defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  retry: []
}>()

const store = useMappingPreviewStore()
const {
  preview,
  refreshError,
  stale,
  isEmpty,
  isUnauthorized,
  isLoading,
} = storeToRefs(store)

const data = computed(() =>
  preview.value.status === 'success' ? preview.value.data : null,
)

interface RowView {
  sample_row_id: string
  status_current: string | null
  status_candidate: string | null
  fields: MappingPreviewDiffField[]
  issues: MappingPreviewIssue[]
}

function indexBySampleRowId<T extends { sample_row_id: string }>(
  rows: T[],
): Record<string, T> {
  const out: Record<string, T> = {}
  for (const row of rows) out[row.sample_row_id] = row
  return out
}

const rowViews = computed((): RowView[] => {
  const d = data.value
  if (!d) return []
  // 显式收窄,避免 openapi-typescript 深层联合在 Map/泛型上触发 TS2589
  const currentRows = (d.current?.rows ?? []) as MappingPreviewRow[]
  const candidateRows = (d.candidate.rows ?? []) as MappingPreviewRow[]
  const diffRows = (d.diff.rows ?? []) as MappingPreviewDiffRow[]
  const currentById = indexBySampleRowId(currentRows)
  const candidateById = indexBySampleRowId(candidateRows)
  const diffById = indexBySampleRowId(diffRows)
  const idSet: Record<string, true> = {}
  for (const id of Object.keys(currentById)) idSet[id] = true
  for (const id of Object.keys(candidateById)) idSet[id] = true
  for (const id of Object.keys(diffById)) idSet[id] = true

  const views: RowView[] = []
  for (const id of Object.keys(idSet)) {
    const cur = currentById[id]
    const cand = candidateById[id]
    const diff = diffById[id]
    views.push({
      sample_row_id: id,
      status_current: diff?.status_before ?? cur?.status ?? null,
      status_candidate: diff?.status_after ?? cand?.status ?? null,
      fields: (diff?.fields ?? []) as MappingPreviewDiffField[],
      issues: (cand?.issues ?? []) as MappingPreviewIssue[],
    })
  }
  return views
})

const evaluation = computed(() => data.value?.candidate ?? null)

function formatFields(fields: MappingPreviewDiffField[]): string {
  if (!fields.length) return '—'
  return fields
    .map((f) => `${f.field}: ${formatJsonValue(f.before)} → ${formatJsonValue(f.after)}`)
    .join('; ')
}

function formatIssues(issues: MappingPreviewIssue[]): string {
  if (!issues.length) return '—'
  return issues.map((i) => `${i.reason_code}${i.field ? `(${i.field})` : ''}`).join(', ')
}

function statusLabel(s: string | null): string {
  if (s === 'mapped') return '已映射'
  if (s === 'quarantined') return '隔离'
  return '—'
}

function close(): void {
  emit('update:open', false)
}

function onClosed(): void {
  emit('update:open', false)
}
</script>

<template>
  <el-drawer
    :model-value="open"
    title="映射预览结果"
    size="720px"
    data-testid="mapping-preview-drawer"
    @close="close"
    @closed="onClosed"
  >
    <p
      class="readonly-banner"
      data-testid="preview-readonly-banner"
    >
      只读预览，不会保存/发布
    </p>

    <LoadingState
      v-if="isLoading && !data"
      text="正在试算样本…"
    />

    <div
      v-else-if="preview.status === 'error' && isUnauthorized"
      class="auth-error"
      data-testid="preview-unauthorized"
    >
      <p>需要有效的控制台登录才能执行映射预览。</p>
      <p class="auth-error__hint">
        请在管理页完成配置后重试。
      </p>
      <el-button
        size="small"
        data-testid="preview-retry"
        @click="emit('retry')"
      >
        重试
      </el-button>
    </div>

    <ErrorState
      v-else-if="preview.status === 'error'"
      :error="preview.error"
      @retry="emit('retry')"
    />

    <template v-else-if="data">
      <p
        v-if="stale"
        class="refresh-warning"
        data-testid="preview-stale"
      >
        输入已变更,以下为上一次成功结果;请重新提交预览。
      </p>
      <p
        v-if="refreshError"
        class="refresh-warning"
        data-testid="preview-refresh-error"
      >
        刷新失败({{ refreshError.message }}),展示上一次成功数据
      </p>
      <ul
        v-if="data.warnings.length"
        class="warnings"
        data-testid="preview-warnings"
      >
        <li
          v-for="w in data.warnings"
          :key="w"
        >
          {{ w }}
        </li>
      </ul>

      <EmptyState
        v-if="isEmpty"
        title="样本为空"
        hint="本次请求未取到 raw 行;可调整批次/offset/limit 后重试。"
      />

      <div
        class="summary-grid"
        data-testid="preview-summary"
      >
        <StatCard
          label="映射"
          :value="String(evaluation?.summary.mapped ?? 0)"
          :hint="`共 ${evaluation?.summary.total ?? 0} 行`"
          tone="good"
        />
        <StatCard
          label="隔离"
          :value="String(evaluation?.summary.quarantined ?? 0)"
          :hint="formatPercent(evaluation?.summary.quarantine_rate, '—')"
          :tone="(evaluation?.summary.quarantined ?? 0) > 0 ? 'warn' : 'default'"
        />
        <StatCard
          label="熔断预测"
          :value="evaluation?.summary.would_trip_breaker ? '会触发' : '不会'"
          hint="仅报告,不写数据"
          :tone="evaluation?.summary.would_trip_breaker ? 'bad' : 'good'"
        />
        <StatCard
          label="样本批次"
          :value="(data.sample.sample_batch_ids ?? []).join(', ') || '—'"
          :hint="`fingerprint ${data.sample.sample_fingerprint}`"
        />
      </div>

      <div
        class="meta-row"
        data-testid="preview-meta"
      >
        <span>模式: {{ data.mode }}</span>
        <span>模板: {{ data.template_version }}</span>
        <span>当前 hash: {{ data.current_binding_hash || '—' }}</span>
        <span>候选 hash: {{ data.candidate_binding_hash }}</span>
        <span>采样: {{ data.sample.sampled_rows }} 行</span>
      </div>

      <div class="section-title">
        行对比
      </div>
      <el-table
        :data="rowViews"
        size="small"
        data-testid="preview-rows-table"
        empty-text="无行数据"
      >
        <el-table-column
          prop="sample_row_id"
          label="样本行"
          min-width="120"
        />
        <el-table-column
          label="当前状态"
          width="100"
        >
          <template #default="{ row }">
            {{ statusLabel(row.status_current) }}
          </template>
        </el-table-column>
        <el-table-column
          label="候选状态"
          width="100"
        >
          <template #default="{ row }">
            {{ statusLabel(row.status_candidate) }}
          </template>
        </el-table-column>
        <el-table-column
          label="字段 diff"
          min-width="200"
        >
          <template #default="{ row }">
            <span class="mono">{{ formatFields(row.fields) }}</span>
          </template>
        </el-table-column>
        <el-table-column
          label="安全问题"
          min-width="140"
        >
          <template #default="{ row }">
            <span class="mono">{{ formatIssues(row.issues) }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="section-title">
        枚举缺口
      </div>
      <el-table
        :data="evaluation?.enum_gaps ?? []"
        size="small"
        data-testid="preview-enum-gaps"
        empty-text="无枚举缺口"
      >
        <el-table-column
          prop="field"
          label="字段"
          width="140"
        />
        <el-table-column
          prop="source_value"
          label="源值"
          min-width="120"
        />
        <el-table-column
          prop="count"
          label="次数"
          width="80"
        />
      </el-table>

      <div class="section-title">
        业务键问题(样本口径)
      </div>
      <div
        class="key-issues"
        data-testid="preview-key-issues"
      >
        <span>缺失: {{ evaluation?.business_key_issues.missing ?? 0 }}</span>
        <span>重复: {{ evaluation?.business_key_issues.duplicate ?? 0 }}</span>
        <span>范围: {{ evaluation?.business_key_issues.scope ?? 'sample' }}</span>
      </div>

      <div class="section-title">
        派生覆盖率
      </div>
      <el-table
        :data="evaluation?.derived_coverage ?? []"
        size="small"
        data-testid="preview-derived-coverage"
        empty-text="无派生字段覆盖数据"
      >
        <el-table-column
          prop="field"
          label="字段"
          width="140"
        />
        <el-table-column
          prop="matched_rows"
          label="命中"
          width="70"
        />
        <el-table-column
          prop="default_hits"
          label="默认"
          width="70"
        />
        <el-table-column
          prop="unmatched_rows"
          label="未匹配"
          width="80"
        />
        <el-table-column
          prop="eligible_rows"
          label="可达"
          width="70"
        />
        <el-table-column
          label="覆盖率"
          width="90"
        >
          <template #default="{ row }">
            {{ formatPercent(row.row_coverage, '—') }}
          </template>
        </el-table-column>
      </el-table>
    </template>

    <EmptyState
      v-else-if="preview.status === 'idle' && props.open"
      title="尚未提交预览"
      hint="设置样本后点击「预览映射」。"
    />
  </el-drawer>
</template>

<style scoped>
.readonly-banner {
  margin: 0 0 12px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-color-warning-dark-2, var(--d2a-status-warning));
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.warnings {
  padding: 8px 12px;
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--d2a-status-stale);
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
  list-style: none;
}

.summary-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 12px;
}

.meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.section-title {
  margin: 14px 0 8px;
  font-size: 13px;
  font-weight: 600;
}

.key-issues {
  display: flex;
  gap: 16px;
  font-size: 13px;
  margin-bottom: 8px;
}

.mono {
  font-size: 12px;
  word-break: break-all;
}

.auth-error {
  padding: 16px;
  border-left: 3px solid var(--d2a-status-failed);
  background: var(--el-fill-color-light);
}

.auth-error__hint {
  margin: 6px 0 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
