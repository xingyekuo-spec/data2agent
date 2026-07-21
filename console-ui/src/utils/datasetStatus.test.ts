import { describe, expect, it } from 'vitest'
import {
  canPublish,
  canRollback,
  datasetStatusLabel,
  isBuildingReady,
} from './datasetStatus'
import type { ObjectVersionSummary } from './datasetStatus'

function obj(partial: Partial<ObjectVersionSummary> & Pick<ObjectVersionSummary, 'object' | 'status'>): ObjectVersionSummary {
  return {
    object_version: 'ov-1',
    binding_hash: 'sha256:ab',
    row_count: 1,
    built_at: '2026-07-21T10:00:00+08:00',
    published_at: null,
    batch_id: null,
    build_table: 'objv_x',
    ...partial,
  }
}

describe('datasetStatus', () => {
  it('derives building-ready only when manifest objects are all built', () => {
    const summary = {
      status: 'building' as const,
      object_manifest: ['Customer', 'Material'],
    }
    expect(isBuildingReady(summary, [
      obj({ object: 'Customer', status: 'built' }),
      obj({ object: 'Material', status: 'built' }),
    ])).toBe(true)
    expect(isBuildingReady(summary, [
      obj({ object: 'Customer', status: 'built' }),
      obj({ object: 'Material', status: 'building' }),
    ])).toBe(false)
    expect(datasetStatusLabel(summary, [
      obj({ object: 'Customer', status: 'built' }),
      obj({ object: 'Material', status: 'built' }),
    ])).toBe('待发布')
    expect(datasetStatusLabel(summary, [
      obj({ object: 'Customer', status: 'built' }),
      obj({ object: 'Material', status: 'failed' }),
    ])).toBe('构建中')
  })

  it('gates publish on building and rollback on published+previous', () => {
    expect(canPublish({ status: 'building' })).toBe(true)
    expect(canPublish({ status: 'failed' })).toBe(false)
    expect(canPublish({ status: 'published' })).toBe(false)
    expect(canRollback({ status: 'published', previous_dataset_version: 'ds-1' })).toBe(true)
    expect(canRollback({ status: 'published', previous_dataset_version: null })).toBe(false)
    expect(canRollback({ status: 'retired', previous_dataset_version: 'ds-1' })).toBe(false)
  })

  it('labels published/failed/retired distinctly', () => {
    expect(datasetStatusLabel({ status: 'published', object_manifest: null })).toBe('已发布')
    expect(datasetStatusLabel({ status: 'failed', object_manifest: null })).toBe('失败')
    expect(datasetStatusLabel({ status: 'retired', object_manifest: null })).toBe('已退役')
  })
})
