<script setup lang="ts">
// 对象数据(原数据浏览 对象层 tab):对象目录 + 业务键搜索浏览 + 血缘抽屉 + 安全 JSON。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import Pager from '@/components/shared/Pager.vue'
import ObjectLineageDrawer from '@/components/shared/ObjectLineageDrawer.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useDataStore } from '@/stores/data'
import { useLineageStore } from '@/stores/lineage'
import { formatDateTime } from '@/utils/time'
import { formatCell } from '@/utils/format'

const store = useDataStore()
const {
  objCatalog,
  objCatalogRefreshError,
  objSel,
  objPage,
  objPageRefreshError,
} = storeToRefs(store)
const { objQuery } = store
const route = useRoute()
const router = useRouter()
const lineageStore = useLineageStore()
const lineageDrawerVisible = ref(false)
const showJson = ref(false)
let restoringQuery = false

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

const objCols = computed(() =>
  objPage.value?.status === 'success' ? objPage.value.data.columns : [],
)
const objSearchable = computed(() =>
  objPage.value?.status !== 'success' || objPage.value.data.searchable,
)
const currentJson = computed(() =>
  objPage.value?.status === 'success' ? objPage.value.data : null,
)

function onObjPage(offset: number, limit: number): void {
  objQuery.offset = offset
  objQuery.limit = limit
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
  let browseObject = false
  const nextObj = firstString(query.object)
  const nextObjQ = firstString(query.q)
  const nextObjOffset = pageOffset(query.page, objQuery.limit)
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
  if (browse && browseObject) {
    void store.browseObject()
  }
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  void router.replace({
    query: {
      ...(objSel.value ? { object: objSel.value } : {}),
      ...(objQuery.q ? { q: objQuery.q } : {}),
      ...(pageQuery(objQuery.offset, objQuery.limit) ? {
        page: pageQuery(objQuery.offset, objQuery.limit),
      } : {}),
    },
  })
}

function selectObject(object: string): void {
  store.selectObject(object)
  syncRouteQuery()
}

function searchObject(): void {
  store.searchObject()
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
    <div class="d2a-card">
      <h3 class="card-title">
        对象目录
      </h3>
      <LoadingState v-if="objCatalog.status === 'idle' || objCatalog.status === 'loading'" />
      <ErrorState
        v-else-if="objCatalog.status === 'error'"
        :error="objCatalog.error"
        @retry="store.refreshRawCatalog()"
      />
      <EmptyState
        v-else-if="objCatalog.data.length === 0"
        title="没有对象"
      />
      <template v-else>
        <p
          v-if="objCatalogRefreshError"
          class="refresh-warning"
          data-testid="obj-catalog-refresh-error"
        >
          刷新失败({{ objCatalogRefreshError.message }}),展示上一次成功数据
        </p>
        <el-table
          :data="objCatalog.data"
          size="small"
          data-testid="obj-catalog"
        >
          <el-table-column
            prop="display_name"
            label="对象"
            width="130"
          />
          <el-table-column
            prop="object"
            label="object"
            width="150"
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
            label="版本"
            width="160"
          >
            <template #default="{ row }">
              <span
                v-if="row.version"
                data-testid="obj-version"
              >{{ row.version }}</span>
              <span
                v-else
                class="version-na"
                data-testid="obj-version-na"
              >尚未发布</span>
            </template>
          </el-table-column>
          <el-table-column
            label="物化时间"
            width="150"
          >
            <template #default="{ row }">
              {{ formatDateTime(row.mapped_at) }}
            </template>
          </el-table-column>
          <el-table-column
            label="隔离"
            width="80"
          >
            <template #default="{ row }">
              <StatusBadge :status="row.quarantined > 0 ? 'warning' : 'healthy'" />
            </template>
          </el-table-column>
          <el-table-column label="状态">
            <template #default="{ row }">
              {{ row.warning ?? '' }}
            </template>
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

    <div
      v-if="objPage"
      class="d2a-card d2a-toolbar"
    >
      <el-input
        v-model="objQuery.q"
        :placeholder="objPage.status === 'success' && objPage.data.searchable ? '按业务键搜索' : '该资源没有可搜索业务键'"
        size="small"
        clearable
        :disabled="!objSearchable"
        data-testid="obj-search"
        @change="searchObject()"
      />
      <span
        v-if="objPage.status === 'success'"
        class="toolbar-hint"
      >
        {{ objSel }} · 排序 {{ objPage.data.sort }}
      </span>
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="obj-refresh"
          @click="store.browseObject()"
        >
          刷新
        </el-button>
      </div>
    </div>
    <div
      v-if="objPage"
      class="d2a-card"
    >
      <LoadingState v-if="objPage.status === 'loading'" />
      <ErrorState
        v-else-if="objPage.status === 'error'"
        :error="objPage.error"
        @retry="store.browseObject()"
      />
      <template v-else-if="objPage.status === 'success'">
        <p
          v-if="objPageRefreshError"
          class="refresh-warning"
          data-testid="obj-page-refresh-error"
        >
          刷新失败({{ objPageRefreshError.message }}),展示上一次成功数据
        </p>
        <ul
          v-if="objPage.data.warnings.length"
          class="warnings"
          data-testid="obj-warnings"
        >
          <li
            v-for="w in objPage.data.warnings"
            :key="w"
          >
            {{ w }}
          </li>
        </ul>
        <el-table
          :data="objPage.data.rows"
          size="small"
          data-testid="obj-table"
        >
          <el-table-column
            v-for="col in objCols"
            :key="col.name"
            :prop="col.name"
            min-width="120"
          >
            <template #header>
              <span>{{ col.name }}</span>
              <el-tag
                v-if="col.classification === 'sensitive'"
                size="small"
                type="warning"
                class="col-flag"
              >
                脱敏
              </el-tag>
              <el-tag
                v-else-if="col.classification === 'unknown'"
                size="small"
                type="info"
                class="col-flag"
              >
                未知
              </el-tag>
            </template>
            <template #default="{ row }">
              {{ formatCell(row[col.name]) }}
            </template>
          </el-table-column>
          <el-table-column
            v-if="hasLineageRefs"
            label="血缘"
            width="80"
            data-testid="obj-lineage-col"
          >
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
        <p
          v-if="objPage.data.truncations.length"
          class="trunc-note"
          data-testid="obj-truncations"
        >
          {{ objPage.data.truncations.length }} 行存在截断字段(预览不是完整值):
          {{ objPage.data.truncations.map((t) => `#${t.row_index}(${t.fields.join('/')})`).join(', ') }}
        </p>
        <Pager
          :total="objPage.data.total"
          :limit="objQuery.limit"
          :offset="objQuery.offset"
          data-testid="obj-pager"
          @change="onObjPage"
        />
      </template>
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

.version-na {
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
