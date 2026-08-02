<script setup lang="ts">
// 白底顶栏(参考 UI):左侧当前页面标题;右侧源状态 / 更新时间 / 隔离数 /
// 模式标识 / 用户入口。只读两个观测 store,不直接调 API。
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowDown, Expand } from '@element-plus/icons-vue'
import EnvironmentBadge from '@/components/shared/EnvironmentBadge.vue'
import StatusBadge from '@/components/shared/StatusBadge.vue'
import { useOverviewStore } from '@/stores/overview'
import { usePipelineStore } from '@/stores/pipeline'
import { useSessionStore } from '@/stores/session'
import { formatTimeHM } from '@/utils/time'

const emit = defineEmits<{
  'toggle-menu': []
}>()

const route = useRoute()
const session = useSessionStore()
const overviewStore = useOverviewStore()
const pipelineStore = usePipelineStore()

const title = computed(() => (typeof route.meta.title === 'string' ? route.meta.title : ''))
const overall = computed(() => pipelineStore.data?.overall_status ?? null)
const quarantine = computed(() => overviewStore.data?.summary.quarantine_pending ?? null)
const updatedAt = computed(() => formatTimeHM(overviewStore.data?.summary.data_updated_at))

function onUserCommand(command: string): void {
  if (command === 'login') {
    session.requireAuth()
  } else if (command === 'logout') {
    session.logout()
  }
}
</script>

<template>
  <div class="topbar">
    <div class="topbar__left">
      <button
        type="button"
        class="topbar__menu-btn"
        aria-label="打开导航菜单"
        data-testid="menu-toggle"
        @click="emit('toggle-menu')"
      >
        <el-icon><Expand /></el-icon>
      </button>
      <h1
        class="topbar__title"
        data-testid="topbar-title"
      >
        {{ title }}
      </h1>
    </div>
    <div class="topbar__right">
      <StatusBadge
        v-if="overall"
        :status="overall"
        data-testid="topbar-overall"
      />
      <span
        v-if="quarantine !== null"
        class="topbar__metric"
        data-testid="topbar-quarantine"
      >
        隔离 {{ quarantine }}
      </span>
      <span
        v-if="updatedAt"
        class="topbar__metric"
        data-testid="topbar-updated"
      >
        更新 {{ updatedAt }}
      </span>
      <EnvironmentBadge />
      <el-dropdown
        trigger="click"
        @command="onUserCommand"
      >
        <span
          class="topbar__user"
          data-testid="user-menu"
        >
          <span class="topbar__user-avatar">管</span>
          <span>管理员</span>
          <el-icon><ArrowDown /></el-icon>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item
              v-if="session.authenticated"
              command="logout"
              data-testid="logout-button"
            >
              退出登录
            </el-dropdown-item>
            <el-dropdown-item
              v-else
              command="login"
              data-testid="login-button"
            >
              登录
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </div>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 100%;
  padding: 0 20px;
  color: var(--d2a-text-primary);
}

.topbar__title {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
}

.topbar__left {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

/* 汉堡按钮:仅窄屏(≤900px)显示,宽屏隐藏 */
.topbar__menu-btn {
  display: none;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--d2a-border);
  border-radius: 6px;
  background: #fff;
  color: var(--d2a-text-primary);
  cursor: pointer;
}

@media (max-width: 900px) {
  .topbar__menu-btn {
    display: inline-flex;
  }

  /* 窄屏顶栏指标让位于标题:隔离/更新时间收起 */
  .topbar__metric {
    display: none;
  }

  .topbar {
    padding: 0 10px;
  }
}

.topbar__right {
  display: flex;
  gap: 14px;
  align-items: center;
}

.topbar__metric {
  font-size: 12px;
  color: var(--d2a-text-secondary);
}

.topbar__user {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  cursor: pointer;
  outline: none;
}

.topbar__user-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 6px;
  background: var(--d2a-brand-bg);
  color: #fff;
  font-size: 12px;
}
</style>
