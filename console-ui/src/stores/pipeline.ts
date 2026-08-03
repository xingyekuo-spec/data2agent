/**
 * pipeline 单一所有者(M3-T07):pipeline / services 请求与合并刷新状态。
 * 与 overview store 同一模式:防重入、旧数据保留、刷新错误标记。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { ApiError } from '@/api/errors'
import { getPipeline, getServices } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type PipelineResponse = components['schemas']['PipelineResponse']
type ServicesStatusResponse = components['schemas']['ServicesStatusResponse']

export const usePipelineStore = defineStore('pipeline', () => {
  const pipeline = ref<RequestState<PipelineResponse>>({ status: 'idle' })
  const services = ref<RequestState<ServicesStatusResponse>>({ status: 'idle' })
  const lastSuccessAt = ref<Date | null>(null)
  const refreshError = ref<ApiError | null>(null)
  /** 多源场景:当前查看的数据源;null = 后端 default_source(兼容)
   *  AppLayout 统一轮询也走此源——切换后下一轮自动跟随。 */
  const currentSource = ref<string | null>(null)
  let inFlight = false

  const data = computed(() =>
    pipeline.value.status === 'success' ? pipeline.value.data : null,
  )

  function setSource(source: string | null): void {
    if (currentSource.value === source) {
      return
    }
    currentSource.value = source
    // 切源即废弃当前快照,避免展示上一源的数据冒充
    pipeline.value = { status: 'idle' }
    refreshError.value = null
  }

  async function refresh(): Promise<void> {
    if (inFlight) {
      return
    }
    inFlight = true
    const firstLoad = pipeline.value.status !== 'success'
    if (firstLoad) {
      pipeline.value = { status: 'loading' }
      services.value = { status: 'loading' }
    }
    try {
      const [pl, sv] = await Promise.all([
        getPipeline(currentSource.value ?? undefined),
        getServices(),
      ])
      let ok = true
      if (pl.ok) {
        pipeline.value = { status: 'success', data: pl.data }
      } else {
        ok = false
        if (firstLoad) {
          pipeline.value = { status: 'error', error: pl.error }
        } else {
          refreshError.value = pl.error
        }
      }
      if (sv.ok) {
        services.value = { status: 'success', data: sv.data }
      } else {
        ok = false
        if (firstLoad) {
          services.value = { status: 'error', error: sv.error }
        } else {
          refreshError.value = sv.error
        }
      }
      if (ok) {
        lastSuccessAt.value = new Date()
        refreshError.value = null
      }
    } finally {
      inFlight = false
    }
  }

  return {
    pipeline,
    services,
    data,
    lastSuccessAt,
    refreshError,
    currentSource,
    setSource,
    refresh,
  }
})
