<script setup lang="ts">
// 本体库 · 拓扑(B 类可视化页):本体结构图谱——类 + 属性 + 关系 + 规则编码。
// 数据从 /api/templates 前端派生;节点交互全部页内完成:点类节点就地展开/收起
// 该类属性层,点属性节点就地弹详情抽屉(不跳转其他页面)。
// 规则视觉编码:敏感属性橙描边、业务键菱形、枚举 tooltip;类节点按领域着色。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import OntologyGraph, {
  type OntologyGraphLink,
  type OntologyGraphNode,
} from '@/components/ontology/OntologyGraph.vue'
import PropertyDetailDrawer from '@/components/ontology/PropertyDetailDrawer.vue'
import { useTemplatesStore } from '@/stores/templates'
import { makeDomainColor } from '@/utils/domain'

const PROPERTY_CATEGORY = '属性'
const PROPERTY_COLOR = '#94a3b8'
const SENSITIVE_BORDER = '#d97706'

const store = useTemplatesStore()
const { templates, templatesRefreshError } = storeToRefs(store)
const router = useRouter()
const domainFilter = ref('')
const showProperties = ref(false)
/** 就地展开属性层的类(点类节点切换);showProperties 为全局展开 */
const expandedClasses = ref<ReadonlySet<string>>(new Set())
/** 属性详情抽屉目标(点属性节点就地打开) */
const propDetail = ref<{ owner: string; name: string } | null>(null)

onMounted(() => {
  void store.fetchTemplates()
})

const objects = computed(() =>
  templates.value.status === 'success' ? templates.value.data : [],
)
const domains = computed(() =>
  [...new Set(objects.value.map((o) => o.domain ?? '未分组'))].sort(),
)
const domainColor = computed(() => makeDomainColor(domains.value))

const filteredObjects = computed(() =>
  objects.value.filter((o) =>
    !domainFilter.value || (o.domain ?? '未分组') === domainFilter.value),
)
const includedClassNames = computed(() =>
  new Set(filteredObjects.value.map((o) => o.object)),
)

/** 该类是否展开属性层(全局开关或逐类展开) */
function propertyLayerOn(objectName: string): boolean {
  return showProperties.value || expandedClasses.value.has(objectName)
}

const objectByName = computed(() => {
  const map = new Map<string, (typeof objects.value)[number]>()
  for (const o of objects.value) map.set(o.object, o)
  return map
})
const propDetailObject = computed(() =>
  propDetail.value ? (objectByName.value.get(propDetail.value.owner) ?? null) : null,
)

const categories = computed(() => [...domains.value, PROPERTY_CATEGORY])
const categoryColors = computed(() => [
  ...domains.value.map((d) => domainColor.value(d)),
  PROPERTY_COLOR,
])

/** 图数据派生(节点/边);jsdom 测试可直接断言组件 props */
const graphNodes = computed<OntologyGraphNode[]>(() => {
  const nodes: OntologyGraphNode[] = []
  for (const o of filteredObjects.value) {
    const domain = o.domain ?? '未分组'
    nodes.push({
      id: `class:${o.object}`,
      name: o.display_name,
      kind: 'class',
      category: categories.value.indexOf(domain),
      symbolSize: 30 + Math.min(o.properties.length, 24),
      refObject: o.object,
      tip: [
        `${o.object} · ${domain}`,
        `属性 ${o.properties.length} · 关系 ${o.relations?.length ?? 0}`,
        o.description ?? '',
      ].join('<br/>'),
    })
    if (propertyLayerOn(o.object)) {
      for (const p of o.properties) {
        nodes.push({
          id: `prop:${o.object}.${p.name}`,
          name: p.name,
          kind: 'property',
          category: categories.value.indexOf(PROPERTY_CATEGORY),
          symbol: o.keys.includes(p.name) ? 'diamond' : 'circle',
          symbolSize: o.keys.includes(p.name) ? 14 : 10,
          itemStyle: p.sensitive
            ? { borderColor: SENSITIVE_BORDER, borderWidth: 2 }
            : undefined,
          refObject: o.object,
          propName: p.name,
          tip: [
            `${o.object}.${p.name} · ${p.type}`,
            p.sensitive ? '敏感(出网默认脱敏)' : '',
            p.enum_values?.length ? `枚举 ${p.enum_values.join(' / ')}` : '',
            p.ref ? `引用 → ${p.ref}` : '',
            p.desc ?? '',
          ].filter(Boolean).join('<br/>'),
        })
      }
    }
  }
  return nodes
})

