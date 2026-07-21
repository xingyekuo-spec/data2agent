import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ApiResult } from '@/api/services'
import type { components } from '@/types/api'
import { useDatasetsStore } from './datasets'

vi.mock('@/api/services', () => ({
  getDatasets: vi.fn(),
  getDatasetDetail: vi.fn(),
  postDatasetPublish: vi.fn(),
  postDatasetRollback: vi.fn(),
  postApply: vi.fn(),
}))

import {
  getDatasetDetail,
  getDatasets,
  postApply,
  postDatasetPublish,
  postDatasetRollback,
} from '@/api/services'

type DatasetSummary = components['schemas']['DatasetSummary']
type DatasetDetail = components['schemas']['DatasetDetail']
type DatasetActionResult = components['schemas']['DatasetActionResult']
type ApplyActionResult = components['schemas']['ApplyActionResult']

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data, response: new Response() }
}

function summary(partial: Partial<DatasetSummary> & Pick<DatasetSummary, 'dataset_version' | 'status'>): DatasetSummary {
  return {
    source: 'digiwin_e10',
    template_version: '0.1.0',
    built_at: '2026-07-21T10:00:00+08:00',
    published_at: partial.status === 'published' || partial.status === 'retired'
      ? '2026-07-21T10:05:00+08:00'
      : null,
    previous_dataset_version: null,
    error: null,
    error_id: null,
    object_manifest: ['Customer'],
    ...partial,
  }
}

describe('datasets store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads list with X-Total-Count and opens detail', async () => {
    vi.mocked(getDatasets).mockResolvedValue(
      ok({ items: [summary({ dataset_version: 'ds-ready', status: 'building' })], total: 1 }),
    )
    const detail: DatasetDetail = {
      ...summary({ dataset_version: 'ds-ready', status: 'building' }),
      objects: [{
        object: 'Customer',
        object_version: 'ov-1',
        binding_hash: 'sha256:aa',
        row_count: 10,
        status: 'built',
        built_at: '2026-07-21T10:00:00+08:00',
        published_at: null,
      }],
    }
    vi.mocked(getDatasetDetail).mockResolvedValue(ok(detail))

    const store = useDatasetsStore()
    await store.refresh()
    expect(store.list.status).toBe('success')
    if (store.list.status === 'success') {
      expect(store.list.data[0]?.dataset_version).toBe('ds-ready')
    }
    expect(store.total).toBe(1)

    await store.openDetail('ds-ready')
    expect(store.detail?.status).toBe('success')
    if (store.detail?.status === 'success') {
      expect(store.detail.data.objects?.[0]?.status).toBe('built')
    }
  })

  it('publish refreshes list; rollback follows activated version', async () => {
    vi.mocked(getDatasets).mockResolvedValue(ok({ items: [], total: 0 }))
    const published: DatasetActionResult = {
      executed: true,
      dataset_version: 'ds-2',
      note: 'published',
    }
    vi.mocked(postDatasetPublish).mockResolvedValue(ok(published))
    vi.mocked(getDatasetDetail).mockResolvedValue(
      ok({ ...summary({ dataset_version: 'ds-2', status: 'published', previous_dataset_version: 'ds-1' }), objects: [] }),
    )

    const store = useDatasetsStore()
    store.detailVersion = 'ds-2'
    const okPublish = await store.publish('ds-2')
    expect(okPublish).toBe(true)
    expect(getDatasets).toHaveBeenCalled()
    expect(postDatasetPublish).toHaveBeenCalledWith('ds-2')

    vi.mocked(postDatasetRollback).mockResolvedValue(
      ok({ executed: true, dataset_version: 'ds-1', note: 'rolled back' }),
    )
    vi.mocked(getDatasetDetail).mockResolvedValue(
      ok({ ...summary({ dataset_version: 'ds-1', status: 'published' }), objects: [] }),
    )
    const okRollback = await store.rollback('ds-2')
    expect(okRollback).toBe(true)
    expect(postDatasetRollback).toHaveBeenCalledWith('ds-2')
    expect(getDatasetDetail).toHaveBeenCalledWith('ds-1')
  })

  it('stage-only apply uses publish=false and refreshes candidates', async () => {
    const applyBody: ApplyActionResult = {
      executed: true,
      results: [],
      aborted: [],
      dataset_version: 'ds-stage',
      published: false,
      previous_dataset_version: 'ds-1',
    }
    vi.mocked(postApply).mockResolvedValue(ok(applyBody))
    vi.mocked(getDatasets).mockResolvedValue(
      ok({ items: [summary({ dataset_version: 'ds-stage', status: 'building' })], total: 1 }),
    )

    const store = useDatasetsStore()
    const okApply = await store.apply({ source: 'digiwin_e10', publish: false })
    expect(okApply).toBe(true)
    expect(postApply).toHaveBeenCalledWith({ source: 'digiwin_e10', publish: false })
    expect(store.applyResult?.status).toBe('success')
    if (store.applyResult?.status === 'success') {
      expect(store.applyResult.data.published).toBe(false)
      expect(store.applyResult.data.dataset_version).toBe('ds-stage')
    }
  })
})
