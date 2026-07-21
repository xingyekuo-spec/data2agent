<script setup lang="ts">
/**
 * 模板页(M5-T09):只读展示模板对象、属性、绑定与指标。
 * 左侧对象列表,右侧详情(概览/属性/绑定/指标 tab)。
 */
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import MappingPreviewPanel from '@/components/templates/MappingPreviewPanel.vue'
import { useTemplatesStore } from '@/stores/templates'
import { formatDateTime } from '@/utils/time'
import type { components } from '@/types/api'

type TemplateObject = components['schemas']['TemplateObject']
type TemplateBinding = components['schemas']['TemplateBinding']
type DeriveRule = components['schemas']['DeriveRule']
type TemplateMetric = components['schemas']['TemplateMetric']

const store = useTemplatesStore()
const {
  templates,
  templatesRefreshError,
  selectedObject,
  metrics,
  metricsRefreshError,
} = storeToRefs(store)

const activeTab = ref('overview')

onMounted(() => {
  void store.fetchTemplates()
  void store.fetchMetrics()
})

const objects = computed(() =>
  templates.value.status === 'success' ? templates.value.data : [],
)

// ---- status tag helpers ----

type TagType = 'success' | 'warning' | 'info' | 'danger'

type BindingStatus = TemplateBinding['status']
type MetricStatus = TemplateMetric['status']
type CalibrationState = TemplateMetric['calibration_state']
type MaterializedState = 'materialized' | 'not_materialized' | 'unknown'

function bindingTag(b: TemplateBinding): { type: TagType; label: string } {
  const map: Record<BindingStatus, { type: TagType; label: string }> = {
    verified: { type: 'success', label: '已验证' },
    draft: { type: 'warning', label: '未校准' },
    disabled: { type: 'info', label: '已禁用' },
  }
  return map[b.status]
}

function metricStatusTag(s: MetricStatus): { type: TagType; label: string } {
  const map: Record<MetricStatus, { type: TagType; label: string }> = {
    certified: { type: 'success', label: '认证' },
    draft: { type: 'warning', label: '未校准' },
    deprecated: { type: 'info', label: '已废弃' },
  }
  return map[s]
}

function calibrationTag(s: CalibrationState): { type: TagType; label: string } {
  const map: Record<CalibrationState, { type: TagType; label: string }> = {
    calibrated: { type: 'success', label: '已校准' },
    uncalibrated: { type: 'warning', label: '未校准' },
    deprecated: { type: 'info', label: '已废弃' },
  }
  return map[s]
}

function materializedTag(s: MaterializedState): { type: TagType; label: string } {
  const map: Record<MaterializedState, { type: TagType; label: string }> = {
    materialized: { type: 'success', label: '已物化' },
    not_materialized: { type: 'warning', label: '未物化' },
    unknown: { type: 'info', label: '未知' },
  }
  return map[s]
}

function bindingCountLabel(obj: TemplateObject): string {
  const verifiedCount = obj.bindings.filter((b) => b.status === 'verified').length
  const draftCount = obj.bindings.filter((b) => b.status === 'draft').length
  const disabledCount = obj.bindings.filter((b) => b.status === 'disabled').length
  return `${verifiedCount}已验证/${draftCount}未校准/${disabledCount}已禁用`
}

// ---- enums in field_map expressions ----

function enumMapEntries(enumMap: Record<string, Record<string, string>> | undefined): { field: string; source: string; target: string }[] {
  if (!enumMap) return []
  const entries: { field: string; source: string; target: string }[] = []
  for (const [field, mapping] of Object.entries(enumMap)) {
    for (const [src, tgt] of Object.entries(mapping)) {
      entries.push({ field, source: src, target: tgt })
    }
  }
  return entries
}

// ---- derived rules ----

function derivedRulesEntries(rules: DeriveRule[] | undefined): { condition: string; value: string }[] {
  if (!rules) return []
  return rules.map((r) => ({
    condition: Object.entries(r.when)
      .map(([k, v]) => `${k} = ${v ?? 'NULL'}`)
      .join(' AND '),
    value: r.value,
  }))
}

// ---- binding field map to table ----

function fieldMapEntries(map: Record<string, string> | undefined): { field: string; expression: string }[] {
  if (!map) return []
  return Object.entries(map).map(([k, v]) => ({ field: k, expression: v }))
}

