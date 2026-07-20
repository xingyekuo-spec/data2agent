<script setup lang="ts">
// 数据浏览(M4):Raw/Object 双 tab,目录驱动、服务端列驱动表格、业务键搜索、
// 分类/脱敏/截断标记、同源安全 JSON。raw 403 显示安全配置指引(不降级)。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useDataStore } from '@/stores/data'
import { formatDateTime } from '@/utils/time'

const store = useDataStore()
const {
  rawCatalog,
  rawCatalogRefreshError,
  rawPage,
  rawPageRefreshError,
  objCatalog,
  objCatalogRefreshError,
  objSel,
  objPage,
  objPageRefreshError,
} = storeToRefs(store)
const { rawSel, rawQuery, objQuery } = store
const route = useRoute()
const router = useRouter()

// el-tabs 默认激活 pane 行为不明确,显式固定为 Raw
const activeTab = ref('raw')
const showJson = ref(false)
let restoringQuery = false

onMounted(() => {
  applyRouteQuery(route.query, true)
  void store.refreshRawCatalog()
})

const rawCols = computed(() =>
  rawPage.value?.status === 'success' ? rawPage.value.data.columns : [],
)
const rawCatalogData = computed(() =>
  rawCatalog.value.status === 'success' ? rawCatalog.value.data : null,
)
const objCols = computed(() =>
  objPage.value?.status === 'success' ? objPage.value.data.columns : [],
)
const objSearchable = computed(() =>
  objPage.value?.status !== 'success' || objPage.value.data.searchable,
)
const currentJson = computed(() => {
  if (activeTab.value === 'raw') {
    return rawPage.value?.status === 'success' ? rawPage.value.data : null
  }
  return objPage.value?.status === 'success' ? objPage.value.data : null
})

const raw403 = computed(
  () =>
    (rawCatalog.value.status === 'error' && rawCatalog.value.error.status === 403)
    || (rawPage.value?.status === 'error' && rawPage.value.error.status === 403)
    || rawPageRefreshError.value?.status === 403,
)

type CellValue = string | number | boolean | null | { __blob__?: boolean; bytes?: number }

function formatCell(value: unknown): string {
  if (value === null || value === undefined) {
    return '—'
  }
  if (typeof value === 'object' && value !== null && (value as { __blob__?: boolean }).__blob__) {
    const blob = value as { bytes?: number }
    return `[BLOB ${blob.bytes ?? '?'} bytes]`
  }
  return String(value as CellValue)
}

function onRawPage(current: number): void {
  rawQuery.offset = (current - 1) * rawQuery.limit
  void store.browseRaw()
  syncRouteQuery()
}

