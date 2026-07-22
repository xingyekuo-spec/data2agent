<script setup lang="ts">
// 仪表盘(M3):摘要卡、状态摘要、抽取趋势、对象分布、最近运行、告警、
// 版本与治理、数量口径说明。数据来自 overview/pipeline 两个观测 store
// (统一轮询);视图不直接调 API、不另建 timer。
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import StatCard from '@/components/dashboard/StatCard.vue'
import TrendChart from '@/components/dashboard/TrendChart.vue'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useOverviewStore } from '@/stores/overview'
import { usePipelineStore } from '@/stores/pipeline'
import type { HealthStatus } from '@/types/state'
import { formatDateTime, formatTimeHM } from '@/utils/time'

const overviewStore = useOverviewStore()
const pipelineStore = usePipelineStore()
const { overview, lastSuccessAt } = storeToRefs(overviewStore)
const { pipeline, services } = storeToRefs(pipelineStore)

const ov = computed(() => (overview.value.status === 'success' ? overview.value.data : null))
const overall = computed(() =>
  pipeline.value.status === 'success' ? pipeline.value.data.overall_status : null,
)

const SERVICE_LABELS: Record<string, string> = {
  ingest: 'ingest',
  mcp: 'mcp',
  apply: 'apply',
  console: 'console',
}
const serviceItems = computed(() => {
  if (services.value.status !== 'success') {
    return []
  }
  return Object.entries(services.value.data).map(([name, probe]) => ({
    name: SERVICE_LABELS[name] ?? name,
    ok: probe.ok,
    method: probe.method,
  }))
})

const runStatusMap: Record<string, HealthStatus> = {
  ok: 'healthy',
  failed: 'failed',
  aborted: 'failed',
  running: 'running',
  paused: 'warning',
}

function runTypeLabel(t: string | null): string {
  return t ?? '类型未知'
}

const cards = computed(() => {
  if (!ov.value) {
    return []
  }
  const s = ov.value.summary
  const coverage = `${s.materialized_objects}/${s.template_objects}`
  const lastRun = ov.value.recent_runs?.[0] ?? null
  const runsUnknown = ov.value.recent_runs === null
  return [
    {
      label: '对象层总行数',
      value: s.object_rows === null ? '不可检测' : String(s.object_rows),
      tone: s.object_rows === null ? ('unknown' as const) : ('default' as const),
      hint: 'obj_* 合计,与 raw 因隔离/软删有差',
    },
    {
      label: '对象覆盖率',
      value: coverage,
      tone: s.materialized_objects === 0 ? ('unknown' as const) : ('default' as const),
      hint: '已物化对象 ÷ 模板对象',
    },
    {
      label: '隔离待处理',
      value: s.quarantine_pending === null ? '不可检测' : String(s.quarantine_pending),
      tone:
        s.quarantine_pending === null
          ? ('unknown' as const)
          : s.quarantine_pending > 0
            ? ('warn' as const)
            : ('good' as const),
      hint: '映射失败等待处理的行数',
    },
    {
      label: '最近运行',
      value: runsUnknown ? '不可检测' : lastRun ? runTypeLabel(lastRun.run_type) : '从未运行',
      tone:
        runsUnknown || !lastRun
          ? ('unknown' as const)
          : lastRun.status === 'ok'
            ? ('good' as const)
            : ('bad' as const),
      hint: runsUnknown ? '运行记录查询失败' : lastRun ? formatDateTime(lastRun.started_at) : '首次同步后显示',
    },
  ]
})

const objectRows = computed(() => {
  if (!ov.value) {
    return []
  }
  const rows = ov.value.objects.filter((o) => o.rows !== null)
  const total = rows.reduce((sum, o) => sum + (o.rows ?? 0), 0)
  return rows.map((o) => ({
    ...o,
    share: total > 0 ? ((o.rows ?? 0) / total) * 100 : 0,
  }))
})

const severityOrder: Record<string, number> = { critical: 0, warning: 1, info: 2 }
const sortedAlerts = computed(() =>
  [...(ov.value?.alerts ?? [])].sort(
    (a, b) => (severityOrder[a.severity] ?? 9) - (severityOrder[b.severity] ?? 9),
  ),
)
</script>

