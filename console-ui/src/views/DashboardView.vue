<script setup lang="ts">
// 仪表盘:overview 垂直切片的 view 层。请求状态(loading/error)与领域状态
// (needs_setup / 空数据)分开表达;M3 在此扩展完整 Dashboard。
// 页面标题由顶栏展示(参考 UI),内容区只放卡片。
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import EmptyState from '@/components/shared/EmptyState.vue'
import ErrorState from '@/components/shared/ErrorState.vue'
import LoadingState from '@/components/shared/LoadingState.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useOverviewStore } from '@/stores/overview'

const store = useOverviewStore()
const { overview, services } = storeToRefs(store)

onMounted(() => {
  void store.refresh()
})
</script>

<template>
  <section>
    <div class="d2a-card">
      <h3 class="card-title">
        服务健康
      </h3>
      <LoadingState v-if="services.status === 'idle' || services.status === 'loading'" />
      <ErrorState
        v-else-if="services.status === 'error'"
        :error="services.error"
        @retry="store.refresh()"
      />
      <ul
        v-else
        class="service-list"
      >
        <li
          v-for="(probe, name) in services.data"
          :key="name"
          class="service-list__item"
        >
          <span class="service-list__name">{{ name }}</span>
          <StatusBadge :status="probe.ok ? 'healthy' : 'failed'" />
          <span class="service-list__method">{{ probe.method }}</span>
        </li>
      </ul>
    </div>

    <div class="d2a-card">
      <h3 class="card-title">
        数据源与对象
      </h3>
      <LoadingState v-if="overview.status === 'idle' || overview.status === 'loading'" />
      <ErrorState
        v-else-if="overview.status === 'error'"
        :error="overview.error"
        @retry="store.refresh()"
      />
      <template v-else>
        <!-- 首次安装:空态,不是 0 即健康 -->
        <EmptyState
          v-if="overview.data.needs_setup"
          title="尚未完成首次配置"
          hint="请先在管理页完成 /config 首次配置"
        />
        <EmptyState
          v-else-if="overview.data.sources.length === 0 && overview.data.objects.length === 0"
          title="暂无数据"
          hint="首次同步后显示来源与对象"
        />
        <template v-else>
          <h4>来源</h4>
          <el-table
            :data="overview.data.sources"
            size="small"
          >
            <el-table-column
              prop="source"
              label="source"
            />
            <el-table-column label="同步表数">
              <template #default="{ row }">
                {{ row.state.length }}
              </template>
            </el-table-column>
            <el-table-column label="待处理隔离">
              <template #default="{ row }">
                <StatusBadge :status="row.quarantined > 0 ? 'warning' : 'healthy'" />
                <span class="cell-num">{{ row.quarantined }}</span>
              </template>
            </el-table-column>
          </el-table>

          <h4>对象</h4>
          <el-table
            :data="overview.data.objects"
            size="small"
          >
            <el-table-column
              prop="display_name"
              label="对象"
            />
            <el-table-column label="object">
              <template #default="{ row }">
                <code class="cell-mono">{{ row.object }}</code>
              </template>
            </el-table-column>
            <el-table-column label="行数">
              <template #default="{ row }">
                <!-- rows 为 null 表示尚未物化,不等于 0 -->
                <span v-if="row.rows === null">未物化</span>
                <span v-else>{{ row.rows }}</span>
              </template>
            </el-table-column>
            <el-table-column label="待处理隔离">
              <template #default="{ row }">
                <StatusBadge :status="row.quarantined > 0 ? 'warning' : 'healthy'" />
                <span class="cell-num">{{ row.quarantined }}</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
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

.service-list {
  display: flex;
  gap: 16px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.service-list__item {
  display: flex;
  gap: 6px;
  align-items: center;
}

.service-list__name {
  font-weight: 600;
}

.service-list__method {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.cell-num {
  margin-left: 6px;
}

.cell-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
</style>
