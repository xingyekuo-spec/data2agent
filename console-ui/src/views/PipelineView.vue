<script setup lang="ts">
// 管道页(M3):桌面横向 / 小屏纵向的关键节点流程 + 节点详情面板。
// 状态由后端 observability 计算;视图只展示,不重复推导业务规则。
// 数据由 AppLayout 统一轮询;视图只消费 pipeline store。
import { computed, onBeforeUnmount, ref } from 'vue'
import { storeToRefs } from 'pinia'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { usePipelineStore } from '@/stores/pipeline'
import type { components } from '@/types/api'
import { formatDateTime } from '@/utils/time'

type PipelineNode = components['schemas']['PipelineNode']

const store = usePipelineStore()
const { pipeline, refreshError } = storeToRefs(store)

// 只存节点 ID:轮询替换 pipeline 数据后,详情面板展示的是当前快照而不是旧对象
const selectedId = ref<string | null>(null)
const hiddenPipelineNodeIds = new Set(['erp', 'extract'])

const data = computed(() => (pipeline.value.status === 'success' ? pipeline.value.data : null))
const visibleNodes = computed(() =>
  data.value?.nodes.filter((n) => !hiddenPipelineNodeIds.has(n.node)) ?? [],
)
const selected = computed(() =>
  selectedId.value === null
    ? null
    : (visibleNodes.value.find((n) => n.node === selectedId.value) ?? null),
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
        <span class="overall__time">截至 {{ formatDateTime(data.generated_at) }}</span>
      </div>

      <!-- 关键节点流程:连接线只表达顺序,不用绿色掩盖 unknown -->
      <ol
        class="flow"
        data-testid="pipeline-flow"
      >
        <template
          v-for="(node, i) in visibleNodes"
          :key="node.node"
        >
          <li class="flow__node-wrap">
            <button
              type="button"
              class="flow__node"
              :class="`flow__node--${node.status}`"
              :data-status="node.status"
              :aria-pressed="selected?.node === node.node"
              :aria-label="`节点 ${node.node} 状态 ${node.status},点击查看详情`"
              @click="open(node)"
            >
              <span class="flow__name">{{ node.node }}</span>
              <StatusBadge :status="node.status" />
              <span
                v-if="metricOf(node)"
                class="flow__metric"
              >{{ metricOf(node) }}</span>
              <span
                v-if="node.status_reason"
                class="flow__reason"
                :title="node.status_reason"
              >
                {{ node.status_reason }}
              </span>
            </button>
            <span
              v-if="i < visibleNodes.length - 1"
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
          <h3>节点 {{ selected.node }}</h3>
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
          <span v-else>运行详情页将在 M4 提供</span>
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
