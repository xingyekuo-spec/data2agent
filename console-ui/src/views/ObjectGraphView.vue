<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { Refresh, Search } from '@element-plus/icons-vue'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { useTemplatesStore } from '@/stores/templates'
import { makeDomainColor } from '@/utils/domain'
import type { components } from '@/types/api'

type TemplateObject = components['schemas']['TemplateObject']
type TemplateRelation = components['schemas']['TemplateRelation']
type TagType = 'success' | 'warning' | 'info' | 'danger'
type MaterializedState = 'materialized' | 'not_materialized' | 'unknown'

type GraphNode = {
  object: string
  display_name: string
  domain: string
  rows: number | null
  state: MaterializedState
  outgoing: number
  incoming: number
}

type GraphEdge = {
  source: string
  sourceName: string
  target: string
  targetName: string
  relation: string
  cardinality: string
  desc: string
}

const store = useTemplatesStore()
const { templates, templatesRefreshError } = storeToRefs(store)
const router = useRouter()
const selectedObjectName = ref('')
const keyword = ref('')

onMounted(() => {
  void store.fetchTemplates()
})

const objects = computed(() =>
  templates.value.status === 'success' ? templates.value.data : [],
)

const objectByName = computed(() => {
  const map = new Map<string, TemplateObject>()
  for (const obj of objects.value) map.set(obj.object, obj)
  return map
})

const graphEdges = computed<GraphEdge[]>(() => {
  const edges: GraphEdge[] = []
  for (const obj of objects.value) {
    for (const rel of obj.relations ?? []) {
      const target = objectByName.value.get(rel.target)
      edges.push({
        source: obj.object,
        sourceName: obj.display_name,
        target: rel.target,
        targetName: target?.display_name ?? rel.target,
        relation: rel.name,
        cardinality: rel.cardinality,
        desc: rel.desc ?? '',
      })
    }
  }
  return edges
})

const graphNodes = computed<GraphNode[]>(() => {
  const incoming = new Map<string, number>()
  const outgoing = new Map<string, number>()
  for (const edge of graphEdges.value) {
    outgoing.set(edge.source, (outgoing.get(edge.source) ?? 0) + 1)
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1)
  }
  return objects.value.map((obj) => ({
    object: obj.object,
    display_name: obj.display_name,
    domain: obj.domain ?? '未分组',
    rows: obj.materialized?.rows ?? null,
    state: obj.materialized?.state ?? 'unknown',
    outgoing: outgoing.get(obj.object) ?? 0,
    incoming: incoming.get(obj.object) ?? 0,
  }))
})

const domains = computed(() =>
  [...new Set(graphNodes.value.map((node) => node.domain))].sort(),
)

const domainColor = computed(() => makeDomainColor(domains.value))

function openInClasses(objectName: string): void {
  void router.push({ path: '/ontology/classes', query: { object: objectName } })
}

const selectedObject = computed(() =>
  objectByName.value.get(selectedObjectName.value) ?? objects.value[0] ?? null,
)

const selectedNode = computed(() =>
  graphNodes.value.find((node) => node.object === selectedObject.value?.object) ?? null,
)

const selectedEdges = computed(() => {
  if (!selectedObject.value) return []
  return graphEdges.value.filter((edge) =>
    edge.source === selectedObject.value?.object || edge.target === selectedObject.value?.object,
  )
})

const selectedOutgoingRelations = computed<TemplateRelation[]>(() =>
  (selectedObject.value?.relations ?? []) as TemplateRelation[],
)

const filteredNodes = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return graphNodes.value
  return graphNodes.value.filter((node) =>
    `${node.object} ${node.display_name} ${node.domain}`.toLowerCase().includes(q),
  )
})

const filteredNodeNames = computed(() => new Set(filteredNodes.value.map((node) => node.object)))

const visibleEdges = computed(() =>
  graphEdges.value.filter((edge) =>
    filteredNodeNames.value.has(edge.source) || filteredNodeNames.value.has(edge.target),
  ),
)

function materializedTag(s: MaterializedState): { type: TagType; label: string } {
  const map: Record<MaterializedState, { type: TagType; label: string }> = {
    materialized: { type: 'success', label: '已构建' },
    not_materialized: { type: 'warning', label: '未构建' },
    unknown: { type: 'info', label: '未知' },
  }
  return map[s]
}

function selectObject(objectName: string): void {
  selectedObjectName.value = objectName
}

function relationTone(edge: GraphEdge): 'out' | 'in' {
  return edge.source === selectedObject.value?.object ? 'out' : 'in'
}
</script>

