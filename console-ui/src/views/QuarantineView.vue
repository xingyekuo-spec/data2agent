<script setup lang="ts">
// 隔离区(M5-T08):分组摘要、对象筛选、记录表格、详情抽屉(按需加载 raw)、重试确认。
// 无自动轮询;显式刷新。raw 数据仅在抽屉打开时请求,关闭/切换即从内存清除。
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessageBox } from 'element-plus'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { useQuarantineStore } from '@/stores/quarantine'
import { formatDateTime } from '@/utils/time'

const store = useQuarantineStore()
const {
  groups,
  groupsRefreshError,
  selectedGroup,
  records,
  recordsTotal,
  recordsRefreshError,
  detail,
  detailId,
  detailRefreshError,
  retryResult,
  retryError,
  summary,
} = storeToRefs(store)
const { page } = store

const drawerOpen = computed(() => detail.value !== null)

const retryDialogVisible = computed({
  get: () => retryResult.value !== null,
  set: (val: boolean) => {
    if (!val) store.clearRetry()
  },
})

// ---- tag mappings ----

const rateStateType: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
  ok: 'success',
  warning: 'warning',
  tripped: 'danger',
  unknown: 'info',
}

const rateStateLabel: Record<string, string> = {
  ok: '正常',
  warning: '警告',
  tripped: '熔断',
  unknown: '未知',
}

const servingStateType: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
  fresh: 'success',
  stale: 'warning',
  not_materialized: 'info',
  unavailable: 'danger',
  unknown: 'info',
}

const servingStateLabel: Record<string, string> = {
  fresh: '新鲜',
  stale: '旧版本',
  not_materialized: '未物化',
  unavailable: '不可用',
  unknown: '未知',
}

// ---- computed ----

const selectedGroupName = computed(() => {
  if (!selectedGroup.value || groups.value.status !== 'success') return ''
  const g = groups.value.data.find(
    (x) => x.source === selectedGroup.value!.source && x.object === selectedGroup.value!.object,
  )
  return g?.display_name ?? selectedGroup.value.object
})

const isDetailAuthError = computed(() => {
  if (detail.value?.status !== 'error') return false
  const e = detail.value.error
  return e.kind === 'http' && (e.status === 401 || e.status === 403)
})

// ---- retry gating (backend decides; frontend only reads retry_allowed/retry_disabled_reason) ----

// ---- formatters ----

