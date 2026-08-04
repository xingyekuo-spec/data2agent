<script setup lang="ts">
/** 规范分页栏(页面结构规范 §二.4):共 N 条 + 每页条数选择 + 页码,右对齐。
 *  事件统一为 change(offset, limit);换页保持 limit,换条数回到第一页。 */
withDefaults(
  defineProps<{
    total: number
    limit: number
    offset: number
    sizes?: number[]
  }>(),
  { sizes: () => [20, 50, 100] },
)

const emit = defineEmits<{
  change: [offset: number, limit: number]
}>()

function onPage(current: number, limit: number): void {
  emit('change', (current - 1) * limit, limit)
}

function onSize(size: number): void {
  emit('change', 0, size)
}
</script>

<template>
  <el-pagination
    class="d2a-pager"
    layout="total, sizes, prev, pager, next"
    background
    :total="total"
    :page-size="limit"
    :page-sizes="sizes"
    :current-page="offset / limit + 1"
    @current-change="(current: number) => onPage(current, limit)"
    @size-change="onSize"
  />
</template>

<style scoped>
.d2a-pager {
  justify-content: flex-end;
  margin-top: 10px;
}
</style>