<template>
  <section class="object-graph-page">
    <header class="page-head">
      <div>
        <h1>对象关系</h1>
        <p>对象构建状态与引用清单:物化状态、行数与 relations 入/出边;节点按领域着色。结构图谱见「拓扑」。</p>
      </div>
      <el-button
        :icon="Refresh"
        data-testid="object-graph-refresh"
        @click="store.fetchTemplates()"
      >
        刷新
      </el-button>
    </header>

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
        data-testid="object-graph-refresh-error"
      >
        刷新失败({{ templatesRefreshError.message }}),展示上一次成功数据
      </p>

      <section class="graph-toolbar">
        <el-input
          v-model="keyword"
          :prefix-icon="Search"
          clearable
          placeholder="搜索对象、显示名或领域"
          data-testid="object-graph-search"
        />
        <div
          class="stats"
          data-testid="object-graph-stats"
        >
          <span>{{ graphNodes.length }} 对象</span>
          <span>{{ graphEdges.length }} 关系</span>
          <span>{{ domains.length }} 领域</span>
        </div>
      </section>

      <!-- 领域图例(与节点着色同源) -->
      <div
        v-if="objects.length"
        class="domain-legend"
        data-testid="object-graph-legend"
      >
        <span
          v-for="d in domains"
          :key="d"
          class="domain-legend__item"
        >
          <i
            class="domain-dot"
            :style="{ background: domainColor(d) }"
          />{{ d }}
        </span>
      </div>

      <EmptyState
        v-if="objects.length === 0"
        title="没有模板对象"
      />
      <section
        v-else
        class="graph-workbench"
        data-testid="object-graph"
      >
        <main class="graph-panel">
          <div class="panel-head">
            <h2>对象节点</h2>
            <span>{{ filteredNodes.length }} 个</span>
          </div>
          <div class="graph-nodes">
            <button
              v-for="node in filteredNodes"
              :key="node.object"
              type="button"
              class="graph-node"
              :class="{ 'graph-node--selected': selectedObject?.object === node.object }"
              :style="{ borderLeftColor: domainColor(node.domain) }"
              :data-testid="`graph-node-${node.object}`"
              @click="selectObject(node.object)"
            >
              <span class="graph-node__title">{{ node.display_name }}</span>
              <span class="graph-node__object">{{ node.object }}</span>
              <span class="graph-node__meta">
                {{ node.domain }} · 入{{ node.incoming }} / 出{{ node.outgoing }}
              </span>
              <span class="graph-node__footer">
                <el-tag
                  size="small"
                  :type="materializedTag(node.state).type"
                >
                  {{ materializedTag(node.state).label }}
                </el-tag>
                <span>{{ node.rows ?? '—' }} 行</span>
              </span>
            </button>
          </div>
        </main>

        <aside class="edge-panel">
          <div class="panel-head">
            <h2>关系边</h2>
            <span>{{ visibleEdges.length }} 条</span>
          </div>
          <div
            v-if="visibleEdges.length === 0"
            class="graph-empty"
          >
            暂无关系定义
          </div>
          <button
            v-for="edge in visibleEdges"
            v-else
            :key="`${edge.source}:${edge.relation}:${edge.target}`"
            type="button"
            class="graph-edge"
            :class="{
              'graph-edge--selected': selectedObject?.object === edge.source || selectedObject?.object === edge.target,
            }"
            @click="selectObject(edge.source)"
          >
            <span class="graph-edge__objects">
              {{ edge.source }}
              <span>→</span>
              {{ edge.target }}
            </span>
            <span class="graph-edge__relation">
              {{ edge.relation }} · {{ edge.cardinality }}
            </span>
          </button>
        </aside>

        <aside
          class="detail-panel"
          data-testid="object-graph-detail"
        >
          <template v-if="selectedObject && selectedNode">
            <div class="panel-head">
              <div>
                <h2>{{ selectedObject.display_name }}</h2>
                <span>{{ selectedObject.object }}</span>
              </div>
              <el-tag
                size="small"
                :type="materializedTag(selectedNode.state).type"
              >
                {{ materializedTag(selectedNode.state).label }}
              </el-tag>
            </div>
            <div class="detail-actions">
              <el-button
                size="small"
                text
                type="primary"
                data-testid="object-graph-open-class"
                @click="openInClasses(selectedObject.object)"
              >
                在类页查看属性 / 绑定
              </el-button>
            </div>
            <dl class="object-facts">
              <dt>领域</dt>
              <dd>{{ selectedNode.domain }}</dd>
              <dt>行数</dt>
              <dd>{{ selectedNode.rows ?? '—' }}</dd>
              <dt>业务键</dt>
              <dd>{{ selectedObject.keys.join(', ') || '—' }}</dd>
              <dt>入边/出边</dt>
              <dd>{{ selectedNode.incoming }}/{{ selectedNode.outgoing }}</dd>
            </dl>

            <div class="section-title">
              关联关系
            </div>
            <EmptyState
              v-if="selectedEdges.length === 0"
              title="该对象暂无关系"
            />
            <div
              v-for="edge in selectedEdges"
              v-else
              :key="`${edge.source}:${edge.relation}:${edge.target}`"
              class="relation-card"
              :class="`relation-card--${relationTone(edge)}`"
            >
              <div class="relation-card__line">
                <button
                  type="button"
                  @click="selectObject(edge.source)"
                >
                  {{ edge.sourceName }} <span>{{ edge.source }}</span>
                </button>
                <strong>{{ relationTone(edge) === 'out' ? '→' : '←' }}</strong>
                <button
                  type="button"
                  @click="selectObject(edge.target)"
                >
                  {{ edge.targetName }} <span>{{ edge.target }}</span>
                </button>
              </div>
              <div class="relation-card__meta">
                <el-tag
                  size="small"
                  type="info"
                >
                  {{ edge.relation }}
                </el-tag>
                <el-tag size="small">
                  {{ edge.cardinality }}
                </el-tag>
                <span>{{ edge.desc || '—' }}</span>
              </div>
            </div>

            <div class="section-title">
              出边定义
            </div>
            <el-table
              :data="selectedOutgoingRelations"
              size="small"
              data-testid="object-graph-outgoing"
            >
              <el-table-column
                prop="name"
                label="关系名"
                min-width="150"
              />
              <el-table-column
                prop="target"
                label="目标对象"
                min-width="150"
              >
                <template #default="{ row }">
                  <el-button
                    link
                    type="primary"
                    @click="selectObject(row.target)"
                  >
                    {{ row.target }}
                  </el-button>
                </template>
              </el-table-column>
              <el-table-column
                prop="cardinality"
                label="基数"
                width="90"
              />
            </el-table>
          </template>
        </aside>
      </section>
    </template>
  </section>
