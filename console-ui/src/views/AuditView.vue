<script setup lang="ts">
// 审计日志(M4):SQL 操作 / 数据访问双 tab;筛选、服务端分页、SQL 默认折叠、
// 访问范围说明(当前仅审计 raw 浏览)。无自动轮询。
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import PagerBar from '@/components/shared/PagerBar.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useAuditStore } from '@/stores/audit'
import { formatDateTime } from '@/utils/time'

const store = useAuditStore()
const { sql, sqlTotal, sqlRefreshError, access, accessRefreshError } = storeToRefs(store)
const { sqlFilters, accessFilters, sqlPage, accessPage } = store
const route = useRoute()
const router = useRouter()
const activeTab = ref('sql')
let restoringQuery = false

onMounted(() => {
  applyRouteQuery(route.query, false)
  void store.refreshSql()
  void store.refreshAccess()
})

function truncate(text: string, max = 80): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function onSqlPage(offset: number, limit: number): void {
  sqlPage.offset = offset
  sqlPage.limit = limit
  void store.refreshSql()
  syncRouteQuery()
}

function onAccessPage(offset: number, limit: number): void {
  accessPage.offset = offset
  accessPage.limit = limit
  void store.refreshAccess()
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

function applyRouteQuery(query: LocationQuery, refresh: boolean): void {
  restoringQuery = true
  activeTab.value = firstString(query.tab) === 'access' ? 'access' : 'sql'
  let changed: boolean
  if (activeTab.value === 'access') {
    const nextAllowed = firstString(query.allowed)
    const nextResourceType = firstString(query.resource_type)
    const resourceType: '' | 'raw' | 'object' | 'quarantine_raw' =
      nextResourceType === 'raw' || nextResourceType === 'object' || nextResourceType === 'quarantine_raw'
        ? nextResourceType
        : ''
    const allowed: '' | 'true' | 'false' =
      nextAllowed === 'true' || nextAllowed === 'false' ? nextAllowed : ''
    const values = {
      subject: firstString(query.subject),
      resource_type: resourceType,
      allowed,
      from: firstString(query.from),
      to: firstString(query.to),
      offset: pageOffset(query.page, accessPage.limit),
    }
    changed = accessFilters.subject !== values.subject
      || accessFilters.resource_type !== values.resource_type
      || accessFilters.allowed !== values.allowed
      || accessFilters.from !== values.from
      || accessFilters.to !== values.to
      || accessPage.offset !== values.offset
    accessFilters.subject = values.subject
    accessFilters.resource_type = values.resource_type
    accessFilters.allowed = values.allowed
    accessFilters.from = values.from
    accessFilters.to = values.to
    accessPage.offset = values.offset
  } else {
    const values = {
      source: firstString(query.source),
      action: firstString(query.action),
      from: firstString(query.from),
      to: firstString(query.to),
      offset: pageOffset(query.page, sqlPage.limit),
    }
    changed = sqlFilters.source !== values.source
      || sqlFilters.action !== values.action
      || sqlFilters.from !== values.from
      || sqlFilters.to !== values.to
      || sqlPage.offset !== values.offset
    sqlFilters.source = values.source
    sqlFilters.action = values.action
    sqlFilters.from = values.from
    sqlFilters.to = values.to
    sqlPage.offset = values.offset
  }
  restoringQuery = false
  if (refresh && changed) {
    if (activeTab.value === 'access') {
      void store.refreshAccess()
    } else {
      void store.refreshSql()
    }
  }
}

function syncRouteQuery(): void {
  if (restoringQuery) {
    return
  }
  const page = activeTab.value === 'access'
    ? pageQuery(accessPage.offset, accessPage.limit)
    : pageQuery(sqlPage.offset, sqlPage.limit)
  if (activeTab.value === 'access') {
    void router.replace({
      query: {
        tab: 'access',
        ...(accessFilters.subject ? { subject: accessFilters.subject } : {}),
        ...(accessFilters.resource_type ? { resource_type: accessFilters.resource_type } : {}),
        ...(accessFilters.allowed ? { allowed: accessFilters.allowed } : {}),
        ...(accessFilters.from ? { from: accessFilters.from } : {}),
        ...(accessFilters.to ? { to: accessFilters.to } : {}),
        ...(page ? { page } : {}),
      },
    })
    return
  }
  void router.replace({
    query: {
      tab: 'sql',
      ...(sqlFilters.source ? { source: sqlFilters.source } : {}),
      ...(sqlFilters.action ? { action: sqlFilters.action } : {}),
      ...(sqlFilters.from ? { from: sqlFilters.from } : {}),
      ...(sqlFilters.to ? { to: sqlFilters.to } : {}),
      ...(page ? { page } : {}),
    },
  })
}

function filterSql(): void {
  store.filterSql()
  syncRouteQuery()
}

function filterAccess(): void {
  store.filterAccess()
  syncRouteQuery()
}

watch(() => route.query, (query) => applyRouteQuery(query, true))
</script>

<template>
  <section class="audit-page d2a-page-flush">
    <el-tabs
      v-model="activeTab"
      data-testid="audit-tabs"
      @tab-change="syncRouteQuery"
    >
      <!-- SQL 操作审计 -->
      <el-tab-pane
        label="SQL 操作"
        name="sql"
      >
        <div class="d2a-card d2a-toolbar">
          <el-input
            v-model="sqlFilters.source"
            placeholder="来源(source)"
            size="small"
            clearable
            class="toolbar__input"
            data-testid="filter-source"
            @change="filterSql()"
          />
          <el-input
            v-model="sqlFilters.action"
            placeholder="动作(action)"
            size="small"
            clearable
            class="toolbar__input"
            data-testid="filter-action"
            @change="filterSql()"
          />
          <el-input
            v-model="sqlFilters.from"
            placeholder="from(带时区 ISO)"
            size="small"
            clearable
            class="toolbar__input toolbar__input--wide"
            @change="filterSql()"
          />
          <el-input
            v-model="sqlFilters.to"
            placeholder="to(带时区 ISO)"
            size="small"
            clearable
            class="toolbar__input toolbar__input--wide"
            @change="filterSql()"
          />
          <div class="d2a-toolbar__actions">
            <el-button
              size="small"
              data-testid="sql-refresh"
              @click="store.refreshSql()"
            >
              刷新
            </el-button>
          </div>
        </div>

        <div class="d2a-card">
          <LoadingState v-if="sql.status === 'idle' || sql.status === 'loading'" />
          <ErrorState
            v-else-if="sql.status === 'error'"
            :error="sql.error"
            @retry="store.refreshSql()"
          />
          <EmptyState
            v-else-if="sql.data.length === 0"
            title="没有符合条件的审计记录"
          />
          <template v-else>
            <p
              v-if="sqlRefreshError"
              class="refresh-warning"
              data-testid="sql-refresh-error"
            >
              刷新失败({{ sqlRefreshError.message }}),展示上一次成功数据
            </p>
            <el-table
              :data="sql.data"
              size="small"
              data-testid="sql-table"
            >
              <el-table-column type="expand">
                <template #default="{ row }">
                  <pre
                    class="sql-full"
                    data-testid="sql-full"
                  >{{ row.sql }}</pre>
                </template>
              </el-table-column>
              <el-table-column
                label="时间"
                width="150"
              >
                <template #default="{ row }">
                  {{ formatDateTime(row.ts) }}
                </template>
              </el-table-column>
              <el-table-column
                prop="source"
                label="来源"
                width="130"
              />
              <el-table-column
                prop="action"
                label="动作"
                width="90"
              />
              <el-table-column label="SQL(默认折叠)">
                <template #default="{ row }">
                  <span class="sql-preview">{{ truncate(row.sql) }}</span>
                </template>
              </el-table-column>
              <el-table-column
                label="行数"
                width="80"
              >
                <template #default="{ row }">
                  {{ row.rows ?? '—' }}
                </template>
              </el-table-column>
              <el-table-column
                label="耗时"
                width="90"
              >
                <template #default="{ row }">
                  {{ row.duration_ms == null ? '—' : `${row.duration_ms}ms` }}
                </template>
              </el-table-column>
            </el-table>
            <PagerBar
              :total="sqlTotal"
              :limit="sqlPage.limit"
              :offset="sqlPage.offset"
              data-testid="sql-pager"
              @change="onSqlPage"
            />
          </template>
        </div>
      </el-tab-pane>

      <!-- 控制台数据访问审计 -->
      <el-tab-pane
        label="数据访问"
        name="access"
      >
        <div class="d2a-card d2a-toolbar">
          <el-input
            v-model="accessFilters.subject"
            placeholder="主体(subject)"
            size="small"
            clearable
            class="toolbar__input"
            data-testid="filter-subject"
            @change="filterAccess()"
          />
          <el-select
            v-model="accessFilters.resource_type"
            placeholder="资源类型"
            clearable
            size="small"
            class="toolbar__input"
            data-testid="filter-resource-type"
            @change="filterAccess()"
          >
            <el-option
              label="raw"
              value="raw"
            />
            <el-option
              label="object"
              value="object"
            />
            <el-option
              label="quarantine_raw"
              value="quarantine_raw"
            />
          </el-select>
          <el-select
            v-model="accessFilters.allowed"
            placeholder="允许/拒绝"
            clearable
            size="small"
            class="toolbar__input"
            data-testid="filter-allowed"
            @change="filterAccess()"
          >
            <el-option
              label="允许"
              value="true"
            />
            <el-option
              label="拒绝"
              value="false"
            />
          </el-select>
          <el-input
            v-model="accessFilters.from"
            placeholder="from(带时区 ISO)"
            size="small"
            clearable
            class="toolbar__input toolbar__input--wide"
            data-testid="filter-access-from"
            @change="filterAccess()"
          />
          <el-input
            v-model="accessFilters.to"
            placeholder="to(带时区 ISO)"
            size="small"
            clearable
            class="toolbar__input toolbar__input--wide"
            data-testid="filter-access-to"
            @change="filterAccess()"
          />
          <div class="d2a-toolbar__actions">
            <el-button
              size="small"
              data-testid="access-refresh"
              @click="store.refreshAccess()"
            >
              刷新
            </el-button>
          </div>
        </div>
        <p
          class="scope-note"
          data-testid="access-scope-note"
        >
          访问审计当前覆盖 raw 数据浏览与 quarantine_raw 隔离详情查看(允许与拒绝)。
        </p>

        <div class="d2a-card">
          <LoadingState v-if="access.status === 'idle' || access.status === 'loading'" />
          <ErrorState
            v-else-if="access.status === 'error'"
            :error="access.error"
            @retry="store.refreshAccess()"
          />
          <EmptyState
            v-else-if="access.data.items.length === 0"
            title="没有符合条件的访问记录"
          />
          <template v-else>
            <p
              v-if="accessRefreshError"
              class="refresh-warning"
              data-testid="access-refresh-error"
            >
              刷新失败({{ accessRefreshError.message }}),展示上一次成功数据
            </p>
            <el-table
              :data="access.data.items"
              size="small"
              data-testid="access-table"
            >
              <el-table-column
                label="时间"
                width="150"
              >
                <template #default="{ row }">
                  {{ formatDateTime(row.ts) }}
                </template>
              </el-table-column>
              <el-table-column
                prop="subject"
                label="主体"
                width="130"
              />
              <el-table-column
                prop="resource_type"
                label="类型"
                width="70"
              />
              <el-table-column
                label="资源"
                min-width="140"
              >
                <template #default="{ row }">
                  {{ row.source ? `${row.source}/` : '' }}{{ row.resource }}
                </template>
              </el-table-column>
              <el-table-column
                label="结果"
                width="80"
              >
                <template #default="{ row }">
                  <StatusBadge :status="row.allowed ? 'healthy' : 'failed'" />
                </template>
              </el-table-column>
              <el-table-column
                prop="reason_code"
                label="原因码"
                width="150"
              />
              <el-table-column
                label="行数"
                width="70"
              >
                <template #default="{ row }">
                  {{ row.returned_rows ?? '—' }}
                </template>
              </el-table-column>
            </el-table>
            <PagerBar
              :total="access.status === 'success' ? access.data.total : 0"
              :limit="accessPage.limit"
              :offset="accessPage.offset"
              data-testid="access-pager"
              @change="onAccessPage"
            />
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.audit-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.scope-note {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.sql-preview {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.sql-full {
  margin: 0;
  padding: 10px;
  overflow-x: auto;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
