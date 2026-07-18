<script setup lang="ts">
// 功能占位:未进入当前里程碑的能力明确标注,不伪装成已完成或空数据。
withDefaults(
  defineProps<{
    /** 归属里程碑,如 M3 */
    milestone?: string
    note?: string
  }>(),
  { milestone: '', note: '' },
)
</script>

<template>
  <div
    class="feature-placeholder"
    data-testid="feature-placeholder"
  >
    <el-tag
      size="small"
      type="info"
    >
      尚未接入{{ milestone ? `(${milestone})` : '' }}
    </el-tag>
    <p class="feature-placeholder__note">
      {{ note || '该能力将在对应里程碑接入;当前不展示模拟业务数据。' }}
    </p>
  </div>
</template>

<style scoped>
.feature-placeholder {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  padding: 20px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
}

.feature-placeholder__note {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
