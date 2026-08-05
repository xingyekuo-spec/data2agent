<script setup lang="ts">
// 数据集版本(原数据浏览 数据集 tab):构建/发布操作条、版本列表、
// publish/rollback、stage-only apply、详情抽屉(内含安全 JSON)。
// 详情规范(05-console §3.2):行点击开右侧抽屉,安全 JSON 折叠在抽屉内。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import PagerBar from '@/components/shared/PagerBar.vue'
import { useDatasetsStore } from '@/stores/datasets'
import {
  canPublish,
  canRollback,
  rollbackTarget,
  datasetStatusLabel,
} from '@/utils/datasetStatus'
import { formatDateTime } from '@/utils/time'

const datasetsStore = useDatasetsStore()
const {
  list: datasetList,
  total: datasetTotal,
  listRefreshError,
  objectsByVersion,
  detail: datasetDetail,
  detailRefreshError,
  actionResult,
  actionError,
  applyResult,
  applyError,
} = storeToRefs(datasetsStore)
const { filters: datasetFilters, page: datasetPage } = datasetsStore
const route = useRoute()
const router = useRouter()
const showJson = ref(false)
const stageOnly = ref(false)
const applySource = ref('digiwin_e10')
let restoringQuery = false

const currentJson = computed(() =>
  datasetDetail.value?.status === 'success' ? datasetDetail.value.data : null,
)
const datasetDetailObjects = computed(() =>
  datasetDetail.value?.status === 'success' ? (datasetDetail.value.data.objects ?? []) : [],
)

function onDatasetPage(offset: number, limit: number): void {
  datasetPage.offset = offset
  datasetPage.limit = limit
  void datasetsStore.refresh()
  syncRouteQuery()
}

function firstString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function pageOffset(value: unknown, limit: number): number {
  const page = Number(firstString(value))
  return Number.isFinite(page) && page > 1 ? (page - 1) * limit : 0
}

function pageQuery(offset: number, limit: number): string | undefined {
  const page = offset / limit + 1
  return page > 1 ? String(page) : undefined
}

function applyRouteQuery(query: LocationQuery, load: boolean): void {
  restoringQuery = true
  const nextVersion = firstString(query.version)
  const nextOffset = pageOffset(query.page, datasetPage.limit)
  if (datasetPage.offset !== nextOffset) {
    datasetPage.offset = nextOffset
  }
  restoringQuery = false
  if (load) {
    void datasetsStore.refresh()
    if (nextVersion) {
      void datasetsStore.openDetail(nextVersion)
    } else if (datasetsStore.detailVersion !== null) {
      datasetsStore.closeDetail()
    }
  }
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  void router.replace({
    query: {
      ...(datasetsStore.detailVersion ? { version: datasetsStore.detailVersion } : {}),
      ...(pageQuery(datasetPage.offset, datasetPage.limit) ? {
        page: pageQuery(datasetPage.offset, datasetPage.limit),
      } : {}),
    },
  })
}

function openDataset(version: string): void {
  showJson.value = false
  void datasetsStore.openDetail(version)
  syncRouteQuery()
}

function closeDrawer(): void {
  datasetsStore.closeDetail()
  syncRouteQuery()
}

async function onPublish(version: string): Promise<void> {
  await datasetsStore.publish(version)
}

async function onRollback(row: { previous_dataset_version?: string | null }): Promise<void> {
  const target = rollbackTarget(row)
  if (!target) {
    return
  }
  await datasetsStore.rollback(target)
}

async function onApply(): Promise<void> {
  await datasetsStore.apply({
    source: applySource.value || 'digiwin_e10',
    publish: !stageOnly.value,
  })
}

onMounted(() => {
  applyRouteQuery(route.query, true)
})

watch(() => route.query, (query) => applyRouteQuery(query, true))
</script>

