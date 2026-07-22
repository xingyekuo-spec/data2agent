<script setup lang="ts">
// 数据浏览(M4+M2):Raw/Object/数据集 三 tab。
// Raw/Object:目录驱动、服务端列驱动表格、业务键搜索、脱敏/截断、同源安全 JSON。
// 数据集:版本列表、building-ready/failed/published/retired、publish/rollback、stage-only apply。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import ObjectLineageDrawer from '@/components/shared/ObjectLineageDrawer.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useDataStore } from '@/stores/data'
import { useDatasetsStore } from '@/stores/datasets'
import { useLineageStore } from '@/stores/lineage'
import {
  canPublish,
  canRollback,
  rollbackTarget,
  datasetStatusLabel,
} from '@/utils/datasetStatus'
import { formatDateTime } from '@/utils/time'

const store = useDataStore()
const datasetsStore = useDatasetsStore()
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
const { rawSel, rawQuery, objQuery } = store
const { filters: datasetFilters, page: datasetPage } = datasetsStore
const route = useRoute()
const router = useRouter()
const lineageStore = useLineageStore()
const lineageDrawerVisible = ref(false)

function openLineage(rowIndex: number) {
  const page = objPage.value
  if (page?.status !== 'success') return
  const refs = page.data.lineage_refs
  const ref_ = refs?.[rowIndex]
  if (!ref_ || !ref_.key_token) return
  lineageStore.setTarget(page.data.object, ref_.key_token)
  lineageDrawerVisible.value = true
  void lineageStore.load()
}

function closeLineage() {
  lineageDrawerVisible.value = false
}

const hasLineageRefs = computed(() => {
  const page = objPage.value
  return page?.status === 'success'
    && Array.isArray(page.data.lineage_refs)
    && page.data.lineage_refs.length > 0
})

// el-tabs 默认激活 pane 行为不明确,显式固定为 Raw
const activeTab = ref('raw')
const showJson = ref(false)
const stageOnly = ref(false)
const applySource = ref('digiwin_e10')
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
  if (activeTab.value === 'object') {
    return objPage.value?.status === 'success' ? objPage.value.data : null
  }
  return datasetDetail.value?.status === 'success' ? datasetDetail.value.data : null
})

const raw403 = computed(
  () =>
    (rawCatalog.value.status === 'error' && rawCatalog.value.error.status === 403)
    || (rawPage.value?.status === 'error' && rawPage.value.error.status === 403)
    || rawPageRefreshError.value?.status === 403,
)

