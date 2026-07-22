/**
 * Field Lineage Mock 响应与错误体(typed against generated schemas)。
 * 场景:available(direct+map+derived)、unavailable(旧版)、404/409/422/500。
 */
import type { components } from '@/types/api'

export type ObjectLineageResponse = components['schemas']['ObjectLineageResponse']
export type ObjectLineageError = components['schemas']['ObjectLineageError']

const KEY_TOKEN = 'ab'.repeat(32)

export function lineageAvailable(): ObjectLineageResponse {
  return {
    state: 'available',
    reason_code: null,
    source: 'digiwin_e10',
    object: 'SalesOrderLine',
    display_name: '销售订单明细',
    object_key: [['order_no', 'SO-001'], ['line_no', 10]],
    key_token: KEY_TOKEN,
    dataset_version: 'ds_20260722T000000Z_mock0001',
    object_version: 'ov_mock0001',
    template_version: '0.1.0',
    binding_hash: `sha256:${'ab'.repeat(32)}`,
    binding_status: 'verified',
    map_batch_id: 'mb_mock0001',
    fields: [
      {
        property: 'order_no',
        display_name: '订单号',
        final_value: { kind: 'scalar', value: 'SO-001', preview: null, sha256: null, length: null },
        state: 'available',
        reason_code: null,
        steps: [
          { kind: 'read', before: null, after: { kind: 'scalar', value: 'SO2604-001', preview: null, sha256: null, length: null } },
        ],
        inputs: [
          {
            role: 'value',
            source_table: 'SALES_ORDER_D',
            source_column: 'DOC_NO',
            source_pk: [['Id', 1]],
            source_value: { kind: 'scalar', value: 'SO2604-001', preview: null, sha256: null, length: null },
            extract_batch_id: 'batch001',
            join: null,
          },
        ],
      },
      {
        property: 'status',
        display_name: '状态',
        final_value: { kind: 'scalar', value: 'open', preview: null, sha256: null, length: null },
        state: 'available',
        reason_code: null,
        steps: [
          { kind: 'read', before: null, after: { kind: 'scalar', value: 'O', preview: null, sha256: null, length: null } },
          { kind: 'map', before: { kind: 'scalar', value: 'O', preview: null, sha256: null, length: null }, after: { kind: 'scalar', value: 'open', preview: null, sha256: null, length: null }, map_hit: true },
        ],
        inputs: [
          {
            role: 'value',
            source_table: 'SALES_ORDER_D',
            source_column: 'STATUS',
            source_pk: [['Id', 1]],
            source_value: { kind: 'scalar', value: 'O', preview: null, sha256: null, length: null },
            extract_batch_id: 'batch001',
            join: null,
          },
        ],
      },
      {
        property: 'biz_status',
        display_name: '业务状态',
        final_value: { kind: 'scalar', value: '正常', preview: null, sha256: null, length: null },
        state: 'available',
        reason_code: null,
        steps: [
          { kind: 'derived_rule', before: null, after: { kind: 'scalar', value: '正常', preview: null, sha256: null, length: null }, derived_rule_index: 0 },
        ],
        inputs: [
          {
            role: 'derived_condition',
            source_table: 'SALES_ORDER_D',
            source_column: 'STATUS',
            source_pk: [['Id', 1]],
            source_value: { kind: 'scalar', value: 'O', preview: null, sha256: null, length: null },
            extract_batch_id: 'batch001',
            join: null,
          },
        ],
      },
    ],
    warnings: [],
    generated_at: '2026-07-22T10:00:00+08:00',
  }
}

export function lineageUnavailableOld(): ObjectLineageResponse {
  return {
    state: 'unavailable',
    reason_code: 'lineage_not_recorded',
    source: 'digiwin_e10',
    object: 'SalesOrderLine',
    display_name: '销售订单明细',
    object_key: [],
    key_token: KEY_TOKEN,
    dataset_version: 'ds_20260701T000000Z_old0001',
    object_version: 'ov_old0001',
    template_version: '0.1.0',
    binding_hash: `sha256:${'cd'.repeat(32)}`,
    binding_status: null,
    map_batch_id: null,
    fields: [],
    warnings: ['该数据集版本未记录字段血缘（发布于字段血缘功能上线前）'],
    generated_at: '2026-07-22T10:00:00+08:00',
  }
}

export function lineageError(
  status: number,
  reasonCode: ObjectLineageError['reason_code'],
  detail: string,
): ObjectLineageError {
  return {
    status,
    reason_code: reasonCode,
    detail,
    error_id: status >= 500 ? 'mock0001' : null,
  }
}