<template>
  <section class="data-page d2a-page-flush">
    <div class="d2a-card d2a-toolbar">
      <el-select
        v-model="datasetFilters.status"
        placeholder="状态"
        clearable
        size="small"
        data-testid="dataset-filter-status"
        @change="datasetsStore.refresh()"
      >
        <el-option
          label="building"
          value="building"
        />
        <el-option
          label="published"
          value="published"
        />
        <el-option
          label="failed"
          value="failed"
        />
        <el-option
          label="retired"
          value="retired"
        />
      </el-select>
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="datasets-refresh"
          @click="datasetsStore.refresh()"
        >
          刷新
        </el-button>
      </div>
    </div>

    <div class="d2a-card">
      <h3 class="card-title">
        构建 / 发布
      </h3>
      <div
        class="apply-bar"
        data-testid="dataset-apply-bar"
      >
        <el-input
          v-model="applySource"
          size="small"
          class="apply-bar__source"
          placeholder="source"
          data-testid="apply-source"
        />
        <label
          class="inline"
          data-testid="stage-only-toggle"
        >
          <input
            v-model="stageOnly"
            type="checkbox"
          >
          仅构建不发布(stage-only)
        </label>
        <el-button
          type="primary"
          size="small"
          data-testid="apply-run"
          @click="onApply"
        >
          {{ stageOnly ? '构建候选' : '构建并发布' }}
        </el-button>
      </div>
      <p
        v-if="applyError"
        class="refresh-warning"
        data-testid="apply-error"
      >
        构建失败({{ applyError.message }})
      </p>
      <p
        v-else-if="applyResult?.status === 'success'"
        class="action-note"
        data-testid="apply-result"
      >
        候选 {{ applyResult.data.dataset_version ?? '—' }}
        · published={{ applyResult.data.published ?? false }}
        · previous={{ applyResult.data.previous_dataset_version ?? '—' }}
      </p>
    </div>

    <div class="d2a-card">
      <p
        v-if="listRefreshError"
        class="refresh-warning"
        data-testid="datasets-refresh-error"
      >
        刷新失败({{ listRefreshError.message }}),展示上一次成功数据
      </p>
      <p
        v-if="actionError"
        class="refresh-warning"
        data-testid="dataset-action-error"
      >
        操作失败({{ actionError.message }})
      </p>
      <p
        v-else-if="actionResult?.status === 'success'"
        class="action-note"
        data-testid="dataset-action-result"
      >
        已执行 → {{ actionResult.data.dataset_version }} ({{ actionResult.data.note || 'ok' }})
      </p>
      <LoadingState v-if="datasetList.status === 'idle' || datasetList.status === 'loading'" />
      <ErrorState
        v-else-if="datasetList.status === 'error'"
        :error="datasetList.error"
        @retry="datasetsStore.refresh()"
      />
      <EmptyState
        v-else-if="datasetList.status === 'success' && datasetList.data.length === 0"
        title="尚无数据集版本"
        hint="先执行构建(可选择 stage-only)生成候选"
      />
      <template v-else-if="datasetList.status === 'success'">
        <el-table
          :data="datasetList.data"
          size="small"
          data-testid="datasets-table"
          @row-click="(row: { dataset_version: string }) => openDataset(row.dataset_version)"
        >
          <el-table-column
            prop="dataset_version"
            label="版本"
            min-width="200"
          />
          <el-table-column
            prop="source"
            label="来源"
            width="130"
          />
          <el-table-column
            label="状态"
            width="100"
          >
            <template #default="{ row }">
              <span :data-testid="`dataset-status-${row.dataset_version}`">
                {{ datasetStatusLabel(row, objectsByVersion[row.dataset_version]) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column
            label="构建时间"
            width="150"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.built_at) }}
            </template>
          </el-table-column>
          <el-table-column
            label="发布时间"
            width="150"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.published_at) || '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="上一版本"
            width="160"
          >
            <template #default="{ row }">
              {{ row.previous_dataset_version ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            width="140"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                v-if="canPublish(row, objectsByVersion[row.dataset_version])"
                size="small"
                type="primary"
                text
                :data-testid="`dataset-publish-${row.dataset_version}`"
                @click.stop="onPublish(row.dataset_version)"
              >
                发布
              </el-button>
              <el-button
                v-if="canRollback(row)"
                size="small"
                type="warning"
                text
                :data-testid="`dataset-rollback-${row.dataset_version}`"
                @click.stop="onRollback(row)"
              >
                回滚
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <PagerBar
          :total="datasetTotal"
          :limit="datasetPage.limit"
          :offset="datasetPage.offset"
          data-testid="datasets-pager"
          @change="onDatasetPage"
        />
      </template>
    </div>

    <el-drawer
      :model-value="datasetDetail !== null"
      title="数据集详情"
      size="560px"
      data-testid="dataset-detail-drawer"
      @close="closeDrawer"
    >
      <LoadingState v-if="datasetDetail?.status === 'loading'" />
      <ErrorState
        v-else-if="datasetDetail?.status === 'error'"
        :error="datasetDetail.error"
        @retry="datasetsStore.detailVersion && datasetsStore.openDetail(datasetsStore.detailVersion)"
      />
      <template v-else-if="datasetDetail?.status === 'success'">
        <p
          v-if="detailRefreshError"
          class="refresh-warning"
        >
          刷新失败({{ detailRefreshError.message }}),展示上一次成功数据
        </p>
        <dl class="summary">
          <dt>版本</dt>
          <dd>{{ datasetDetail.data.dataset_version }}</dd>
          <dt>状态</dt>
          <dd>{{ datasetStatusLabel(datasetDetail.data, datasetDetailObjects) }}</dd>
          <dt>template</dt>
          <dd>{{ datasetDetail.data.template_version }}</dd>
          <dt>上一版本</dt>
          <dd>{{ datasetDetail.data.previous_dataset_version ?? '—' }}</dd>
          <dt>错误</dt>
          <dd>{{ datasetDetail.data.error ?? '—' }}</dd>
        </dl>

        <h4>对象</h4>
        <el-table
          :data="datasetDetailObjects"
          size="small"
          data-testid="dataset-objects-table"
        >
          <el-table-column
            prop="object"
            label="对象"
            width="140"
          />
          <el-table-column
            prop="object_version"
            label="对象版本"
            min-width="140"
          />
          <el-table-column
            prop="status"
            label="状态"
            width="100"
          />
          <el-table-column
            prop="row_count"
            label="行数"
            width="80"
          />
          <el-table-column
            prop="binding_hash"
            label="binding_hash"
            min-width="180"
          />
        </el-table>

        <el-button
          class="json-toggle"
          size="small"
          text
          data-testid="json-toggle"
          @click="showJson = !showJson"
        >
          {{ showJson ? '隐藏' : '查看' }}安全 JSON(与表格同源)
        </el-button>
        <pre
          v-if="showJson"
          class="json-view"
          data-testid="json-view"
        >{{ JSON.stringify(currentJson, null, 2) }}</pre>
      </template>
    </el-drawer>
  </section>
</template>

<style scoped>
.data-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.apply-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.apply-bar__source {
  width: 240px;
}

.inline {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.action-note {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-text-secondary);
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

.json-toggle {
  margin-top: 12px;
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.json-view {
  max-height: 320px;
  margin: 8px 0 0;
  padding: 10px;
  overflow: auto;
  font-size: 11px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