function keyMapEntries(map: Record<string, string> | undefined): { objectKey: string; sourceKey: string }[] {
  if (!map) return []
  return Object.entries(map).map(([k, v]) => ({ objectKey: k, sourceKey: v }))
}

function selectObj(obj: TemplateObject): void {
  store.selectObject(obj.object)
  activeTab.value = 'overview'
}

function propertyTagType(p: { sensitive: boolean }): TagType {
  return p.sensitive ? 'danger' : 'info'
}
</script>

<template>
  <section class="templates-page">
    <div class="templates-layout">
      <!-- 左侧:对象列表 -->
      <aside class="templates-list">
        <div class="d2a-card">
          <h3 class="card-title">模板对象</h3>
          <LoadingState v-if="templates.status === 'idle' || templates.status === 'loading'" />
          <ErrorState
            v-else-if="templates.status === 'error'"
            :error="templates.error"
            @retry="store.fetchTemplates()"
          />
          <EmptyState v-else-if="objects.length === 0" title="没有模板对象" />
          <template v-else>
            <p
              v-if="templatesRefreshError"
              class="refresh-warning"
              data-testid="templates-refresh-error"
            >
              刷新失败({{ templatesRefreshError.message }}),展示上一次成功数据
            </p>
            <div
              v-for="obj in objects"
              :key="obj.object"
              class="tpl-list-item"
              :class="{ 'tpl-list-item--selected': selectedObject?.object === obj.object }"
              :data-testid="`tpl-item-${obj.object}`"
              @click="selectObj(obj)"
            >
              <div class="tpl-list-item__header">
                <span class="tpl-list-item__name">{{ obj.display_name }}</span>
                <span class="tpl-list-item__object">{{ obj.object }}</span>
              </div>
              <div class="tpl-list-item__meta">
                <span v-if="obj.domain" class="tpl-list-item__domain">{{ obj.domain }}</span>
                <span class="tpl-list-item__bindings">{{ obj.bindings.length }} 个绑定({{ bindingCountLabel(obj) }})</span>
              </div>
              <div class="tpl-list-item__status">
                <el-tag
                  size="small"
                  :type="materializedTag(obj.materialized?.state ?? 'unknown').type"
                  data-testid="mat-tag"
                >
                  {{ materializedTag(obj.materialized?.state ?? 'unknown').label }}
                </el-tag>
                <el-tag
                  v-if="obj.quarantine_pending > 0"
                  size="small"
                  type="warning"
                  data-testid="quarantine-tag"
                >
                  隔离{{ obj.quarantine_pending }}
                </el-tag>
                <el-tag
                  v-if="obj.warnings?.length"
                  size="small"
                  type="warning"
                  data-testid="warnings-tag"
                >
                  警告
                </el-tag>
              </div>
            </div>
          </template>
        </div>
      </aside>

      <!-- 右侧:对象详情 -->
      <main class="templates-detail">
        <template v-if="selectedObject">
          <div class="d2a-card">
            <h3 class="card-title">
              {{ selectedObject.display_name }}
              <span class="card-subtitle">{{ selectedObject.object }}</span>
            </h3>
          </div>

          <MappingPreviewPanel
            :object-name="selectedObject.object"
            :bindings="selectedObject.bindings"
          />

          <div class="d2a-card">
            <el-tabs v-model="activeTab" data-testid="tpl-detail-tabs">
              <!-- 概览 tab -->
              <el-tab-pane label="概览" name="overview">
                <div class="overview-grid" data-testid="tpl-overview">
                  <div class="overview-item">
                    <span class="overview-label">描述</span>
                    <span class="overview-value">{{ selectedObject.description || '—' }}</span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">领域</span>
                    <span class="overview-value">{{ selectedObject.domain || '—' }}</span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">业务键</span>
                    <span class="overview-value">
                      <el-tag
                        v-for="k in selectedObject.keys"
                        :key="k"
                        size="small"
                        class="key-tag"
                      >{{ k }}</el-tag>
                    </span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">来源</span>
                    <span class="overview-value">{{ selectedObject.source_of_truth }}</span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">知识引用</span>
                    <span class="overview-value">
                      <template v-if="selectedObject.knowledge_refs?.length">
                        <span
                          v-for="ref in selectedObject.knowledge_refs"
                          :key="ref"
                          class="ref-link"
                        >{{ ref }}</span>
                      </template>
                      <span v-else>—</span>
                    </span>
                  </div>
                </div>

                <!-- 物化信息 -->
                <div class="section-title">物化信息</div>
                <div class="overview-grid" data-testid="tpl-materialization">
                  <div class="overview-item">
                    <span class="overview-label">状态</span>
                    <span class="overview-value">
                      <el-tag
                        size="small"
                        :type="materializedTag(selectedObject.materialized?.state ?? 'unknown').type"
                      >
                        {{ materializedTag(selectedObject.materialized?.state ?? 'unknown').label }}
                      </el-tag>
                    </span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">来源</span>
                    <span class="overview-value">{{ selectedObject.materialized?.source || '—' }}</span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">行数</span>
                    <span class="overview-value">{{ selectedObject.materialized?.rows ?? '—' }}</span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">物化时间</span>
                    <span class="overview-value">{{ formatDateTime(selectedObject.materialized?.mapped_at) || '—' }}</span>
                  </div>
                  <div class="overview-item">
                    <span class="overview-label">批次</span>
                    <span class="overview-value">{{ selectedObject.materialized?.batch_id || '—' }}</span>
                  </div>
                </div>

                <!-- 警告 -->
                <template v-if="selectedObject.warnings?.length">
                  <div class="section-title">警告</div>
                  <ul class="warnings" data-testid="tpl-warnings">
                    <li v-for="w in selectedObject.warnings" :key="w">{{ w }}</li>
                  </ul>
                </template>
              </el-tab-pane>

              <!-- 属性 tab -->
              <el-tab-pane label="属性" name="properties">
                <el-table :data="selectedObject.properties" size="small" data-testid="tpl-properties">
                  <el-table-column prop="name" label="名称" width="150" />
                  <el-table-column prop="type" label="类型" width="100" />
                  <el-table-column prop="desc" label="描述" min-width="150" />
                  <el-table-column label="敏感" width="80">
                    <template #default="{ row }">
                      <el-tag
                        v-if="row.sensitive"
                        size="small"
                        type="danger"
                        data-testid="sensitive-tag"
                      >敏感</el-tag>
                      <span v-else class="text-muted">—</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="枚举值" width="180">
                    <template #default="{ row }">
                      <template v-if="row.enum_values?.length">
                        <el-tag
                          v-for="ev in row.enum_values"
                          :key="ev"
                          size="small"
                          :type="propertyTagType(row)"
                          class="enum-tag"
                        >{{ ev }}</el-tag>
                      </template>
                      <span v-else class="text-muted">—</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="ref" label="引用" width="120">
                    <template #default="{ row }">
                      <span v-if="row.ref" class="text-muted">{{ row.ref }}</span>
                      <span v-else class="text-muted">—</span>
                    </template>
                  </el-table-column>
                </el-table>
              </el-tab-pane>

              <!-- 绑定 tab -->
              <el-tab-pane label="绑定" name="bindings">
                <template v-if="selectedObject.bindings.length === 0">
                  <EmptyState title="该对象没有绑定" />
                </template>
                <div
                  v-for="binding in selectedObject.bindings"
                  :key="binding.source"
                  class="binding-section"
                  :data-testid="`binding-${binding.source}`"
                >
                  <!-- 绑定头部 -->
                  <div class="binding-header">
                    <span class="binding-source">{{ binding.source }}</span>
                    <el-tag
                      size="small"
                      :type="bindingTag(binding).type"
                      data-testid="binding-status-tag"
                    >{{ bindingTag(binding).label }}</el-tag>
                    <el-tag
                      v-if="!binding.enabled"
                      size="small"
                      type="info"
                    >已停用</el-tag>
                  </div>

                  <!-- 表列表 -->
                  <div class="binding-subsection">
                    <span class="subsection-label">映射表：</span>
                    <el-tag
                      v-for="t in binding.tables"
                      :key="t"
                      size="small"
                      type="info"
                      class="table-tag"
                    >{{ t }}</el-tag>
                  </div>

                  <!-- 键映射 -->
                  <details class="binding-details" open>
                    <summary class="subsection-label">键映射</summary>
                    <el-table
                      :data="keyMapEntries(binding.key_map)"
                      size="small"
                      class="subsection-table"
                    >
                      <el-table-column prop="objectKey" label="对象键" width="180" />
                      <el-table-column prop="sourceKey" label="源字段" />
                    </el-table>
                  </details>

                  <!-- 字段映射 -->
                  <details class="binding-details" open>
                    <summary class="subsection-label">字段映射</summary>
                    <el-table
                      :data="fieldMapEntries(binding.field_map)"
                      size="small"
                      class="subsection-table"
                    >
                      <el-table-column prop="field" label="对象字段" width="180" />
                      <el-table-column prop="expression" label="映射表达式" />
                    </el-table>
                  </details>

                  <!-- 枚举映射 -->
                  <template v-if="enumMapEntries(binding.enum_map).length > 0">
                    <details class="binding-details" open>
                      <summary class="subsection-label">枚举映射</summary>
                      <el-table
                        :data="enumMapEntries(binding.enum_map)"
                        size="small"
                        class="subsection-table"
                        data-testid="enum-map-table"
                      >
                        <el-table-column prop="field" label="字段" width="150" />
                        <el-table-column prop="source" label="源值" width="120" />
                        <el-table-column label="映射" width="40">
                          <template>→</template>
                        </el-table-column>
                        <el-table-column prop="target" label="对象值" width="120" />
                      </el-table>
                    </details>
                  </template>

                  <!-- 派生规则 -->
                  <template v-if="binding.derived && Object.keys(binding.derived).length > 0">
                    <div
                      v-for="(df, fieldName) in binding.derived"
                      :key="fieldName"
                    >
                      <details class="binding-details" open>
                        <summary class="subsection-label">
                          派生规则: <code>{{ fieldName }}</code>
                        </summary>
                        <template v-if="derivedRulesEntries(df.rules).length > 0">
                          <el-table
                            :data="derivedRulesEntries(df.rules)"
                            size="small"
                            class="subsection-table"
                            data-testid="derived-rules-table"
                          >
                            <el-table-column label="条件" min-width="200">
                              <template #default="{ row }">
                                <code>{{ row.condition }}</code>
                              </template>
                            </el-table-column>
                            <el-table-column label="取值" width="160">
                              <template #default="{ row }">
                                <code>{{ row.value }}</code>
                              </template>
                            </el-table-column>
                          </el-table>
                        </template>
                        <div v-if="df.default != null" class="derived-default">
                          默认值: <code>{{ df.default }}</code>
                        </div>
                      </details>
                    </div>
                  </template>

                  <!-- Watermark / Notes -->
                  <div class="binding-meta">
                    <div v-if="binding.watermark" class="binding-meta-item">
                      <span class="subsection-label">水位线：</span>
                      <code>{{ binding.watermark }}</code>
                    </div>
                    <div v-if="binding.notes" class="binding-meta-item">
                      <span class="subsection-label">备注：</span>
                      <span class="text-muted">{{ binding.notes }}</span>
                    </div>
                  </div>
                </div>
              </el-tab-pane>

              <!-- 指标 tab -->
              <el-tab-pane label="指标" name="metrics">
                <template v-if="metrics.status === 'idle' || metrics.status === 'loading'">
                  <LoadingState />
                </template>
                <ErrorState
                  v-else-if="metrics.status === 'error'"
                  :error="metrics.error"
                  @retry="store.fetchMetrics()"
                />
                <EmptyState
                  v-else-if="metrics.status === 'success' && metrics.data.length === 0"
                  title="没有模板指标"
                />
                <template v-else-if="metrics.status === 'success'">
                  <p
                    v-if="metricsRefreshError"
                    class="refresh-warning"
                    data-testid="metrics-refresh-error"
                  >
                    刷新失败({{ metricsRefreshError.message }}),展示上一次成功数据
                  </p>
                  <el-table :data="metrics.data" size="small" data-testid="tpl-metrics">
                    <el-table-column prop="metric" label="指标名" width="160" />
                    <el-table-column prop="display_name" label="显示名" width="140" />
                    <el-table-column label="状态" width="90">
                      <template #default="{ row }">
                        <el-tag
                          size="small"
                          :type="metricStatusTag(row.status).type"
                        >{{ metricStatusTag(row.status).label }}</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column label="校准" width="90">
                      <template #default="{ row }">
                        <el-tag
                          size="small"
                          :type="calibrationTag(row.calibration_state).type"
                          data-testid="calibration-tag"
                        >{{ calibrationTag(row.calibration_state).label }}</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column prop="formula" label="公式" min-width="200">
                      <template #default="{ row }">
                        <code>{{ row.formula }}</code>
                      </template>
                    </el-table-column>
                    <el-table-column label="粒度" width="140">
                      <template #default="{ row }">
                        <template v-if="row.grain?.length">
                          <el-tag
                            v-for="g in row.grain"
                            :key="g"
                            size="small"
                            type="info"
                            class="grain-tag"
                          >{{ g }}</el-tag>
                        </template>
                        <span v-else class="text-muted">—</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="维度" width="140">
                      <template #default="{ row }">
                        <template v-if="row.dimensions?.length">
                          <el-tag
                            v-for="d in row.dimensions"
                            :key="d"
                            size="small"
                            type="info"
                            class="dim-tag"
                          >{{ d }}</el-tag>
                        </template>
                        <span v-else class="text-muted">—</span>
                      </template>
                    </el-table-column>
                    <el-table-column prop="freshness_sla" label="时效" width="100" />
                    <el-table-column prop="caveats" label="注意事项" min-width="160">
                      <template #default="{ row }">
                        <span v-if="row.caveats" class="text-muted">{{ row.caveats }}</span>
                        <span v-else class="text-muted">—</span>
                      </template>
                    </el-table-column>
                  </el-table>
                </template>
              </el-tab-pane>
            </el-tabs>
          </div>
        </template>

        <!-- 未选择对象时的空状态 -->
        <div v-else class="d2a-card">
          <EmptyState title="请从左侧选择一个模板对象以查看详情" />
        </div>
      </main>
    </div>

    <!-- 刷新按钮 -->
    <div class="templates-actions">
      <el-button
        size="small"
        data-testid="tpl-refresh"
        @click="store.fetchTemplates()"
      >刷新模板</el-button>
    </div>
  </section>