function onObjPage(current: number): void {
  objQuery.offset = (current - 1) * objQuery.limit
  void store.browseObject()
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

function applyRouteQuery(query: LocationQuery, browse: boolean): void {
  restoringQuery = true
  const tab = firstString(query.tab)
  activeTab.value = tab === 'object' ? 'object' : 'raw'

  let browseRaw = false
  let browseObject = false
  const nextRawSource = firstString(query.source)
  const nextRawTable = firstString(query.table)
  const nextRawQ = activeTab.value === 'raw' ? firstString(query.q) : ''
  const nextRawOffset = activeTab.value === 'raw' ? pageOffset(query.page, rawQuery.limit) : 0
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

  const nextObj = activeTab.value === 'object' ? firstString(query.object) : ''
  const nextObjQ = activeTab.value === 'object' ? firstString(query.q) : ''
  const nextObjOffset = activeTab.value === 'object' ? pageOffset(query.page, objQuery.limit) : 0
  const objResourceChanged = objSel.value !== nextObj
  if (objSel.value !== nextObj || objQuery.q !== nextObjQ || objQuery.offset !== nextObjOffset) {
    objSel.value = nextObj
    objQuery.q = nextObjQ
    objQuery.offset = nextObjOffset
    if (objResourceChanged || !nextObj) {
      objPage.value = null
    }
    browseObject = Boolean(nextObj)
  }
  restoringQuery = false
  if (browse) {
    if (browseRaw && activeTab.value === 'raw') {
      void store.browseRaw()
    }
    if (browseObject && activeTab.value === 'object') {
      void store.browseObject()
    }
  }
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  if (activeTab.value === 'object') {
    void router.replace({
      query: {
        tab: 'object',
        ...(objSel.value ? { object: objSel.value } : {}),
        ...(objQuery.q ? { q: objQuery.q } : {}),
        ...(pageQuery(objQuery.offset, objQuery.limit) ? {
          page: pageQuery(objQuery.offset, objQuery.limit),
        } : {}),
      },
    })
    return
  }
  void router.replace({
    query: {
      tab: 'raw',
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
  syncRouteQuery()
}

function selectObject(object: string): void {
  store.selectObject(object)
  syncRouteQuery()
}

function searchRaw(): void {
  store.searchRaw()
  syncRouteQuery()
}

function searchObject(): void {
  store.searchObject()
  syncRouteQuery()
}

watch(() => route.query, (query) => applyRouteQuery(query, true))
</script>

<template>
  <section class="data-page">
    <el-tabs v-model="activeTab" data-testid="data-tabs" @tab-change="syncRouteQuery">
      <!-- Raw 浏览 -->
      <el-tab-pane label="Raw" name="raw">
        <div class="d2a-card">
          <h3 class="card-title">raw 目录</h3>
          <LoadingState v-if="rawCatalog.status === 'idle' || rawCatalog.status === 'loading'" />
          <ErrorState
            v-else-if="rawCatalog.status === 'error' && rawCatalog.error.status !== 403"
            :error="rawCatalog.error"
            @retry="store.refreshRawCatalog()"
          />
          <EmptyState v-else-if="rawCatalogData && rawCatalogData.items.length === 0" title="没有可浏览的 raw 表" />
          <template v-else-if="rawCatalogData">
            <p
              v-if="rawCatalogRefreshError"
              class="refresh-warning"
              data-testid="raw-catalog-refresh-error"
            >
              刷新失败({{ rawCatalogRefreshError.message }}),展示上一次成功数据
            </p>
            <ul v-if="rawCatalogData.warnings?.length" class="warnings" data-testid="raw-catalog-warnings">
              <li v-for="w in rawCatalogData.warnings" :key="w">{{ w }}</li>
            </ul>
            <el-table :data="rawCatalogData.items" size="small" data-testid="raw-catalog">
              <el-table-column prop="source" label="来源" width="130" />
              <el-table-column prop="table" label="表" min-width="150" />
              <el-table-column label="行数" width="90">
                <template #default="{ row }">{{ row.rows ?? '不可检测' }}</template>
              </el-table-column>
              <el-table-column label="最近批次" width="150">
                <template #default="{ row }">{{ row.latest_batch_id ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="抽取时间" width="150">
                <template #default="{ row }">{{ formatDateTime(row.extracted_at) }}</template>
              </el-table-column>
              <el-table-column label="分类" width="90">
                <template #default="{ row }">
                  <StatusBadge v-if="row.classification_warning" status="warning" />
                  <StatusBadge v-else status="healthy" />
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
        <div v-if="raw403" class="d2a-card security-guide" data-testid="raw-403-guide">
          <h3 class="card-title">raw 浏览已按安全基线关闭</h3>
          <p>
            raw 原始数据只允许授权管理主体访问:当前控制台未配置 Token。
            请在管理页完成首次配置,或以 D2A_CONSOLE_TOKEN 启动控制台后重试。
          </p>
        </div>

        <div v-else-if="rawPage" class="d2a-card">
          <div class="toolbar">
            <el-input
              v-model="rawQuery.q"
              :placeholder="rawPage.status === 'success' && rawPage.data.searchable ? '按业务键搜索' : '该资源没有可搜索业务键'"
              size="small"
              clearable
              class="toolbar__search"
              :disabled="rawPage.status === 'success' && !rawPage.data.searchable"
              data-testid="raw-search"
              @change="searchRaw()"
            />
            <el-button size="small" data-testid="raw-refresh" @click="store.browseRaw()">刷新</el-button>
            <span class="toolbar__meta">
              <template v-if="rawPage.status === 'success'">
                {{ rawSel.source }}/{{ rawSel.table }} · 共 {{ rawPage.data.total }} 行 ·
                排序 {{ rawPage.data.sort }}
              </template>
            </span>
          </div>

          <LoadingState v-if="rawPage.status === 'loading'" />
          <ErrorState v-else-if="rawPage.status === 'error'" :error="rawPage.error" @retry="store.browseRaw()" />
          <template v-else-if="rawPage.status === 'success'">
            <p v-if="rawPageRefreshError" class="refresh-warning" data-testid="raw-page-refresh-error">
              刷新失败({{ rawPageRefreshError.message }}),展示上一次成功数据
            </p>
            <ul v-if="rawPage.data.warnings.length" class="warnings" data-testid="raw-warnings">
              <li v-for="w in rawPage.data.warnings" :key="w">{{ w }}</li>
            </ul>
            <el-table :data="rawPage.data.rows" size="small" data-testid="raw-table">
              <el-table-column
                v-for="col in rawCols"
                :key="col.name"
                :prop="col.name"
                min-width="130"
              >
                <template #header>
                  <span>{{ col.name }}</span>
                  <el-tag v-if="col.classification === 'sensitive'" size="small" type="warning" class="col-flag">
                    脱敏
                  </el-tag>
                  <el-tag v-else-if="col.classification === 'unknown'" size="small" type="info" class="col-flag">
                    未知
                  </el-tag>
                </template>
                <template #default="{ row }">{{ formatCell(row[col.name]) }}</template>
              </el-table-column>
            </el-table>
            <p v-if="rawPage.data.truncations.length" class="trunc-note" data-testid="raw-truncations">
              {{ rawPage.data.truncations.length }} 行存在截断字段(预览不是完整值):
              {{ rawPage.data.truncations.map((t) => `#${t.row_index}(${t.fields.join('/')})`).join(', ') }}
            </p>
            <el-pagination
              class="pager"
              layout="prev, pager, next"
              :total="rawPage.data.total"
              :page-size="rawQuery.limit"
              :current-page="rawQuery.offset / rawQuery.limit + 1"
              data-testid="raw-pager"
              @current-change="onRawPage"
            />
          </template>
        </div>
      </el-tab-pane>

      <!-- 对象层浏览 -->
      <el-tab-pane label="对象层" name="object">
        <div class="d2a-card">
          <h3 class="card-title">对象目录</h3>
          <LoadingState v-if="objCatalog.status === 'idle' || objCatalog.status === 'loading'" />
          <ErrorState v-else-if="objCatalog.status === 'error'" :error="objCatalog.error" @retry="store.refreshRawCatalog()" />
          <EmptyState v-else-if="objCatalog.data.length === 0" title="没有对象" />
          <template v-else>
            <p v-if="objCatalogRefreshError" class="refresh-warning" data-testid="obj-catalog-refresh-error">
              刷新失败({{ objCatalogRefreshError.message }}),展示上一次成功数据
            </p>
            <el-table :data="objCatalog.data" size="small" data-testid="obj-catalog">
              <el-table-column prop="display_name" label="对象" width="130" />
              <el-table-column prop="object" label="object" width="150" />
              <el-table-column label="行数" width="90">
                <template #default="{ row }">{{ row.rows ?? '不可检测' }}</template>
              </el-table-column>
              <el-table-column label="物化时间" width="150">
                <template #default="{ row }">{{ formatDateTime(row.mapped_at) }}</template>
              </el-table-column>
              <el-table-column label="隔离" width="80">
                <template #default="{ row }">
                  <StatusBadge :status="row.quarantined > 0 ? 'warning' : 'healthy'" />
                </template>
              </el-table-column>
              <el-table-column label="状态">
                <template #default="{ row }">{{ row.warning ?? '' }}</template>
              </el-table-column>
              <el-table-column width="90">
                <template #default="{ row }">
                  <el-button
                    size="small"
                    text
                    :data-testid="`browse-${row.object}`"
                    @click="selectObject(row.object)"
                  >
                    浏览
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </div>

        <div v-if="objPage" class="d2a-card">
          <div class="toolbar">
            <el-input
              v-model="objQuery.q"
              :placeholder="objPage.status === 'success' && objPage.data.searchable ? '按业务键搜索' : '该资源没有可搜索业务键'"
              size="small"
              clearable
              class="toolbar__search"
              :disabled="!objSearchable"
              data-testid="obj-search"
              @change="searchObject()"
            />
            <el-button size="small" data-testid="obj-refresh" @click="store.browseObject()">刷新</el-button>
            <span class="toolbar__meta">
              <template v-if="objPage.status === 'success'">
                {{ objSel }} · 共 {{ objPage.data.total }} 行 · 排序 {{ objPage.data.sort }}
              </template>
            </span>
          </div>
          <LoadingState v-if="objPage.status === 'loading'" />
          <ErrorState v-else-if="objPage.status === 'error'" :error="objPage.error" @retry="store.browseObject()" />
          <template v-else-if="objPage.status === 'success'">
            <p v-if="objPageRefreshError" class="refresh-warning" data-testid="obj-page-refresh-error">
              刷新失败({{ objPageRefreshError.message }}),展示上一次成功数据
            </p>
            <ul v-if="objPage.data.warnings.length" class="warnings" data-testid="obj-warnings">
              <li v-for="w in objPage.data.warnings" :key="w">{{ w }}</li>
            </ul>
            <el-table :data="objPage.data.rows" size="small" data-testid="obj-table">
              <el-table-column
                v-for="col in objCols"
                :key="col.name"
                :prop="col.name"
                min-width="120"
              >
                <template #header>
                  <span>{{ col.name }}</span>
                  <el-tag v-if="col.classification === 'sensitive'" size="small" type="warning" class="col-flag">
                    脱敏
                  </el-tag>
                  <el-tag v-else-if="col.classification === 'unknown'" size="small" type="info" class="col-flag">
                    未知
                  </el-tag>
                </template>
                <template #default="{ row }">{{ formatCell(row[col.name]) }}</template>
              </el-table-column>
            </el-table>
            <p v-if="objPage.data.truncations.length" class="trunc-note" data-testid="obj-truncations">
              {{ objPage.data.truncations.length }} 行存在截断字段(预览不是完整值):
              {{ objPage.data.truncations.map((t) => `#${t.row_index}(${t.fields.join('/')})`).join(', ') }}
            </p>
            <el-pagination
              class="pager"
              layout="prev, pager, next"
              :total="objPage.data.total"
              :page-size="objQuery.limit"
              :current-page="objQuery.offset / objQuery.limit + 1"
              data-testid="obj-pager"
              @current-change="onObjPage"
            />
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>

    <div v-if="currentJson" class="d2a-card">
      <el-button size="small" text data-testid="json-toggle" @click="showJson = !showJson">
        {{ showJson ? '隐藏' : '查看' }}安全 JSON(与表格同源)
      </el-button>
      <pre
        v-if="showJson"
        class="json-view"
        data-testid="json-view"
      >{{ JSON.stringify(currentJson, null, 2) }}</pre>
    </div>
  </section>
</template>

<style scoped>
.data-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.card-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}

.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.toolbar__search {
  width: 240px;
}

.toolbar__meta {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.security-guide p {
  margin: 0;
  font-size: 13px;
  color: var(--d2a-text-secondary);
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

.col-flag {
  margin-left: 4px;
}

.trunc-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.pager {
  margin-top: 10px;
  justify-content: flex-end;
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
