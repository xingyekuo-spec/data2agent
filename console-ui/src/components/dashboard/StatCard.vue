<script setup lang="ts">
// Dashboard 摘要卡:数值 + 口径脚注;tone 只表达语义,unknown 不是绿。
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    label: string
    value: string
    hint?: string
    tone?: 'default' | 'good' | 'warn' | 'bad' | 'unknown'
  }>(),
  { hint: '', tone: 'default' },
)

const toneClass = computed(() => `stat-card__value--${props.tone}`)
</script>

<template>
  <div
    class="stat-card"
    :data-tone="tone"
  >
    <span class="stat-card__label">{{ label }}</span>
    <span
      class="stat-card__value"
      :class="toneClass"
      data-testid="stat-value"
    >{{ value }}</span>
    <span
      v-if="hint"
      class="stat-card__hint"
    >{{ hint }}</span>
  </div>
</template>

<style scoped>
.stat-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 16px;
  background: #fff;
  border: 1px solid var(--d2a-border);
  border-radius: 8px;
}

.stat-card__label {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.stat-card__value {
  font-size: 22px;
  font-weight: 700;
  color: var(--d2a-text-primary);
}

.stat-card__value--good {
  color: var(--d2a-status-healthy);
}

.stat-card__value--warn {
  color: var(--d2a-status-warning);
}

.stat-card__value--bad {
  color: var(--d2a-status-failed);
}

.stat-card__value--unknown {
  color: var(--d2a-status-unknown);
}

.stat-card__hint {
  font-size: 11px;
  color: var(--d2a-text-secondary);
}
</style>
