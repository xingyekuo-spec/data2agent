<script setup lang="ts">
// 管道(shell):M2 只证明 Mock / Real 同一渲染路径 —— Mock 下渲染 fixture
// 节点徽标,Real 下 /api/pipeline 是契约桩(501),显示「尚未接入」。
// M3 将替换为带聚合口径的管道图与专属 store。
import { onMounted, ref } from 'vue'
import { getPipeline } from '@/api/services'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type PipelineResponse = components['schemas']['PipelineResponse']

const state = ref<RequestState<PipelineResponse>>({ status: 'idle' })

async function load(): Promise<void> {
  state.value = { status: 'loading' }
  const result = await getPipeline()
  state.value = result.ok
    ? { status: 'success', data: result.data }
    : { status: 'error', error: result.error }
}

onMounted(load)
</script>

<template>
  <section>
    <div class="d2a-card">
      <h3 class="card-title">
        ERP → 抽取 → 推送 → Raw → 映射 → 对象层 → MCP
      </h3>

      <LoadingState v-if="state.status === 'idle' || state.status === 'loading'" />
      <!-- 501 契约桩:ErrorState 显示「尚未接入」且不提供重试 -->
      <ErrorState
        v-else-if="state.status === 'error'"
        :error="state.error"
        @retry="load"
      />
      <ul
        v-else
        class="pipeline-nodes"
        data-testid="pipeline-nodes"
      >
        <li
          v-for="node in state.data.nodes"
          :key="node.node"
          class="pipeline-nodes__item"
        >
          <span class="pipeline-nodes__name">{{ node.node }}</span>
          <StatusBadge :status="node.status" />
          <span
            v-if="node.error"
            class="pipeline-nodes__error"
          >{{ node.error }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.card-title {
  margin: 0 0 12px;
  font-size: 13px;
  font-weight: 500;
  color: var(--d2a-text-secondary);
}

.pipeline-nodes {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.pipeline-nodes__item {
  display: flex;
  gap: 8px;
  align-items: center;
}

.pipeline-nodes__name {
  min-width: 72px;
  font-weight: 600;
}

.pipeline-nodes__error {
  font-size: 12px;
  color: var(--d2a-status-failed);
}
</style>
