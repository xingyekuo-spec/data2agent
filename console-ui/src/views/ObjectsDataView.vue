<script setup lang="ts">
// 对象数据(原数据浏览 对象层 tab):对象目录 + 浏览抽屉(搜索/分页/血缘/安全 JSON)。
// 详情规范(05-console §3.2):行点击开右侧抽屉,安全 JSON 折叠在抽屉内。
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import ObjectDataDrawer from '@/components/shared/ObjectDataDrawer.vue'
import ObjectLineageDrawer from '@/components/shared/ObjectLineageDrawer.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useDataStore } from '@/stores/data'
import { useLineageStore } from '@/stores/lineage'
import { formatDateTime } from '@/utils/time'

const store = useDataStore()
const { objCatalog, objCatalogRefreshError, objSel, objPage } = storeToRefs(store)
const route = useRoute()
const router = useRouter()
const lineageStore = useLineageStore()
const objDrawerVisible = ref(false)
const lineageDrawerVisible = ref(false)
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
  const nextObjOffset = pageOffset(query.page, store.objQuery.limit)
  const objResourceChanged = objSel.value !== nextObj
  if (objSel.value !== nextObj || store.objQuery.q !== nextObjQ
    || store.objQuery.offset !== nextObjOffset) {
    objSel.value = nextObj
    store.objQuery.q = nextObjQ
    store.objQuery.offset = nextObjOffset
    if (objResourceChanged || !nextObj) {
      objPage.value = null
    }
    browseObject = Boolean(nextObj)
  }
  objDrawerVisible.value = Boolean(nextObj)
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
      ...(store.objQuery.q ? { q: store.objQuery.q } : {}),
      ...(pageQuery(store.objQuery.offset, store.objQuery.limit) ? {
        page: pageQuery(store.objQuery.offset, store.objQuery.limit),
      } : {}),
    },
  })
}

function selectObject(object: string): void {
  store.selectObject(object)
  objDrawerVisible.value = true
  syncRouteQuery()
}

function closeObjDrawer(): void {
  objDrawerVisible.value = false
  syncRouteQuery()
}

function onRefresh(): void {
  void store.refreshRawCatalog()
  if (objSel.value) {
    void store.browseObject()
  }
}

onMounted(() => {
  applyRouteQuery(route.query, true)
  void store.refreshRawCatalog()
})

watch(() => route.query, (query) => applyRouteQuery(query, true))
</script>

<template>
  <section class="data-page d2a-page-flush">
    <!-- 通栏工具栏(A 类规范):左提示、右操作;对象数据详情在抽屉内 -->
    <div class="d2a-card d2a-toolbar">
      <span class="toolbar-hint">对象目录(点击行浏览对象数据)</span>
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="obj-refresh"
          @click="onRefresh"
        >
          刷新
        </el-button>
      </div>
    </div>

    <div class="d2a-card">
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
          @row-click="(row: { object: string }) => selectObject(row.object)"
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
        </el-table>
      </template>
    </div>

    <ObjectDataDrawer
      :visible="objDrawerVisible"
      data-testid="obj-data-drawer"
      @close="closeObjDrawer"
      @open-lineage="openLineage"
    />

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

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}
</style>
