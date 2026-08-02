<script setup lang="ts">
// 运行记录(M4):type/status 筛选、服务端分页、详情抽屉、step 与水位证据、
// 安全 JSON 视图。无动作按钮(只读);无自动轮询。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useRunsStore } from '@/stores/runs'
import type { HealthStatus } from '@/types/state'
import { formatDateTime } from '@/utils/time'

const store = useRunsStore()
// filters/page 是 reactive 对象直接取用;其余经 storeToRefs
const { list, total, refreshError, detail, detailRefreshError } = storeToRefs(store)
const { filters, page } = store
const route = useRoute()
const router = useRouter()

const TYPE_OPTIONS = ['sync', 'apply', 'reconcile', 'ingest', 'validation', 'publish', 'rollback'] as const
const STATUS_OPTIONS = ['running', 'ok', 'paused', 'failed', 'aborted'] as const

const runStatusMap: Record<string, HealthStatus> = {
  ok: 'healthy',
  failed: 'failed',
  aborted: 'failed',
  running: 'running',
  paused: 'warning',
}

const drawerOpen = computed(() => detail.value !== null)
const showJson = ref(false)

function runTypeLabel(t: string | null | undefined): string {
  return t ?? '类型未知'
}

function syncQuery(): void {
  void router.replace({
    query: {
      ...(filters.type ? { type: filters.type } : {}),
      ...(filters.status ? { status: filters.status } : {}),
      ...(page.offset > 0 ? { page: String(page.offset / page.limit + 1) } : {}),
      ...(store.detailId !== null ? { run_id: String(store.detailId) } : {}),
    },
  })
}

function firstString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function routePageOffset(query: LocationQuery): number {
  const pageNo = Number(firstString(query.page))
  return Number.isFinite(pageNo) && pageNo > 1 ? (pageNo - 1) * page.limit : 0
}

function applyRouteQuery(query: LocationQuery, refresh: boolean): void {
  const nextType = firstString(query.type)
  const nextStatus = firstString(query.status)
  const type = (TYPE_OPTIONS as readonly string[]).includes(nextType)
    ? nextType as (typeof TYPE_OPTIONS)[number]
    : ''
  const status = (STATUS_OPTIONS as readonly string[]).includes(nextStatus)
    ? nextStatus as (typeof STATUS_OPTIONS)[number]
    : ''
  const offset = routePageOffset(query)
  const changed = filters.type !== type || filters.status !== status || page.offset !== offset
  filters.type = type
  filters.status = status
  page.offset = offset
  if (refresh && changed) {
    void store.refresh()
  }

  const runId = Number(firstString(query.run_id))
  if (Number.isFinite(runId) && runId > 0 && store.detailId !== runId) {
    void store.openDetail(runId)
  } else if (!firstString(query.run_id) && store.detailId !== null) {
    store.closeDetail()
  }
}

function onFilterChange(): void {
  page.offset = 0
  void store.refresh()
  syncQuery()
}

function onPageChange(current: number): void {
  store.setPage((current - 1) * page.limit, page.limit)
  syncQuery()
}

function openRow(row: { id: number }): void {
  showJson.value = false
  void store.openDetail(row.id)
  syncQuery()
}

function closeDrawer(): void {
  store.closeDetail()
  syncQuery()
}

onMounted(() => {
  // route query 恢复筛选与详情深链
  applyRouteQuery(route.query, false)
  void store.refresh()
})

watch(
  () => route.query,
  (query) => applyRouteQuery(query, true),
)
</script>

