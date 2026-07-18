<script setup lang="ts">
// 请求进行中状态(loading ≠ 领域 running:一个是请求未回,一个是业务在执行)
withDefaults(
  defineProps<{
    text?: string
  }>(),
  { text: '加载中…' },
)
</script>

<template>
  <div
    class="state-block state-block--loading"
    role="status"
    data-testid="loading-state"
  >
    <span
      class="state-block__spinner"
      aria-hidden="true"
    />
    <span>{{ text }}</span>
  </div>
</template>

<style scoped>
.state-block {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 24px;
  color: var(--el-text-color-secondary);
}

.state-block__spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--d2a-status-running);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
