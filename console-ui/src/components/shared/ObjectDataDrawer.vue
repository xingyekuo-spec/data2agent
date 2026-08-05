<script setup lang="ts">
/**
 * 对象数据浏览抽屉:搜索 / 表格 / 分页 / 截断标记 / 血缘入口 / 安全 JSON。
 * 与 RawDataDrawer 对称(05-console §3.2):详情一律右侧抽屉,
 * 安全 JSON 折叠在抽屉内底部;血缘入口经 open-lineage 事件交父视图处理。
 */
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import LoadingState from '@/components/shared/LoadingState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import PagerBar from '@/components/shared/PagerBar.vue'
import { useDataStore } from '@/stores/data'
import { formatCell } from '@/utils/format'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; 'open-lineage': [rowIndex: number] }>()

const store = useDataStore()
const { objQuery } = store
// objSel/objPage 是 ref,必须经 storeToRefs 保持响应式(直接解构会拿到快照)
const { objSel, objPage, objPageRefreshError } = storeToRefs(store)

const showJson = ref(false)

const successData = computed(() =>
  objPage.value?.status === 'success' ? objPage.value.data : null,
)
const isLoading = computed(
  () => objPage.value?.status === 'loading' || objPage.value === null,
)
const isFirstError = computed(() => objPage.value?.status === 'error')

const objCols = computed(() => successData.value?.columns ?? [])
const objSearchable = computed(() => successData.value?.searchable !== false)
const hasLineageRefs = computed(() =>
  Array.isArray(successData.value?.lineage_refs)
  && (successData.value?.lineage_refs.length ?? 0) > 0,
)

function onSearch(): void {
  store.searchObject()
}

function onRefresh(): void {
  void store.browseObject()
}

function onPagerChange(offset: number, limit: number): void {
  objQuery.offset = offset
  objQuery.limit = limit
  void store.browseObject()
}
</script>

<template>
  <el-drawer
    :model-value="props.visible"
    direction="rtl"
    size="70%"
    :close-on-click-modal="true"
    :destroy-on-close="false"
    data-testid="obj-drawer"
    @close="emit('close')"
  >
    <template #header>
      <div class="drawer-header">
        <span class="drawer-title">对象数据浏览</span>
        <span
          v-if="objSel"
          class="drawer-source"
          data-testid="obj-drawer-target"
        >
          {{ objSel }}
        </span>
      </div>
    </template>

    <!-- 工具栏 -->
    <div class="drawer-toolbar">
      <el-input
        v-model="objQuery.q"
        :placeholder="objSearchable ? '按业务键搜索' : '该资源没有可搜索业务键'"
        size="small"
        clearable
        class="toolbar-search"
        :disabled="!objSel || !objSearchable"
        data-testid="obj-search"
        @change="onSearch()"
      />
      <el-button
        size="small"
        data-testid="obj-drawer-refresh"
        @click="onRefresh"
      >
        刷新
      </el-button>
      <span
        v-if="successData"
        class="toolbar-meta"
      >
        共 {{ successData.total }} 行 · 排序 {{ successData.sort }}
      </span>
    </div>

    <LoadingState v-if="isLoading" />

    <ErrorState
      v-else-if="isFirstError"
      :error="(objPage as any).error"
      @retry="onRefresh"
    />

    <template v-else-if="successData">
      <p
        v-if="objPageRefreshError"
        class="refresh-note"
        data-testid="obj-page-refresh-error"
      >
        刷新失败({{ objPageRefreshError.message }}),展示上一次成功数据
      </p>

      <ul
        v-if="successData.warnings.length"
        class="warnings-list"
        data-testid="obj-warnings"
      >
        <li
          v-for="w in successData.warnings"
          :key="w"
        >
          {{ w }}
        </li>
      </ul>

      <el-table
        :data="successData.rows"
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
              @click="emit('open-lineage', $index)"
            >
              血缘
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <p
        v-if="successData.truncations.length"
        class="trunc-note"
        data-testid="obj-truncations"
      >
        {{ successData.truncations.length }} 行存在截断字段(预览不是完整值):
        {{ successData.truncations.map((t) => `#${t.row_index}(${t.fields.join('/')})`).join(', ') }}
      </p>

      <PagerBar
        :total="successData.total"
        :limit="objQuery.limit"
        :offset="objQuery.offset"
        data-testid="obj-pager"
        @change="onPagerChange"
      />

      <!-- 安全 JSON(与表格同源,折叠在抽屉底部) -->
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
      >{{ JSON.stringify(successData, null, 2) }}</pre>
    </template>

    <el-empty
      v-else
      description="点击目录行浏览对象数据"
    />
  </el-drawer>
</template>

<style scoped>
.drawer-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.drawer-title {
  font-weight: 600;
}
.drawer-source {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  font-family: monospace;
}

.drawer-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.toolbar-search {
  width: 240px;
  flex-shrink: 0;
}
.toolbar-meta {
  font-size: 12px;
  color: var(--d2a-text-secondary);
  margin-left: auto;
}

.refresh-note {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.warnings-list {
  margin: 0 0 8px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--d2a-status-stale);
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
  list-style: none;
  border-radius: 4px;
}

.col-flag {
  margin-left: 4px;
}

.trunc-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.json-toggle {
  margin-top: 12px;
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
