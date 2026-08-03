<script setup lang="ts">
// 管道页:数据源(中间机) → 推送 → 落地 → 映射 → 对象层 → MCP 网关 流程。
// 多源:顶部源切换器(多中间机场景,数据源管理同源清单);
// erp/extract 折叠为「数据源(中间机)」起始节点(状态取两者折叠,详情跳 /sources)。
// 状态由后端 observability 计算;视图只展示,不重复推导业务规则。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { usePipelineStore } from '@/stores/pipeline'
import { useOverviewStore } from '@/stores/overview'
import { useSourcesStore } from '@/stores/sources'
import type { components } from '@/types/api'
import { formatDateTime, formatTimeHM } from '@/utils/time'

type PipelineNode = components['schemas']['PipelineNode']

const store = usePipelineStore()
const { pipeline, refreshError, currentSource } = storeToRefs(store)
const sourcesStore = useSourcesStore()
const { cards: sourceCards } = storeToRefs(sourcesStore)

/** 节点中文标签(node ID 仍是契约字段,仅展示层翻译) */
const NODE_LABELS: Record<string, string> = {
  datasource: '数据源(中间机)',
  erp: 'ERP',
  extract: '抽取',
  push: '推送',
  raw: '落地',
  mapping: '映射',
  objects: '对象层',
  mcp: 'MCP 网关',
}

function labelOf(nodeId: string): string {
  return NODE_LABELS[nodeId] ?? nodeId
}

// 只存节点 ID:轮询替换 pipeline 数据后,详情面板展示的是当前快照而不是旧对象
const selectedId = ref<string | null>(null)
const data = computed(() => (pipeline.value.status === 'success' ? pipeline.value.data : null))

/** 折叠优先级与后端 fold_status 一致 */
const FOLD_ORDER = ['failed', 'stale', 'warning', 'running', 'unknown', 'idle', 'healthy']
function foldStatus(statuses: string[]): string {
  for (const s of FOLD_ORDER) {
    if (statuses.includes(s)) return s
  }
  return 'unknown'
}

/** 流程节点:erp/extract 折叠为一个「数据源(中间机)」起始节点 */
const flowNodes = computed<PipelineNode[]>(() => {
  const nodes = data.value?.nodes ?? []
  const srcNodes = nodes.filter((n) => n.node === 'erp' || n.node === 'extract')
  const rest = nodes.filter((n) => n.node !== 'erp' && n.node !== 'extract')
  if (srcNodes.length === 0) {
    return rest
  }
  const successTimes = srcNodes
    .map((n) => n.last_success_at)
    .filter((t): t is string => t !== null)
    .sort()
  const synthesized: PipelineNode = {
    node: 'datasource',
    status: foldStatus(srcNodes.map((n) => n.status)) as PipelineNode['status'],
    status_reason: srcNodes.find((n) => n.status_reason)?.status_reason ?? '',
    observed_at: srcNodes.find((n) => n.observed_at)?.observed_at ?? null,
    last_success_at: successTimes.length ? successTimes[successTimes.length - 1]! : null,
    last_failure_at: srcNodes.find((n) => n.last_failure_at)?.last_failure_at ?? null,
    rows_in: null,
    rows_out: srcNodes.find((n) => n.rows_out !== null)?.rows_out ?? null,
    duration_ms: null,
    error: srcNodes.find((n) => n.error)?.error ?? null,
    version: null,
    run_id: srcNodes.find((n) => n.run_id)?.run_id ?? null,
    source: srcNodes[0]?.source ?? null,
    detail_path: '/sources',
  }
  return [synthesized, ...rest]
})

const selected = computed(() =>
  selectedId.value === null
    ? null
    : (flowNodes.value.find((n) => n.node === selectedId.value) ?? null),
)

function open(node: PipelineNode): void {
  selectedId.value = selectedId.value === node.node ? null : node.node
}

function close(): void {
  selectedId.value = null
}

function onEsc(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    close()
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('keydown', onEsc)
}
onBeforeUnmount(() => window.removeEventListener('keydown', onEsc))

function metricOf(node: PipelineNode): string {
  if (node.rows_out !== null) {
    return `${node.rows_out} 行`
  }
  if (node.duration_ms !== null) {
    return `${Math.round(node.duration_ms)}ms`
  }
  return ''
}

function lastSuccessOf(node: PipelineNode): string {
  return node.last_success_at ? `上次成功 ${formatTimeHM(node.last_success_at)}` : ''
}

function onSourceChange(value: string): void {
  close()
  store.setSource(value || null)
  void store.refresh()
}

