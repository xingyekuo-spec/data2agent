import { describe, expect, it } from 'vitest'
import { resolveMode } from './mode'

describe('resolveMode', () => {
  it('开发未设置默认 mock', () => {
    expect(resolveMode({})).toBe('mock')
    expect(resolveMode({ PROD: false })).toBe('mock')
  })

  it('开发显式设置生效(大小写/空白容忍)', () => {
    expect(resolveMode({ VITE_CONSOLE_MODE: 'real' })).toBe('real')
    expect(resolveMode({ VITE_CONSOLE_MODE: ' Mock ' })).toBe('mock')
  })

  it('生产未设置强制 real', () => {
    expect(resolveMode({ PROD: true })).toBe('real')
  })

  it('生产构建永远进不了 mock', () => {
    expect(resolveMode({ PROD: true, VITE_CONSOLE_MODE: 'mock' })).toBe('real')
    expect(resolveMode({ PROD: true, VITE_CONSOLE_MODE: 'MOCK' })).toBe('real')
  })

  it('生产始终 real', () => {
    expect(resolveMode({ PROD: true, VITE_CONSOLE_MODE: 'demo' })).toBe('real')
    expect(resolveMode({ PROD: true, VITE_CONSOLE_MODE: 'real' })).toBe('real')
  })

  it('非法值回退默认(dev→mock,prod→real)', () => {
    expect(resolveMode({ VITE_CONSOLE_MODE: 'banana' })).toBe('mock')
    expect(resolveMode({ PROD: true, VITE_CONSOLE_MODE: 'banana' })).toBe('real')
  })
})