</template>

<style scoped>
.templates-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.templates-layout {
  display: flex;
  gap: 12px;
  min-height: 0;
}

.templates-list {
  flex: 0 0 320px;
  max-height: calc(100vh - 140px);
  overflow-y: auto;
}

.templates-detail {
  flex: 1;
  min-width: 0;
}

.templates-actions {
  display: flex;
  gap: 8px;
}

.card-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}

.card-subtitle {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

/* 左侧列表项 */
.tpl-list-item {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  margin-bottom: 6px;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
}

.tpl-list-item:hover {
  border-color: var(--el-color-primary);
}

.tpl-list-item--selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.tpl-list-item__header {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 4px;
}

.tpl-list-item__name {
  font-size: 14px;
  font-weight: 600;
}

.tpl-list-item__object {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tpl-list-item__meta {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}

.tpl-list-item__domain {
  background: var(--el-fill-color-light);
  padding: 1px 6px;
  border-radius: 3px;
}

.tpl-list-item__status {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

/* 概览 */
.overview-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 16px;
}

.overview-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.overview-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.overview-value {
  font-size: 13px;
}

.key-tag {
  margin-right: 4px;
}

.ref-link {
  display: block;
  font-size: 12px;
  color: var(--el-color-primary);
  word-break: break-all;
}

.section-title {
  margin: 14px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
}

/* 绑定 */
.binding-section {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 10px;
}

.binding-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.binding-source {
  font-size: 14px;
  font-weight: 600;
}

.binding-subsection {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.subsection-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
}

.table-tag {
  margin-right: 4px;
}

.binding-details {
  margin-bottom: 10px;
}

.binding-details > summary {
  cursor: pointer;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  list-style-position: outside;
}

.subsection-table {
  margin-bottom: 4px;
}

.binding-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 6px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.binding-meta-item {
  font-size: 12px;
}

.derived-default {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  padding: 4px 0;
}

code {
  font-size: 12px;
  background: var(--el-fill-color-light);
  padding: 1px 4px;
  border-radius: 3px;
}

.text-muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.enum-tag,
.grain-tag,
.dim-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}

.refresh-warning {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--d2a-status-stale);
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
</style>