onMounted(() => {
  if (sourceCards.value.status === 'idle') {
    void sourcesStore.refresh()
  }
})

const overviewStore = useOverviewStore()

/** 节点详情行动链接:按节点类型与状态给出下一步(只读面板 → 可行动) */
interface NodeAction {
  label: string
  to: string
}

const detailActions = computed<NodeAction[]>(() => {
  const n = selected.value
  if (!n) {
    return []
  }
  const actions: NodeAction[] = []
  const bad = n.status === 'failed' || n.status === 'stale'
  if (n.node === 'datasource') {
    actions.push({ label: '查看数据源', to: '/sources' })
  }
  if (n.node === 'push' && bad) {
    actions.push({ label: '检查数据源接入', to: '/sources' })
  }
  if (n.node === 'mapping' && (bad || n.status === 'warning')) {
    actions.push({ label: '去校准映射', to: '/templates' })
  }
  const quarantinePending = overviewStore.data?.summary.quarantine_pending ?? 0
  if ((n.node === 'mapping' || n.node === 'objects') && quarantinePending > 0) {
    actions.push({ label: `处理待确认数据(${quarantinePending})`, to: '/quarantine' })
  }
  if (n.node === 'mcp' && bad) {
    actions.push({ label: '打开 MCP Lab 验证', to: '/mcp' })
  }
  if (n.run_id) {
    actions.push({ label: '查看运行详情', to: `/runs?run_id=${n.run_id}` })
  }
  // detail_path(后端给出)优先且不重复
  const seen = new Set(n.detail_path ? [n.detail_path] : [])
  return actions.filter((a) => {
    if (seen.has(a.to)) {
      return false
    }
    seen.add(a.to)
    return true
  })
})

const detailFields = computed(() => {
  const n = selected.value
  if (!n) {
    return []
  }
  return [
    { label: '状态', value: n.status },
    { label: '原因', value: n.status_reason || '—' },
    { label: '观测时间', value: formatDateTime(n.observed_at) || '—' },
    { label: '最近成功', value: formatDateTime(n.last_success_at) || '—' },
    { label: '最近失败', value: formatDateTime(n.last_failure_at) || '—' },
    { label: '本次输入', value: n.rows_in === null ? '—' : `${n.rows_in} 行` },
    { label: '本次输出', value: n.rows_out === null ? '—' : `${n.rows_out} 行` },
    { label: '耗时', value: n.duration_ms === null ? '—' : `${Math.round(n.duration_ms)} ms` },
    { label: '错误', value: n.error ?? '—' },
    { label: '版本', value: n.version ?? '尚未发布' },
    { label: '运行 ID', value: n.run_id ?? '—' },
    { label: '来源', value: n.source ?? '—' },
  ]
})
</script>

