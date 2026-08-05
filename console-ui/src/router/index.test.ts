import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { NAV_GROUPS, NAV_ITEMS, createAppRouter, routes } from './index'

describe('router', () => {
  it('19 个主页面 + 首次配置隐藏路由 + /data 旧链重定向', () => {
    expect(NAV_ITEMS).toHaveLength(19)
    expect(NAV_ITEMS.filter((i) => !i.readonly)).toHaveLength(19)
    expect(routes.filter((r) => r.name)).toHaveLength(20)
    // /data 旧三 tab 深链重定向保留(无 name)
    const legacy = routes.find((r) => r.path === '/data')
    expect(legacy?.redirect).toBeTypeOf('function')
  })

  it('菜单分组遵循数据生命周期:总览/数据源/数据管理/本体库/MCP 服务/平台管理', () => {
    expect(NAV_GROUPS.map((g) => g.title)).toEqual([
      '总览',
      '数据源',
      '数据管理',
      '本体库',
      'MCP 服务',
      '平台管理',
    ])
    expect(NAV_GROUPS.map((g) => g.items.length)).toEqual([1, 4, 3, 5, 2, 4])
  })

  it('路径与规格一致:MCP Lab 固定 /mcp', () => {
    const mcp = NAV_ITEMS.find((i) => i.name === 'mcp-lab')
    expect(mcp?.path).toBe('/mcp')
    expect(NAV_ITEMS.map((i) => i.path)).toEqual([
      '/',
      '/sources',
      '/pipeline',
      '/runs',
      '/quarantine',
      '/data/raw',
      '/data/objects',
      '/data/datasets',
      '/ontology/topology',
      '/ontology/classes',
      '/ontology/properties',
      '/object-graph',
      '/templates',
      '/dead-stock',
      '/mcp',
      '/settings',
      '/logs',
      '/audit',
      '/validation',
    ])
  })

  it(
    '菜单路由均可导航,标题与菜单一致',
    async () => {
      const router = createAppRouter(createMemoryHistory())
      for (const item of NAV_ITEMS) {
        await router.push(item.path)
        expect(router.currentRoute.value.name).toBe(item.name)
        expect(router.currentRoute.value.meta.title).toBe(item.title)
      }
    },
    15_000,
  )

  it('/mcp 直接访问到达 MCP Lab,不被兜底重定向', async () => {
    const router = createAppRouter(createMemoryHistory())
    await router.push('/mcp')
    expect(router.currentRoute.value.name).toBe('mcp-lab')
  })

  it('路由 base 取自构建注入的 BASE_URL(生产为 /,由构建产物检查兜底)', async () => {
    // 单测环境 BASE_URL 恒为 '/',与生产配置一致;
    // 生产 base 的证据见 scripts/check-dist.mjs(CI 在 npm run build 后执行)
    const router = createAppRouter(createMemoryHistory())
    await router.push('/pipeline')
    expect(router.currentRoute.value.name).toBe('pipeline')
  })

  it('首次配置为隐藏路由,不进入菜单', () => {
    const settings = NAV_ITEMS.find((i) => i.name === 'settings')
    expect(settings?.readonly).toBeUndefined()
    const settingsRoute = routes.find((r) => r.name === 'settings')
    expect(settingsRoute?.meta?.readonly).toBe(false)
    expect(NAV_ITEMS.find((i) => i.name === 'setup')).toBeUndefined()
    expect(routes.find((r) => r.name === 'setup')?.path).toBe('/setup')
  })

  it('未知路径回落仪表盘', async () => {
    const router = createAppRouter(createMemoryHistory())
    await router.push('/no-such-page')
    expect(router.currentRoute.value.path).toBe('/')
    expect(router.currentRoute.value.name).toBe('dashboard')
  })
})
