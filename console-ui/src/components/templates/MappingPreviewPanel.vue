<script setup lang="ts">
/**
 * Mapping Preview 控件(M3-T07):source/样本/临时草稿 + 提交打开结果抽屉。
 * 入口挂在模板页已选对象区域;无保存/发布动作。
 */
import { computed, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import MappingPreviewDrawer from '@/components/templates/MappingPreviewDrawer.vue'
import { useMappingPreviewStore } from '@/stores/mappingPreview'
import type { components } from '@/types/api'

type TemplateBinding = components['schemas']['TemplateBinding']
type MappingPreviewDraftBinding = components['schemas']['MappingPreviewDraftBinding']

const props = defineProps<{
  objectName: string
  bindings: TemplateBinding[]
  /** 配置/模板允许的数据源(含无当前 binding 的源),用于新草稿入口 */
  allowedSources?: string[]
}>()

const store = useMappingPreviewStore()
const {
  source,
  sample,
  useDraft,
  draftText,
  draftParseError,
  isLoading,
} = storeToRefs(store)

const drawerOpen = ref(false)
const submitting = ref(false)

const sourceOptions = computed(() => {
  const seen = new Set<string>()
  const opts: { value: string; label: string }[] = []
  for (const b of props.bindings) {
    if (seen.has(b.source)) continue
    seen.add(b.source)
    const tag = b.enabled === false ? '已停用' : b.status
    opts.push({ value: b.source, label: `${b.source} (${tag})` })
  }
  for (const src of props.allowedSources ?? []) {
    if (seen.has(src)) continue
    seen.add(src)
    opts.push({ value: src, label: `${src} (无当前绑定)` })
  }
  return opts
})

const selectedBinding = computed(() =>
  props.bindings.find((b) => b.source === source.value) ?? null,
)

const canSubmit = computed(() => {
  if (submitting.value || isLoading.value || !source.value) return false
  // 无当前 binding 时必须走临时草稿
  if (!selectedBinding.value && !useDraft.value) return false
  return true
})

const localJsonHint = computed(() => {
  if (!useDraft.value) return null
  const text = draftText.value.trim()
  if (!text) return '草稿为空(本地提示;最终以服务端 422 为准)'
  try {
    JSON.parse(text)
    return null
  } catch {
    return '草稿不是合法 JSON(本地提示;最终以服务端 422 为准)'
  }
})

const batchIdModel = computed({
  get: () => sample.value.batch_id ?? '',
  set: (v: string) => {
    const trimmed = v.trim()
    store.setSample({ batch_id: trimmed === '' ? null : trimmed })
  },
})

function bindingToDraft(binding: TemplateBinding | null): MappingPreviewDraftBinding {
  if (!binding) {
    return {
      tables: [],
      key_map: {},
      field_map: {},
      derived: {},
      watermark: null,
      notes: '',
    }
  }
  return {
    tables: [...binding.tables],
    key_map: { ...(binding.key_map ?? {}) },
    field_map: { ...(binding.field_map ?? {}) },
    derived: binding.derived ? structuredClone(binding.derived) : {},
    watermark: binding.watermark ?? null,
    notes: binding.notes ?? '',
  }
}

function enableTempDraft(): void {
  store.setUseDraft(true)
  store.setDraft(null)
  store.setDraftText(JSON.stringify(bindingToDraft(selectedBinding.value), null, 2))
}

function useCurrentBinding(): void {
  store.setUseDraft(false)
  store.setDraft(null)
}

function onDraftTextInput(value: string): void {
  store.setDraft(null)
  store.setDraftText(value)
}

async function onSubmit(): Promise<void> {
  if (submitting.value || isLoading.value) return
  submitting.value = true
  drawerOpen.value = true
  try {
    await store.submit()
  } finally {
    submitting.value = false
  }
}

function onRetry(): void {
  void onSubmit()
}

watch(
  () => props.objectName,
  (name) => {
    if (name) store.setObject(name)
  },
  { immediate: true },
)

watch(
  () => source.value,
  () => {
    if (source.value && !selectedBinding.value && !useDraft.value) {
      enableTempDraft()
    }
  },
)

watch(
  () => [props.objectName, props.bindings, props.allowedSources] as const,
  () => {
    const opts = sourceOptions.value
    if (!opts.length) {
      if (source.value) store.setSource('')
      return
    }
    if (!opts.some((o) => o.value === source.value)) {
      const preferred =
        props.bindings.find((b) => b.enabled !== false)?.source
        ?? opts[0]!.value
      store.setSource(preferred)
    }
    // 选中源无当前 binding 时自动打开临时草稿,否则无法提交
    if (source.value && !selectedBinding.value && !useDraft.value) {
      enableTempDraft()
    }
  },
  { immediate: true, deep: true },
)
</script>

<template>
  <div class="preview-panel d2a-card" data-testid="mapping-preview-panel">
    <div class="toolbar">
      <h3 class="card-title">映射预览</h3>
      <span class="muted">只读试算,不会保存或发布</span>
    </div>

    <div class="form-grid" data-testid="preview-controls">
      <label>
        数据源
        <select
          :value="source"
          data-testid="preview-source"
          @change="store.setSource(($event.target as HTMLSelectElement).value)"
        >
          <option v-if="!sourceOptions.length" value="" disabled>无可用数据源</option>
          <option
            v-for="opt in sourceOptions"
            :key="opt.value"
            :value="opt.value"
          >{{ opt.label }}</option>
        </select>
      </label>

      <label>
        批次 ID(可空)
        <input
          v-model="batchIdModel"
          type="text"
          placeholder="留空=不按批次过滤"
          data-testid="preview-batch-id"
        />
      </label>

      <div class="inline-row">
        <label>
          offset
          <input
            :value="sample.offset"
            type="number"
            min="0"
            max="10000"
            data-testid="preview-offset"
            @change="store.setSample({ offset: Number(($event.target as HTMLInputElement).value) || 0 })"
          />
        </label>
        <label>
          limit
          <input
            :value="sample.limit"
            type="number"
            min="1"
            max="200"
            data-testid="preview-limit"
            @change="store.setSample({ limit: Number(($event.target as HTMLInputElement).value) || 50 })"
          />
        </label>
      </div>

      <div class="draft-actions">
        <el-button
          size="small"
          data-testid="preview-use-draft"
          @click="enableTempDraft"
        >使用临时草稿</el-button>
        <el-button
          v-if="useDraft"
          size="small"
          data-testid="preview-use-current"
          @click="useCurrentBinding"
        >改用当前绑定</el-button>
      </div>

      <template v-if="useDraft">
        <label>
          临时草稿 JSON(仅浏览器内存)
          <textarea
            :value="draftText"
            rows="10"
            data-testid="preview-draft-text"
            @input="onDraftTextInput(($event.target as HTMLTextAreaElement).value)"
          />
        </label>
        <p
          v-if="localJsonHint || draftParseError"
          class="json-hint"
          data-testid="preview-json-hint"
        >{{ localJsonHint || draftParseError }}</p>
      </template>

      <div class="submit-row">
        <el-button
          type="primary"
          size="small"
          data-testid="preview-submit"
          :disabled="!canSubmit"
          @click="onSubmit"
        >预览映射</el-button>
      </div>
    </div>

    <MappingPreviewDrawer
      v-model:open="drawerOpen"
      @retry="onRetry"
    />
  </div>
</template>

<style scoped>
.preview-panel {
  margin-bottom: 8px;
}

.toolbar {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}

.card-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.form-grid {
  display: grid;
  gap: 10px;
  max-width: 720px;
}

.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}

.form-grid input,
.form-grid textarea,
.form-grid select {
  font: inherit;
  padding: 6px 8px;
}

.inline-row {
  display: flex;
  gap: 12px;
}

.inline-row label {
  flex: 1;
}

.draft-actions,
.submit-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.json-hint {
  margin: 0;
  font-size: 12px;
  color: var(--d2a-status-warning);
}
</style>
