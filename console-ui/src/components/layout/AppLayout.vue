<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useOverviewStore } from '@/stores/overview'
import { usePipelineStore } from '@/stores/pipeline'
import { createPoller } from '@/stores/poller'
import { useSessionStore } from '@/stores/session'
import AuthDialog from './AuthDialog.vue'
import SideMenu from './SideMenu.vue'
import TopBar from './TopBar.vue'

const overviewStore = useOverviewStore()
const pipelineStore = usePipelineStore()
const session = useSessionStore()
const route = useRoute()

// 窄屏(≤900px):侧栏改为抽屉,顶栏汉堡按钮开关;路由切换后自动收起
const menuOpen = ref(false)
let mql: MediaQueryList | null = null

function onMediaChange(e: MediaQueryListEvent): void {
  if (!e.matches) {
    menuOpen.value = false
  }
}

watch(
  () => route.fullPath,
  () => {
    menuOpen.value = false
  },
)

// 唯一轮询所有者:Dashboard / 管道页 / TopBar 只消费 store,不另建 timer
const poller = createPoller({
  intervalMs: 5000,
  task: async () => {
    if (route.name === 'setup') {
      return
    }
    await Promise.all([overviewStore.refresh(), pipelineStore.refresh()])
  },
  isFailing: () =>
    overviewStore.refreshError !== null ||
    pipelineStore.refreshError !== null ||
    overviewStore.overview.status === 'error' ||
    pipelineStore.pipeline.status === 'error',
})

onMounted(() => {
  poller.start()
  mql = window.matchMedia('(max-width: 900px)')
  mql.addEventListener('change', onMediaChange)
})
onUnmounted(() => {
  poller.stop()
  mql?.removeEventListener('change', onMediaChange)
})

watch(
  () => session.authenticated,
  (authed, was) => {
    if (authed && !was) {
      void poller.tickNow()
    }
  },
)
</script>

<template>
  <el-container class="app-shell">
    <el-aside
      class="app-shell__aside"
      :class="{ 'app-shell__aside--open': menuOpen }"
    >
      <SideMenu />
    </el-aside>
    <div
      v-if="menuOpen"
      class="app-shell__backdrop"
      data-testid="menu-backdrop"
      @click="menuOpen = false"
    />
    <el-container class="app-shell__body">
      <el-header
        class="app-shell__header"
        height="48px"
      >
        <TopBar @toggle-menu="menuOpen = !menuOpen" />
      </el-header>
      <el-main class="app-shell__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
  <AuthDialog />
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100%;
}

/* 侧栏:白底 + 右侧分隔线(参考 UI);宽度由 tokens 的 --d2a-sidebar-width 驱动 */
.app-shell__aside {
  width: var(--d2a-sidebar-width);
  flex: 0 0 var(--d2a-sidebar-width);
  background: #fff;
  border-right: 1px solid var(--d2a-border);
}

.app-shell__body {
  flex: 1;
  min-width: 0;
}

/* 顶栏:白底吸顶,环境标识不可被滚动隐藏 */
.app-shell__header {
  position: sticky;
  top: 0;
  z-index: var(--d2a-z-topbar);
  padding: 0;
  background: var(--d2a-topbar-bg);
  border-bottom: 1px solid var(--d2a-border);
}

.app-shell__main {
  padding: 8px 12px;
  overflow-y: auto;
  background: var(--d2a-content-bg);
}

/* 窄屏(≤900px):侧栏脱离文档流变抽屉,遮罩点击关闭 */
@media (max-width: 900px) {
  .app-shell__aside {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: var(--d2a-z-drawer);
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    box-shadow: 2px 0 8px rgb(0 0 0 / 15%);
  }

  .app-shell__aside--open {
    transform: translateX(0);
  }

  .app-shell__backdrop {
    position: fixed;
    z-index: var(--d2a-z-backdrop);
    background: rgb(0 0 0 / 30%);
    inset: 0;
  }

  .app-shell__main {
    padding: 8px;
  }
}
</style>