const datasetDetailObjects = computed(() =>
  datasetDetail.value?.status === 'success' ? (datasetDetail.value.data.objects ?? []) : [],
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

function onDatasetPage(current: number): void {
  datasetPage.offset = (current - 1) * datasetPage.limit
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

function applyRouteQuery(query: LocationQuery, browse: boolean): void {
  restoringQuery = true
  const tab = firstString(query.tab)
  activeTab.value = tab === 'object' || tab === 'datasets' ? tab : 'raw'

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

  const nextDsVersion = activeTab.value === 'datasets' ? firstString(query.version) : ''
  const nextDsOffset = activeTab.value === 'datasets' ? pageOffset(query.page, datasetPage.limit) : 0
  if (datasetPage.offset !== nextDsOffset) {
    datasetPage.offset = nextDsOffset
  }
  restoringQuery = false
  if (browse) {
    if (browseRaw && activeTab.value === 'raw') {
      void store.browseRaw()
    }
    if (browseObject && activeTab.value === 'object') {
      void store.browseObject()
    }
    if (activeTab.value === 'datasets') {
      void datasetsStore.refresh()
      if (nextDsVersion) {
        void datasetsStore.openDetail(nextDsVersion)
      }
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
  if (activeTab.value === 'datasets') {
    void router.replace({
      query: {
        tab: 'datasets',
        ...(datasetsStore.detailVersion ? { version: datasetsStore.detailVersion } : {}),
        ...(pageQuery(datasetPage.offset, datasetPage.limit) ? {
          page: pageQuery(datasetPage.offset, datasetPage.limit),
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

function onTabChange(): void {
  if (activeTab.value === 'datasets') {
    void datasetsStore.refresh()
  }
  syncRouteQuery()
}

function openDataset(version: string): void {
  void datasetsStore.openDetail(version)
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
    <el-tabs v-model="activeTab" data-testid="data-tabs" @tab-change="onTabChange">
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
              <el-table-column label="版本" width="160">
                <template #default="{ row }">
                  <span v-if="row.version" data-testid="obj-version">{{ row.version }}</span>
                  <span v-else class="version-na" data-testid="obj-version-na">尚未发布</span>
                </template>
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
              <el-table-column v-if="hasLineageRefs" label="血缘" width="80" data-testid="obj-lineage-col">
                <template #default="{ $index }">
                  <el-button
                    size="small"
                    text
                    :data-testid="`lineage-btn-${$index}`"
                    @click="openLineage($index)"
                  >
                    血缘
                  </el-button>
                </template>
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

      <!-- 数据集版本 -->
      <el-tab-pane label="数据集" name="datasets">
        <div class="d2a-card">
          <h3 class="card-title">构建 / 发布</h3>
          <div class="toolbar" data-testid="dataset-apply-bar">
            <el-input
              v-model="applySource"
              size="small"
              class="toolbar__search"
              placeholder="source"
              data-testid="apply-source"
            />
            <label class="inline" data-testid="stage-only-toggle">
              <input v-model="stageOnly" type="checkbox" />
              仅构建不发布(stage-only)
            </label>
            <el-button type="primary" size="small" data-testid="apply-run" @click="onApply">
              {{ stageOnly ? '构建候选' : '构建并发布' }}
            </el-button>
          </div>
          <p v-if="applyError" class="refresh-warning" data-testid="apply-error">
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
          <div class="toolbar">
            <el-select
              v-model="datasetFilters.status"
              placeholder="状态"
              clearable
              size="small"
              data-testid="dataset-filter-status"
              @change="datasetsStore.refresh()"
            >
              <el-option label="building" value="building" />
              <el-option label="published" value="published" />
              <el-option label="failed" value="failed" />
              <el-option label="retired" value="retired" />
            </el-select>
            <el-button size="small" data-testid="datasets-refresh" @click="datasetsStore.refresh()">
              刷新
            </el-button>
            <span class="toolbar__meta">共 {{ datasetTotal }} 个版本</span>
          </div>
          <p v-if="listRefreshError" class="refresh-warning" data-testid="datasets-refresh-error">
            刷新失败({{ listRefreshError.message }}),展示上一次成功数据
          </p>
          <p v-if="actionError" class="refresh-warning" data-testid="dataset-action-error">
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
            <el-table :data="datasetList.data" size="small" data-testid="datasets-table">
              <el-table-column prop="dataset_version" label="版本" min-width="200" />
              <el-table-column prop="source" label="来源" width="130" />
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <span :data-testid="`dataset-status-${row.dataset_version}`">
                    {{ datasetStatusLabel(row, objectsByVersion[row.dataset_version]) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="构建时间" width="150">
                <template #default="{ row }">{{ formatDateTime(row.built_at) }}</template>
              </el-table-column>
              <el-table-column label="发布时间" width="150">
                <template #default="{ row }">{{ formatDateTime(row.published_at) || '—' }}</template>
              </el-table-column>
              <el-table-column label="上一版本" width="160">
                <template #default="{ row }">{{ row.previous_dataset_version ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="操作" width="220" fixed="right">
                <template #default="{ row }">
                  <el-button
                    size="small"
                    text
                    :data-testid="`dataset-detail-${row.dataset_version}`"
                    @click="openDataset(row.dataset_version)"
                  >
                    详情
                  </el-button>
                  <el-button
                    v-if="canPublish(row, objectsByVersion[row.dataset_version])"
                    size="small"
                    type="primary"
                    text
                    :data-testid="`dataset-publish-${row.dataset_version}`"
                    @click="onPublish(row.dataset_version)"
                  >
                    发布
                  </el-button>
                  <el-button
                    v-if="canRollback(row)"
                    size="small"
                    type="warning"
                    text
                    :data-testid="`dataset-rollback-${row.dataset_version}`"
                    @click="onRollback(row)"
                  >
                    回滚
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-pagination
              class="pager"
              layout="prev, pager, next"
              :total="datasetTotal"
              :page-size="datasetPage.limit"
              :current-page="datasetPage.offset / datasetPage.limit + 1"
              data-testid="datasets-pager"
              @current-change="onDatasetPage"
            />
          </template>
        </div>

        <div v-if="datasetDetail" class="d2a-card" data-testid="dataset-detail">
          <LoadingState v-if="datasetDetail.status === 'loading'" />
          <ErrorState
            v-else-if="datasetDetail.status === 'error'"
            :error="datasetDetail.error"
            @retry="datasetsStore.detailVersion && datasetsStore.openDetail(datasetsStore.detailVersion)"
          />
          <template v-else-if="datasetDetail.status === 'success'">
            <p v-if="detailRefreshError" class="refresh-warning">
              刷新失败({{ detailRefreshError.message }})
            </p>
            <h3 class="card-title">
              {{ datasetDetail.data.dataset_version }}
              · {{ datasetStatusLabel(datasetDetail.data, datasetDetailObjects) }}
            </h3>
            <p class="toolbar__meta">
              template {{ datasetDetail.data.template_version }}
              · previous {{ datasetDetail.data.previous_dataset_version ?? '—' }}
              · error {{ datasetDetail.data.error ?? '—' }}
            </p>
            <el-table :data="datasetDetailObjects" size="small" data-testid="dataset-objects-table">
              <el-table-column prop="object" label="对象" width="140" />
              <el-table-column prop="object_version" label="对象版本" min-width="140" />
              <el-table-column prop="status" label="状态" width="100" />
              <el-table-column prop="row_count" label="行数" width="80" />
              <el-table-column prop="binding_hash" label="binding_hash" min-width="180" />
            </el-table>
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

    <ObjectLineageDrawer
      :visible="lineageDrawerVisible"
      data-testid="obj-lineage-drawer"
      @close="closeLineage"
    />
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

.version-na {
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
