<script setup lang="ts">
// 原始数据(原数据浏览 Raw tab):raw 目录 + 浏览抽屉 + 安全 JSON。
// Raw:目录驱动、服务端列驱动表格、脱敏/截断;raw 数据仅在抽屉打开时请求。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import RawDataDrawer from '@/components/shared/RawDataDrawer.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useDataStore } from '@/stores/data'
import { formatDateTime } from '@/utils/time'

const store = useDataStore()
const { rawCatalog, rawCatalogRefreshError, rawPage, rawPageRefreshError } = storeToRefs(store)
const { rawSel, rawQuery } = store
const route = useRoute()
const router = useRouter()
const rawDrawerVisible = ref(false)
const showJson = ref(false)
let restoringQuery = false

const rawCatalogData = computed(() =>
  rawCatalog.value.status === 'success' ? rawCatalog.value.data : null,
)
const currentJson = computed(() =>
  rawPage.value?.status === 'success' ? rawPage.value.data : null,
)
const raw403 = computed(
  () =>
    (rawCatalog.value.status === 'error' && rawCatalog.value.error.status === 403)
    || (rawPage.value?.status === 'error' && rawPage.value.error.status === 403)
    || rawPageRefreshError.value?.status === 403,
)

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

function applyRouteQuery(query: LocationQuery, browse: boolean): void {
  restoringQuery = true
  let browseRaw = false
  const nextRawSource = firstString(query.source)
  const nextRawTable = firstString(query.table)
  const nextRawQ = firstString(query.q)
  const nextRawOffset = pageOffset(query.page, rawQuery.limit)
  const rawResourceChanged = rawSel.source !== nextRawSource || rawSel.table !== nextRawTable
  if (
    rawResourceChanged
    || rawQuery.q !== nextRawQ
    || rawQuery.offset !== nextRawOffset
  ) {
    rawSel.source = nextRawSource
    rawSel.table = nextRawTable
    rawQuery.q = nextRawQ
    rawQuery.offset = nextRawOffset
    if (rawResourceChanged || !nextRawSource || !nextRawTable) {
      rawPage.value = null
    }
    browseRaw = Boolean(nextRawSource && nextRawTable)
  }
  restoringQuery = false
  if (browse && browseRaw) {
    void store.browseRaw()
  }
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  void router.replace({
    query: {
      ...(rawSel.source ? { source: rawSel.source } : {}),
      ...(rawSel.table ? { table: rawSel.table } : {}),
      ...(rawQuery.q ? { q: rawQuery.q } : {}),
      ...(pageQuery(rawQuery.offset, rawQuery.limit) ? {
        page: pageQuery(rawQuery.offset, rawQuery.limit),
      } : {}),
    },
  })
}

function selectRaw(source: string, table: string): void {
  store.selectRaw(source, table)
  rawDrawerVisible.value = true
  syncRouteQuery()
}

onMounted(() => {
  applyRouteQuery(route.query, true)
  void store.refreshRawCatalog()
})

watch(() => route.query, (query) => applyRouteQuery(query, true))
</script>

<template>
  <section class="data-page d2a-page-flush">
    <!-- 通栏工具栏(A 类规范):左提示右操作 -->
    <div class="d2a-card d2a-toolbar">
      <span class="toolbar-hint">raw 原始数据目录(点「浏览」查看表数据)</span>
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="raw-refresh"
          @click="store.refreshRawCatalog()"
        >
          刷新
        </el-button>
      </div>
    </div>

    <div class="d2a-card">
      <h3 class="card-title">
        raw 目录
      </h3>
      <LoadingState v-if="rawCatalog.status === 'idle' || rawCatalog.status === 'loading'" />
      <ErrorState
        v-else-if="rawCatalog.status === 'error' && rawCatalog.error.status !== 403"
        :error="rawCatalog.error"
        @retry="store.refreshRawCatalog()"
      />
      <EmptyState
        v-else-if="rawCatalogData && rawCatalogData.items.length === 0"
        title="没有可浏览的 raw 表"
      />
      <template v-else-if="rawCatalogData">
        <p
          v-if="rawCatalogRefreshError"
          class="refresh-warning"
          data-testid="raw-catalog-refresh-error"
        >
          刷新失败({{ rawCatalogRefreshError.message }}),展示上一次成功数据
        </p>
        <ul
          v-if="rawCatalogData.warnings?.length"
          class="warnings"
          data-testid="raw-catalog-warnings"
        >
          <li
            v-for="w in rawCatalogData.warnings"
            :key="w"
          >
            {{ w }}
          </li>
        </ul>
        <el-table
          :data="rawCatalogData.items"
          size="small"
          data-testid="raw-catalog"
        >
          <el-table-column
            prop="source"
            label="来源"
            width="130"
          />
          <el-table-column
            prop="table"
            label="表"
            min-width="150"
          />
          <el-table-column
            label="行数"
            width="90"
          >
            <template #default="{ row }">
              {{ row.rows ?? '不可检测' }}
            </template>
          </el-table-column>
          <el-table-column
            label="最近批次"
            width="150"
          >
            <template #default="{ row }">
              {{ row.latest_batch_id ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="抽取时间"
            width="150"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.extracted_at) }}
            </template>
          </el-table-column>
          <el-table-column
            label="分类"
            width="90"
          >
            <template #default="{ row }">
              <StatusBadge
                v-if="row.classification_warning"
                status="warning"
              />
              <StatusBadge
                v-else
                status="healthy"
              />
            </template>
          </el-table-column>
          <el-table-column width="90">
            <template #default="{ row }">
              <el-button
                size="small"
                text
                :data-testid="`browse-${row.table}`"
                @click="selectRaw(row.source, row.table)"
              >
                浏览
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </div>

    <!-- 403:安全配置指引(不降级到对象或 Mock) -->
    <div
      v-if="raw403"
      class="d2a-card security-guide"
      data-testid="raw-403-guide"
    >
      <h3 class="card-title">
        raw 浏览已按安全基线关闭
      </h3>
      <p>
        raw 原始数据只允许授权管理主体访问:当前控制台未配置 Token。
        请在管理页完成首次配置,或以 D2A_CONSOLE_TOKEN 启动控制台后重试。
      </p>
    </div>

    <div
      v-if="currentJson"
      class="d2a-card"
    >
      <el-button
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
    </div>

    <RawDataDrawer
      :visible="rawDrawerVisible"
      data-testid="raw-data-drawer"
      @close="rawDrawerVisible = false"
    />
  </section>
</template>

<style scoped>
.data-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.warnings {
  padding: 8px 12px;
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
  list-style: none;
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.security-guide p {
  margin: 0;
  font-size: 13px;
  color: var(--d2a-text-secondary);
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
