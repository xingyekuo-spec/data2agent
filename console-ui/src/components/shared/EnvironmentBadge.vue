<script setup lang="ts">
// 顶栏持续展示环境模式:REAL(产品与开发均连真实后端)。
import { MODE } from '@/config/mode'
</script>

<template>
  <span
    class="env-badge env-badge--real"
    title="数据来自真实后端"
    aria-label="环境模式:REAL,数据来自真实后端"
    data-testid="env-badge"
  >{{ MODE.toUpperCase() }}</span>
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

.env-badge--real {
  background: var(--d2a-env-real);
}
</style>
