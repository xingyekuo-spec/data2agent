import { describe, expect, it } from 'vitest'
import {
  formatCell,
  formatDuration,
  formatJsonPretty,
  formatJsonValue,
  formatPercent,
} from './format'

describe('utils/format', () => {
  describe('formatCell', () => {
    it('null/undefined → 占位符(默认 —,可自定义)', () => {
      expect(formatCell(null)).toBe('—')
      expect(formatCell(undefined)).toBe('—')
      expect(formatCell(null, '-')).toBe('-')
    })

    it('标量 → String()', () => {
      expect(formatCell('abc')).toBe('abc')
      expect(formatCell(42)).toBe('42')
      expect(formatCell(false)).toBe('false')
    })

    it('blob 标记 → [BLOB n bytes]', () => {
      expect(formatCell({ __blob__: true, bytes: 128 })).toBe('[BLOB 128 bytes]')
      expect(formatCell({ __blob__: true })).toBe('[BLOB ? bytes]')
    })
  })

  describe('formatDuration', () => {
    it('null → 占位符', () => {
      expect(formatDuration(null)).toBe('-')
      expect(formatDuration(undefined, '—')).toBe('—')
    })

    it('按量级缩写', () => {
      expect(formatDuration(30)).toBe('30s')
      expect(formatDuration(300)).toBe('5m')
      expect(formatDuration(5400)).toBe('1.5h')
      expect(formatDuration(172800)).toBe('2d')
    })
  })

  describe('formatPercent', () => {
    it('null → 占位符', () => {
      expect(formatPercent(null)).toBe('-')
      expect(formatPercent(undefined, '—')).toBe('—')
    })

    it('0~1 → 百分比(1 位小数)', () => {
      expect(formatPercent(0.856)).toBe('85.6%')
      expect(formatPercent(1)).toBe('100.0%')
      expect(formatPercent(0)).toBe('0.0%')
    })
  })

  describe('formatJsonPretty', () => {
    it('缩进 2 空格美化', () => {
      expect(formatJsonPretty({ a: 1 })).toBe('{\n  "a": 1\n}')
    })

    it('循环引用退化为 String()', () => {
      const circular: Record<string, unknown> = {}
      circular.self = circular
      expect(formatJsonPretty(circular)).toBe('[object Object]')
    })
  })

  describe('formatJsonValue', () => {
    it('null/undefined → 占位符', () => {
      expect(formatJsonValue(null)).toBe('—')
      expect(formatJsonValue(undefined, '-')).toBe('-')
    })

    it('字符串原样返回', () => {
      expect(formatJsonValue('已映射')).toBe('已映射')
    })

    it('对象/数字 → 单行 JSON', () => {
      expect(formatJsonValue({ a: 1 })).toBe('{"a":1}')
      expect(formatJsonValue(3.14)).toBe('3.14')
    })
  })
})
