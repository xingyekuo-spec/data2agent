<script setup lang="ts">
// 领域 unknown 视图:后端无法检测,明确置灰,绝不显示为正常。
withDefaults(
  defineProps<{
    text?: string
  }>(),
  { text: '尚不可检测:后端暂时无法提供该状态' },
)
</script>

<template>
  <div
    class="state-block state-block--unknown"
    role="status"
    data-testid="unknown-state"
  >
    <span
      class="state-block__dot"
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
  padding: 16px 20px;
  color: var(--el-text-color-secondary);
}

.state-block__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--d2a-status-unknown);
}
</style>
