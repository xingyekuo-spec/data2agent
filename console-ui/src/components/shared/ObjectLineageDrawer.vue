<script setup lang="ts">
/**
 * M4-T09: 对象字段追溯只读抽屉。
 * 三次点击:对象表行 → 查看血缘 → 点击字段展开步骤/输入。
 * 无编辑/重放/保存入口。
 */
import { computed } from 'vue'
import { useLineageStore } from '@/stores/lineage'
import LoadingState from '@/components/shared/LoadingState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import type { ApiError } from '@/api/errors'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: [] }>()

const store = useLineageStore()

/**
 * 与 ObjectLineageResponse 生成契约同构的本地视图类型。
 * 生成类型嵌套过深会触发 TS2589;此接口保持字段名一致,
 * 由 store 保证运行时形状与生成契约匹配。
 */
interface Evidence {
  kind: string
  value?: unknown
  preview?: string | null
  sha256?: string | null
  length?: number | null
}
interface StepView {
  kind: string
  before?: Evidence | null
  after?: Evidence | null
  map_hit?: boolean | null
  coerce_type?: string | null
  derived_rule_index?: number | null
  derived_when?: Record<string, unknown> | null
}
interface InputView {
  role: string
  source_table?: string | null
  source_column?: string | null
  source_pk?: unknown[][] | null
  source_value?: Evidence | null
  extract_batch_id?: string | null
  join?: Record<string, unknown> | null
}
interface FieldView {
  property: string
  display_name: string
  final_value?: Evidence | null
  state: string
  reason_code?: string | null
  steps?: StepView[]
  inputs?: InputView[]
}
interface LineageView {
  state: string
  reason_code?: string | null
  source?: string | null
  object: string
  display_name: string
  object_key: [string, unknown][]
  key_token: string
  dataset_version?: string | null
  object_version?: string | null
  template_version?: string | null
  binding_hash?: string | null
  binding_status?: string | null
  map_batch_id?: string | null
  fields: FieldView[]
  warnings: string[]
}

const data = computed<LineageView | null>(() => {
  const st = store.lineage as { status: string; data?: unknown; error?: unknown }
  return st.status === 'success' ? (st.data as LineageView) : null
})
const error = computed<ApiError | null>(() => {
  const st = store.lineage as { status: string; error?: ApiError }
  return st.status === 'error' ? (st.error ?? null) : null
})
const isAvailable = computed(() => data.value?.state === 'available')
const isUnavailable = computed(() => data.value?.state === 'unavailable')

function formatEvidence(ev: { kind: string; value?: unknown; preview?: string | null; length?: number | null } | null | undefined): string {
  if (!ev) return '—'
  if (ev.kind === 'null') return 'NULL'
  if (ev.kind === 'scalar') return String(ev.value ?? '—')
  if (ev.kind === 'bytes') return `[BLOB ${ev.length ?? '?'} bytes]`
  if (ev.kind === 'truncated') return `${ev.preview ?? ''}… [${ev.length ?? '?'} bytes]`
  return String(ev.value ?? '—')
}

function stepLabel(kind: string): string {
  const labels: Record<string, string> = {
    read: '读取',
    join: '关联',
    map: '映射',
    coerce: '类型转换',
    derived_rule: '派生规则',
    derived_default: '派生默认',
  }
  return labels[kind] ?? kind
}

function formatPk(pk: unknown[][] | null | undefined): string {
  if (!pk) return '—'
  return pk.map((p) => `${p[0]}=${p[1]}`).join(', ')
}

function fieldStateTag(state: string): 'success' | 'info' | 'warning' {
  if (state === 'available') return 'success'
  return 'warning'
}
</script>