<template>
  <section class="runs-page">
    <div class="d2a-card toolbar">
      <el-select
        v-model="filters.type"
        placeholder="类型"
        clearable
        size="small"
        data-testid="filter-type"
        @change="onFilterChange"
      >
        <el-option
          v-for="t in TYPE_OPTIONS"
          :key="t"
          :label="t"
          :value="t"
        />
      </el-select>
      <el-select
        v-model="filters.status"
        placeholder="状态"
        clearable
        size="small"
        data-testid="filter-status"
        @change="onFilterChange"
      >
        <el-option
          v-for="s in STATUS_OPTIONS"
          :key="s"
          :label="s"
          :value="s"
        />
      </el-select>
      <el-button
        size="small"
        data-testid="refresh-button"
        @click="store.refresh()"
      >
        刷新
      </el-button>
      <span class="toolbar__total">共 {{ total }} 条</span>
    </div>

    <div class="d2a-card">
      <LoadingState v-if="list.status === 'idle' || list.status === 'loading'" />
      <ErrorState
        v-else-if="list.status === 'error'"
        :error="list.error"
        @retry="store.refresh()"
      />
      <EmptyState
        v-else-if="list.data.length === 0"
        title="没有符合条件的运行"
      />
      <template v-else>
        <p
          v-if="refreshError"
          class="refresh-warning"
          data-testid="runs-refresh-error"
        >
          刷新失败({{ refreshError.message }}),展示上一次成功数据
        </p>
        <el-table
          :data="list.data"
          size="small"
          data-testid="runs-table"
          @row-click="openRow"
        >
          <el-table-column
            label="ID"
            width="70"
            prop="id"
          />
          <el-table-column
            label="类型"
            width="100"
          >
            <template #default="{ row }">
              {{ runTypeLabel(row.type) }}
            </template>
          </el-table-column>
          <el-table-column
            prop="source"
            label="来源"
            width="130"
          />
          <el-table-column
            label="状态"
            width="90"
          >
            <template #default="{ row }">
              <StatusBadge :status="runStatusMap[row.status ?? ''] ?? 'unknown'" />
            </template>
          </el-table-column>
          <el-table-column
            label="开始"
            width="140"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.started_at) }}
            </template>
          </el-table-column>
          <el-table-column
            label="结束"
            width="140"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.finished_at) }}
            </template>
          </el-table-column>
          <el-table-column
            label="耗时"
            width="90"
          >
            <template #default="{ row }">
              {{ row.duration_ms == null ? '—' : `${Math.round(row.duration_ms)}ms` }}
            </template>
          </el-table-column>
          <el-table-column
            label="表/对象"
            width="80"
          >
            <template #default="{ row }">
              {{ row.tables ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="行数"
            width="90"
          >
            <template #default="{ row }">
              {{ row.rows ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column label="隔离">
            <template #default="{ row }">
              {{ row.quarantined ?? '—' }}
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          class="pager"
          layout="prev, pager, next"
          :total="total"
          :page-size="page.limit"
          :current-page="page.offset / page.limit + 1"
          data-testid="runs-pager"
          @current-change="onPageChange"
        />
      </template>
    </div>

    <el-drawer
      :model-value="drawerOpen"
      title="运行详情"
      size="560px"
      data-testid="run-detail-drawer"
      @close="closeDrawer"
    >
      <LoadingState v-if="detail?.status === 'loading'" />
      <ErrorState
        v-else-if="detail?.status === 'error'"
        :error="detail.error"
        @retry="store.detailId !== null && store.openDetail(store.detailId)"
      />
      <template v-else-if="detail?.status === 'success'">
        <p
          v-if="detailRefreshError"
          class="refresh-warning"
          data-testid="run-detail-refresh-error"
        >
          刷新失败({{ detailRefreshError.message }}),展示上一次成功数据
        </p>
        <dl class="summary">
          <dt>类型</dt>
          <dd>{{ runTypeLabel(detail.data.type) }}</dd>
          <dt>状态</dt>
          <dd><StatusBadge :status="runStatusMap[detail.data.status ?? ''] ?? 'unknown'" /></dd>
          <dt>来源</dt>
          <dd>{{ detail.data.source }}</dd>
          <dt>开始 / 结束</dt>
          <dd>{{ formatDateTime(detail.data.started_at) }} → {{ formatDateTime(detail.data.finished_at) }}</dd>
          <dt>耗时</dt>
          <dd>{{ detail.data.duration_ms == null ? '—' : `${Math.round(detail.data.duration_ms)} ms` }}</dd>
          <dt>错误</dt>
          <dd>{{ detail.data.error ?? '—' }}</dd>
        </dl>

        <h4>步骤</h4>
        <p
          v-if="detail.data.steps_state === 'legacy_unavailable'"
          class="legacy-note"
          data-testid="legacy-note"
        >
          历史记录没有逐步证据(该运行产生于 step 记录引入之前)。
        </p>
        <EmptyState
          v-else-if="detail.data.steps.length === 0"
          title="该运行没有工作单元"
        />
        <el-table
          v-else
          :data="detail.data.steps"
          size="small"
          data-testid="steps-table"
        >
          <el-table-column
            prop="ordinal"
            label="#"
            width="40"
          />
          <el-table-column
            prop="kind"
            label="kind"
            width="70"
          />
          <el-table-column
            prop="name"
            label="目标"
            min-width="130"
          />
          <el-table-column
            label="状态"
            width="80"
          >
            <template #default="{ row }">
              <StatusBadge :status="runStatusMap[row.status] ?? 'unknown'" />
            </template>
          </el-table-column>
          <el-table-column
            label="in/out"
            width="90"
          >
            <template #default="{ row }">
              {{ row.rows_in ?? '—' }}/{{ row.rows_out ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="隔离"
            width="60"
          >
            <template #default="{ row }">
              {{ row.quarantined ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="水位 before → after"
            min-width="160"
          >
            <template #default="{ row }">
              {{ row.watermark_before ?? '—' }} → {{ row.watermark_after ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="错误"
            min-width="120"
          >
            <template #default="{ row }">
              {{ row.error ?? '—' }}
            </template>
          </el-table-column>
        </el-table>

        <el-button
          class="json-toggle"
          size="small"
          text
          data-testid="json-toggle"
          @click="showJson = !showJson"
        >
          {{ showJson ? '隐藏' : '查看' }}安全 JSON
        </el-button>
        <pre
          v-if="showJson"
          class="json-view"
          data-testid="json-view"
        >{{ JSON.stringify(detail.data, null, 2) }}</pre>
      </template>
    </el-drawer>
  </section>
</template>

<style scoped>
.runs-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}

.toolbar__total {
  margin-left: auto;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.pager {
  margin-top: 10px;
  justify-content: flex-end;
}

.summary {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: 6px 12px;
  margin: 0 0 12px;
}

.summary dt {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.summary dd {
  margin: 0;
  font-size: 13px;
}

.legacy-note {
  padding: 10px 12px;
  border-left: 3px solid var(--d2a-status-unknown);
  background: var(--el-fill-color-light);
  font-size: 13px;
  color: var(--d2a-text-secondary);
}

.json-toggle {
  margin-top: 12px;
}

.json-view {
  max-height: 320px;
  padding: 10px;
  overflow: auto;
  font-size: 11px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
