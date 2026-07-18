<script setup lang="ts">
// 白底顶栏(参考 UI):左侧当前页面标题,右侧模式标识 + 用户入口。
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowDown } from '@element-plus/icons-vue'
import EnvironmentBadge from '@/components/shared/EnvironmentBadge.vue'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const session = useSessionStore()
const title = computed(() => (typeof route.meta.title === 'string' ? route.meta.title : ''))

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
    <h1
      class="topbar__title"
      data-testid="topbar-title"
    >
      {{ title }}
    </h1>
    <div class="topbar__right">
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

.topbar__right {
  display: flex;
  gap: 16px;
  align-items: center;
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
