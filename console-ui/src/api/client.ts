/**
 * 唯一 API 客户端:Mock(MSW 拦截 fetch)与 DEMO/REAL(真实 /api)共用,
 * 保证路径、请求体、错误处理与响应解析在三种模式下一致。
 *
 * baseUrl 取当前源(浏览器为页面源,jsdom 为测试源),等价于同源空字符串且
 * 在 Node/undici 下也能构造 Request;路径一律 /api 开头,不产生 /api/api 双前缀。
 */
import createClient from 'openapi-fetch'
import type { paths } from '@/types/api'

/** Token 仅存当前标签页 sessionStorage;不得进 localStorage / URL / 日志 / fixture */
export const TOKEN_KEY = 'd2a_token'

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null

/** 401 时除清除 Token 外回调(由 session store / 布局注册,进入认证提示状态) */
export function setUnauthorizedHandler(fn: UnauthorizedHandler | null): void {
  onUnauthorized = fn
}

const ORIGIN =
  typeof window !== 'undefined' && window.location?.origin
    ? window.location.origin
    : ''

export const client = createClient<paths>({
  baseUrl: ORIGIN,
  // 调用时解析当前 globalThis.fetch:保证 MSW / 测试 stub 在任意装配顺序下
  // 都能拦截(client 创建时不固化 fetch 引用)。
  fetch: (input: RequestInfo | URL, init?: RequestInit) => globalThis.fetch(input, init),
})

client.use({
  onRequest({ request }) {
    const token = getToken()
    if (token) {
      request.headers.set('Authorization', `Bearer ${token}`)
    }
    return request
  },
  onResponse({ response }) {
    if (response.status === 401) {
      clearToken()
      onUnauthorized?.()
    }
    return response
  },
})
