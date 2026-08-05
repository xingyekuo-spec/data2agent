<script setup lang="ts">
// 本体库 · 属性(A 类):全量属性数据字典(敏感/类型/所属类筛选),治理入口。
// 数据从 /api/templates 前端派生平铺;客户端筛选 + PagerBar 分页。
// 详情规范(05-console §3.2):行点击开右侧抽屉,安全 JSON 折叠在抽屉内。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import PagerBar from '@/components/shared/PagerBar.vue'
import PropertyDetailDrawer from '@/components/ontology/PropertyDetailDrawer.vue'
import { useTemplatesStore } from '@/stores/templates'
import type { components } from '@/types/api'

type TemplateObject = components['schemas']['TemplateObject']

interface PropertyRow {
  name: string
  type: string
  desc: string | null
  sensitive: boolean
  ref: string | null
  enum_values: string[]
  object: string
  objectDisplay: string
  domain: string
}

const store = useTemplatesStore()
const { templates, templatesRefreshError } = storeToRefs(store)
const route = useRoute()
const router = useRouter()
const filters = reactive({ owner: '', type: '', sensitive: '', q: '' })
const page = reactive({ offset: 0, limit: 50 })
const selected = ref<{ owner: string; name: string } | null>(null)
let restoringQuery = false

const objects = computed(() =>
  templates.value.status === 'success' ? templates.value.data : [],
)

const rows = computed<PropertyRow[]>(() =>
  objects.value.flatMap((o) =>
    o.properties.map((p) => ({
      name: p.name,
      type: p.type,
      desc: p.desc ?? null,
      sensitive: p.sensitive,
      ref: p.ref ?? null,
      enum_values: p.enum_values ?? [],
      object: o.object,
      objectDisplay: o.display_name,
      domain: o.domain ?? '未分组',
    }))),
)

const ownerOptions = computed(() =>
  objects.value.map((o) => ({ value: o.object, label: `${o.display_name}(${o.object})` })),
)
const typeOptions = computed(() => [...new Set(rows.value.map((r) => r.type))].sort())

const filteredRows = computed(() => {
  const q = filters.q.trim().toLowerCase()
  return rows.value.filter((r) =>
    (!filters.owner || r.object === filters.owner)
    && (!filters.type || r.type === filters.type)
    && (!filters.sensitive
      || (filters.sensitive === 'yes' ? r.sensitive : !r.sensitive))
    && (!q || `${r.name} ${r.desc ?? ''} ${r.object}`.toLowerCase().includes(q)),
  )
})

const pagedRows = computed(() =>
  filteredRows.value.slice(page.offset, page.offset + page.limit),
)

const selectedObject = computed<TemplateObject | null>(() =>
  objects.value.find((o) => o.object === selected.value?.owner) ?? null,
)

function firstString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function pageOffset(value: unknown): number {
  const pageNo = Number(firstString(value))
  return Number.isFinite(pageNo) && pageNo > 1 ? (pageNo - 1) * page.limit : 0
}

function applyRouteQuery(query: LocationQuery): void {
  restoringQuery = true
  filters.owner = firstString(query.class)
  filters.type = firstString(query.type)
  filters.sensitive = firstString(query.sensitive)
  filters.q = firstString(query.q)
  page.offset = pageOffset(query.page)
  const owner = firstString(query.owner)
  const prop = firstString(query.prop)
  selected.value = owner && prop ? { owner, name: prop } : null
  restoringQuery = false
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  const pageNo = page.offset / page.limit + 1
  void router.replace({
    query: {
      ...(filters.owner ? { class: filters.owner } : {}),
      ...(filters.type ? { type: filters.type } : {}),
      ...(filters.sensitive ? { sensitive: filters.sensitive } : {}),
      ...(filters.q ? { q: filters.q } : {}),
      ...(pageNo > 1 ? { page: String(pageNo) } : {}),
      ...(selected.value ? { owner: selected.value.owner, prop: selected.value.name } : {}),
    },
  })
}

function onFilterChange(): void {
  page.offset = 0
  syncRouteQuery()
}

function onPagerChange(offset: number, limit: number): void {
  page.offset = offset
  page.limit = limit
  syncRouteQuery()
}

function openRow(row: PropertyRow): void {
  selected.value = { owner: row.object, name: row.name }
  syncRouteQuery()
}

