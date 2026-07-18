<script setup lang="ts">
// 领域健康状态徽标:颜色语义固定,unknown 是灰不是绿。
import { computed } from 'vue'
import { HEALTH_LABELS, type HealthStatus } from '@/types/state'

const props = defineProps<{
  status: HealthStatus
}>()

const label = computed(() => HEALTH_LABELS[props.status])
</script>

<template>
  <span
    class="status-badge"
    :class="`status-badge--${status}`"
    :data-status="status"
    :aria-label="`状态:${label}`"
  >{{ label }}</span>
</template>

<style scoped>
.status-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
}

.status-badge--unknown {
  background: var(--d2a-status-unknown);
}

.status-badge--idle {
  background: var(--d2a-status-idle);
}

.status-badge--running {
  background: var(--d2a-status-running);
}

.status-badge--healthy {
  background: var(--d2a-status-healthy);
}

.status-badge--warning {
  background: var(--d2a-status-warning);
}

.status-badge--failed {
  background: var(--d2a-status-failed);
}

.status-badge--stale {
  background: var(--d2a-status-stale);
}
</style>