<template>
  <section class="dashboard">
    <!-- 刷新失败:保留上一次成功数据并明确标记(M3 语义) -->
    <div
      v-if="overviewStore.refreshError || pipelineStore.refreshError"
      class="refresh-warning"
      data-testid="refresh-error"
    >
      刷新失败({{ (overviewStore.refreshError ?? pipelineStore.refreshError)?.message }}),
      展示上一次成功数据
      <template v-if="lastSuccessAt">
        (截至 {{ formatTimeHM(lastSuccessAt.toISOString()) }})
      </template>
    </div>

    <LoadingState v-if="overview.status === 'idle' || overview.status === 'loading'" />
    <ErrorState
      v-else-if="overview.status === 'error'"
      :error="overview.error"
      @retry="overviewStore.refresh()"
    />
    <template v-else-if="ov">
      <EmptyState
        v-if="ov.needs_setup"
        title="尚未完成首次配置"
          hint="请打开 /v1/setup 完成首次配置"
      />
      <EmptyState
        v-else-if="ov.sources.length === 0 && ov.objects.length === 0"
        title="暂无数据"
        hint="首次同步后显示来源与对象"
      />
      <template v-else>
        <!-- 状态摘要:整体状态 / 更新时间 / 告警数 / 旧结果提示 / 服务健康 -->
        <div
          class="status-strip d2a-card"
          data-testid="status-strip"
        >
          <StatusBadge
            v-if="overall"
            :status="overall"
          />
          <span
            v-else
            class="status-strip__unknown"
          >整体状态不可检测</span>
          <span
            v-if="ov.summary.data_updated_at"
            class="status-strip__item"
          >
            数据更新 {{ formatDateTime(ov.summary.data_updated_at) }}
          </span>
          <span class="status-strip__item">告警 {{ ov.alerts.length }} 条</span>
          <span
            v-if="overall === 'stale'"
            class="status-strip__stale"
          >
            部分数据为上一稳定结果
          </span>
          <span
            class="status-strip__services"
            data-testid="services-strip"
          >
            <template v-if="services.status === 'success'">
              <span
                v-for="s in serviceItems"
                :key="s.name"
                class="status-strip__service"
                :title="s.method"
              >
                {{ s.name }}
                <StatusBadge :status="s.ok ? 'healthy' : 'failed'" />
              </span>
            </template>
            <span
              v-else-if="services.status === 'error'"
              class="status-strip__svc-error"
            >
              服务状态查询失败(HTTP {{ services.error.status }})
            </span>
          </span>
        </div>

        <!-- 四张摘要卡 -->
        <div
          class="stat-grid"
          data-testid="stat-grid"
        >
          <StatCard
            v-for="card in cards"
            :key="card.label"
            :label="card.label"
            :value="card.value"
            :tone="card.tone"
            :hint="card.hint"
          />
        </div>

        <div class="d2a-card">
          <h3 class="card-title">
            最近 24 小时抽取趋势
          </h3>
          <p
            v-if="ov.sync_trend === null"
            class="undetectable"
            data-testid="trend-unknown"
          >
            趋势不可检测(查询失败)
          </p>
          <TrendChart
            v-else
            :points="ov.sync_trend"
          />
        </div>

        <div class="d2a-card">
          <h3 class="card-title">
            对象分布
          </h3>
          <EmptyState
            v-if="objectRows.length === 0"
            title="尚未物化"
            hint="apply 后显示对象分布"
          />
          <ul
            v-else
            class="dist"
            data-testid="object-dist"
          >
            <li
              v-for="o in objectRows"
              :key="o.object"
              class="dist__row"
            >
              <span class="dist__name">{{ o.display_name }}</span>
              <span class="dist__bar-wrap">
                <span
                  class="dist__bar"
                  :style="{ width: `${o.share}%` }"
                />
              </span>
              <span class="dist__num">{{ o.rows }}</span>
            </li>
          </ul>
        </div>

        <div class="d2a-card">
          <h3 class="card-title">
            最近运行
          </h3>
          <p
            v-if="ov.recent_runs === null"
            class="undetectable"
            data-testid="runs-unknown"
          >
            运行记录不可检测(查询失败)
          </p>
          <EmptyState
            v-else-if="ov.recent_runs.length === 0"
            title="从未运行"
            hint="首次同步后显示"
          />
          <el-table
            v-else
            :data="ov.recent_runs"
            size="small"
            data-testid="recent-runs"
          >
            <el-table-column
              label="类型"
              width="110"
            >
              <template #default="{ row }">
                {{ runTypeLabel(row.run_type) }}
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
              label="行数"
              width="90"
            >
              <template #default="{ row }">
                {{ row.rows ?? '—' }}
              </template>
            </el-table-column>
            <el-table-column label="开始">
              <template #default="{ row }">
                {{ formatDateTime(row.started_at) }}
              </template>
            </el-table-column>
            <el-table-column label="结束">
              <template #default="{ row }">
                {{ formatDateTime(row.finished_at) }}
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="d2a-card">
          <h3 class="card-title">
            当前告警
          </h3>
          <EmptyState
            v-if="sortedAlerts.length === 0"
            title="无告警"
          />
          <ul
            v-else
            class="alerts"
            data-testid="alerts"
          >
            <li
              v-for="a in sortedAlerts"
              :key="a.id"
              class="alerts__row"
            >
              <el-tag
                size="small"
                :type="a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info'"
              >
                {{ a.severity }}
              </el-tag>
              <span class="alerts__title">{{ a.title }}</span>
              <span class="alerts__reason">{{ a.reason }}</span>
              <span class="alerts__time">{{ formatDateTime(a.observed_at) }}</span>
            </li>
          </ul>
        </div>

        <div class="d2a-card">
          <h3 class="card-title">
            版本与治理
          </h3>
          <el-descriptions
            :column="2"
            size="small"
            border
            data-testid="versions"
          >
            <el-descriptions-item label="应用版本">
              {{ ov.versions.app ?? '不可检测' }}
            </el-descriptions-item>
            <el-descriptions-item label="模板版本">
              {{ ov.versions.template ?? '不可检测' }}
            </el-descriptions-item>
            <el-descriptions-item label="dataset 版本">
              <span v-if="ov.versions.dataset">{{ ov.versions.dataset }}</span>
              <span
                v-else
                class="version-na"
                data-testid="dataset-version-na"
              >尚未发布</span>
            </el-descriptions-item>
            <el-descriptions-item label="object 版本">
              <span v-if="ov.versions.object">{{ ov.versions.object }}</span>
              <span
                v-else
                class="version-na"
                data-testid="object-version-na"
              >尚未发布</span>
            </el-descriptions-item>
            <el-descriptions-item label="binding 状态">
              verified {{ ov.binding_summary.verified }} / draft {{ ov.binding_summary.draft }}
              / disabled {{ ov.binding_summary.disabled }}
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <div class="d2a-card">
          <h3 class="card-title">
            数量口径说明
          </h3>
          <ul
            class="notes"
            data-testid="count-notes"
          >
            <li
              v-for="n in ov.count_notes"
              :key="n.name"
            >
              <code>{{ n.name }}</code>:{{ n.semantics }}(来源:{{ n.source }})
            </li>
          </ul>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.refresh-warning {
  padding: 8px 12px;
  border-left: 3px solid var(--d2a-status-warning);
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.card-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: var(--d2a-text-primary);
}