</template>

<style scoped>
.object-graph-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.page-head h1,
.panel-head h2 {
  margin: 0;
}

.page-head p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
}

.refresh-warning {
  padding: 10px 12px;
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  border-radius: 6px;
}

.graph-toolbar {
  display: grid;
  grid-template-columns: minmax(260px, 420px) 1fr;
  gap: 12px;
  align-items: center;
}

.stats {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.graph-workbench {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px 390px;
  gap: 12px;
  align-items: start;
}

.graph-panel,
.edge-panel,
.detail-panel {
  min-width: 0;
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
  background: var(--d2a-surface);
  padding: 14px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-head span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.graph-nodes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 8px;
}

.graph-node,
.graph-edge {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.graph-node {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 112px;
  padding: 10px;
  border-left-width: 3px;
}

.domain-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.domain-legend__item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.domain-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.detail-actions {
  margin: -4px 0 10px;
}

.graph-node:hover,
.graph-edge:hover {
  border-color: var(--el-color-primary);
}

.graph-node--selected,
.graph-edge--selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.graph-node__title {
  font-weight: 600;
}

.graph-node__object,
.graph-node__meta,
.graph-node__footer,
.graph-edge__relation,
.graph-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.graph-node__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
}

.edge-panel {
  max-height: calc(100vh - 190px);
  overflow-y: auto;
}

.graph-edge {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 8px 10px;
  margin-bottom: 6px;
}

.graph-edge__objects {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
}

.graph-edge__objects span {
  color: var(--el-color-primary);
}

.object-facts {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 8px 10px;
  margin: 0;
}

.object-facts dt {
  color: var(--el-text-color-secondary);
}

.object-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.section-title {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
}

.relation-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
}

.relation-card--out {
  border-left: 3px solid var(--el-color-primary);
}

.relation-card--in {
  border-left: 3px solid var(--el-color-success);
}

.relation-card__line {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 24px minmax(0, 1fr);
  gap: 8px;
  align-items: center;
}

.relation-card__line button {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--el-color-primary);
  cursor: pointer;
  text-align: left;
  overflow-wrap: anywhere;
}

.relation-card__line button span {
  display: block;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.relation-card__line strong {
  text-align: center;
  color: var(--el-color-primary);
}

.relation-card__meta {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

@media (max-width: 1280px) {
  .graph-workbench {
    grid-template-columns: 1fr;
  }

  .edge-panel {
    max-height: none;
  }
}

@media (max-width: 760px) {
  .graph-toolbar,
  .page-head {
    grid-template-columns: 1fr;
    flex-direction: column;
  }

  .stats {
    justify-content: flex-start;
  }
}
</style>
