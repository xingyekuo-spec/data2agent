<script setup lang="ts">
/**
 * Raw 数据浏览抽屉：搜索 / 表格 / 分页 / 截断标记。
 * 警告默认折叠，减少对数据的遮挡。
 */
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import LoadingState from '@/components/shared/LoadingState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import { useDataStore } from '@/stores/data'
import type { ApiError } from '@/api/errors'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: [] }>()

const store = useDataStore()
const { rawSel, rawQuery } = store
const { rawPage, rawPageRefreshError } = storeToRefs(store)

const showWarnings = ref(false)

const rawCols = computed(() =>
  rawPage.value?.status === 'success' ? rawPage.value.data.columns : [],
)

// Only report page-level error on first-load or auth failure.
const isFirstError = computed(
  () => rawPage.value?.status === 'error',
)

const isLoading = computed(
  () => rawPage.value?.status === 'loading' || rawPage.value === null,
)

const successData = computed(() =>
  rawPage.value?.status === 'success' ? rawPage.value.data : null,
)

const totalRows = computed(() => successData.value?.total ?? 0)
const currentPage = computed(() => rawQuery.offset / rawQuery.limit + 1)

type CellValue = string | number | boolean | null | { __blob__?: boolean; bytes?: number }

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object' && value !== null && (value as { __blob__?: boolean }).__blob__) {
    const blob = value as { bytes?: number }
    return `[BLOB ${blob.bytes ?? '?'} bytes]`
  }
  return String(value as CellValue)
}

function onPageChange(current: number): void {
  rawQuery.offset = (current - 1) * rawQuery.limit
  void store.browseRaw()
}

function onRefresh(): void {
  void store.browseRaw()
}

function onSearch(): void {
  void store.browseRaw()
}
</script>

<template>
  <el-drawer
    :model-value="props.visible"
    direction="rtl"
    size="70%"
    :close-on-click-modal="true"
    :destroy-on-close="false"
    data-testid="raw-drawer"
    @close="emit('close')"
  >
    <template #header>
      <div class="drawer-header">
        <span class="drawer-title">Raw 数据浏览</span>
        <span v-if="successData" class="drawer-source">
          {{ rawSel.source }} / {{ rawSel.table }}
        </span>
      </div>
    </template>

    <!-- 工具栏 -->
    <div class="drawer-toolbar">
      <el-input
        :model-value="rawQuery.q"
        :placeholder="successData?.searchable ? '按业务键搜索' : '该资源没有可搜索业务键'"
        size="small"
        clearable
        class="toolbar-search"
        :disabled="!successData?.searchable"
        data-testid="raw-drawer-search"
        @change="rawQuery.q = $event; onSearch()"
      />
      <el-button size="small" data-testid="raw-drawer-refresh" @click="onRefresh">
        刷新
      </el-button>
      <span v-if="successData" class="toolbar-meta">
        共 {{ successData.total }} 行 · 排序 {{ successData.sort }}
      </span>
    </div>

    <LoadingState v-if="isLoading" />

    <ErrorState
      v-else-if="isFirstError"
      :error="rawPage.error"
      @retry="onRefresh"
    />

    <template v-else-if="successData">
      <!-- 刷新错误 -->
      <p
        v-if="rawPageRefreshError"
        class="refresh-note"
        data-testid="raw-drawer-refresh-error"
      >
        刷新失败({{ rawPageRefreshError.message }}),展示上一次成功数据
      </p>

      <!-- 折叠警告 -->
      <div v-if="successData.warnings.length" class="warnings-block">
        <button
          class="warnings-toggle"
          data-testid="raw-drawer-warnings-toggle"
          @click="showWarnings = !showWarnings"
        >
          <span class="toggle-icon">{{ showWarnings ? '▾' : '▸' }}</span>
          {{ successData.warnings.length }} 条列分类警告
          <span class="toggle-hint">{{ showWarnings ? '收起' : '展开' }}</span>
        </button>
        <ul v-if="showWarnings" class="warnings-list" data-testid="raw-drawer-warnings">
          <li v-for="w in successData.warnings" :key="w">{{ w }}</li>
        </ul>
      </div>

      <!-- 表格 -->
      <el-table :data="successData.rows" size="small" data-testid="raw-drawer-table">
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

      <!-- 截断提示 -->
      <p
        v-if="successData.truncations.length"
        class="trunc-note"
        data-testid="raw-drawer-truncations"
      >
        {{ successData.truncations.length }} 行存在截断字段(预览不是完整值):
        {{ successData.truncations.map((t: { row_index: number; fields: string[] }) => `#${t.row_index}(${t.fields.join('/')})`).join(', ') }}
      </p>

      <!-- 分页 -->
      <el-pagination
        v-if="totalRows > rawQuery.limit"
        class="drawer-pager"
        layout="prev, pager, next"
        :total="totalRows"
        :page-size="rawQuery.limit"
        :current-page="currentPage"
        data-testid="raw-drawer-pager"
        @current-change="onPageChange"
      />
    </template>

    <el-empty v-else description="选择一张 raw 表后点击「浏览」查看数据" />
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

/* 折叠警告 */
.warnings-block {
  margin-bottom: 10px;
}
.warnings-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--d2a-status-stale);
  background: var(--el-fill-color-light);
  border: 1px solid var(--d2a-border);
  border-radius: 4px;
  padding: 4px 10px;
  cursor: pointer;
  transition: background 0.15s;
  font-family: inherit;
}
.warnings-toggle:hover {
  background: var(--el-fill-color);
}
.toggle-icon {
  font-size: 10px;
  width: 12px;
  text-align: center;
}
.toggle-hint {
  margin-left: 8px;
  color: var(--d2a-text-secondary);
  font-size: 11px;
}
.warnings-list {
  margin: 6px 0 0;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--d2a-status-stale);
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--d2a-status-warning);
  list-style: none;
  border-radius: 4px;
  max-height: 200px;
  overflow-y: auto;
}

.col-flag {
  margin-left: 4px;
}

.trunc-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.drawer-pager {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