.status-strip {
  display: flex;
  gap: 14px;
  align-items: center;
}

.status-strip__item {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.status-strip__unknown {
  font-size: 12px;
  color: var(--d2a-status-unknown);
}

.status-strip__stale {
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.status-strip__services {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-left: auto;
}

.status-strip__service {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.status-strip__svc-error {
  font-size: 12px;
  color: var(--d2a-status-failed);
}

.undetectable {
  margin: 0;
  font-size: 13px;
  color: var(--d2a-status-unknown);
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

.dist {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.dist__row {
  display: flex;
  gap: 10px;
  align-items: center;
}

.dist__name {
  width: 110px;
  font-size: 13px;
}

.dist__bar-wrap {
  flex: 1;
  height: 10px;
  overflow: hidden;
  background: var(--el-fill-color-light);
  border-radius: 5px;
}

.dist__bar {
  display: block;
  height: 100%;
  background: var(--d2a-primary);
  border-radius: 5px;
}

.dist__num {
  width: 56px;
  font-size: 12px;
  text-align: right;
  color: var(--d2a-text-secondary);
}

.alerts {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.alerts__row {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.alerts__title {
  font-weight: 600;
}

.alerts__reason {
  flex: 1;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.alerts__time {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.version-na {
  color: var(--d2a-status-unknown);
}

.notes {
  padding-left: 18px;
  margin: 0;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

@media (width <= 900px) {
  .stat-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
