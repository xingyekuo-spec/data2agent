import {
  createRouter,
  createWebHistory,
  type Router,
  type RouterHistory,
  type RouteRecordRaw,
} from 'vue-router'

/** 静态声明,保证 Vite 可按需分包且路径可分析 */
const viewComponents = {
  dashboard: () => import('@/views/DashboardView.vue'),
  pipeline: () => import('@/views/PipelineView.vue'),
  runs: () => import('@/views/RunsView.vue'),
  validation: () => import('@/views/ValidationView.vue'),
  audit: () => import('@/views/AuditView.vue'),
  data: () => import('@/views/DataView.vue'),
  quarantine: () => import('@/views/QuarantineView.vue'),
  'object-graph': () => import('@/views/ObjectGraphView.vue'),
  templates: () => import('@/views/TemplatesView.vue'),
  'dead-stock-validation': () => import('@/views/DeadStockValidationView.vue'),
  'mcp-lab': () => import('@/views/McpLabView.vue'),
  logs: () => import('@/views/LogsView.vue'),
  settings: () => import('@/views/SettingsView.vue'),
  setup: () => import('@/views/SetupView.vue'),
}

export type ViewName = keyof typeof viewComponents

export interface NavItem {
  name: ViewName
  path: string
  /** 菜单与页面标题 */
  title: string
  /** 辅助页标记 */
  readonly?: boolean
}

export interface NavGroup {
  title: string
  items: readonly NavItem[]
}

/**
 * 菜单结构(设计规格 §3.3,两级:分组标题 + 页面项)。
 */
export const NAV_GROUPS: readonly NavGroup[] = [
  {
    title: '运维监控',
    items: [
      { name: 'dashboard', path: '/', title: '仪表盘' },
      { name: 'pipeline', path: '/pipeline', title: '管道状态' },
      { name: 'runs', path: '/runs', title: '运行记录' },
      { name: 'validation', path: '/validation', title: '验收报告' },
      { name: 'audit', path: '/audit', title: '审计日志' },
    ],
  },
  {
    title: '数据管理',
    items: [
      { name: 'data', path: '/data', title: '数据浏览' },
      { name: 'quarantine', path: '/quarantine', title: '隔离区' },
      { name: 'object-graph', path: '/object-graph', title: '对象关系' },
      { name: 'templates', path: '/templates', title: '模板' },
    ],
  },
  {
    title: 'Agent',
    items: [
      { name: 'dead-stock-validation', path: '/dead-stock', title: '呆滞验证' },
      { name: 'mcp-lab', path: '/mcp', title: 'MCP Lab' },
    ],
  },
  {
    title: '系统',
    items: [
      { name: 'settings', path: '/settings', title: '配置' },
      { name: 'logs', path: '/logs', title: '日志' },
    ],
  },
]

export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((g) => g.items)

export const routes: RouteRecordRaw[] = [
  ...NAV_ITEMS.map<RouteRecordRaw>((item) => ({
    path: item.path,
    name: item.name,
    component: viewComponents[item.name],
    meta: { title: item.title, readonly: item.readonly === true },
  })),
  {
    path: '/setup',
    name: 'setup',
    component: viewComponents.setup,
    meta: { title: '首次配置' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export function createAppRouter(history?: RouterHistory): Router {
  const router = createRouter({
    // BASE_URL 固定 /v1/(vite.config.ts);直接访问深链接与菜单激活一致
    history: history ?? createWebHistory(import.meta.env.BASE_URL),
    routes,
  })
  router.afterEach((to) => {
    const title = typeof to.meta.title === 'string' ? to.meta.title : ''
    document.title = title ? `${title} · data2agent 控制台` : 'data2agent 控制台'
  })
  return router
}

export const router = createAppRouter()
