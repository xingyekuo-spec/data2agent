import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { setToken } from '@/api/client'
import { setScenario } from '@/mocks/scenario'
import { useOverviewStore } from './overview'
import { useSessionStore } from './session'
import { useSettingsStore } from './settings'

describe('settings store(只读)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setScenario('healthy')
  })

  it('healthy:加载只读配置', async () => {
    const store = useSettingsStore()
    await store.refresh()
    expect(store.config.status).toBe('success')
    if (store.config.status === 'success') {
      expect(store.config.data.templates).toBe('templates')
      expect(store.config.data.needs_setup).toBe(false)
    }
  })

  it('token-invalid:401 保留为 error,不变空配置', async () => {
    setScenario('token-invalid')
    const store = useSettingsStore()
    await store.refresh()
    expect(store.config.status).toBe('error')
    if (store.config.status === 'error') {
      expect(store.config.error.status).toBe(401)
    }
  })
})

describe('session store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setScenario('healthy')
    sessionStorage.clear()
  })

  it('login/logout 只写 sessionStorage', () => {
    const session = useSessionStore()
    session.login('tok')
    expect(session.authenticated).toBe(true)
    expect(sessionStorage.getItem('d2a_token')).toBe('tok')
    expect(localStorage.getItem('d2a_token')).toBeNull()
    session.logout()
    expect(session.authenticated).toBe(false)
    expect(sessionStorage.getItem('d2a_token')).toBeNull()
  })

  it('API 返回 401:清除认证态并进入认证提示', async () => {
    const session = useSessionStore()
    session.login('stale-token')
    setToken('stale-token')
    setScenario('token-invalid')
    const overview = useOverviewStore()
    await overview.refresh()
    expect(session.authenticated).toBe(false)
    expect(session.authRequired).toBe(true)
    expect(sessionStorage.getItem('d2a_token')).toBeNull()
  })
})
