import { describe, expect, it } from 'vitest'
import { floorNote, marginTone, signed } from './signal'
import type { SignalMeasurement } from './types'

const sig = { v: 1, status: 'ok' } as SignalMeasurement

describe('marginTone', () => {
  it('splits at 0 and 3 dB of margin', () => {
    expect(marginTone(19.7, 7.8)).toBe('success')
    expect(marginTone(8.5, 7.8)).toBe('warning')
    expect(marginTone(7.0, 7.8)).toBe('danger')
  })
})

describe('signed', () => {
  it('uses a real minus sign', () => {
    expect(signed(-3027)).toBe('−3027')
    expect(signed(2051)).toBe('+2051')
    expect(signed(0)).toBe('0')
  })
})

describe('floorNote', () => {
  it('is silent for the normal pre-burst floor', () => {
    expect(floorNote({ ...sig, floor_method: 'pre_burst' })).toBeNull()
  })
  it('explains floors moved by an uplink', () => {
    expect(floorNote({ ...sig, floor_method: 'pre_burst_earlier' })?.label).toBe('FLOOR: EARLIER')
    expect(floorNote({ ...sig, floor_method: 'beside_signal', floor_tx_affected: true })?.detail).toMatch(/uplink/)
  })
})
