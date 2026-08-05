<script setup lang="ts">
// 原始数据(原数据浏览 Raw tab):raw 目录 + 浏览抽屉(内含安全 JSON)。
// Raw:目录驱动、服务端列驱动表格、脱敏/截断;raw 数据仅在抽屉打开时请求。
// 详情规范(05-console §3.2):行点击开右侧抽屉,安全 JSON 折叠在抽屉内。
import { computed, onMounted, reactive, ref, watch } from 'vue'
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
const { rawCatalog, rawCatalogRefreshError, rawPage } = storeToRefs(store)
const { rawSel, rawQuery } = store
const route = useRoute()
const router = useRouter()
const rawDrawerVisible = ref(false)
let restoringQuery = false

const rawCatalogData = computed(() =>
  rawCatalog.value.status === 'success' ? rawCatalog.value.data : null,
)

// 目录筛选:来源 / 表 / 抽取时间段(目录为全量小数据,客户端筛选即可)
const catalogFilters = reactive({
  source: '',
  table: '',
  extractedRange: null as [string, string] | null,
})

const catalogSources = computed(() =>
  [...new Set((rawCatalogData.value?.items ?? []).map((i) => i.source))].sort(),
)
const catalogTables = computed(() =>
  [...new Set(
    (rawCatalogData.value?.items ?? [])
      .filter((i) => !catalogFilters.source || i.source === catalogFilters.source)
      .map((i) => i.table),
  )].sort(),
)

function inExtractedRange(extractedAt: string | null): boolean {
  if (!catalogFilters.extractedRange) {
    return true
  }
  if (!extractedAt) {
    return false // 无抽取时间的行不进入时间段筛选结果
  }
  const [start, end] = catalogFilters.extractedRange
  const t = new Date(extractedAt).getTime()
  return t >= new Date(`${start}T00:00:00`).getTime()
    && t <= new Date(`${end}T23:59:59.999`).getTime()
}

const filteredCatalogItems = computed(() =>
  (rawCatalogData.value?.items ?? []).filter((i) =>
    (!catalogFilters.source || i.source === catalogFilters.source)
    && (!catalogFilters.table || i.table === catalogFilters.table)
    && inExtractedRange(i.extracted_at)),
)

watch(() => catalogFilters.source, () => {
  // 来源变化后清掉不再可用的表筛选
  if (catalogFilters.table && !catalogTables.value.includes(catalogFilters.table)) {
    catalogFilters.table = ''
  }
})

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
    <!-- 通栏工具栏(A 类规范):左筛选、右操作 -->
    <div class="d2a-card d2a-toolbar">
      <el-select
        v-model="catalogFilters.source"
        placeholder="来源"
        clearable
        size="small"
        data-testid="filter-source"
      >
        <el-option
          v-for="s in catalogSources"
          :key="s"
          :label="s"
          :value="s"
        />
      </el-select>
      <el-select
        v-model="catalogFilters.table"
        placeholder="表"
        clearable
        filterable
        size="small"
        data-testid="filter-table"
      >
        <el-option
          v-for="t in catalogTables"
          :key="t"
          :label="t"
          :value="t"
        />
      </el-select>
      <el-date-picker
        v-model="catalogFilters.extractedRange"
        type="daterange"
        range-separator="至"
        start-placeholder="抽取开始"
        end-placeholder="抽取结束"
        value-format="YYYY-MM-DD"
        size="small"
        class="toolbar-daterange"
      />
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
      <LoadingState v-if="rawCatalog.status === 'idle' || rawCatalog.status === 'loading'" />
      <ErrorState
        v-else-if="rawCatalog.status === 'error'"
        :error="rawCatalog.error"
        @retry="store.refreshRawCatalog()"
      />
      <EmptyState
        v-else-if="rawCatalogData && rawCatalogData.items.length === 0"
        title="没有可浏览的 raw 表"
      />
      <EmptyState
        v-else-if="rawCatalogData && filteredCatalogItems.length === 0"
        title="没有符合筛选条件的 raw 表"
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
          :data="filteredCatalogItems"
          size="small"
          data-testid="raw-catalog"
          @row-click="(row: { source: string; table: string }) => selectRaw(row.source, row.table)"
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
        </el-table>
      </template>
    </div>

    <RawDataDrawer
      :visible="rawDrawerVisible"
      data-testid="raw-data-drawer"
      @close="rawDrawerVisible = false"
    />
  </section>
</template>

<style scoped>
.warnings {
  padding: 8px 12px;
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
  list-style: none;
}

.data-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.toolbar-daterange {
  width: 260px;
  flex: 0 0 auto;
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}
</style>
