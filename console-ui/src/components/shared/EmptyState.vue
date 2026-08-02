<script setup lang="ts">
// 空数据视图:只在请求成功后数据为空时使用,与请求失败是两种语义。
withDefaults(
  defineProps<{
    title?: string
    hint?: string
  }>(),
  { title: '暂无数据', hint: '' },
)
</script>

<template>
  <div
    class="state-block state-block--empty"
    role="status"
    data-testid="empty-state"
  >
    <span class="state-block__title">{{ title }}</span>
    <span
      v-if="hint"
      class="state-block__hint"
    >{{ hint }}</span>
  </div>
</template>

<style scoped>
.state-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
  padding: 32px 24px;
  color: var(--el-text-color-secondary);
}

.state-block__title {
  font-size: 14px;
}

.state-block__hint {
  font-size: 12px;
}
</style>
