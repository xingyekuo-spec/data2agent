<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { Refresh, Search } from '@element-plus/icons-vue'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useDeadStockValidationStore } from '@/stores/deadStockValidation'
import { formatDateTime } from '@/utils/time'

const store = useDeadStockValidationStore()
const {
  overview,
  deadItems,
  attributionDistribution,
  consumableMetric,
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
  publishState,
} = storeToRefs(store)

onMounted(() => {
  if (deadItems.value.status === 'idle') {
    void store.refresh()
  }
})

const ov = computed(() => overview.value.status === 'success' ? overview.value.data : null)
const summary = computed(() => ov.value?.summary ?? null)
const deadStockObjects = [
  'DeadStockItem',
  'DeadStockAttribution',
  'PurchaseOverbuyEvidence',
  'ProductionLossEvidence',
  'MaterialOrderEvidence',
  'EcnChangeEvidence',
  'SpecialConditionEvidence',
  'DuplicateMaterialCandidate',
  'MaterialBomUsage',
  'MaterialSubstituteCandidate',
]
const objectStatusRows = computed(() => {
  const rows = ov.value?.objects ?? []
  return deadStockObjects.map((name) => rows.find((row) => row.object === name) ?? {
    object: name,
    rows: null,
    status: 'unknown',
    mapped_at: null,
  })
})

