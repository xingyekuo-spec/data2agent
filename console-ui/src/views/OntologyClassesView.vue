<script setup lang="ts">
// 本体库 · 类(A 类目录页):类目录 + 类详情抽屉(属性/关系/绑定 + 查看实例深链)。
// 数据来自 /api/templates(本体库单一事实来源);目录为全量小数据,客户端筛选。
// 详情规范(05-console §3.2):行点击开右侧抽屉,安全 JSON 折叠在抽屉内。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useTemplatesStore } from '@/stores/templates'
import type { components } from '@/types/api'
import type { HealthStatus } from '@/types/state'

type TemplateObject = components['schemas']['TemplateObject']

const store = useTemplatesStore()
const { templates, templatesRefreshError } = storeToRefs(store)
const route = useRoute()
const router = useRouter()
const selectedName = ref('')
const showJson = ref(false)
const domainFilter = ref('')
const keyword = ref('')
let restoringQuery = false

const objects = computed(() =>
  templates.value.status === 'success' ? templates.value.data : [],
)
const domains = computed(() =>
  [...new Set(objects.value.map((o) => o.domain ?? '未分组'))].sort(),
)
const filteredObjects = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  return objects.value.filter((o) =>
    (!domainFilter.value || (o.domain ?? '未分组') === domainFilter.value)
    && (!q || `${o.object} ${o.display_name} ${o.description ?? ''}`.toLowerCase().includes(q)),
  )
})

const selected = computed<TemplateObject | null>(() =>
  objects.value.find((o) => o.object === selectedName.value) ?? null,
)
const drawerVisible = computed(() => selectedName.value !== '')

const materializedStatus: Record<string, HealthStatus> = {
  materialized: 'healthy',
  not_materialized: 'warning',
  unknown: 'unknown',
}

function firstString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function applyRouteQuery(query: LocationQuery): void {
  restoringQuery = true
  domainFilter.value = firstString(query.domain)
  keyword.value = firstString(query.q)
  selectedName.value = firstString(query.object)
  restoringQuery = false
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  void router.replace({
    query: {
      ...(domainFilter.value ? { domain: domainFilter.value } : {}),
      ...(keyword.value ? { q: keyword.value } : {}),
      ...(selectedName.value ? { object: selectedName.value } : {}),
    },
  })
}

function openClass(name: string): void {
  showJson.value = false
  selectedName.value = name
  syncRouteQuery()
}

function closeDrawer(): void {
  selectedName.value = ''
  syncRouteQuery()
}

function onFilterChange(): void {
  syncRouteQuery()
}

function viewInstances(object: string): void {
  void router.push({ path: '/data/objects', query: { object } })
}

onMounted(() => {
  applyRouteQuery(route.query)
  void store.fetchTemplates()
})

watch(() => route.query, (query) => applyRouteQuery(query))
</script>

