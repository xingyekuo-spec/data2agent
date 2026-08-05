<script setup lang="ts">
/**
 * 属性详情抽屉(本体库共享):属性定义 + 各来源映射表达式 + 安全 JSON。
 * 由属性字典页与拓扑页共用;「查看所属类」经 view-class 事件交父页面决定跳转。
 */
import { computed, ref } from 'vue'
import EmptyState from '@/components/shared/EmptyState.vue'
import type { components } from '@/types/api'

type TemplateObject = components['schemas']['TemplateObject']

const props = defineProps<{
  visible: boolean
  /** 所属类(未找到传 null) */
  object: TemplateObject | null
  /** 属性名(未选择传 null) */
  propName: string | null
}>()
const emit = defineEmits<{ close: []; 'view-class': [] }>()

const showJson = ref(false)

const row = computed(() => {
  if (!props.object || !props.propName) {
    return null
  }
  const p = props.object.properties.find((x) => x.name === props.propName)
  if (!p) {
    return null
  }
  return {
    name: p.name,
    type: p.type,
    desc: p.desc ?? null,
    sensitive: p.sensitive,
    ref: p.ref ?? null,
    enum_values: p.enum_values ?? [],
    object: props.object.object,
    objectDisplay: props.object.display_name,
    domain: props.object.domain ?? '未分组',
  }
})

/** 该属性在各 binding 下的映射表达式(field_map / key_map) */
const mappings = computed(() => {
  if (!row.value || !props.object) {
    return []
  }
  const name = row.value.name
  return props.object.bindings
    .map((b) => ({
      source: b.source,
      expr: b.field_map?.[name] ?? b.key_map?.[name] ?? null,
    }))
    .filter((m) => m.expr !== null)
})

function onClose(): void {
  showJson.value = false
  emit('close')
}
</script>

<template>
  <el-drawer
    :model-value="visible"
    size="560px"
    data-testid="prop-detail-drawer"
    @close="onClose"
  >
    <template #header>
      <div class="drawer-header">
        <span class="drawer-title">属性详情</span>
        <span
          v-if="row"
          class="drawer-target"
          data-testid="prop-detail-target"
        >{{ row.objectDisplay }}({{ row.object }}) · {{ row.name }}</span>
      </div>
    </template>
    <EmptyState
      v-if="!row"
      title="未找到该属性"
      hint="模板目录中不存在对应属性,可能已被移除"
    />
    <template v-else>
      <dl class="summary">
        <dt>属性</dt>
        <dd>{{ row.name }}</dd>
        <dt>所属类</dt>
        <dd>{{ row.objectDisplay }}({{ row.object }})</dd>
        <dt>领域</dt>
        <dd>{{ row.domain }}</dd>
        <dt>类型</dt>
        <dd>{{ row.type }}</dd>
        <dt>敏感</dt>
        <dd>{{ row.sensitive ? '是(出网默认脱敏)' : '否' }}</dd>
        <dt>枚举</dt>
        <dd>{{ row.enum_values.length ? row.enum_values.join(' / ') : '—' }}</dd>
        <dt>引用目标</dt>
        <dd>{{ row.ref ?? '—' }}</dd>
        <dt>说明</dt>
        <dd>{{ row.desc ?? '—' }}</dd>
      </dl>

      <div class="drawer-actions">
        <el-button
          size="small"
          type="primary"
          text
          data-testid="prop-view-class"
          @click="emit('view-class')"
        >
          查看所属类
        </el-button>
      </div>

      <h4>映射表达式({{ mappings.length }})</h4>
      <EmptyState
        v-if="mappings.length === 0"
        title="该属性没有映射表达式"
      />
      <el-table
        v-else
        :data="mappings"
        size="small"
        data-testid="prop-mappings-table"
      >
        <el-table-column
          prop="source"
          label="来源"
          width="130"
        />
        <el-table-column
          prop="expr"
          label="表达式"
          min-width="220"
        />
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
      >{{ JSON.stringify(row, null, 2) }}</pre>
    </template>
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