function textOf(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function numeric(value: unknown): string {
  if (typeof value === 'number') return value.toLocaleString('zh-CN')
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : '—'
}

function itemRowClass({ row }: { row: Record<string, unknown> }) {
  return row.item_code === selectedItemCode.value ? 'is-selected-row' : ''
}
</script>

<template>
  <section class="dead-stock-page">
    <header class="page-head">
      <div>
        <h1>呆滞库存验证</h1>
        <p>面向测试 ERP 验证的只读工作台，集中查看 M1/M2/M3 对象、归因分布和证据链。</p>
      </div>
      <el-button
        :icon="Refresh"
        :loading="loading"
        data-testid="dead-stock-refresh"
        @click="store.refresh()"
      >
        刷新
      </el-button>
    </header>

    <LoadingState v-if="deadItems.status === 'idle' || deadItems.status === 'loading'" />
    <div
      v-else-if="notPublished && !deadItemRows.length"
      class="not-published-state"
      data-testid="dead-stock-not-published"
    >
      <div>
        <h2>还没有已发布数据集</h2>
        <p>需要先完成一次 ERP 同步，并把对象层构建为 published dataset，之后才能查询呆滞库存对象和 MCP 指标。</p>
      </div>
      <div class="not-published-actions">
        <el-button
          type="primary"
          :loading="publishState?.status === 'loading'"
          data-testid="dead-stock-build-publish"
          @click="store.buildAndPublish()"
        >
          构建并发布 digiwin_e10
        </el-button>
        <el-button
          :icon="Refresh"
          data-testid="dead-stock-retry-after-not-published"
          @click="store.refresh()"
        >
          重新检查
        </el-button>
      </div>
      <ErrorState
        v-if="publishState?.status === 'error'"
        :error="publishState.error"
        @retry="store.buildAndPublish()"
      />
    </div>
    <ErrorState
      v-else-if="error && !deadItemRows.length"
      :error="error"
      @retry="store.refresh()"
    />
    <template v-else>
      <div
        v-if="error"
        class="refresh-warning"
        data-testid="dead-stock-refresh-error"
      >
        刷新失败({{ error.message }}),展示上一次成功数据
      </div>

      <section
        class="status-band"
        data-testid="dead-stock-status"
      >
        <div class="status-item">
          <span class="label">数据集版本</span>
          <strong>{{ ov?.versions.dataset || '—' }}</strong>
        </div>
        <div class="status-item">
          <span class="label">模板版本</span>
          <strong>{{ ov?.versions.template || '—' }}</strong>
        </div>
        <div class="status-item">
          <span class="label">对象覆盖</span>
          <strong>{{ summary?.materialized_objects ?? '—' }}/{{ summary?.template_objects ?? '—' }}</strong>
        </div>
        <div class="status-item">
          <span class="label">数据更新时间</span>
          <strong>{{ formatDateTime(summary?.data_updated_at) || '—' }}</strong>
        </div>
        <div class="status-item">
          <span class="label">状态</span>
          <StatusBadge :status="summary?.materialized_objects ? 'healthy' : 'unknown'" />
        </div>
      </section>

      <section class="filter-band">
        <el-input
          v-model="filters.plant_id"
          placeholder="工厂 plant_id"
          clearable
          data-testid="dead-stock-filter-plant"
        />
        <el-input
          v-model="filters.item_code"
          placeholder="品号 item_code"
          clearable
          data-testid="dead-stock-filter-item"
        />
        <el-select
          v-model="filters.root_cause"
          placeholder="根因"
          clearable
          data-testid="dead-stock-filter-root"
        >
          <el-option
            v-for="cause in ['R1', 'R2', 'R2M', 'R3', 'R4', 'R5', 'R6']"
            :key="cause"
            :label="cause"
            :value="cause"
          />
        </el-select>
        <el-select
          v-model="filters.confidence_level"
          placeholder="置信度"
          clearable
          data-testid="dead-stock-filter-confidence"
        >
          <el-option label="HIGH" value="HIGH" />
          <el-option label="MEDIUM" value="MEDIUM" />
          <el-option label="LOW" value="LOW" />
        </el-select>
        <el-button
          type="primary"
          :icon="Search"
          data-testid="dead-stock-apply-filters"
          @click="store.applyFilters()"
        >
          查询
        </el-button>
      </section>

      <section class="summary-grid">
        <div class="metric-panel">
          <div class="panel-head">
            <h2>归因分布</h2>
            <span>{{ attributionDistribution.status === 'success' ? attributionRows.length : 0 }} 组</span>
          </div>
          <EmptyState
            v-if="attributionDistribution.status === 'success' && attributionRows.length === 0"
            title="暂无归因分布"
          />
          <div
            v-for="row in attributionRows"
            v-else
            :key="String(row.group)"
            class="metric-row"
          >
            <span>{{ row.group }}</span>
            <strong>{{ numeric(row.value) }}</strong>
          </div>
        </div>

        <div class="metric-panel">
          <div class="panel-head">
            <h2>潜在可消耗数量</h2>
            <span>{{ consumableMetric.status === 'success' ? consumableRows.length : 0 }} 组</span>
          </div>
          <EmptyState
            v-if="consumableMetric.status === 'success' && consumableRows.length === 0"
            title="暂无可消耗候选"
          />
          <div
            v-for="row in consumableRows"
            v-else
            :key="String(row.group)"
            class="metric-row"
          >
            <span>{{ row.group }}</span>
            <strong>{{ numeric(row.value) }}</strong>
          </div>
        </div>

        <div class="metric-panel">
          <div class="panel-head">
            <h2>M3 对象状态</h2>
            <span>{{ objectStatusRows.length }} 个</span>
          </div>
          <div
            v-for="row in objectStatusRows"
            :key="row.object"
            class="object-row"
          >
            <span>{{ row.object }}</span>
            <strong>{{ row.rows ?? '—' }}</strong>
          </div>
        </div>
      </section>

      <section class="workbench">
        <main class="list-pane">
          <div class="panel-head">
            <h2>呆滞库存</h2>
            <span>{{ deadItemRows.length }} 条</span>
          </div>
          <el-table
            v-if="deadItemRows.length"
            :data="deadItemRows"
            :row-class-name="itemRowClass"
            height="520"
            data-testid="dead-stock-items"
            @row-click="(row: Record<string, unknown>) => store.selectItem(String(row.item_code ?? ''))"
          >
            <el-table-column prop="item_code" label="品号" min-width="120" />
            <el-table-column prop="plant_id" label="工厂" width="90" />
            <el-table-column prop="warehouse_code" label="仓库" width="90" />
            <el-table-column prop="inventory_qty" label="库存" width="110">
              <template #default="{ row }">{{ numeric(row.inventory_qty) }}</template>
            </el-table-column>
            <el-table-column prop="dead_stock_amount" label="金额" width="120">
              <template #default="{ row }">{{ numeric(row.dead_stock_amount) }}</template>
            </el-table-column>
            <el-table-column prop="dead_stock_days" label="天数" width="90" />
          </el-table>
          <EmptyState
            v-else
            title="暂无呆滞库存"
            hint="同步并发布数据集后显示"
          />
        </main>

        <aside class="evidence-pane">
          <div class="panel-head">
            <h2>证据链</h2>
            <span>{{ selectedItemCode || '未选择品号' }}</span>
          </div>
          <EmptyState
            v-if="!selectedItemCode"
            title="请选择一个呆滞品号"
          />
          <el-collapse
            v-else
            model-value="DeadStockAttribution"
            data-testid="dead-stock-evidence"
          >
            <el-collapse-item
              v-for="section in evidenceSections"
              :key="section.object"
              :name="section.object"
            >
              <template #title>
                <span class="collapse-title">{{ section.title }}</span>
                <span
                  v-if="section.state.status === 'success'"
                  class="collapse-count"
                >{{ (section.state.data.rows as unknown[] | undefined)?.length ?? 0 }}</span>
              </template>
              <LoadingState v-if="section.state.status === 'loading'" />
              <ErrorState
                v-else-if="section.state.status === 'error'"
                :error="section.state.error"
              />
              <EmptyState
                v-else-if="section.state.status === 'success' && ((section.state.data.rows as unknown[] | undefined)?.length ?? 0) === 0"
                title="无匹配记录"
              />
              <div
                v-else-if="section.state.status === 'success'"
                class="evidence-list"
              >
                <div
                  v-for="(row, idx) in section.state.data.rows as Record<string, unknown>[]"
                  :key="idx"
                  class="evidence-item"
                >
                  <dl>
                    <template
                      v-for="(value, key) in row"
                      :key="key"
                    >
                      <dt>{{ key }}</dt>
                      <dd>{{ textOf(value) }}</dd>
                    </template>
                  </dl>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </aside>
      </section>

      <section
        v-if="substituteRows.length || warnings.length"
        class="bottom-grid"
      >
        <div class="metric-panel">
          <div class="panel-head">
            <h2>转用候选</h2>
            <span>{{ substituteRows.length }} 条</span>
          </div>
          <div
            v-for="row in substituteRows"
            :key="`${row.item_code}-${row.candidate_parent_item_code}`"
            class="candidate-row"
          >
            <strong>{{ row.item_code }}</strong>
            <span>{{ row.candidate_parent_item_code }}</span>
            <em>{{ numeric(row.potential_consume_qty) }}</em>
          </div>
        </div>
        <div class="metric-panel">
          <div class="panel-head">
            <h2>查询警示</h2>
            <span>{{ warnings.length }} 条</span>
          </div>
          <div
            v-for="warning in warnings"
            :key="warning"
            class="warning-row"
          >
            {{ warning }}
          </div>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.dead-stock-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head,
.status-band,
.filter-band,
.summary-grid,
.workbench,
.bottom-grid {
  width: 100%;
}

.page-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}

