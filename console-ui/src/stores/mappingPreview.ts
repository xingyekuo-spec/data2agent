/**
 * Mapping Preview store(M3-T06):只读样本试算状态。
 * 无保存/发布动作;代际 + AbortController 防止迟到响应覆盖新选择。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { ApiError } from '@/api/errors'
import {
  postMappingPreview,
  type MappingPreviewApiError,
  type MappingPreviewDraftBinding,
  type MappingPreviewRequest,
  type MappingPreviewResponse,
} from '@/api/services'
import type { RequestState } from '@/types/state'

export interface MappingPreviewSampleInput {
  offset: number
  limit: number
  batch_id: string | null
}

export const useMappingPreviewStore = defineStore('mappingPreview', () => {
  const object = ref('')
  const source = ref('')
  const sample = ref<MappingPreviewSampleInput>({
    offset: 0,
    limit: 50,
    batch_id: null,
  })
  const useDraft = ref(false)
  /** 结构化草稿;优先于 draftText */
  const draft = ref<MappingPreviewDraftBinding | null>(null)
  /** JSON 文本草稿(浏览器内存);useDraft 且无 structured draft 时解析 */
  const draftText = ref('')

  const preview = ref<RequestState<MappingPreviewResponse>>({ status: 'idle' })
  const refreshError = ref<MappingPreviewApiError | ApiError | null>(null)
  /** 输入变更后旧成功结果仍可见时为 true */
  const stale = ref(false)
  const draftParseError = ref<string | null>(null)

  let gen = 0
  let abort: AbortController | null = null

  const isEmpty = computed(() =>
    preview.value.status === 'success' && preview.value.data.sample.sampled_rows === 0,
  )
  const isUnauthorized = computed(() => {
    const err = preview.value.status === 'error'
      ? preview.value.error
      : refreshError.value
    return Boolean(
      err
      && 'reason_code' in err
      && (err as MappingPreviewApiError).reason_code === 'unauthorized',
    )
  })
  const isLoading = computed(() => preview.value.status === 'loading')

  function clearResult(): void {
    abort?.abort()
    abort = null
    gen += 1
    preview.value = { status: 'idle' }
    refreshError.value = null
    stale.value = false
    draftParseError.value = null
  }

  function markStale(): void {
    if (preview.value.status === 'success') {
      stale.value = true
    }
  }

  function setObject(next: string): void {
    if (object.value === next) return
    object.value = next
    clearResult()
  }

  function setSource(next: string): void {
    if (source.value === next) return
    source.value = next
    clearResult()
  }

  function setSample(next: Partial<MappingPreviewSampleInput>): void {
    sample.value = { ...sample.value, ...next }
    markStale()
  }

  function setUseDraft(next: boolean): void {
    if (useDraft.value === next) return
    useDraft.value = next
    markStale()
  }

  function setDraft(next: MappingPreviewDraftBinding | null): void {
    draft.value = next
    markStale()
  }

  function setDraftText(next: string): void {
    draftText.value = next
    draftParseError.value = null
    markStale()
  }

  function buildBody(): MappingPreviewRequest | null {
    const body: MappingPreviewRequest = {
      source: source.value,
      sample: {
        offset: sample.value.offset,
        limit: sample.value.limit,
        batch_id: sample.value.batch_id,
      },
    }
    if (!useDraft.value) {
      return body
    }
    if (draft.value) {
      body.draft_binding = draft.value
      return body
    }
    const text = draftText.value.trim()
    if (!text) {
      draftParseError.value = '草稿为空'
      return null
    }
    try {
      body.draft_binding = JSON.parse(text) as MappingPreviewDraftBinding
      draftParseError.value = null
      return body
    } catch {
      draftParseError.value = '草稿不是合法 JSON'
      return null
    }
  }

  async function submit(): Promise<void> {
    // 加载中禁止重复提交(迟到响应由 gen 守卫)
    if (preview.value.status === 'loading') {
      return
    }
    if (!object.value || !source.value) {
      preview.value = {
        status: 'error',
        error: { kind: 'unknown', message: '请先选择对象与数据源', retriable: false },
      }
      return
    }

    const body = buildBody()
    if (!body) {
      preview.value = {
        status: 'error',
        error: {
          kind: 'parse',
          message: draftParseError.value ?? '草稿无效',
          retriable: false,
        },
      }
      return
    }

    abort?.abort()
    abort = new AbortController()
    const requestGen = ++gen
    const first = preview.value.status !== 'success'
    if (first) {
      preview.value = { status: 'loading' }
    }
    stale.value = false

    const result = await postMappingPreview(object.value, body, { signal: abort.signal })
    if (requestGen !== gen) {
      return
    }
    if (result.ok) {
      preview.value = { status: 'success', data: result.data }
      refreshError.value = null
    } else if (first) {
      preview.value = { status: 'error', error: result.error }
      refreshError.value = null
    } else {
      refreshError.value = result.error
    }
  }

  function reset(): void {
    object.value = ''
    source.value = ''
    sample.value = { offset: 0, limit: 50, batch_id: null }
    useDraft.value = false
    draft.value = null
    draftText.value = ''
    clearResult()
  }

  return {
    object,
    source,
    sample,
    useDraft,
    draft,
    draftText,
    preview,
    refreshError,
    stale,
    draftParseError,
    isEmpty,
    isUnauthorized,
    isLoading,
    setObject,
    setSource,
    setSample,
    setUseDraft,
    setDraft,
    setDraftText,
    submit,
    clearResult,
    reset,
  }
})
