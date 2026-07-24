import { describe, expect, it } from 'vitest'
import { IS_MOCK, MODE } from './mode'

describe('console mode', () => {
  it('is always real (no product mock mode)', () => {
    expect(MODE).toBe('real')
    expect(IS_MOCK).toBe(false)
  })
})