.page-head h1,
.panel-head h2 {
  margin: 0;
}

.page-head p {
  margin: 6px 0 0;
  color: var(--d2a-text-secondary);
}

.refresh-warning {
  padding: 10px 12px;
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  border-radius: 6px;
}

.not-published-state {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 24px;
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
  background: var(--d2a-surface);
}

.not-published-state h2 {
  margin: 0;
}

.not-published-state p {
  margin: 8px 0 0;
  color: var(--d2a-text-secondary);
}

.not-published-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.status-band,
.filter-band {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  align-items: center;
}

.status-item {
  min-height: 72px;
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
  padding: 12px;
  background: var(--d2a-surface);
}

.label {
  display: block;
  margin-bottom: 6px;
  color: var(--d2a-text-secondary);
  font-size: 12px;
}

.summary-grid,
.bottom-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.bottom-grid {
  grid-template-columns: 1fr 1fr;
}

.metric-panel,
.list-pane,
.evidence-pane {
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
  background: var(--d2a-surface);
  min-width: 0;
}

.metric-panel {
  padding: 14px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-head span {
  color: var(--d2a-text-secondary);
}

.metric-row,
.object-row,
.candidate-row,
.warning-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-top: 1px solid var(--d2a-border);
}

.candidate-row em {
  font-style: normal;
  color: var(--el-color-primary);
}

.warning-row {
  display: block;
  color: var(--el-color-warning-dark-2);
}

.workbench {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(360px, 0.8fr);
  gap: 12px;
}

.list-pane,
.evidence-pane {
  padding: 14px;
}

:deep(.is-selected-row) {
  --el-table-tr-bg-color: var(--el-color-primary-light-9);
}

.collapse-title {
  font-weight: 600;
}

.collapse-count {
  margin-left: 8px;
  color: var(--d2a-text-secondary);
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.evidence-item {
  border: 1px solid var(--d2a-border);
  border-radius: 6px;
  padding: 10px;
}

.evidence-item dl {
  display: grid;
  grid-template-columns: minmax(110px, 0.35fr) minmax(0, 0.65fr);
  gap: 6px 12px;
  margin: 0;
}

.evidence-item dt {
  color: var(--d2a-text-secondary);
}

.evidence-item dd {
  margin: 0;
  overflow-wrap: anywhere;
}

@media (max-width: 1100px) {
  .status-band,
  .filter-band,
  .summary-grid,
  .bottom-grid,
  .workbench {
    grid-template-columns: 1fr;
  }
}
</style>
