/**
 * 会话 store:Token(仅存 sessionStorage)+ 认证提示状态。
 *
 * 401 时 client 已清除 Token 并回调这里进入认证提示;Token 不进 Pinia 持久化、
 * URL、日志或 fixture。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  clearEvidenceSessionId,
  clearToken,
  getToken,
  setToken,
  setUnauthorizedHandler,
} from '@/api/client'
import { useMcpLabStore } from '@/stores/mcpLab'

export const useSessionStore = defineStore('session', () => {
  const authenticated = ref(getToken() !== null)
  /** 401 或手动点登录时为 true,驱动认证提示对话框 */
  const authRequired = ref(false)

  function login(token: string): void {
    setToken(token)
    authenticated.value = true
    authRequired.value = false
  }

  function clearEvidenceBoundary(): void {
    clearEvidenceSessionId()
    useMcpLabStore().resetForSessionBoundary()
  }

  function logout(): void {
    clearToken()
    clearEvidenceBoundary()
    authenticated.value = false
  }

  function requireAuth(): void {
    authRequired.value = true
  }

  /** 关闭认证提示;若 API 仍 401,下一次请求会重新触发 */
  function dismissAuth(): void {
    authRequired.value = false
  }

  // client 在 401 时已清 Token;这里清理同一标签页的 evidence 边界并进入认证提示。
  setUnauthorizedHandler(() => {
    clearEvidenceBoundary()
    authenticated.value = false
    authRequired.value = true
  })

  return { authenticated, authRequired, login, logout, requireAuth, dismissAuth }
})
