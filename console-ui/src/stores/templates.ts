/**
 * 模板页 store(M5-T09):模板对象列表、对象详情与模板指标。
 * 显式刷新,无自动轮询;只读,无编辑/保存操作。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ApiError } from '@/api/errors'
import {
  getTemplates,
  getTemplateMetrics,
} from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

type TemplateObject = components['schemas']['TemplateObject']
type TemplateMetric = components['schemas']['TemplateMetric']

export const useTemplatesStore = defineStore('templates', () => {
  const templates = ref<RequestState<TemplateObject[]>>({ status: 'idle' })
  const templatesRefreshError = ref<ApiError | null>(null)
  const selectedObject = ref<TemplateObject | null>(null)

  const metrics = ref<RequestState<TemplateMetric[]>>({ status: 'idle' })
  const metricsRefreshError = ref<ApiError | null>(null)

  let templatesGen = 0
  let metricsGen = 0

  async function fetchTemplates(): Promise<void> {
    const gen = ++templatesGen
    const firstLoad = templates.value.status !== 'success'
    if (firstLoad) {
      templates.value = { status: 'loading' }
    }
    const result = await getTemplates()
    if (gen !== templatesGen) {
      return
    }
    if (result.ok) {
      templates.value = { status: 'success', data: result.data }
      templatesRefreshError.value = null
      // 若当前选中对象在新列表中找不到,清除选中
      if (selectedObject.value) {
        const found = result.data.find((o) => o.object === selectedObject.value!.object)
        if (!found) {
          selectedObject.value = null
        }
      }
    } else if (firstLoad) {
      templates.value = { status: 'error', error: result.error }
      templatesRefreshError.value = null
    } else {
      templatesRefreshError.value = result.error
    }
  }

  async function fetchMetrics(): Promise<void> {
    const gen = ++metricsGen
    const firstLoad = metrics.value.status !== 'success'
    if (firstLoad) {
      metrics.value = { status: 'loading' }
    }
    const result = await getTemplateMetrics()
    if (gen !== metricsGen) {
      return
    }
    if (result.ok) {
      metrics.value = { status: 'success', data: result.data }
      metricsRefreshError.value = null
    } else if (firstLoad) {
      metrics.value = { status: 'error', error: result.error }
    } else {
      metricsRefreshError.value = result.error
    }
  }

  function selectObject(objectName: string): void {
    if (templates.value.status === 'success') {
      selectedObject.value = templates.value.data.find((o) => o.object === objectName) ?? null
    }
  }

  return {
    templates,
    templatesRefreshError,
    selectedObject,
    metrics,
    metricsRefreshError,
    fetchTemplates,
    fetchMetrics,
    selectObject,
  }
})
