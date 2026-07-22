<script setup lang="ts">
// 顶栏持续展示环境模式:MOCK / REAL,大写,不可被滚动或切换隐藏。
import { computed } from 'vue'
import { MODE } from '@/config/mode'

const label = computed(() => MODE.toUpperCase())

const hints: Record<string, string> = {
  mock: '全部数据来自本地 fixture,不是真实后端',
  real: '数据来自真实后端',
}
</script>

<template>
  <span
    class="env-badge"
    :class="`env-badge--${MODE}`"
    :title="hints[MODE]"
    data-testid="env-badge"
  >{{ label }}</span>
</template>

<style scoped>
.env-badge {
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 2px;
  color: #fff;
  user-select: none;
}

.env-badge--mock {
  background: var(--d2a-env-mock);
}

.env-badge--real {
  background: var(--d2a-env-real);
}
</style>