const graphLinks = computed<OntologyGraphLink[]>(() => {
  const links: OntologyGraphLink[] = []
  for (const o of filteredObjects.value) {
    for (const rel of o.relations ?? []) {
      if (!includedClassNames.value.has(rel.target)) {
        continue
      }
      links.push({
        source: `class:${o.object}`,
        target: `class:${rel.target}`,
        labelText: `${rel.name} ${rel.cardinality}`,
      })
    }
    if (propertyLayerOn(o.object)) {
      for (const p of o.properties) {
        links.push({
          source: `class:${o.object}`,
          target: `prop:${o.object}.${p.name}`,
          lineStyle: { type: 'solid', opacity: 0.35 },
        })
        if (p.ref && includedClassNames.value.has(p.ref)) {
          links.push({
            source: `prop:${o.object}.${p.name}`,
            target: `class:${p.ref}`,
            labelText: 'ref',
            lineStyle: { type: 'dashed' },
          })
        }
      }
    }
  }
  return links
})

function onNodeClick(node: OntologyGraphNode): void {
  if (node.kind === 'class') {
    // 就地展开/收起该类属性层,不跳转
    const next = new Set(expandedClasses.value)
    if (next.has(node.refObject)) {
      next.delete(node.refObject)
    } else {
      next.add(node.refObject)
    }
    expandedClasses.value = next
  } else if (node.propName) {
    // 就地弹属性详情抽屉,不跳转
    propDetail.value = { owner: node.refObject, name: node.propName }
  }
}

function viewPropOwnerClass(): void {
  if (propDetail.value) {
    void router.push({ path: '/ontology/classes', query: { object: propDetail.value.owner } })
  }
}
</script>

<template>
  <section class="topology-page">
    <LoadingState v-if="templates.status === 'idle' || templates.status === 'loading'" />
    <ErrorState
      v-else-if="templates.status === 'error'"
      :error="templates.error"
      @retry="store.fetchTemplates()"
    />
    <template v-else>
      <p
        v-if="templatesRefreshError"
        class="refresh-warning"
        data-testid="topology-refresh-error"
      >
        刷新失败({{ templatesRefreshError.message }}),展示上一次成功数据
      </p>

      <section class="topology-toolbar">
        <el-select
          v-model="domainFilter"
          placeholder="领域"
          clearable
          size="small"
          data-testid="filter-domain"
        >
          <el-option
            v-for="d in domains"
            :key="d"
            :label="d"
            :value="d"
          />
        </el-select>
        <label
          class="prop-toggle"
          data-testid="property-toggle"
        >
          <el-switch v-model="showProperties" />
          展开属性层
        </label>
        <el-button
          class="toolbar-refresh"
          size="small"
          data-testid="topology-refresh"
          @click="store.fetchTemplates()"
        >
          刷新
        </el-button>
      </section>

      <EmptyState
        v-if="objects.length === 0"
        title="没有本体类"
      />
      <OntologyGraph
        v-else
        :nodes="graphNodes"
        :links="graphLinks"
        :categories="categories"
        :category-colors="categoryColors"
        @node-click="onNodeClick"
      />

      <!-- 属性详情抽屉(共享组件,页内就地查看,不跳转属性页) -->
      <PropertyDetailDrawer
        :visible="propDetail !== null"
        :object="propDetailObject"
        :prop-name="propDetail?.name ?? null"
        @close="propDetail = null"
        @view-class="viewPropOwnerClass"
      />
    </template>
  </section>
</template>

<style scoped>
.topology-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.refresh-warning {
  padding: 10px 12px;
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  border-radius: 6px;
}

.topology-toolbar {
  display: flex;
  gap: 16px;
  align-items: center;
}

.topology-toolbar .el-select {
  width: 140px;
}

.prop-toggle {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.toolbar-refresh {
  margin-left: auto;
}
</style>
