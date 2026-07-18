<script setup lang="ts">
// 两级菜单(参考 UI):可折叠分组(右侧箭头)+ 带图标页面项;
// 选中项为浅蓝圆角胶囊。品牌区固定在侧栏顶部。
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  Coin,
  Document,
  Files,
  MagicStick,
  Notebook,
  Odometer,
  Setting,
  Share,
  Warning,
} from '@element-plus/icons-vue'
import type { ViewName } from '@/router'
import { NAV_GROUPS } from '@/router'

const route = useRoute()
// 菜单激活态跟随当前路由(直接访问深链接也一致)
const active = computed(() => route.path)
// 参考 UI:全部分组默认展开
const openeds = NAV_GROUPS.map((g) => g.title)

const icons: Record<ViewName, typeof Odometer> = {
  dashboard: Odometer,
  pipeline: Share,
  runs: Document,
  audit: Notebook,
  data: Coin,
  quarantine: Warning,
  templates: Files,
  'mcp-lab': MagicStick,
  settings: Setting,
}
</script>

<template>
  <div class="sidemenu">
    <div class="sidemenu__brand">
      <span class="sidemenu__brand-badge">D2A</span>
      <span class="sidemenu__brand-name">data2agent</span>
    </div>
    <el-menu
      class="sidemenu__menu"
      :default-active="active"
      :default-openeds="openeds"
      router
    >
      <el-sub-menu
        v-for="group in NAV_GROUPS"
        :key="group.title"
        :index="group.title"
      >
        <template #title>
          <span class="sidemenu__group-title">{{ group.title }}</span>
        </template>
        <el-menu-item
          v-for="item in group.items"
          :key="item.name"
          :index="item.path"
        >
          <el-icon><component :is="icons[item.name]" /></el-icon>
          <span>{{ item.title }}</span>
          <el-tag
            v-if="item.readonly"
            class="sidemenu__tag"
            size="small"
            type="info"
          >
            只读
          </el-tag>
        </el-menu-item>
      </el-sub-menu>
    </el-menu>
  </div>
</template>

<style scoped>
.sidemenu {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.sidemenu__brand {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 10px 12px 14px;
}

.sidemenu__brand-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  background: var(--d2a-brand-bg);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
}

.sidemenu__brand-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--d2a-text-primary);
}

.sidemenu__menu {
  flex: 1;
  border-right: none;
}

.sidemenu__group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--d2a-text-secondary);
}

.sidemenu__tag {
  margin-left: 8px;
}

/* 菜单项:圆角胶囊;紧凑密度(13px 字、40px 行高、缩进 12px)。
   padding-left 必须 !important:EP 的 .el-menu--vertical:not(...):not(...) .el-menu-item
   选择器权重是 4 个类,普通覆盖永远输给它 */
.sidemenu__menu :deep(.el-menu-item) {
  height: 40px;
  margin: 0px 6px;
  padding-left: 12px !important;
  border-radius: 6px;
  font-size: 13px;
}

.sidemenu__menu :deep(.el-menu-item .el-icon) {
  margin-right: 0px;
  font-size: 16px;
}

/* 分组标题:小字灰色,组间留 12px;首组不留 */
.sidemenu__menu :deep(.el-sub-menu .el-sub-menu__title) {
  height: 32px;
  margin-top: 12px;
  padding-left: 12px;
  font-size: 12px;
}

.sidemenu__menu :deep(.el-sub-menu:first-child .el-sub-menu__title) {
  margin-top: 0;
}

.sidemenu__menu :deep(.el-sub-menu__title:hover) {
  background: transparent;
}

.sidemenu__menu :deep(.el-menu-item:hover) {
  background: var(--d2a-content-bg);
}

.sidemenu__menu :deep(.el-menu-item.is-active) {
  background: var(--d2a-menu-active-bg);
  color: var(--d2a-primary);
}
</style>
