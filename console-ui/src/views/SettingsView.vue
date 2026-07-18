<script setup lang="ts">
// 配置(只读):展示 /api/config 的非敏感配置;不提供编辑入口。
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import { useSettingsStore } from '@/stores/settings'

const store = useSettingsStore()
const { config } = storeToRefs(store)

onMounted(() => {
  void store.refresh()
})
</script>

<template>
  <section>
    <div class="d2a-card">
      <h3 class="card-title">
        非敏感配置 <el-tag
          size="small"
          type="info"
        >
          只读
        </el-tag>
      </h3>

      <LoadingState v-if="config.status === 'idle' || config.status === 'loading'" />
      <ErrorState
        v-else-if="config.status === 'error'"
        :error="config.error"
        @retry="store.refresh()"
      />
      <template v-else>
        <EmptyState
          v-if="config.data.needs_setup"
          title="尚未完成首次配置"
          hint="请先在管理页完成 /config 首次配置"
        />
        <el-descriptions
          v-else
          :column="1"
          border
          size="small"
          data-testid="config-view"
        >
          <el-descriptions-item label="templates">
            {{ config.data.templates || '(空)' }}
          </el-descriptions-item>
          <el-descriptions-item label="landing">
            {{ config.data.landing || '(空)' }}
          </el-descriptions-item>
        </el-descriptions>
      </template>
    </div>
  </section>
</template>

<style scoped>
.card-title {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--d2a-text-primary);
}
</style>