<template>
  <section class="classes-page d2a-page-flush">
    <!-- 通栏工具栏(A 类规范):左筛选、右操作 -->
    <div class="d2a-card d2a-toolbar">
      <el-select
        v-model="domainFilter"
        placeholder="领域"
        clearable
        size="small"
        data-testid="filter-domain"
        @change="onFilterChange"
      >
        <el-option
          v-for="d in domains"
          :key="d"
          :label="d"
          :value="d"
        />
      </el-select>
      <el-input
        v-model="keyword"
        placeholder="搜索对象 / 显示名 / 描述"
        clearable
        size="small"
        data-testid="filter-keyword"
        @change="onFilterChange"
      />
      <div class="d2a-toolbar__actions">
        <el-button
          size="small"
          data-testid="classes-refresh"
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
        v-else-if="objects.length === 0"
        title="没有本体类"
      />
      <EmptyState
        v-else-if="filteredObjects.length === 0"
        title="没有符合筛选条件的类"
      />
      <template v-else>
        <p
          v-if="templatesRefreshError"
          class="refresh-warning"
          data-testid="classes-refresh-error"
        >
          刷新失败({{ templatesRefreshError.message }}),展示上一次成功数据
        </p>
        <el-table
          :data="filteredObjects"
          size="small"
          data-testid="classes-table"
          @row-click="(row: TemplateObject) => openClass(row.object)"
        >
          <el-table-column
            prop="display_name"
            label="类"
            min-width="130"
          />
          <el-table-column
            prop="object"
            label="object"
            min-width="140"
          />
          <el-table-column
            label="领域"
            width="90"
          >
            <template #default="{ row }">
              {{ row.domain ?? '未分组' }}
            </template>
          </el-table-column>
          <el-table-column
            label="业务键"
            min-width="130"
          >
            <template #default="{ row }">
              {{ row.keys.join(', ') || '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="属性"
            width="70"
          >
            <template #default="{ row }">
              {{ row.properties.length }}
            </template>
          </el-table-column>
          <el-table-column
            label="关系"
            width="70"
          >
            <template #default="{ row }">
              {{ row.relations?.length ?? 0 }}
            </template>
          </el-table-column>
          <el-table-column
            label="物化"
            width="90"
          >
            <template #default="{ row }">
              <StatusBadge :status="materializedStatus[row.materialized?.state ?? 'unknown']" />
            </template>
          </el-table-column>
          <el-table-column
            label="行数"
            width="90"
          >
            <template #default="{ row }">
              {{ row.materialized?.rows ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column
            label="隔离"
            width="80"
          >
            <template #default="{ row }">
              <StatusBadge :status="row.quarantine_pending > 0 ? 'warning' : 'healthy'" />
            </template>
          </el-table-column>
        </el-table>
      </template>
    </div>

    <!-- 类详情抽屉:属性 / 关系 / 绑定 + 查看实例深链 + 安全 JSON -->
    <el-drawer
      :model-value="drawerVisible"
      size="640px"
      data-testid="class-detail-drawer"
      @close="closeDrawer"
    >
      <template #header>
        <div class="drawer-header">
          <span class="drawer-title">类详情</span>
          <span
            v-if="selected"
            class="drawer-target"
            data-testid="class-detail-target"
          >{{ selected.display_name }}({{ selected.object }})</span>
        </div>
      </template>
      <LoadingState v-if="templates.status === 'idle' || templates.status === 'loading'" />
      <EmptyState
        v-else-if="!selected"
        title="未找到该类"
        hint="模板目录中不存在对应对象,可能已被移除"
      />
      <template v-else>
        <dl class="summary">
          <dt>领域</dt>
          <dd>{{ selected.domain ?? '未分组' }}</dd>
          <dt>业务键</dt>
          <dd>{{ selected.keys.join(', ') || '—' }}</dd>
          <dt>权威来源</dt>
          <dd>{{ selected.source_of_truth }}</dd>
          <dt>物化</dt>
          <dd>
            <StatusBadge :status="materializedStatus[selected.materialized?.state ?? 'unknown']" />
            <span class="summary-inline">{{ selected.materialized?.rows ?? '—' }} 行</span>
          </dd>
          <dt>隔离待处理</dt>
          <dd>{{ selected.quarantine_pending }}</dd>
          <dt>描述</dt>
          <dd>{{ selected.description ?? '—' }}</dd>
        </dl>

        <div class="drawer-actions">
          <el-button
            size="small"
            type="primary"
            text
            data-testid="class-view-instances"
            @click="viewInstances(selected.object)"
          >
            查看实例(对象数据)
          </el-button>
        </div>

        <h4>属性({{ selected.properties.length }})</h4>
        <el-table
          :data="selected.properties"
          size="small"
          data-testid="class-props-table"
        >
          <el-table-column
            prop="name"
            label="属性"
            min-width="120"
          />
          <el-table-column
            prop="type"
            label="类型"
            width="80"
          />
          <el-table-column
            label="敏感"
            width="80"
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
            min-width="130"
          >
            <template #default="{ row }">
              <span v-if="row.enum_values?.length">{{ row.enum_values.join(' / ') }}</span>
              <span v-else-if="row.ref">→ {{ row.ref }}</span>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column
            label="说明"
            min-width="120"
          >
            <template #default="{ row }">
              {{ row.desc ?? '—' }}
            </template>
          </el-table-column>
        </el-table>

        <h4>关系({{ selected.relations?.length ?? 0 }})</h4>
        <EmptyState
          v-if="!selected.relations?.length"
          title="该类暂无关系定义"
        />
        <el-table
          v-else
          :data="selected.relations"
          size="small"
          data-testid="class-relations-table"
        >
          <el-table-column
            prop="name"
            label="关系名"
            min-width="110"
          />
          <el-table-column
            label="目标类"
            min-width="130"
          >
            <template #default="{ row }">
              <el-button
                size="small"
                text
                type="primary"
                :data-testid="`class-rel-target-${row.target}`"
                @click="openClass(row.target)"
              >
                {{ row.target }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column
            prop="cardinality"
            label="基数"
            width="80"
          />
          <el-table-column
            label="说明"
            min-width="110"
          >
            <template #default="{ row }">
              {{ row.desc ?? '—' }}
            </template>
          </el-table-column>
        </el-table>

        <h4>绑定({{ selected.bindings.length }})</h4>
        <el-table
          :data="selected.bindings"
          size="small"
          data-testid="class-bindings-table"
        >
          <el-table-column
            prop="source"
            label="来源"
            min-width="120"
          />
          <el-table-column
            label="表"
            min-width="150"
          >
            <template #default="{ row }">
              {{ row.tables.join(', ') }}
            </template>
          </el-table-column>
          <el-table-column
            prop="status"
            label="状态"
            width="90"
          />
          <el-table-column
            label="水位"
            min-width="130"
          >
            <template #default="{ row }">
              {{ row.watermark ?? '—' }}
            </template>
          </el-table-column>
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
        >{{ JSON.stringify(selected, null, 2) }}</pre>
      </template>
    </el-drawer>
  </section>
</template>

<style scoped>
.classes-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.drawer-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.drawer-title {
  font-weight: 600;
}

.drawer-target {
  font-size: 13px;
  color: var(--el-text-color-secondary);
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

.summary-inline {
  margin-left: 8px;
  color: var(--d2a-text-secondary);
}

.drawer-actions {
  margin: 0 0 8px;
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