function formatKeys(keys: Record<string, unknown> | null | undefined): string {
  if (!keys) return '-'
  return Object.entries(keys)
    .map(([k, v]) => `${k}=${v}`)
    .join(', ')
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return '-'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`
  return `${Math.round(seconds / 86400)}d`
}

function formatRate(rate: number | null | undefined): string {
  if (rate == null) return '-'
  return `${(rate * 100).toFixed(1)}%`
}

function formatJson(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

function runLink(runId: number | null | undefined): string {
  if (runId == null) return ''
  return `/runs?run_id=${runId}`
}

// ---- group table ----

function onGroupClick(row: { source: string; object: string }): void {
  store.selectGroup({ source: row.source, object: row.object })
}

function onClearGroup(): void {
  store.selectGroup(null)
}

// ---- record table ----

function onRecordClick(row: { id: number }): void {
  void store.openDetail(row.id)
}

function onPageChange(current: number): void {
  store.setPage((current - 1) * page.limit, page.limit)
}

// ---- detail drawer ----

function closeDrawer(): void {
  store.closeDetail()
}

// ---- retry ----

async function onRetry(row: {
  source: string
  object: string
  display_name?: string | null
  pending: number
}): Promise<void> {
  store.clearRetry()
  try {
    await ElMessageBox.confirm(
      `会重新映射整个对象 ${row.display_name ?? row.object},当前有 ${row.pending} 条未处理隔离。确认继续?`,
      '确认重新映射',
      {
        confirmButtonText: '确认',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return // user cancelled
  }
  await store.retryObject(row.source, row.object)
}

// ---- lifecycle ----

onMounted(() => {
  void store.fetchGroups()
  void store.fetchRecords()
})
</script>

<template>
  <section class="quarantine-page">
    <!-- summary bar -->
    <div v-if="summary" class="d2a-card summary-bar" data-testid="quarantine-summary">
      <div class="summary-item">
        <span class="summary-item__label">未处理隔离</span>
        <span class="summary-item__value" data-testid="summary-pending">{{ summary.totalPending }}</span>
      </div>
      <div class="summary-item">
        <span class="summary-item__label">受影响对象</span>
        <span class="summary-item__value" data-testid="summary-affected">{{ summary.affectedObjects }}</span>
      </div>
      <div class="summary-item">
        <span class="summary-item__label">熔断</span>
        <span
          class="summary-item__value"
          :class="{ 'summary-item__value--danger': summary.overThreshold > 0 }"
          data-testid="summary-tripped"
        >{{ summary.overThreshold }}</span>
      </div>
      <div class="summary-item">
        <span class="summary-item__label">最新隔离</span>
        <span class="summary-item__value summary-item__value--time" data-testid="summary-latest">
          {{ summary.latestTime ? formatDateTime(summary.latestTime) : '-' }}
        </span>
      </div>
    </div>

    <!-- group table -->
    <div class="d2a-card">
      <div class="toolbar">
        <h3 class="card-title">对象分组</h3>
        <el-button size="small" data-testid="quarantine-refresh" @click="store.fetchGroups(); store.fetchRecords()">
          刷新
        </el-button>
      </div>

      <!-- groups loading / error / empty -->
      <LoadingState v-if="groups.status === 'idle' || groups.status === 'loading'" />
      <ErrorState
        v-else-if="groups.status === 'error'"
        :error="groups.error"
        @retry="store.fetchGroups()"
      />
      <EmptyState v-else-if="groups.data.length === 0" title="隔离区为空" />
      <template v-else>
        <p v-if="groupsRefreshError" class="refresh-warning" data-testid="groups-refresh-error">
          刷新失败({{ groupsRefreshError.message }}),展示上一次成功数据
        </p>
        <el-table
          :data="groups.data"
          size="small"
          data-testid="quarantine-groups-table"
          highlight-current-row
          @row-click="onGroupClick"
        >
          <el-table-column label="对象" min-width="120">
            <template #default="{ row }">
              {{ row.display_name ?? row.object }}
              <span v-if="!row.display_name" class="unknown-hint">(未知)</span>
            </template>
          </el-table-column>
          <el-table-column prop="source" label="来源" width="120" />
          <el-table-column label="未处理" width="80">
            <template #default="{ row }">
              <span :class="{ 'text--danger': row.pending > 0 }">{{ row.pending }}</span>
            </template>
          </el-table-column>
          <el-table-column label="隔离率 / 阈值" width="130">
            <template #default="{ row }">
              {{ formatRate(row.quarantine_rate) }} / {{ formatRate(row.breaker_threshold) }}
            </template>
          </el-table-column>
          <el-table-column label="隔离率状态" width="110">
            <template #default="{ row }">
              <span :data-testid="`rate-state-${row.rate_state}`">
                <el-tag
                  :type="rateStateType[row.rate_state] ?? 'info'"
                  size="small"
                >
                  {{ rateStateLabel[row.rate_state] ?? row.rate_state }}
                </el-tag>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="数据状态" width="110">
            <template #default="{ row }">
              <span :data-testid="`serving-state-${row.serving_state}`">
                <el-tag
                  :type="servingStateType[row.serving_state] ?? 'info'"
                  size="small"
                >
                  {{ servingStateLabel[row.serving_state] ?? row.serving_state }}
                </el-tag>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="最近批次" width="150">
            <template #default="{ row }">{{ row.latest_batch_id ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="最近原因" min-width="180">
            <template #default="{ row }">
              <span class="reason-text">{{ row.latest_reason ?? '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-tooltip
                v-if="!row.retry_allowed"
                :content="row.retry_disabled_reason ?? '重试暂不可用'"
                placement="top"
              >
                <el-button
                  size="small"
                  text
                  type="warning"
                  disabled
                  :data-testid="`retry-${row.object}`"
                >
                  重试
                </el-button>
              </el-tooltip>
              <el-button
                v-else
                size="small"
                text
                type="warning"
                :data-testid="`retry-${row.object}`"
                @click.stop="onRetry(row)"
              >
                重试
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </div>

    <!-- record table (filtered by selected group) -->
    <div class="d2a-card">
      <div class="toolbar">
        <h3 class="card-title">
          <template v-if="selectedGroup">隔离记录:{{ selectedGroupName }}</template>
          <template v-else>所有隔离记录</template>
        </h3>
        <el-button
          v-if="selectedGroup"
          size="small"
          data-testid="clear-group-filter"
          @click="onClearGroup"
        >
          清除筛选
        </el-button>
        <span class="toolbar__total">共 {{ recordsTotal }} 条</span>
      </div>

      <LoadingState v-if="records.status === 'idle' || records.status === 'loading'" />
      <ErrorState
        v-else-if="records.status === 'error'"
        :error="records.error"
        @retry="store.fetchRecords()"
      />
      <EmptyState v-else-if="records.data.length === 0" title="没有符合条件的隔离记录" />
      <template v-else>
        <p v-if="recordsRefreshError" class="refresh-warning" data-testid="records-refresh-error">
          刷新失败({{ recordsRefreshError.message }}),展示上一次成功数据
        </p>
        <el-table
          :data="records.data"
          size="small"
          data-testid="quarantine-records-table"
          @row-click="onRecordClick"
        >
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column label="业务键" min-width="180">
            <template #default="{ row }">{{ formatKeys(row.keys ?? row.keys_json) }}</template>
          </el-table-column>
          <el-table-column label="原因" min-width="200">
            <template #default="{ row }">
              <span class="reason-text">{{ row.reason }}</span>
            </template>
          </el-table-column>
          <el-table-column label="批次" width="150">
            <template #default="{ row }">{{ row.batch_id ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="存活" width="70">
            <template #default="{ row }">{{ formatAge(row.age_seconds) }}</template>
          </el-table-column>
          <el-table-column prop="source" label="来源" width="110" />
        </el-table>
        <el-pagination
          class="pager"
          layout="prev, pager, next"
          :total="recordsTotal"
          :page-size="page.limit"
          :current-page="page.offset / page.limit + 1"
          data-testid="quarantine-pager"
          @current-change="onPageChange"
        />
      </template>
    </div>

    <!-- detail drawer -->
    <el-drawer
      :model-value="drawerOpen"
      title="隔离详情"
      size="560px"
      data-testid="quarantine-detail-drawer"
      @close="closeDrawer"
    >
      <LoadingState v-if="detail?.status === 'loading'" />
      <!-- auth error (no token / forbidden): show config guidance, no fallback -->
      <div v-else-if="isDetailAuthError" class="detail-auth-error" data-testid="detail-auth-error">
        <p>需要配置控制台 Token 才能查看隔离详情。</p>
        <p class="detail-auth-error__hint">
          请在管理页完成首次配置,或以 D2A_CONSOLE_TOKEN 启动控制台后重试。
        </p>
      </div>
      <ErrorState
        v-else-if="detail?.status === 'error'"
        :error="detail.error"
        @retry="detailId !== null && store.openDetail(detailId)"
      />
      <template v-else-if="detail?.status === 'success'">
        <p v-if="detailRefreshError" class="refresh-warning" data-testid="detail-refresh-error">
          刷新失败({{ detailRefreshError.message }}),展示上一次成功数据
        </p>

        <!-- keys -->
        <section class="detail-section" data-testid="detail-keys">
          <h4>业务键</h4>
          <dl class="detail-kv" v-if="detail.data.keys">
            <template v-for="(v, k) in detail.data.keys" :key="k">
              <dt>{{ k }}</dt>
              <dd>{{ v }}</dd>
            </template>
          </dl>
          <p v-else class="detail-none">-</p>
        </section>

        <!-- reason -->
        <section class="detail-section" data-testid="detail-reason">
          <h4>原因</h4>
          <p>{{ detail.data.reason }}</p>
        </section>

        <!-- meta -->
        <section class="detail-section" data-testid="detail-meta">
          <h4>基本信息</h4>
          <dl class="detail-kv">
            <dt>来源</dt>
            <dd>{{ detail.data.source }}</dd>
            <dt>对象</dt>
            <dd>{{ detail.data.object }}</dd>
            <dt>批次</dt>
            <dd>{{ detail.data.batch_id ?? '-' }}</dd>
            <dt>创建时间</dt>
            <dd>{{ formatDateTime(detail.data.created_at) }}</dd>
            <dt>存活</dt>
            <dd>{{ formatAge(detail.data.age_seconds) }}</dd>
            <dt>请求 ID</dt>
            <dd><code class="request-id">{{ detail.data.request_id }}</code></dd>
          </dl>
        </section>

        <!-- raw preview -->
        <section class="detail-section" data-testid="detail-raw">
          <h4>原始数据(脱敏预览)</h4>
          <pre
            v-if="detail.data.raw"
            class="raw-preview"
            data-testid="detail-raw-content"
          >{{ formatJson(detail.data.raw) }}</pre>
          <p v-else class="detail-none">无原始数据</p>
        </section>

        <!-- warnings -->
        <section v-if="detail.data.warnings?.length" class="detail-section" data-testid="detail-warnings">
          <h4>警告</h4>
          <ul class="detail-warnings-list">
            <li v-for="w in detail.data.warnings" :key="w">{{ w }}</li>
          </ul>
        </section>

        <!-- truncations -->
        <section v-if="detail.data.truncations?.length" class="detail-section" data-testid="detail-truncations">
          <h4>截断标记</h4>
          <p class="detail-trunc-note">
            {{ detail.data.truncations.length }} 行存在截断字段(预览不是完整值):
            {{ detail.data.truncations.map((t) => `#${t.row_index}(${t.fields.join('/')})`).join(', ') }}
          </p>
        </section>
      </template>
    </el-drawer>

    <!-- retry result dialog -->
    <el-dialog
      v-model="retryDialogVisible"
      title="重试结果"
      width="420px"
      data-testid="retry-result-dialog"
      @closed="store.clearRetry()"
    >
      <LoadingState v-if="retryResult?.status === 'loading'" text="正在执行重试…" />
      <template v-else-if="retryResult?.status === 'success'">
        <div class="retry-success" data-testid="retry-success">
          <p>执行成功:{{ retryResult.data.executed ? '已执行' : '已提交' }}</p>
          <dl class="detail-kv">
            <dt>对象</dt>
            <dd>{{ retryResult.data.object }}</dd>
            <dt>总计</dt>
            <dd>{{ retryResult.data.total }}</dd>
            <dt>映射</dt>
            <dd>{{ retryResult.data.mapped }}</dd>
            <dt>隔离</dt>
            <dd>{{ retryResult.data.quarantined }}</dd>
            <dt>状态</dt>
            <dd>{{ retryResult.data.status }}</dd>
          </dl>
          <p class="retry-run-link">
            <router-link :to="runLink(retryResult.data.run_id)" data-testid="retry-run-link">
              查看运行 #{{ retryResult.data.run_id }}
            </router-link>
          </p>
        </div>
      </template>
      <template v-else-if="retryResult?.status === 'error'">
        <div class="retry-error" data-testid="retry-error">
          <p class="retry-error__title">重试失败</p>
          <p class="retry-error__detail" data-testid="retry-error-detail">{{ retryError?.message ?? '未知错误' }}</p>
          <p
            v-if="retryError?.reason_code"
            class="retry-error__reason"
            data-testid="retry-error-reason"
          >
            原因码: {{ retryError.reason_code }}
          </p>
          <p v-if="retryError?.run_id" class="retry-run-link">
            <router-link :to="runLink(retryError.run_id)" data-testid="retry-error-run-link">
              查看运行 #{{ retryError.run_id }}
            </router-link>
          </p>
        </div>
      </template>
      <template #footer>
        <el-button data-testid="retry-result-close" @click="store.clearRetry()">关闭</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.quarantine-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* summary bar */
.summary-bar {
  display: flex;
  gap: 24px;
  padding: 12px 16px;
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.summary-item__label {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.summary-item__value {
  font-size: 18px;
  font-weight: 600;
}

.summary-item__value--danger {
  color: var(--d2a-status-failed);
}

.summary-item__value--time {
  font-size: 14px;
  font-weight: 400;
}

/* toolbar */
.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.card-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.toolbar__total {
  margin-left: auto;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

/* refresh warning */
.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

/* reason text */
.reason-text {
  font-size: 12px;
  word-break: break-all;
}

/* text utilities */
.text--danger {
  color: var(--d2a-status-failed);
  font-weight: 600;
}

.unknown-hint {
  font-size: 11px;
  color: var(--d2a-status-unknown);
}

/* pager */
.pager {
  margin-top: 10px;
  justify-content: flex-end;
}

/* detail drawer sections */
.detail-section {
  margin-bottom: 16px;
}

.detail-section h4 {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.detail-section p {
  margin: 0;
  font-size: 13px;
  word-break: break-all;
}

.detail-kv {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 4px 12px;
  margin: 0;
}

.detail-kv dt {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.detail-kv dd {
  margin: 0;
  font-size: 13px;
  word-break: break-all;
}

.detail-none {
  color: var(--d2a-text-secondary);
}

.request-id {
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

/* raw preview */
.raw-preview {
  max-height: 280px;
  margin: 0;
  padding: 10px;
  overflow: auto;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}

/* auth error */
.detail-auth-error {
  padding: 16px;
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
}

.detail-auth-error p {
  margin: 0;
  font-size: 13px;
  color: var(--d2a-text-secondary);
}

.detail-auth-error__hint {
  margin-top: 8px !important;
  font-size: 12px !important;
}

/* warnings */
.detail-warnings-list {
  margin: 0;
  padding-left: 16px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.detail-warnings-list li {
  margin-bottom: 2px;
}

/* truncations */
.detail-trunc-note {
  font-size: 12px;
  color: var(--d2a-status-stale);
}

/* retry result */
.retry-success p {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}

.retry-run-link {
  margin-top: 12px !important;
  font-weight: 400 !important;
}

.retry-run-link a {
  color: var(--el-color-primary);
  text-decoration: none;
}

.retry-error__title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--d2a-status-failed);
}

.retry-error__detail {
  margin: 0;
  font-size: 13px;
  color: var(--d2a-text-secondary);
  word-break: break-all;
}
</style>
