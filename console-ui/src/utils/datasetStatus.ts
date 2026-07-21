/**
 * 数据集状态展示与动作可用性(M2):building-ready 由 building + 对象全 built 推导,
 * 不新增 ready 枚举。
 */
import type { components } from '@/types/api'

export type DatasetSummary = components['schemas']['DatasetSummary']
export type DatasetDetail = components['schemas']['DatasetDetail']
export type ObjectVersionSummary = components['schemas']['ObjectVersionSummary']

/** UI 展示标签:building-ready / building / failed / published / retired */
export type DatasetStatusLabel =
  | '待发布'
  | '构建中'
  | '已发布'
  | '失败'
  | '已退役'

export function isBuildingReady(
  summary: Pick<DatasetSummary, 'status' | 'object_manifest'>,
  objects: ObjectVersionSummary[],
): boolean {
  if (summary.status !== 'building') {
    return false
  }
  const manifest = summary.object_manifest
  if (!manifest || manifest.length === 0) {
    return false
  }
  if (objects.length !== manifest.length) {
    return false
  }
  const byName = new Map(objects.map((o) => [o.object, o]))
  return manifest.every((name) => byName.get(name)?.status === 'built')
}

export function datasetStatusLabel(
  summary: Pick<DatasetSummary, 'status' | 'object_manifest'>,
  objects?: ObjectVersionSummary[],
): DatasetStatusLabel {
  switch (summary.status) {
    case 'published':
      return '已发布'
    case 'failed':
      return '失败'
    case 'retired':
      return '已退役'
    case 'building':
      if (objects && isBuildingReady(summary, objects)) {
        return '待发布'
      }
      // 列表无对象明细时,building 候选统一标「待发布」语义入口(后端 not_ready → 409)
      return objects ? '构建中' : '待发布'
    default:
      return '构建中'
  }
}

export function canPublish(summary: Pick<DatasetSummary, 'status'>): boolean {
  return summary.status === 'building'
}

export function canRollback(summary: Pick<DatasetSummary, 'status' | 'previous_dataset_version'>): boolean {
  return summary.status === 'published' && Boolean(summary.previous_dataset_version)
}

export function rollbackTarget(summary: Pick<DatasetSummary, 'previous_dataset_version'>): string | null {
  return summary.previous_dataset_version ?? null
}