function closeDrawer(): void {
  selected.value = null
  syncRouteQuery()
}

function viewOwnerClass(): void {
  if (selected.value) {
    void router.push({ path: '/ontology/classes', query: { object: selected.value.owner } })
  }
}

onMounted(() => {
  applyRouteQuery(route.query)
  void store.fetchTemplates()
})

watch(() => route.query, (query) => applyRouteQuery(query))
</script>

<template>
  <section class="props-page d2a-page-flush">
    <!-- 通栏工具栏(A 类规范):左筛选、右操作 -->
    <div class="d2a-card d2a-toolbar">
      <el-select
        v-model="filters.owner"
        placeholder="所属类"
        clearable
        filterable
        size="small"
        data-testid="filter-owner"
        @change="onFilterChange"
      >
        <el-option
          v-for="o in ownerOptions"
          :key="o.value"
          :label="o.label"
          :value="o.value"
        />
      </el-select>
      <el-select
        v-model="filters.type"
        placeholder="类型"
        clearable
        size="small"
        data-testid="filter-type"
        @change="onFilterChange"
      >
        <el-option
          v-for="t in typeOptions"
          :key="t"
          :label="t"
          :value="t"
        />
      </el-select>
      <el-select
        v-model="filters.sensitive"
        placeholder="敏感"
        clearable
        size="small"
        data-testid="filter-sensitive"
        @change="onFilterChange"
      >
        <el-option
          label="敏感(脱敏)"
          value="yes"
        />
        <el-option
          label="非敏感"
          value="no"
        />
      </el-select>
      <el-input
        v-model="filters.q"
        placeholder="搜索属性 / 说明"
        clearable
        size="small"
        data-testid="filter-keyword"
        @change="onFilterChange"
      />
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="props-refresh"
          @click="store.fetchTemplates()"
        >
          刷新
        </el-button>
      </div>
    </div>

    <div class="d2a-card">
      <LoadingState v-if="templates.status === 'idle' || templates.status === 'loading'" />
      <ErrorState
        v-else-if="templates.status === 'error'"
        :error="templates.error"
        @retry="store.fetchTemplates()"
      />
      <EmptyState
        v-else-if="rows.length === 0"
        title="没有属性定义"
      />
      <EmptyState
        v-else-if="filteredRows.length === 0"
        title="没有符合筛选条件的属性"
      />
      <template v-else>
        <p
          v-if="templatesRefreshError"
          class="refresh-warning"
          data-testid="props-refresh-error"
        >
          刷新失败({{ templatesRefreshError.message }}),展示上一次成功数据
        </p>
        <el-table
          :data="pagedRows"
          size="small"
          data-testid="props-table"
          @row-click="openRow"
        >
          <el-table-column
            prop="name"
            label="属性"
            min-width="140"
          />
          <el-table-column
            label="所属类"
            min-width="140"
          >
            <template #default="{ row }">
              {{ row.objectDisplay }}({{ row.object }})
            </template>
          </el-table-column>
          <el-table-column
            prop="domain"
            label="领域"
            width="90"
          />
          <el-table-column
            prop="type"
            label="类型"
            width="90"
          />
          <el-table-column
            label="敏感"
            width="90"
          >
            <template #default="{ row }">
              <el-tag
                v-if="row.sensitive"
                size="small"
                type="warning"
              >
                脱敏
              </el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column
            label="枚举 / 引用"
            min-width="140"
          >
            <template #default="{ row }">
              <span v-if="row.enum_values.length">{{ row.enum_values.join(' / ') }}</span>
              <span v-else-if="row.ref">→ {{ row.ref }}</span>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column
            label="说明"
            min-width="140"
          >
            <template #default="{ row }">
              {{ row.desc ?? '—' }}
            </template>
          </el-table-column>
        </el-table>
        <PagerBar
          :total="filteredRows.length"
          :limit="page.limit"
          :offset="page.offset"
          data-testid="props-pager"
          @change="onPagerChange"
        />
      </template>
    </div>

    <!-- 属性详情抽屉(共享组件):定义 + 各来源映射表达式 + 所属类深链 + 安全 JSON -->
    <PropertyDetailDrawer
      :visible="selected !== null"
      :object="selectedObject"
      :prop-name="selected?.name ?? null"
      @close="closeDrawer"
      @view-class="viewOwnerClass"
    />
  </section>
</template>

<style scoped>
.props-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}
</style>
