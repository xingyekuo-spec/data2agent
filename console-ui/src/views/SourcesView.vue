<script setup lang="ts">
// 数据源管理:平台已接入源的登记式清单 + 详情抽屉 + 添加引导。
// 边界:连接配置与凭据在中间机(安全拓扑),本页只读观测,不提供编辑。
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { Refresh, Plus } from '@element-plus/icons-vue'
import { ref } from 'vue'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useSourcesStore, type SourceCard } from '@/stores/sources'
import { formatDateTime } from '@/utils/time'
import { formatDuration } from '@/utils/format'
import type { HealthStatus } from '@/types/state'

const store = useSourcesStore()
const { cards, detail, detailSource } = storeToRefs(store)

const addDialogVisible = ref(false)

/** 已支持/规划中的数据源类型(添加引导用;只有 ERP 可点) */
const sourceTypes = [
  {
    type: 'erp',
    name: 'ERP',
    examples: '鼎捷 E10 / 易飞',
    supported: true,
    hint: '在中间机管理页(8851)配置连接并选表;凭据只存中间机,不上平台。',
  },
  { type: 'mes', name: 'MES', examples: '生产执行系统', supported: false, hint: '规划中' },
  { type: 'srm', name: 'SRM', examples: '供应商协同', supported: false, hint: '规划中' },
  { type: 'excel', name: 'Excel', examples: '表格导入', supported: false, hint: '规划中' },
  { type: 'api', name: 'API', examples: '轮询适配器', supported: false, hint: '规划中' },
]

const ACCESS_MODE_LABELS: Record<SourceCard['access_mode'], string> = {
  push: '中间机推送',
  local: '本地直连',
  unknown: '未知',
}

const SOURCE_TYPE_LABELS: Record<SourceCard['source_type'], string> = {
  erp: 'ERP',
  unknown: '未登记',
}

function statusOf(card: SourceCard): HealthStatus {
  return card.status as HealthStatus
}

onMounted(() => {
  void store.refresh()
})
</script>