<template>
  <el-drawer
    :model-value="props.visible"
    title="字段血缘"
    direction="rtl"
    size="520px"
    data-testid="lineage-drawer"
    @close="emit('close')"
  >
    <template #header>
      <div class="drawer-header">
        <span>字段血缘</span>
        <el-tag
          v-if="store.stale"
          size="small"
          type="warning"
          data-testid="lineage-stale"
        >
          结果可能过期
        </el-tag>
      </div>
    </template>

    <p
      class="readonly-note"
      data-testid="lineage-readonly-note"
    >
      只读预览 — 血缘证明系统处理事实，不证明源字典语义
    </p>

    <LoadingState v-if="store.isLoading" />
    <ErrorState
      v-else-if="error"
      :error="error"
      data-testid="lineage-error"
      @retry="store.retry()"
    />

    <template v-else-if="isUnavailable">
      <el-empty
        description="该数据集版本未记录字段血缘"
        data-testid="lineage-unavailable"
      >
        <template #description>
          <p>{{ data?.warnings?.[0] ?? '该数据集版本未记录字段血缘' }}</p>
          <p
            v-if="data?.dataset_version"
            class="version-info"
          >
            数据集版本: {{ data.dataset_version }}
          </p>
        </template>
      </el-empty>
    </template>

    <template v-else-if="isAvailable && data">
      <!-- 版本身份 -->
      <div
        class="lineage-identity"
        data-testid="lineage-identity"
      >
        <el-descriptions
          :column="1"
          size="small"
          border
        >
          <el-descriptions-item label="对象">
            {{ data.display_name }} ({{ data.object }})
          </el-descriptions-item>
          <el-descriptions-item label="业务键">
            <span
              v-for="(pair, i) in data.object_key"
              :key="i"
            >
              {{ pair[0] }}={{ pair[1] }}<span v-if="Number(i) < data.object_key.length - 1">, </span>
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="数据集版本">
            {{ data.dataset_version }}
          </el-descriptions-item>
          <el-descriptions-item label="模板版本">
            {{ data.template_version }}
          </el-descriptions-item>
          <el-descriptions-item label="Binding">
            <code class="hash">{{ data.binding_hash?.slice(0, 20) }}…</code>
            ({{ data.binding_status }})
          </el-descriptions-item>
          <el-descriptions-item label="映射批次">
            {{ data.map_batch_id }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <!-- 字段列表 -->
      <div
        class="lineage-fields"
        data-testid="lineage-fields"
      >
        <el-collapse accordion>
          <el-collapse-item
            v-for="field in data.fields"
            :key="field.property"
            :name="field.property"
          >
            <template #title>
              <div
                class="field-title"
                :data-testid="`field-${field.property}`"
              >
                <span class="field-name">{{ field.display_name }}</span>
                <code class="field-prop">{{ field.property }}</code>
                <el-tag
                  :type="fieldStateTag(field.state)"
                  size="small"
                  class="field-state"
                >
                  {{ field.state === 'available' ? '可追溯' : field.reason_code ?? '不可追溯' }}
                </el-tag>
                <span class="field-value">{{ formatEvidence(field.final_value) }}</span>
              </div>
            </template>

            <div
              v-if="field.state === 'unavailable'"
              class="field-unavailable"
            >
              <el-tag
                type="warning"
                size="small"
              >
                {{ field.reason_code }}
              </el-tag>
            </div>

            <template v-else>
              <!-- 转换步骤时间线 -->
              <h4 class="section-title">
                转换步骤
              </h4>
              <el-timeline
                v-if="(field.steps ?? []).length"
                class="step-timeline"
              >
                <el-timeline-item
                  v-for="(step, si) in field.steps"
                  :key="si"
                  :type="step.kind === 'read' ? 'primary' : 'success'"
                  size="small"
                >
                  <div
                    class="step-item"
                    :data-testid="`step-${field.property}-${si}`"
                  >
                    <strong>{{ stepLabel(step.kind) }}</strong>
                    <span v-if="step.map_hit !== null && step.map_hit !== undefined">
                      {{ step.map_hit ? '✓ 命中' : '✗ 未命中' }}
                    </span>
                    <span v-if="step.coerce_type"> → {{ step.coerce_type }}</span>
                    <span v-if="step.derived_rule_index !== null && step.derived_rule_index !== undefined">
                      #{{ step.derived_rule_index }}
                    </span>
                    <div
                      v-if="step.derived_when"
                      class="step-when"
                      :data-testid="`when-${field.property}-${si}`"
                    >
                      条件:
                      <span
                        v-for="(val, col) in step.derived_when"
                        :key="col"
                        class="when-cond"
                      >
                        {{ col }}={{ val }}
                      </span>
                    </div>
                    <div
                      v-if="step.before"
                      class="step-values"
                    >
                      <span class="before">{{ formatEvidence(step.before) }}</span>
                      <span class="arrow"> → </span>
                      <span class="after">{{ formatEvidence(step.after) }}</span>
                    </div>
                  </div>
                </el-timeline-item>
              </el-timeline>
              <p
                v-else
                class="no-steps"
              >
                无转换步骤
              </p>

              <!-- 输入边 -->
              <h4
                v-if="(field.inputs ?? []).length"
                class="section-title"
              >
                源数据输入
              </h4>
              <div
                v-for="(input, ii) in (field.inputs ?? [])"
                :key="ii"
                class="input-card"
                :data-testid="`input-${field.property}-${ii}`"
              >
                <el-tag
                  size="small"
                  :type="input.role === 'value' ? '' : input.role === 'join_fk' ? 'warning' : 'info'"
                >
                  {{ input.role }}
                </el-tag>
                <span
                  v-if="input.source_table"
                  class="input-source"
                >
                  {{ input.source_table }}<span v-if="input.source_column">.{{ input.source_column }}</span>
                </span>
                <span
                  v-if="input.source_value"
                  class="input-value"
                >
                  = {{ formatEvidence(input.source_value) }}
                </span>
                <span
                  v-if="input.source_pk"
                  class="input-pk"
                  :data-testid="`pk-${field.property}-${ii}`"
                >
                  源记录: {{ formatPk(input.source_pk) }}
                </span>
                <span
                  v-if="input.extract_batch_id"
                  class="input-batch"
                >
                  批次: {{ input.extract_batch_id }}
                </span>
                <span
                  v-if="input.join"
                  class="input-join"
                >
                  join: {{ input.join.target_table }}.{{ input.join.fk_column }}
                </span>
              </div>
            </template>
          </el-collapse-item>
        </el-collapse>
      </div>
    </template>

    <el-empty
      v-else
      description="选择一行对象数据后点击「血缘」查看"
    />
  </el-drawer>
</template>

<style scoped>
.drawer-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.readonly-note {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 12px;
  padding: 6px 10px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
.lineage-identity {
  margin-bottom: 16px;
}
.hash {
  font-size: 11px;
}
.version-info {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.field-title {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  overflow: hidden;
}
.field-name {
  font-weight: 500;
}
.field-prop {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.field-state {
  flex-shrink: 0;
}
.field-value {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-text-color-regular);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.section-title {
  font-size: 13px;
  margin: 8px 0 4px;
  color: var(--el-text-color-secondary);
}
.step-item {
  font-size: 13px;
}
.step-values {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.step-values .arrow {
  color: var(--el-color-primary);
}
.no-steps {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
.input-card {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 4px 0;
  font-size: 12px;
}
.input-source {
  font-family: monospace;
}
.input-value {
  color: var(--el-text-color-regular);
}
.input-pk {
  font-family: monospace;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.input-batch,
.input-join {
  color: var(--el-text-color-secondary);
}
.step-when {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.when-cond {
  font-family: monospace;
  margin-right: 6px;
}
.field-unavailable {
  padding: 8px 0;
}
</style>