<template>
  <section class="pipeline-page">
    <div
      v-if="refreshError"
      class="refresh-warning"
      data-testid="refresh-error"
    >
      刷新失败({{ refreshError.message }}),展示上一次成功数据
    </div>

    <LoadingState v-if="pipeline.status === 'idle' || pipeline.status === 'loading'" />
    <ErrorState
      v-else-if="pipeline.status === 'error'"
      :error="pipeline.error"
      @retry="store.refresh()"
    />
    <template v-else-if="data">
      <div class="overall d2a-card">
        <span class="overall__label">整体状态</span>
        <StatusBadge
          :status="data.overall_status"
          data-testid="pipeline-overall"
        />
        <el-select
          :model-value="currentSource ?? ''"
          class="overall__source"
          size="small"
          data-testid="pipeline-source"
          @change="onSourceChange"
        >
          <el-option
            label="默认数据源"
            value=""
          />
          <el-option
            v-for="s in sourceCards.status === 'success' ? sourceCards.data : []"
            :key="s.source"
            :label="s.display_name"
            :value="s.source"
          />
        </el-select>
        <span class="overall__time">截至 {{ formatDateTime(data.generated_at) }}</span>
      </div>

      <!-- 关键节点流程:连接线只表达顺序,不用绿色掩盖 unknown -->
      <ol
        class="flow"
        data-testid="pipeline-flow"
      >
        <template
          v-for="(node, i) in flowNodes"
          :key="node.node"
        >
          <li class="flow__node-wrap">
            <button
              type="button"
              class="flow__node"
              :class="`flow__node--${node.status}`"
              :data-status="node.status"
              :aria-pressed="selected?.node === node.node"
              :aria-label="`节点 ${labelOf(node.node)} 状态 ${node.status},点击查看详情`"
              @click="open(node)"
            >
              <span class="flow__name">{{ labelOf(node.node) }}</span>
              <StatusBadge :status="node.status" />
              <span
                v-if="metricOf(node)"
                class="flow__metric"
              >{{ metricOf(node) }}</span>
              <span
                v-if="lastSuccessOf(node)"
                class="flow__last-success"
              >{{ lastSuccessOf(node) }}</span>
              <span
                v-if="node.status_reason"
                class="flow__reason"
                :title="node.status_reason"
              >
                {{ node.status_reason }}
              </span>
            </button>
            <span
              v-if="i < flowNodes.length - 1"
              class="flow__connector"
              aria-hidden="true"
            >→</span>
          </li>
        </template>
      </ol>

      <!-- 节点详情面板 -->
      <div
        v-if="selected"
        class="detail d2a-card"
        data-testid="node-detail"
        role="dialog"
        :aria-label="`节点 ${selected.node} 详情`"
      >
        <div class="detail__head">
          <h3>{{ labelOf(selected.node) }}</h3>
          <StatusBadge :status="selected.status" />
          <button
            type="button"
            class="detail__close"
            aria-label="关闭详情"
            @click="close"
          >
            ×
          </button>
        </div>
        <dl class="detail__grid">
          <template
            v-for="f in detailFields"
            :key="f.label"
          >
            <dt>{{ f.label }}</dt>
            <dd>{{ f.value }}</dd>
          </template>
        </dl>
        <p class="detail__path">
          <template v-if="selected.detail_path">
            <router-link :to="selected.detail_path">
              查看详情 →
            </router-link>
          </template>
          <template v-if="detailActions.length">
            <router-link
              v-for="action in detailActions"
              :key="action.to"
              class="detail__action"
              :data-testid="`node-action-${action.to.replace(/\W+/g, '-')}`"
              :to="action.to"
            >
              {{ action.label }} →
            </router-link>
          </template>
          <span v-if="!selected.detail_path && detailActions.length === 0">暂无更多详情</span>
        </p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.pipeline-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.refresh-warning {
  padding: 8px 12px;
  border-left: 3px solid var(--d2a-status-warning);
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 12px;
  color: var(--d2a-status-stale);
}

.overall {
  display: flex;
  gap: 10px;
  align-items: center;
}

.overall__label {
  font-size: 13px;
  font-weight: 600;
}

.overall__source {
  width: 160px;
  margin-left: 12px;
}

.overall__time {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

/* 桌面横向流程 */
.flow {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: stretch;
  padding: 0;
  margin: 0;
  list-style: none;
}

.flow__node-wrap {
  display: flex;
  gap: 4px;
  align-items: center;
}

.flow__connector {
  color: var(--d2a-text-secondary);
}

.flow__node {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
  min-width: 128px;
  padding: 10px 12px;
  background: #fff;
  border: 1px solid var(--d2a-border);
  border-top-width: 3px;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
}

.flow__node--healthy {
  border-top-color: var(--d2a-status-healthy);
}

.flow__node--warning {
  border-top-color: var(--d2a-status-warning);
}

.flow__node--failed {
  border-top-color: var(--d2a-status-failed);
}

.flow__node--stale {
  border-top-color: var(--d2a-status-stale);
}

.flow__node--running {
  border-top-color: var(--d2a-status-running);
}

.flow__node--unknown,
.flow__node--idle {
  border-top-color: var(--d2a-status-unknown);
}

.flow__node[aria-pressed='true'] {
  outline: 2px solid var(--d2a-primary);
}

.flow__name {
  font-weight: 700;
}

.flow__metric {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.flow__last-success {
  font-size: var(--d2a-font-xs);
  color: var(--d2a-text-secondary);
}

.flow__reason {
  display: -webkit-box;
  overflow: hidden;
  font-size: 11px;
  color: var(--d2a-text-secondary);
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.detail__head {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
}

.detail__head h3 {
  margin: 0;
  font-size: 14px;
}

.detail__close {
  padding: 2px 8px;
  margin-left: auto;
  font-size: 16px;
  cursor: pointer;
  background: none;
  border: none;
}

.detail__grid {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: 6px 12px;
  margin: 0;
}

.detail__grid dt {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.detail__grid dd {
  margin: 0;
  font-size: 13px;
  word-break: break-all;
}

.detail__action {
  margin-right: 14px;
}

.detail__path {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

/* 小屏纵向 */
@media (width <= 900px) {
  .flow {
    flex-direction: column;
  }

  .flow__node-wrap {
    flex-direction: column;
    align-items: stretch;
  }

  .flow__connector {
    align-self: center;
    transform: rotate(90deg);
  }
}
</style>
