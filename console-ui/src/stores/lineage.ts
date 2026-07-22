/**
 * Field Lineage store(M4-T08):对象字段追溯只读状态。
 * 代际 + AbortController 防止迟到响应覆盖新选择;无编辑/重放入口。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import {
  getObjectLineage,
  type LineageApiError,
  type ObjectLineageResponse,
} from '@/api/services'
import type { RequestState } from '@/types/state'

export const useLineageStore = defineStore('lineage', () => {
  const object = ref('')
  const keyToken = ref('')
  const propertyFilter = ref<string | null>(null)

  const lineage = ref<RequestState<ObjectLineageResponse>>({ status: 'idle' })
  const refreshError = ref<LineageApiError | ApiError | null>(null)
  const stale = ref(false)

  let gen = 0
  let abort: AbortController | null = null

  const isAvailable = computed(() =>
    lineage.value.status === 'success' && lineage.value.data.state === 'available',
  )
  const isUnavailable = computed(() =>
    lineage.value.status === 'success' && lineage.value.data.state === 'unavailable',
  )
  const isLoading = computed(() => lineage.value.status === 'loading')
  const isUnauthorized = computed(() => {
    const err = lineage.value.status === 'error'
      ? lineage.value.error
      : refreshError.value
    return Boolean(
      err
      && 'reason_code' in err
      && (err as LineageApiError).reason_code === 'unauthorized',
    )
  })

  function markStale() {
    if (lineage.value.status === 'success') {
      stale.value = true
    }
  }

  function setTarget(obj: string, token: string, property?: string | null) {
    if (obj !== object.value || token !== keyToken.value) {
      markStale()
    }
    object.value = obj
    keyToken.value = token
    propertyFilter.value = property ?? null
  }

  async function load(opts?: { property?: string | null }) {
    if (!object.value || !keyToken.value) return

    abort?.abort()
    abort = new AbortController()
    const myGen = ++gen
    stale.value = false
    refreshError.value = null
    lineage.value = { status: 'loading' }

    const prop = opts?.property ?? propertyFilter.value
    const result = await getObjectLineage(object.value, keyToken.value, {
      signal: abort.signal,
      property: prop ?? undefined,
    })

    if (myGen !== gen) return // 迟到响应

    if (result.ok) {
      lineage.value = { status: 'success', data: result.data }
    } else {
      lineage.value = { status: 'error', error: result.error }
    }
  }

  async function retry() {
    await load()
  }

  function reset() {
    abort?.abort()
    gen++
    object.value = ''
    keyToken.value = ''
    propertyFilter.value = null
    lineage.value = { status: 'idle' }
    refreshError.value = null
    stale.value = false
  }

  return {
    object,
    keyToken,
    propertyFilter,
    lineage,
    refreshError,
    stale,
    isAvailable,
    isUnavailable,
    isLoading,
    isUnauthorized,
    setTarget,
    load,
    retry,
    reset,
    markStale,
  }
})
