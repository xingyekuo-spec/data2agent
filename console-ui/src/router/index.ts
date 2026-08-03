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
  sources: () => import('@/views/SourcesView.vue'),
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
 * 菜单结构(2026-08 按数据生命周期模块化重构,两级:分组标题 + 页面项)。
 * 分组沿数据流向:总览 → 数据源 → 数据管理 → 本体库 → MCP 服务 → 平台管理,
 * 与后端模块边界一一对应;页面标题保留专业术语(水位/隔离区等口径统一)。
 */
export const NAV_GROUPS: readonly NavGroup[] = [
  {
    title: '总览',
    items: [
      { name: 'dashboard', path: '/', title: '仪表盘' },
    ],
  },
  {
    title: '数据源',
    items: [
      { name: 'sources', path: '/sources', title: '数据源管理' },
      { name: 'pipeline', path: '/pipeline', title: '管道状态' },
      { name: 'runs', path: '/runs', title: '运行记录' },
      { name: 'quarantine', path: '/quarantine', title: '隔离区' },
    ],
  },
  {
    title: '数据管理',
    items: [
      { name: 'data', path: '/data', title: '数据浏览' },
    ],
  },
  {
    title: '本体库',
    items: [
      { name: 'templates', path: '/templates', title: '模板' },
      { name: 'object-graph', path: '/object-graph', title: '对象关系' },
    ],
  },
  {
    title: 'MCP 服务',
    items: [
      { name: 'dead-stock-validation', path: '/dead-stock', title: '呆滞验证' },
      { name: 'mcp-lab', path: '/mcp', title: 'MCP Lab' },
    ],
  },
  {
    title: '平台管理',
    items: [
      { name: 'settings', path: '/settings', title: '配置' },
      { name: 'logs', path: '/logs', title: '日志' },
      { name: 'audit', path: '/audit', title: '审计日志' },
      { name: 'validation', path: '/validation', title: '验收报告' },
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
    // BASE_URL 固定 /(vite.config.ts);直接访问深链接与菜单激活一致
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
