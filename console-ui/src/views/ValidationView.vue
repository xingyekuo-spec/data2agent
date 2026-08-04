<script setup lang="ts">
// M6:报告由服务端冻结并持久化；页面只展示、跳转和下载，绝不在浏览器重新计算。
import { computed, ref } from 'vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { getValidationReport, postValidationRun } from '@/api/services'
import type { ApiError } from '@/api/errors'
import type { components } from '@/types/api'
import { formatDateTime } from '@/utils/time'

type Report = components['schemas']['ValidationReportResponse']
const report = ref<Report | null>(null)
const loading = ref(false)
const error = ref<ApiError | null>(null)
const includeMcpProbe = ref(true)
const latestRunId = ref<number | null>(null)

const health = computed(() => {
  if (!report.value) return 'unknown'
  return report.value.overall_status === 'pass' ? 'healthy'
    : report.value.overall_status === 'warning' ? 'warning' : 'failed'
})

async function run(): Promise<void> {
  loading.value = true
  error.value = null
  const started = await postValidationRun(includeMcpProbe.value)
  if (!started.ok) {
    error.value = started.error
    loading.value = false
    return
  }
  latestRunId.value = started.data.run_id
  const detail = await getValidationReport(started.data.run_id)
  loading.value = false
  if (detail.ok) report.value = detail.data
  else error.value = detail.error
}

function checkHealth(status: Report['checks'][number]['status']): 'healthy' | 'warning' | 'failed' | 'unknown' {
  return status === 'pass' ? 'healthy' : status === 'warning' ? 'warning'
    : status === 'fail' ? 'failed' : 'unknown'
}
</script>

<template>
  <section class="validation-page">
    <div class="d2a-card action-bar">
      <el-checkbox
        v-model="includeMcpProbe"
        :disabled="loading"
      >
        检查 MCP evidence
      </el-checkbox>
      <el-button
        type="primary"
        :loading="loading"
        data-testid="validation-run"
        @click="run"
      >
        开始只读验收
      </el-button>
      <a
        v-if="latestRunId !== null"
        :href="`/api/validation/runs/${latestRunId}/report.json`"
        data-testid="validation-download"
      >下载 JSON</a>
    </div>

    <LoadingState v-if="loading" />
    <ErrorState
      v-else-if="error"
      :error="error"
      @retry="run"
    />
    <div
      v-else-if="report"
      class="d2a-card report"
      data-testid="validation-report"
    >
      <div class="report__headline">
        <div>
          <h2>验收运行 #{{ report.run_id }}</h2>
          <p>{{ report.source }} · {{ formatDateTime(report.finished_at) }}</p>
        </div>
        <StatusBadge :status="health" />
      </div>
      <dl class="summary">
        <dt>数据集</dt><dd>{{ report.dataset_version ?? '—' }}</dd>
        <dt>模板</dt><dd>{{ report.template_version ?? '—' }}</dd>
        <dt>检查</dt><dd>{{ report.summary?.pass_count ?? 0 }} 通过 / {{ report.summary?.warning_count ?? 0 }} 警告 / {{ report.summary?.fail_count ?? 0 }} 失败</dd>
      </dl>
      <el-table
        :data="report.checks"
        size="small"
      >
        <el-table-column
          prop="title"
          label="检查"
          min-width="160"
        />
        <el-table-column
          label="结果"
          width="100"
        >
          <template #default="{ row }">
            <StatusBadge :status="checkHealth(row.status)" />
          </template>
        </el-table-column>
        <el-table-column
          prop="summary"
          label="摘要"
          min-width="260"
        />
        <el-table-column
          label="证据"
          width="110"
        >
          <template #default="{ row }">
            <a
              v-if="row.evidence[0]"
              :href="row.evidence[0].href"
            >查看</a><span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div
      v-else
      class="d2a-card empty"
    >
      尚未运行验收。验收只读取既有事实，不执行同步、发布或写回。
    </div>
  </section>
</template>

<style scoped>
.validation-page { display: grid; gap: 16px; }
.action-bar { display: flex; gap: 12px; align-items: center; }
.report { padding: 20px; }
.report__headline { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.report__headline h2 { margin: 0; }
.report__headline p { color: var(--d2a-text-secondary); }
.summary { display: grid; grid-template-columns: 90px 1fr; gap: 6px 12px; margin: 16px 0; }
.summary dt { color: var(--d2a-text-secondary); }
.summary dd { margin: 0; overflow-wrap: anywhere; }
.empty { color: var(--d2a-text-secondary); }
@media (max-width: 640px) { .action-bar { align-items: flex-start; flex-direction: column; } }
</style>
