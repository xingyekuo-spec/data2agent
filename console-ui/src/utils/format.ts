/**
 * 通用展示格式化:与 utils/time.ts 同级,收纳跨视图/组件复用的值格式化,
 * 避免各视图各自实现(此前 formatCell 在 DataView 与 RawDataDrawer 重复)。
 *
 * 约定:null/undefined 由 empty 参数决定占位符(表格习惯 '-',抽屉习惯 '—')。
 */

export type CellValue = string | number | boolean | null | { __blob__?: boolean; bytes?: number }

/** 表格单元格:null/undefined → 占位;blob 标记 → [BLOB n bytes];其余 String() */
export function formatCell(value: unknown, empty = '—'): string {
  if (value === null || value === undefined) {
    return empty
  }
  if (typeof value === 'object' && (value as { __blob__?: boolean }).__blob__) {
    const blob = value as { bytes?: number }
    return `[BLOB ${blob.bytes ?? '?'} bytes]`
  }
  return String(value as CellValue)
}

/** 秒数 → 紧凑时长:<60s → "30s";<1h → "5m";<1d → "1.5h";否则 "2d" */
export function formatDuration(seconds: number | null | undefined, empty = '-'): string {
  if (seconds == null) return empty
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`
  return `${Math.round(seconds / 86400)}d`
}

/** 0~1 比率 → 百分比字符串(保留 1 位小数) */
export function formatPercent(rate: number | null | undefined, empty = '-'): string {
  if (rate == null) return empty
  return `${(rate * 100).toFixed(1)}%`
}

/** JSON 美化输出(详情面板 <pre> 用);不可序列化时退化为 String() */
export function formatJsonPretty(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

/** 单元格内联 JSON:null → 占位;字符串原样;其余单行 JSON */
export function formatJsonValue(value: unknown, empty = '—'): string {
  if (value === null || value === undefined) return empty
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}
