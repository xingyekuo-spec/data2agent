<script setup lang="ts">
// 请求失败视图:安全摘要(不含 Token / 响应正文)+ 可重试入口。
// 501(契约桩未接入)是独立语义「尚未接入」,不显示为通用失败,也不提供重试。
import { computed } from 'vue'
import type { ApiError } from '@/api/errors'

const props = defineProps<{
  error: ApiError
}>()

const emit = defineEmits<{
  retry: []
}>()

const notImplemented = computed(() => props.error.status === 501)
const title = computed(() => {
  if (notImplemented.value) {
    return '尚未接入'
  }
  return props.error.status ? `请求失败(HTTP ${props.error.status})` : '请求失败'
})
</script>

<template>
  <div
    class="state-block"
    :class="notImplemented ? 'state-block--stub' : 'state-block--error'"
    role="alert"
    :data-testid="notImplemented ? 'not-implemented-state' : 'error-state'"
  >
    <span class="state-block__title">{{ title }}</span>
    <span class="state-block__detail">{{ error.message }}</span>
    <button
      v-if="error.retriable && !notImplemented"
      type="button"
      class="state-block__retry"
      data-testid="error-retry"
      @click="emit('retry')"
    >
      重试
    </button>
  </div>
</template>

<style scoped>
.state-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
  padding: 16px 20px;
  background: var(--el-fill-color-light);
}

.state-block--error {
  border-left: 3px solid var(--d2a-status-failed);
}

.state-block--stub {
  border-left: 3px solid var(--d2a-status-unknown);
}

.state-block--error .state-block__title {
  font-weight: 600;
  color: var(--d2a-status-failed);
}

.state-block--stub .state-block__title {
  font-weight: 600;
  color: var(--el-text-color-regular);
}

.state-block__detail {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.state-block__retry {
  padding: 4px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--el-bg-color);
  cursor: pointer;
}
</style>