<template>
  <section>
    <div class="d2a-card">
      <header class="page-header">
        <div>
          <h3 class="card-title card-title--compact">
            数据源管理
          </h3>
          <p class="page-desc">
            平台已接入的数据源。连接与凭据在中间机维护,平台只读观测;
            新增源请在中间机管理页完成配置后自动出现在此清单。
          </p>
        </div>
        <div class="page-actions">
          <el-button
            :icon="Plus"
            data-testid="source-add"
            @click="addDialogVisible = true"
          >
            添加数据源
          </el-button>
          <el-button
            :icon="Refresh"
            data-testid="sources-refresh"
            @click="store.refresh()"
          >
            刷新
          </el-button>
        </div>
      </header>

      <LoadingState v-if="cards.status === 'idle' || cards.status === 'loading'" />
      <ErrorState
        v-else-if="cards.status === 'error'"
        :error="cards.error"
        @retry="store.refresh()"
      />
      <EmptyState
        v-else-if="cards.data.length === 0"
        title="尚未接入数据源"
        hint="在中间机管理页配置连接并选择要同步的表后,源会出现在这里"
        data-testid="sources-empty"
      />
      <div
        v-else
        class="source-grid"
        data-testid="source-grid"
      >
        <button
          v-for="card in cards.data"
          :key="card.source"
          type="button"
          class="source-card"
          :data-testid="`source-card-${card.source}`"
          @click="store.openDetail(card.source)"
        >
          <div class="source-card__head">
            <span class="source-card__name">{{ card.display_name }}</span>
            <StatusBadge :status="statusOf(card)" />
          </div>
          <div class="source-card__tags">
            <el-tag size="small">
              {{ SOURCE_TYPE_LABELS[card.source_type] }}
            </el-tag>
            <el-tag
              size="small"
              :type="card.access_mode === 'push' ? 'success' : 'info'"
            >
              {{ ACCESS_MODE_LABELS[card.access_mode] }}
            </el-tag>
            <el-tag
              v-if="card.quarantined > 0"
              size="small"
              type="warning"
            >
              待确认 {{ card.quarantined }}
            </el-tag>
          </div>
          <div class="source-card__meta">
            <span>{{ card.tables }} 张表</span>
            <span v-if="card.last_run_at">最近接入 {{ formatDateTime(card.last_run_at) }}</span>
            <span v-else>从未接入</span>
          </div>
          <p
            v-if="card.status_reason"
            class="source-card__reason"
          >
            {{ card.status_reason }}
          </p>
        </button>
      </div>
    </div>

    <!-- 详情抽屉:表级水位 + 最近运行 -->
    <el-drawer
      :model-value="detail !== null"
      :title="detailSource ? `数据源详情:${detailSource}` : '数据源详情'"
      size="640px"
      data-testid="source-detail-drawer"
      @close="store.closeDetail()"
    >
      <LoadingState v-if="detail?.status === 'loading'" />
      <ErrorState
        v-else-if="detail?.status === 'error'"
        :error="detail.error"
        @retry="detailSource && store.openDetail(detailSource)"
      />
      <template v-else-if="detail?.status === 'success'">
        <h4 class="drawer-section-title">
          接入表({{ detail.data.table_states.length }})
        </h4>
        <el-table
          :data="detail.data.table_states"
          size="small"
          data-testid="source-tables"
        >
          <el-table-column
            prop="table_name"
            label="表名"
            min-width="160"
          />
          <el-table-column
            prop="watermark_col"
            label="水位"
            min-width="150"
          >
            <template #default="{ row }">
              {{ row.high_water ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="行数"
            width="90"
            align="right"
          >
            <template #default="{ row }">
              {{ row.rows ?? '不可检测' }}
            </template>
          </el-table-column>
          <el-table-column
            label="最近接入"
            min-width="130"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.last_run_at) || '—' }}
            </template>
          </el-table-column>
        </el-table>

        <h4 class="drawer-section-title">
          最近接入记录
        </h4>
        <el-table
          :data="detail.data.recent_runs"
          size="small"
          data-testid="source-runs"
        >
          <el-table-column
            prop="id"
            label="#"
            width="60"
          />
          <el-table-column
            label="状态"
            width="90"
          >
            <template #default="{ row }">
              <StatusBadge :status="row.status ?? 'unknown'" />
            </template>
          </el-table-column>
          <el-table-column
            label="行数"
            width="90"
            align="right"
          >
            <template #default="{ row }">
              {{ row.rows ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="耗时"
            width="90"
            align="right"
          >
            <template #default="{ row }">
              {{ row.duration_ms != null ? formatDuration(Math.round(row.duration_ms / 1000)) : '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="开始时间"
            min-width="130"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.started_at) }}
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-drawer>

    <!-- 添加数据源引导 -->
    <el-dialog
      v-model="addDialogVisible"
      title="添加数据源"
      width="520px"
      data-testid="source-add-dialog"
    >
      <p class="page-desc">
        选择要接入的系统类型。连接配置在中间机完成(凭据不出中间机),
        配好后源会自动出现在清单里。
      </p>
      <div class="type-list">
        <div
          v-for="t in sourceTypes"
          :key="t.type"
          class="type-item"
          :class="{ 'type-item--disabled': !t.supported }"
          :data-testid="`source-type-${t.type}`"
        >
          <div class="type-item__head">
            <span class="type-item__name">{{ t.name }}</span>
            <span class="type-item__examples">{{ t.examples }}</span>
            <el-tag
              size="small"
              :type="t.supported ? 'success' : 'info'"
            >
              {{ t.supported ? '已支持' : '规划中' }}
            </el-tag>
          </div>
          <p class="type-item__hint">
            {{ t.hint }}
          </p>
        </div>
      </div>
    </el-dialog>
  </section>
</template>

<style scoped>
.page-header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.page-desc {
  margin: 4px 0 0;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

.source-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
}

.source-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px 16px;
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
  background: #fff;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.source-card:hover {
  border-color: var(--d2a-primary);
  box-shadow: 0 2px 8px rgb(22 119 255 / 12%);
}

.source-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.source-card__name {
  font-size: var(--d2a-font-lg);
  font-weight: 600;
  color: var(--d2a-text-primary);
}

.source-card__tags {
  display: flex;
  gap: 6px;
}

.source-card__meta {
  display: flex;
  gap: 14px;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

.source-card__reason {
  margin: 0;
  font-size: var(--d2a-font-xs);
  color: var(--d2a-status-stale);
}

.drawer-section-title {
  margin: 0 0 8px;
  font-size: var(--d2a-font-md);
  font-weight: 600;
}

.drawer-section-title + .el-table {
  margin-bottom: 20px;
}

.type-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}

.type-item {
  padding: 10px 12px;
  border: 1px solid var(--d2a-border);
  border-radius: 6px;
}

.type-item--disabled {
  opacity: 0.55;
}

.type-item__head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.type-item__name {
  font-weight: 600;
}

.type-item__examples {
  flex: 1;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

.type-item__hint {
  margin: 6px 0 0;
  font-size: var(--d2a-font-sm);
  color: var(--d2a-text-secondary);
}

@media (max-width: 900px) {
  .page-header {
    flex-direction: column;
  }

  .source-grid {
    grid-template-columns: 1fr;
  }
}
</style>
