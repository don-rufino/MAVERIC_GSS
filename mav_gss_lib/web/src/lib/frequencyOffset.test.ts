import { describe, expect, it } from 'vitest'
import { formatKhzOffset, formatMhz } from './frequencyOffset'

describe('formatKhzOffset (shared TX Δf sweep / RX Δf nom format)', () => {
  it('matches the TX sweep column format', () => {
    expect(formatKhzOffset(1500)).toBe('+1.5 kHz')
    expect(formatKhzOffset(7771)).toBe('+7.8 kHz')
    expect(formatKhzOffset(-10152)).toBe('-10.2 kHz')
    expect(formatKhzOffset(0)).toBe('0.0 kHz')
  })

  it('never shows a signed zero', () => {
    expect(formatKhzOffset(-40)).toBe('0.0 kHz')
  })
})

describe('formatMhz', () => {
  it('groups to Hz resolution', () => {
    expect(formatMhz(437_508_258.6)).toBe('437.508 259')
  })
})
