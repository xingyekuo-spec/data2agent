<script setup lang="ts">
import ScenarioSwitcher from '@/components/shared/ScenarioSwitcher.vue'
import { IS_MOCK } from '@/config/mode'
import { scenarioEpoch } from '@/config/scenario-epoch'
import AuthDialog from './AuthDialog.vue'
import SideMenu from './SideMenu.vue'
import TopBar from './TopBar.vue'
</script>

<template>
  <el-container class="app-shell">
    <el-aside
      class="app-shell__aside"
      width="240px"
    >
      <SideMenu />
    </el-aside>
    <el-container class="app-shell__body">
      <el-header
        class="app-shell__header"
        height="48px"
      >
        <TopBar />
      </el-header>
      <el-main class="app-shell__main">
        <!-- Mock 场景切换时代际 +1,重挂载当前视图以重新取数 -->
        <router-view :key="IS_MOCK ? scenarioEpoch : 'real'" />
      </el-main>
    </el-container>
  </el-container>
  <AuthDialog />
  <!-- 仅 Mock 模式可见;不随页面滚动隐藏 -->
  <ScenarioSwitcher v-if="IS_MOCK" />
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100%;
}

/* 侧栏:白底 + 右侧分隔线(参考 UI) */
.app-shell__aside {
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
  z-index: 100;
  padding: 0;
  background: var(--d2a-topbar-bg);
  border-bottom: 1px solid var(--d2a-border);
}

.app-shell__main {
  padding: 8px 12px;
  overflow-y: auto;
  background: var(--d2a-content-bg);
}
</style>
