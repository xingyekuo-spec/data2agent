/**
 * 数据源管理 store:清单(卡片聚合)+ 详情抽屉(按需加载)。
 * 与 quarantine store 同一语义:first failure → error,refresh failure → 保留旧数据。
 * 无自动轮询,显式刷新(数据源状态由接入链路自己更新)。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import { getSourceDetail, getSources } from '@/api/services'
import type { components } from '@/types/api'
import type { RequestState } from '@/types/state'

export type SourceCard = components['schemas']['SourceCard']
export type SourceDetail = components['schemas']['SourceDetail']

export const useSourcesStore = defineStore('sources', () => {
  const cards = ref<RequestState<SourceCard[]>>({ status: 'idle' })
  const refreshError = ref<ApiError | null>(null)
  let cardsGen = 0

  const detail = ref<RequestState<SourceDetail> | null>(null)
  const detailSource = ref<string | null>(null)
  let detailGen = 0

  async function refresh(): Promise<void> {
    const gen = ++cardsGen
    if (cards.value.status !== 'success') {
      cards.value = { status: 'loading' }
    }
    const result = await getSources()
    if (gen !== cardsGen) return
    if (!result.ok) {
      if (cards.value.status === 'success') {
        refreshError.value = result.error
      } else {
        cards.value = { status: 'error', error: result.error }
      }
      return
    }
    refreshError.value = null
    cards.value = { status: 'success', data: result.data }
  }

  async function openDetail(source: string): Promise<void> {
    detailSource.value = source
    const gen = ++detailGen
    detail.value = { status: 'loading' }
    const result = await getSourceDetail(source)
    if (gen !== detailGen) return
    if (!result.ok) {
      detail.value = { status: 'error', error: result.error }
      return
    }
    detail.value = { status: 'success', data: result.data }
  }

  function closeDetail(): void {
    detailGen += 1
    detailSource.value = null
    detail.value = null
  }

  return {
    cards,
    refreshError,
    detail,
    detailSource,
    refresh,
    openDetail,
    closeDetail,
  }
})
